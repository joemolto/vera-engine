"""Account suggestions — self-regulating candidate pool.

Apollo org-search produces candidate companies matching the client's
ICP. They land in the watchlist table as `status='pending'` rows. The
rep sees them in a "Suggested accounts" tray on the dashboard:
  - **Accept** → promote to status='active' (becomes a real account,
    enrichment starts on the next monitor pass).
  - **Dismiss** → status='dismissed' with dismissed_at=now(). Won't be
    re-suggested for 90 days.

Pool semantics:
  - Target pool size = 10 pending suggestions visible at any time.
  - Top-up runs as a FastAPI BackgroundTask after every accept/dismiss
    AND optionally from a daily cron (safety net for failed top-ups).
  - Opt-in via `icp_config.features.account_suggestions` — every
    endpoint no-ops if the flag isn't set on the calling org.

This is intentionally a NEW router, not bolted onto watchlist.py, so
the suggestion lifecycle can be reasoned about independently and the
feature can be flagged off cleanly per org.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from app.database import get_supabase
from app.services.api_auth import ApiContext, verify_api_key
from app.services.apollo_orgs import search_by_name, search_companies
from app.services.internal_match import canonical_industries
from app.services.suggestion_classifier import (
    build_seller_profile,
    classify_candidates,
)
from app.services.why_this_matters import refresh_why_this_matters
from app.services.security_team_profiler import refresh_security_team_profile
from app.services.account_profile_scorer import refresh_profile_score
from app.services.website_posture import create_website_posture_signals_for_company
from app.services.signal_writer import insert_signals

logger = logging.getLogger(__name__)

router = APIRouter(tags=["account-suggestions"])

# Pool target — number of pending suggestions to keep visible at any
# time. Tune as a constant rather than per-client; if an org wants
# more / fewer, change here and redeploy.
TARGET_POOL_SIZE = 10

# Dismissed-suggestion cooldown — don't re-suggest a company the rep
# said no to within this window. Long enough that ICP fit / signals
# may have shifted; short enough that the rep doesn't permanently
# blacklist Apollo's whole candidate pool.
DISMISS_COOLDOWN_DAYS = 90


# ---------------------------------------------------------------------------
# Feature-flag gate
# ---------------------------------------------------------------------------
def _suggestions_enabled(org_id: str) -> bool:
    """Returns True only if the org has icp_config.features.account_suggestions
    set to truthy. Other orgs get no-ops on every endpoint here so the
    feature doesn't affect orgs that haven't opted in."""
    db = get_supabase()
    res = (
        db.table("organisations")
        .select("icp_config")
        .eq("id", org_id)
        .maybe_single()
        .execute()
    )
    cfg = (res.data or {}).get("icp_config") or {}
    if not isinstance(cfg, dict):
        return False
    features = cfg.get("features") or {}
    if not isinstance(features, dict):
        return False
    return bool(features.get("account_suggestions"))


def _normalise_employee_range(raw: str) -> Optional[str]:
    """Convert any of our internal employee-range formats into Apollo's
    "min,max" shape.

    Inputs we may receive (icp_config has historically held BOTH formats):
      - "501,1000"           → already correct
      - "1,000-5,000"        → "1000,5000"  (persona format, dashes + commas-in-numbers)
      - "501-1000"           → "501,1000"
      - "10000+"             → "10000,100000" (open-ended cap so Apollo accepts it)

    Bug 2026-06-07: Apollo silently ignored ranges in the persona
    format, returning popular top brands (Microsoft, Google) instead
    of mid-market companies. Normalising here keeps all callers safe
    regardless of which icp_config shape they happen to read.
    """
    if not raw:
        return None
    s = raw.strip().replace(" ", "")
    # Drop commas used as digit separators ("1,000-5,000" → "1000-5000")
    # but only when they're between digits, not when the format is
    # already "501,1000" (min,max).
    # Heuristic: if string has both ',' and '-', the dash separates
    # the min/max; commas are number-formatters.
    if "-" in s and "," in s:
        s = s.replace(",", "")
    if "+" in s:
        # Open-ended ranges like "10000+" — set max to a large number
        lo = "".join(c for c in s if c.isdigit())
        return f"{lo},100000" if lo else None
    if "-" in s:
        parts = s.split("-")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            return f"{parts[0]},{parts[1]}"
        return None
    if "," in s:
        parts = s.split(",")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            return f"{parts[0]},{parts[1]}"
        return None
    # Plain number → treat as single-cap range
    if s.isdigit():
        return f"{s},{s}"
    return None


def _get_icp_criteria(org_id: str) -> Dict[str, List[str]]:
    """Pull the persona-driven ICP criteria we feed to Apollo. Reads
    icp_config.personas[0] for industries/locations/employee bands.
    Falls back to top-level icp_config.target_* if persona is missing
    OR if persona's list is empty for a given field.

    Employee ranges are normalised to Apollo's "min,max" format here
    so the wrapper doesn't have to care which onboarding format the
    config came from."""
    db = get_supabase()
    res = (
        db.table("organisations")
        .select("icp_config")
        .eq("id", org_id)
        .maybe_single()
        .execute()
    )
    cfg = (res.data or {}).get("icp_config") or {}
    persona: Dict[str, Any] = {}
    personas = cfg.get("personas")
    if isinstance(personas, list) and personas:
        first = personas[0]
        if isinstance(first, dict):
            persona = first

    def pick(*keys: str) -> List[str]:
        # Try persona first, then top-level icp_config. Either source
        # might have the field; persona is preferred when both have it.
        for k in keys:
            v = persona.get(k) if persona else None
            if isinstance(v, list) and v:
                return [str(x) for x in v if x]
            v = cfg.get(k)
            if isinstance(v, list) and v:
                return [str(x) for x in v if x]
        return []

    raw_ranges = pick("target_company_size")
    normalised: List[str] = []
    for r in raw_ranges:
        n = _normalise_employee_range(r)
        if n and n not in normalised:
            normalised.append(n)

    return {
        "industries": pick("target_industries"),
        "locations": pick("target_locations"),
        "employee_ranges": normalised,
    }


# ---------------------------------------------------------------------------
# Topup helper — the actual pool-fill logic, used by both /topup and
# the BackgroundTask kicked off after accept/dismiss.
# ---------------------------------------------------------------------------

# Per-org stampede guard. Accepting N suggestions in a burst (the wizard's
# add step accepts up to 10 back to back) fires N background topups that
# all read the pending count BEFORE any of them insert — each fills the
# pool independently. Seen live 2026-07-08: a 10-accept burst left one org
# with 85 pending suggestions. One topup per org at a time, and a cooldown
# after a run so the burst's stragglers no-op. Process-local state — the
# engine runs a single uvicorn process; the daily topup cron remains the
# safety net if a debounced run was actually needed.
_TOPUP_COOLDOWN_SECONDS = 45
_topup_guard = threading.Lock()
_topup_running: set = set()
_topup_last_run: Dict[str, float] = {}


def _topup_permitted(org_id: str) -> bool:
    """Claim the per-org topup slot. Caller MUST call _topup_release()
    when it holds the claim and finishes."""
    with _topup_guard:
        if org_id in _topup_running:
            return False
        if time.monotonic() - _topup_last_run.get(org_id, float("-inf")) < _TOPUP_COOLDOWN_SECONDS:
            return False
        _topup_running.add(org_id)
        return True


def _topup_release(org_id: str) -> None:
    with _topup_guard:
        _topup_running.discard(org_id)
        _topup_last_run[org_id] = time.monotonic()


def _topup_pool(org_id: str, target: int = TARGET_POOL_SIZE) -> Dict[str, Any]:
    """Fill the pending pool back up to `target`. No-op when flag is off,
    when another topup for this org is running or just ran, or when the
    pool is already at/above target."""
    if not _suggestions_enabled(org_id):
        return {"skipped": "feature_flag_off"}
    if not _topup_permitted(org_id):
        return {"skipped": "debounced"}
    try:
        return _topup_pool_inner(org_id, target)
    finally:
        _topup_release(org_id)


def _topup_pool_inner(org_id: str, target: int) -> Dict[str, Any]:
    db = get_supabase()

    # Lapsed orgs get no suggestion refills — the classifier pass is real
    # LLM spend and the tray is invisible to a read-only org anyway
    # (2026-08-23 lapsed-org audit).
    from app.services.plan_limits import org_lapsed
    if org_lapsed(org_id, db):
        return {"pending": 0, "added": 0, "reason": "subscription_lapsed"}

    # Current pending count
    pending_res = (
        db.table("watchlist")
        .select("id", count="exact")
        .eq("org_id", org_id)
        .eq("status", "pending")
        .execute()
    )
    pending_count = pending_res.count if pending_res.count is not None else len(pending_res.data or [])

    needed = max(0, target - pending_count)
    if needed == 0:
        return {"pending": pending_count, "added": 0}

    # Fetch icp_config once — we use it for both the Apollo criteria
    # AND the LLM classifier's seller_profile.
    cfg_res = (
        db.table("organisations")
        .select("icp_config")
        .eq("id", org_id)
        .maybe_single()
        .execute()
    )
    icp_config = (cfg_res.data or {}).get("icp_config") or {}

    criteria = _get_icp_criteria(org_id)
    if not any(criteria.values()):
        logger.warning("Topup for org=%s: no ICP criteria configured — skipping", org_id)
        return {"pending": pending_count, "added": 0, "reason": "no_icp_criteria"}

    seller_profile = build_seller_profile(icp_config)

    # Domains we must NOT re-suggest:
    #   - currently active or pending for this org
    #   - dismissed within the cooldown window
    skip_domains: set[str] = set()
    skip_res = (
        db.table("watchlist")
        .select("company_domain, status, dismissed_at")
        .eq("org_id", org_id)
        .execute()
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=DISMISS_COOLDOWN_DAYS)
    for row in skip_res.data or []:
        dom = (row.get("company_domain") or "").lower().strip()
        if not dom:
            continue
        status = row.get("status")
        if status in ("active", "pending"):
            skip_domains.add(dom)
            continue
        if status == "dismissed":
            d_at = row.get("dismissed_at")
            if d_at:
                try:
                    d_dt = datetime.fromisoformat(d_at.replace("Z", "+00:00"))
                    if d_dt > cutoff:
                        skip_domains.add(dom)
                except Exception:
                    skip_domains.add(dom)  # be conservative

    # Apollo's mixed_companies/search returns a fixed page-1 ordering
    # heavy on the same big-name companies, and after skipping ones
    # already on the rep's watchlist we routinely come up short with
    # only page 1. So we paginate: keep pulling pages until we've
    # inserted `needed` rows or hit MAX_PAGES (safety cap so a
    # genuinely-tight ICP doesn't loop forever).
    MAX_PAGES = 5
    PER_PAGE = 100

    inserted = 0
    pages_tried = 0
    for page in range(1, MAX_PAGES + 1):
        if inserted >= needed:
            break
        pages_tried += 1
        candidates = search_companies(
            industries=canonical_industries(criteria["industries"]) or None,
            locations=criteria["locations"] or None,
            employee_ranges=criteria["employee_ranges"] or None,
            per_page=PER_PAGE,
            page=page,
        )
        if not candidates:
            # No more candidates in Apollo for this ICP — stop.
            break

        # Pre-filter against existing skip set so we only spend
        # classifier tokens on candidates that have a chance of
        # actually landing.
        candidates = [
            c for c in candidates
            if (c.get("domain") or "").lower().strip() not in skip_domains
        ]
        if not candidates:
            continue

        # Cheap LLM classifier — drops publishers, regulators,
        # holding companies that match Apollo's fuzzy keyword tags
        # but aren't real buyers for this client's selling motion.
        # Failure is non-blocking: classify_candidates returns the
        # input list unchanged on any error.
        candidates = classify_candidates(candidates, seller_profile=seller_profile)
        if not candidates:
            continue

        for c in candidates:
            if inserted >= needed:
                break
            dom = (c.get("domain") or "").lower().strip()
            if not dom or dom in skip_domains:
                continue
            try:
                ins = (
                    db.table("watchlist")
                    .insert(
                        {
                            "org_id": org_id,
                            "company_name": c["name"],
                            "company_domain": dom,
                            "status": "pending",
                            # Never monitor a suggestion nobody accepted —
                            # the column default is true and the batch
                            # monitor selects on it, so omitting this burned
                            # daily passes on pending rows. The accept path
                            # sets the real value explicitly.
                            "monitoring_active": False,
                            "suggested_reasons": c.get("matched_reasons") or [],
                        }
                    )
                    .execute()
                )
                skip_domains.add(dom)
                inserted += 1
                # Fire account.suggestion_created webhook so customers
                # can route fresh candidates into their CRM / Slack /
                # n8n. Best-effort, never blocks pool fill.
                try:
                    from app.services.webhooks import enqueue_event
                    new_row = (ins.data or [{}])[0] if ins.data else {}
                    enqueue_event(db, org_id, "account.suggestion_created", {
                        "suggestion_id": new_row.get("id"),
                        "company_name": c["name"],
                        "company_domain": dom,
                        "matched_reasons": c.get("matched_reasons") or [],
                    })
                except Exception as e:
                    logger.warning(
                        "account.suggestion_created webhook failed: %s", e,
                    )
            except Exception as e:
                # Most likely the unique index caught a race (concurrent
                # topup call). Skip and move on.
                logger.info("Topup insert skipped (%s): %s", dom, e)
                continue

    return {
        "pending": pending_count,
        "added": inserted,
        "target": target,
        "pages_tried": pages_tried,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
class TopupRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    target: Optional[int] = Field(default=None, ge=1, le=50)


@router.post("/suggestions/topup")
def topup_endpoint(
    req: TopupRequest, ctx: ApiContext = Depends(verify_api_key),
):
    """Top-up entrypoint. Can be called daily by a cron as a safety net. Also
    invoked synchronously after manual seed. Returns a small dict so
    cron logs are informative."""
    ctx.enforce_org(req.org_id)
    target = req.target or TARGET_POOL_SIZE
    return _topup_pool(req.org_id, target=target)


class SuggestionActionRequest(BaseModel):
    org_id: str = Field(..., min_length=1)


@router.post("/suggestions/{suggestion_id}/accept")
def accept_suggestion(
    suggestion_id: str,
    req: SuggestionActionRequest,
    background_tasks: BackgroundTasks,
    ctx: ApiContext = Depends(verify_api_key),
):
    """Promote a pending suggestion to status='active'. Top-up fires in
    the background so the rep's click doesn't wait on Apollo.

    org_id is required and the update is scoped to it — a suggestion id
    alone must never be enough to mutate another org's watchlist.
    """
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    # Solo plan: accepting a suggestion adds an account → 20-account cap.
    from app.services.plan_limits import enforce_account_cap
    enforce_account_cap(req.org_id, db)
    from app.services.org_features import monitoring_default, org_feature_enabled
    research_only = not monitoring_default(req.org_id)
    if research_only:
        # Zero-watch silence trap (2026-07-16 sim): a fresh org whose wizard
        # accepts all landed unwatched never got a digest (zero-watch skip)
        # while the welcome email said everything is switched on. The first
        # few accepts are watched automatically (cap-checked) so day one
        # produces a heartbeat; past the floor, research-first behaviour is
        # unchanged and watching stays the rep's deliberate choice.
        SEED_WATCH_FLOOR = 3
        try:
            from app.services.plan_limits import current_usage, enforce_watch_cap
            if current_usage(req.org_id, db)["watched"] < SEED_WATCH_FLOOR:
                enforce_watch_cap(req.org_id, db)
                research_only = False
        except HTTPException:
            pass  # watch cap full — keep research mode, never block the accept
        except Exception as e:
            logger.warning("seed-watch check failed for %s: %s", req.org_id, e)
    res = (
        db.table("watchlist")
        .update(
            {
                "status": "active",
                # Research by default for decoupled-monitoring orgs.
                "monitoring_active": not research_only,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("id", suggestion_id)
        .eq("org_id", req.org_id)
        .eq("status", "pending")
        .execute()
    )
    if not res.data:
        # Idempotent retry: the clara-api proxy occasionally 502s AFTER the
        # engine has completed the accept (seen live 2026-07-08 — row went
        # active, browser saw 502). A client retry must read as success,
        # not 404, or the wizard reports a phantom failure. No re-charge,
        # no re-enrich, no topup on this path — the first pass did all that.
        cur = (
            db.table("watchlist").select("status")
            .eq("id", suggestion_id).eq("org_id", req.org_id)
            .maybe_single().execute()
        )
        if cur and (cur.data or {}).get("status") == "active":
            return {"ok": True, "id": suggestion_id, "already_active": True}
        raise HTTPException(status_code=404, detail="suggestion not found or already actioned")

    # Decoupled orgs: the account lands as Research and the batch monitor
    # skips it (monitoring_active=false), so its snapshot would never get
    # built. Fire the one-time research enrichment now (the same 5-credit
    # deep read a URL paste gets). Monitored orgs get enriched by their
    # first monitor pass, so we only do this for the research path.
    domain = (res.data[0].get("company_domain") or "").strip().lower()
    if research_only and domain:
        from app.services import credits as _credits
        _credits.charge(
            db, org_id=req.org_id, action="deep_enrich",
            metadata={"domain": domain, "source": "suggestion_accept"},
        )
        from app.jobs import enqueue_research
        enqueue_research(req.org_id, domain, background_tasks)

    background_tasks.add_task(_topup_pool, req.org_id)
    return {"ok": True, "id": suggestion_id}


def _research_enrich(org_id: str, domain: str) -> None:
    """One-time research snapshot for a newly accepted account (decoupled
    orgs). Delegates to the shared job body so the queue and thread-pool paths
    run identical work."""
    from app.jobs import _run_research_enrich
    _run_research_enrich(org_id, domain)


# ---------------------------------------------------------------------------
# Delete — remove an accidental account or person from the user's view. SOFT
# delete (status='deleted') so the monthly research allowance / a spent unlock
# is NEVER refunded; monitoring stops, freeing the standing watched slot.
# (2026-06-30: a user accidentally researched "LinkedIn" as an account.)
# ---------------------------------------------------------------------------
class DeleteAccountRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    domain: str = Field(..., min_length=1)


@router.post("/delete")
def delete_account_row(
    req: DeleteAccountRequest, ctx: ApiContext = Depends(verify_api_key),
):
    """Soft-delete a watched/researched account by domain. status='deleted'
    (still counted toward the monthly research allowance — no enrich refunded),
    monitoring_active=False (frees the standing watched slot, stops the monitor).
    Hidden from the dashboard (active-only). Org-scoped."""
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    dom = (req.domain or "").strip().lower()
    res = (
        db.table("watchlist")
        .update({
            "status": "deleted",
            "monitoring_active": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        .eq("org_id", req.org_id)
        .eq("company_domain", dom)
        .in_("status", ["active"])
        .execute()
    )
    return {"ok": True, "deleted": len(res.data or [])}


class DeletePersonRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    lead_id: str = Field(..., min_length=1)


@router.post("/people/delete")
def delete_person(
    req: DeletePersonRequest, ctx: ApiContext = Depends(verify_api_key),
):
    """Soft-delete a person (lead). status='deleted' (hidden, but the row stays
    so an already-spent unlock is never refunded), person_monitoring=False
    (frees the standing watched-people slot). Org-scoped."""
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    res = (
        db.table("leads")
        .update({
            "status": "deleted",
            "person_monitoring": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        .eq("org_id", req.org_id)
        .eq("id", req.lead_id)
        .execute()
    )
    return {"ok": True, "deleted": len(res.data or [])}


# ---------------------------------------------------------------------------
# Photo backfill — fill leads.photo_url for existing people. FREE: uses
# Apollo people-search by domain (no reveal credits), maps photos to
# leads by apollo_id. Spec 2026-06-11 (richer People table).
# ---------------------------------------------------------------------------
class PhotoBackfillRequest(BaseModel):
    org_id: str = Field(..., min_length=1)


def _apollo_people_by_domain(domain: str) -> Dict[str, Dict[str, Any]]:
    """One free Apollo people-search for a domain →
    {apollo_id: {photo_url, first_name, last_name}}."""
    import httpx
    from app.config import settings
    api_key = settings.APOLLO_API_KEY
    if not api_key:
        return {}
    try:
        res = httpx.post(
            "https://api.apollo.io/api/v1/mixed_people/api_search",
            json={"q_organization_domains_list": [domain], "per_page": 100, "page": 1},
            headers={"X-Api-Key": api_key},
            timeout=30,
        )
        res.raise_for_status()
        people = res.json().get("people") or []
    except Exception as e:
        logger.warning("people backfill: Apollo search failed for %s: %s", domain, e)
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for p in people:
        pid = p.get("id")
        if pid:
            out[pid] = {
                "photo_url": p.get("photo_url"),
                "first_name": p.get("first_name"),
                "last_name": p.get("last_name"),
            }
    return out


@router.post("/people/photos/backfill")
def backfill_people_photos(
    req: PhotoBackfillRequest, ctx: ApiContext = Depends(verify_api_key),
):
    """Fill photo_url AND any missing first/last name for the org's leads that
    have an apollo_id, from one free Apollo people-search per domain. Returns
    how many were updated."""
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    rows = (
        db.table("leads")
        .select("id, apollo_id, company_domain, first_name, last_name, photo_url")
        .eq("org_id", req.org_id)
        .not_.is_("apollo_id", "null")
        .execute()
    ).data or []
    # Only the leads actually missing a photo or a last name — leave the rest.
    rows = [
        r for r in rows
        if not (r.get("photo_url") or "").strip() or not (r.get("last_name") or "").strip()
    ]

    by_domain: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        dom = (r.get("company_domain") or "").strip().lower()
        if dom:
            by_domain.setdefault(dom, []).append(r)

    photos_filled = 0
    names_filled = 0
    for dom, leads in by_domain.items():
        people = _apollo_people_by_domain(dom)
        if not people:
            continue
        for lead in leads:
            match = people.get(lead.get("apollo_id"))
            if not match:
                continue
            update: Dict[str, Any] = {}
            if not (lead.get("photo_url") or "").strip() and match.get("photo_url"):
                update["photo_url"] = match["photo_url"]
            if not (lead.get("last_name") or "").strip() and match.get("last_name"):
                update["last_name"] = match["last_name"]
            if not (lead.get("first_name") or "").strip() and match.get("first_name"):
                update["first_name"] = match["first_name"]
            if not update:
                continue
            try:
                db.table("leads").update(update).eq("id", lead["id"]).execute()
                if "photo_url" in update:
                    photos_filled += 1
                if "last_name" in update:
                    names_filled += 1
            except Exception as e:
                logger.warning("people backfill update failed for %s: %s", lead["id"], e)

    return {
        "ok": True,
        "domains_searched": len(by_domain),
        "photos_filled": photos_filled,
        "names_filled": names_filled,
    }


# ---------------------------------------------------------------------------
# Monitoring pause/resume — per-account control over the weekly scan.
# Setting monitoring_active=false stops the batch monitor picking the
# account up (it filters on monitoring_active=True); credits stop too.
# Spec 2026-06-11: users should see and control the scan cadence,
# not have it run invisibly.
# ---------------------------------------------------------------------------
class SoloLimitsRequest(BaseModel):
    org_id: str = Field(..., min_length=1)


@router.post("/solo/limits")
def solo_limits(
    req: SoloLimitsRequest, ctx: ApiContext = Depends(verify_api_key),
):
    """Usage + caps + remaining for the Solo cap chrome on the dashboard."""
    ctx.enforce_org(req.org_id)
    from app.services.plan_limits import limits_status
    return limits_status(req.org_id)


class ProposeSignalsRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    answers: Dict[str, Any]


@router.post("/solo/propose-signals")
def solo_propose_signals(
    req: ProposeSignalsRequest, ctx: ApiContext = Depends(verify_api_key),
):
    """The wizard's custom signal step: Vera PROPOSES which signal types fit
    what this seller sells (one Claude pass over their confirmed answers +
    website scan), each with a why in their language, plus qualifier
    suggestions. The user corrects; the confirmed set persists via
    /solo/onboarding's signal_config. Deterministic fallback inside —
    never blocks the wizard."""
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    row = (db.table("organisations").select("icp_config")
           .eq("id", req.org_id).maybe_single().execute().data or {})
    website_scan = (((row.get("icp_config") or {}).get("onboarding") or {})
                    .get("website_scan") or {})
    from app.services.onboarding_intel import propose_signal_config
    return propose_signal_config(req.answers, website_scan)


class SoloOnboardingRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    answers: Dict[str, Any]
    # User-confirmed signal step: {angle, enabled: {type: bool},
    # hiring_roles: [...], funding_event_types: [...]}. Optional — an older
    # wizard build that doesn't send it keeps today's preset behaviour.
    signal_config: Optional[Dict[str, Any]] = None


@router.post("/solo/onboarding")
def solo_onboarding(
    req: SoloOnboardingRequest,
    background_tasks: BackgroundTasks,
    ctx: ApiContext = Depends(verify_api_key),
):
    """Store the Solo onboarding-wizard answers, build the org's lens, and
    mark onboarding done.

    Fully self-serve (2026-07-08, supersedes "guided wizard + review"):
    the wizard's answers ARE the config. This endpoint expands them into a
    working ICP (Apollo-variant titles/industries/locations + personas +
    signal priorities) and builds the client_context seller understanding
    the operator used to hand-write on setup calls, so account matching,
    enrichment prompts and the suggestions classifier all see accounts
    through this customer's lens from minute one. A setup call stays
    optional, never a dependency.
    """
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    cur = (
        db.table("organisations").select("icp_config, name, client_context")
        .eq("id", req.org_id).maybe_single().execute()
    )
    org_row = cur.data or {}
    icp = org_row.get("icp_config") or {}
    org_name = org_row.get("name") or ""
    prior_onboarding = icp.get("onboarding") or {}
    website_scan = prior_onboarding.get("website_scan") or {}

    onboarding = {
        **prior_onboarding,
        "answers": req.answers,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }

    from app.services.onboarding_intel import (
        map_answers_to_icp,
        build_wizard_personas,
        signal_priorities_from_answers,
        build_client_context,
        generate_company_summary,
        build_solo_seller_profile,
        deal_context_from_band,
    )
    icp.update(map_answers_to_icp(req.answers))
    icp["icp_source"] = "wizard_auto"

    # Personas — account matching + suggestions prefer personas[0] over the
    # top-level target_* fields, so the persona MUST reflect the wizard
    # answers or the two disagree (test org 2026-07-23: manufacturing answers,
    # but suggestions used a stale insurance persona because it wasn't
    # rebuilt). The old "only if missing" guard was to protect a hand-tuned
    # persona, but a hand-configured org never reaches the wizard
    # (onboarding_completed=true gates it), so when the wizard DOES run it is
    # the ICP source of truth — always rebuild.
    personas = build_wizard_personas(req.answers)
    if personas:
        icp["personas"] = personas

    # Signal priorities — presence turns per-org relevance ranking on.
    # With a confirmed signal step, the USER'S choices are the source of
    # truth (off = weight 0 = never shown, Phase 1 rule). Without one,
    # preset from the ambulance axis; existing keys win so hand-tuned
    # weights survive a wizard re-run.
    if req.signal_config:
        from app.services.onboarding_intel import apply_signal_config
        icp["signal_priorities"] = apply_signal_config(req.answers, req.signal_config)
        qualifiers: Dict[str, Any] = {}
        roles = [str(r) for r in (req.signal_config.get("hiring_roles") or []) if str(r).strip()]
        events = [str(t) for t in (req.signal_config.get("funding_event_types") or []) if str(t).strip()]
        if roles:
            qualifiers["hiring_security"] = {"roles": roles[:8]}
        if events:
            qualifiers["funding"] = {"event_types": events[:4]}
        if qualifiers:
            icp["signal_qualifiers"] = qualifiers
    else:
        preset = signal_priorities_from_answers(req.answers)
        existing_priorities = icp.get("signal_priorities") or {}
        icp["signal_priorities"] = {**preset, **existing_priorities}

    # Seller profile — the same lever hand-configured orgs use on the live tray. Without it
    # the classifier can't tell a real FS firm from an FS recruiter; with
    # it, it knows this is a cyber vendor and drops the recruiters.
    client_context = icp.get("client_context") or {}
    client_context["product_pitch"] = build_solo_seller_profile(req.answers, website_scan)
    # Deal-size band → numeric fields so revenue_potential can estimate
    # (otherwise every account shows "unknown" deal potential).
    client_context.update(deal_context_from_band((req.answers or {}).get("deal_band")))
    icp["client_context"] = client_context

    # The org-level lens — organisations.client_context is what the
    # enricher's client-aware prompts and similar-companies filter read.
    # One-time AI build; only when empty so hand-written
    # context is never overwritten.
    update: Dict[str, Any] = {}
    if not org_row.get("client_context"):
        update["client_context"] = build_client_context(
            org_name, req.answers, website_scan,
        )

    # End-of-wizard AI summary (what they do + who they want to win).
    summary = generate_company_summary(website_scan=website_scan, answers=req.answers)
    if summary:
        onboarding["ai_summary"] = summary

    # Seller's own domain → drives the workspace logo (logo.dev) in the app.
    from app.services.onboarding_intel import _domain_of
    brand_domain = _domain_of((req.answers or {}).get("website") or "")
    if brand_domain:
        icp["brand_domain"] = brand_domain

    icp["onboarding"] = onboarding
    icp["onboarding_completed"] = True

    # Your market is core for wizard orgs (2026-08-15): flip the flag at
    # completion and kick the first market compute in the background so
    # the wizard's finale (and the nav page) have a snapshot to poll.
    features = icp.get("features") or {}
    features["market_v1"] = True
    icp["features"] = features

    update["icp_config"] = icp
    db.table("organisations").update(update).eq("id", req.org_id).execute()

    background_tasks.add_task(_kick_market_compute, req.org_id)
    return {"ok": True, "ai_summary": summary}


def _kick_market_compute(org_id: str) -> None:
    """First market snapshot after onboarding — best-effort. A failed
    compute (vendor throttle, anything) must never surface into the
    signup flow; the finale just polls until a snapshot exists or its
    own timeout falls back to the typeahead."""
    try:
        from app.services.market_radar import compute_overview
        compute_overview(org_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("post-onboarding market compute failed for %s: %s", org_id, e)


class ScanWebsiteRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    url: str = Field(..., min_length=3)


@router.post("/solo/scan-website")
def solo_scan_website(
    req: ScanWebsiteRequest, ctx: ApiContext = Depends(verify_api_key),
):
    """Scan the customer's own website (Tavily + Claude) so the wizard can
    prefill what they sell. Best-effort: returns empty fields if the scan
    yields nothing, and caches the result on the onboarding blob so the
    end-of-wizard summary can reuse it."""
    ctx.enforce_org(req.org_id)
    from app.services.onboarding_intel import scan_website

    scan = scan_website(req.url)

    if any(scan.values()):
        db = get_supabase()
        cur = (
            db.table("organisations").select("icp_config")
            .eq("id", req.org_id).maybe_single().execute()
        )
        icp = (cur.data or {}).get("icp_config") or {}
        onboarding = icp.get("onboarding") or {}
        onboarding["website_scan"] = {
            **scan,
            "url": req.url,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        }
        icp["onboarding"] = onboarding
        db.table("organisations").update({"icp_config": icp}).eq("id", req.org_id).execute()

    return scan


class MatchAccountsRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    limit: int = 5


@router.post("/solo/match-accounts")
def solo_match_accounts(
    req: MatchAccountsRequest, ctx: ApiContext = Depends(verify_api_key),
):
    """Surface the top accounts matching the org's (provisional) ICP and
    seed them as pending suggestions, so the wizard can offer "we already
    found these" before the onboarding call. Accepting one reuses the
    existing /suggestions/{id}/accept flow (cap enforcement + enrich +
    monitor).

    Mirrors the core of _topup_pool but runs synchronously, returns the
    rows (with ids), and is NOT gated on the account_suggestions flag —
    this is the onboarding path, available to every Solo signup.
    """
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    limit = max(1, min(int(req.limit or 5), 10))

    criteria = _get_icp_criteria(req.org_id)
    if not any(criteria.values()):
        return {"accounts": [], "reason": "no_icp_criteria"}

    cfg_res = (
        db.table("organisations").select("icp_config")
        .eq("id", req.org_id).maybe_single().execute()
    )
    icp_config = (cfg_res.data or {}).get("icp_config") or {}
    seller_profile = build_seller_profile(icp_config)

    # Don't surface domains already active/pending for this org.
    skip_domains: set[str] = set()
    existing = (
        db.table("watchlist").select("company_domain, status")
        .eq("org_id", req.org_id).execute()
    )
    for row in existing.data or []:
        dom = (row.get("company_domain") or "").lower().strip()
        if dom and row.get("status") in ("active", "pending"):
            skip_domains.add(dom)

    collected: List[Dict[str, Any]] = []

    def _seed(c: Dict[str, Any], source: str) -> None:
        """Insert a candidate as a pending suggestion + collect it for the
        wizard. Shared by the internal and Apollo paths."""
        dom = (c.get("domain") or "").strip().lower()
        if not dom or dom in skip_domains:
            return
        try:
            ins = (
                db.table("watchlist").insert({
                    "org_id": req.org_id,
                    "company_name": c.get("name"),
                    "company_domain": dom,
                    "status": "pending",
                    "suggested_reasons": c.get("matched_reasons") or [],
                }).execute()
            )
        except Exception as e:
            logger.info("match-accounts insert skipped (%s): %s", dom, e)
            return
        skip_domains.add(dom)
        row = (ins.data or [{}])[0] if ins.data else {}
        collected.append({
            "id": row.get("id"),
            "name": c.get("name"),
            "domain": dom,
            "industry": c.get("industry"),
            "size_band": c.get("size_band"),
            "hq_country": c.get("hq_country"),
            "hq_city": c.get("hq_city"),
            "matched_reasons": c.get("matched_reasons") or [],
            "source": source,
        })

    # 1) Internal first — recommend from companies we've ALREADY enriched.
    #    Free, instant, recruiter-free (clean stored taxonomy), and often
    #    pre-researched so a new user gets value with no enrichment wait.
    from app.services.internal_match import search_internal_companies
    internal = search_internal_companies(
        db,
        industries=criteria["industries"],
        locations=criteria["locations"],
        employee_ranges=criteria["employee_ranges"],
        exclude_domains=skip_domains,
        limit=limit,
    )
    for c in internal:
        if len(collected) >= limit:
            break
        _seed(c, "internal")

    # 2) Apollo fallback — only top up the shortfall (cold-start ICPs the
    #    internal pool doesn't cover yet). As the internal pool grows this path
    #    fires less and less.
    if len(collected) < limit:
        MAX_PAGES = 3
        for page in range(1, MAX_PAGES + 1):
            if len(collected) >= limit:
                break
            candidates = search_companies(
                industries=canonical_industries(criteria["industries"]) or None,
                locations=criteria["locations"] or None,
                employee_ranges=criteria["employee_ranges"] or None,
                per_page=50,
                page=page,
            )
            if not candidates:
                break
            candidates = [
                c for c in candidates
                if (c.get("domain") or "").lower().strip() not in skip_domains
            ]
            if not candidates:
                continue
            candidates = classify_candidates(candidates, seller_profile=seller_profile)
            for c in candidates:
                if len(collected) >= limit:
                    break
                _seed(c, "apollo")

    return {
        "accounts": collected,
        "internal_count": sum(1 for c in collected if c.get("source") == "internal"),
        "apollo_count": sum(1 for c in collected if c.get("source") == "apollo"),
    }


# ---------------------------------------------------------------------------
# Onboarding seed — the day-one dashboard (2026-07-21).
#
# The wizard used to hand back ~10 unenriched candidate names the user had to
# opt into. New model: the moment targeting is known we seed ICP-matched
# accounts as ACTIVE + monitored and enrich them IN PARALLEL, so by the time
# the wizard finishes the dashboard is already full of real, scored,
# signal-populated accounts. The frontend polls /solo/onboarding-status,
# streams a live discovery feed, and holds the door until 5 are enriched or a
# timeout — whichever first (the gate + timeout live in the frontend; the
# engine just enriches honestly and reports state).
#
# Enrichment is NOT charged to the user's monthly research allowance — this is
# comped onboarding, not the user spending their cap.
# ---------------------------------------------------------------------------

SEED_ENRICH_CONCURRENCY = 6  # bounded so a signup can't hammer vendor limits
SEED_ACCOUNT_CAP = 5  # accounts enriched on signup. Fewer = faster first screen
                      # and a shallower queue under a launch-day burst (30 users x
                      # 5 drains in ~a third the time of x12). Rest can be added later.


class SeedAccount(BaseModel):
    domain: str = Field(..., min_length=3)
    name: Optional[str] = None


class OnboardingSeedRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    # The accounts the USER chose (typeahead picks / suggestion chips). We
    # don't guess who to seed — they pick their real targets, so relevance
    # is 100% and the reveal lands on companies they actually care about.
    accounts: List[SeedAccount] = Field(default_factory=list)
    # Market these accounts belong to (054, adds from a non-primary market
    # page). Empty = the org's primary market (market_id NULL on the row).
    market_id: str = ""


class CompanySearchRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    query: str = Field(..., min_length=1, max_length=80)
    limit: int = Field(default=8, ge=1, le=10)


@router.post("/solo/company-search")
def solo_company_search(
    req: CompanySearchRequest, ctx: ApiContext = Depends(verify_api_key),
):
    """Typeahead for the onboarding account picker. Internal (already-enriched,
    so an instant reveal if picked) first, then Apollo fuzzy name match. Returns
    real companies with logo + domain so the user picks rather than us guessing
    a domain from a typed string — that's what makes it robust to misspellings
    and name/domain mismatches."""
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    q = req.query.strip()
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []

    try:
        rows = (db.table("companies").select("domain, name")
                .ilike("name", f"%{q}%").limit(6).execute().data or [])
        for r in rows:
            dom = (r.get("domain") or "").lower().strip()
            if dom and dom not in seen and r.get("name"):
                seen.add(dom)
                out.append({"name": r["name"], "domain": dom,
                            "logo_url": None, "source": "internal"})
    except Exception as e:  # noqa: BLE001
        logger.info("company-search internal lookup failed: %s", e)

    if len(out) < req.limit:
        for c in search_by_name(q, req.limit):
            if c["domain"] in seen:
                continue
            seen.add(c["domain"])
            out.append({**c, "source": "apollo"})
            if len(out) >= req.limit:
                break

    return {"results": out[:req.limit]}


def _seed_enrich_parallel(org_id: str, domains: List[str]) -> None:
    """Enrich each seeded account. With Redis, fan out onto the worker (which
    handles concurrency and keeps the work off the web tier). Without it, fall
    back to a local thread pool — FastAPI background tasks run sequentially, so
    the parallelism has to come from here; the work is I/O-bound vendor calls."""
    from app.config import settings
    if settings.REDIS_URL:
        from app.jobs import enqueue_lite_batch, enqueue_research_batch
        # Fast firmographic pass first so cards paint in seconds, then the deep
        # research streams the signals/score behind it.
        enqueue_lite_batch(domains)
        enqueue_research_batch(org_id, domains)
        return
    from concurrent.futures import ThreadPoolExecutor
    try:
        with ThreadPoolExecutor(max_workers=SEED_ENRICH_CONCURRENCY) as ex:
            futures = [ex.submit(_research_enrich, org_id, d) for d in domains]
            for f in futures:
                try:
                    f.result()
                except Exception as e:  # noqa: BLE001
                    logger.warning("seed enrich task failed: %s", e)
    except Exception as e:  # noqa: BLE001
        logger.error("seed enrich pool failed for %s: %s", org_id, e)


@router.post("/solo/onboarding-seed")
def solo_onboarding_seed(
    req: OnboardingSeedRequest,
    background_tasks: BackgroundTasks,
    ctx: ApiContext = Depends(verify_api_key),
):
    """Seed the accounts the USER picked (typeahead + suggestion chips) as
    active + monitored, and kick parallel enrichment. Returns immediately so
    the wizard shows them straight away; findings stream in as enrichment
    completes (polled via /solo/onboarding-status)."""
    ctx.enforce_org(req.org_id)
    db = get_supabase()

    # Market stamping (054): only a VERIFIED own-org market id is stamped;
    # anything else (foreign org's id, junk) degrades to NULL = primary.
    market_id = None
    if req.market_id:
        from app.services.markets import get_market
        market = get_market(db, req.org_id, req.market_id)
        market_id = market["id"] if market else None

    # Dedupe the picks, skip anything the org already has, and remember
    # soft-deleted rows — re-adding one must REACTIVATE it, not hit the
    # unique constraint and silently skip (customer report 2026-08-24).
    skip: set[str] = set()
    removed: Dict[str, str] = {}  # domain -> watchlist id (deleted/dismissed)
    for row in (db.table("watchlist").select("id, company_domain, status")
                .eq("org_id", req.org_id).execute().data or []):
        dom = (row.get("company_domain") or "").lower().strip()
        if not dom:
            continue
        if row.get("status") in ("active", "pending"):
            skip.add(dom)
        elif row.get("status") in ("deleted", "dismissed"):
            removed[dom] = row.get("id")

    from app.services.plan_limits import enforce_account_cap, enforce_watch_cap
    accounts: List[Dict[str, Any]] = []
    for a in req.accounts[:SEED_ACCOUNT_CAP]:  # cap the fan-out regardless of what's sent
        dom = (a.domain or "").lower().strip()
        if "://" in dom:
            dom = dom.split("://", 1)[1]
        dom = dom.split("/", 1)[0]
        if not dom or "." not in dom or dom in skip:
            continue
        # Cap per account: each seed lands an active + monitored account (a
        # standing weekly monitor AND a research pass), so a looped seed must
        # not exceed the plan's account/watch caps. Stop cleanly at the ceiling
        # rather than 403-ing the whole request — partial seed is fine.
        try:
            enforce_account_cap(req.org_id, db)
            enforce_watch_cap(req.org_id, db)
        except HTTPException:
            break
        skip.add(dom)
        if dom in removed:
            # Reactivate the soft-deleted row instead of colliding with the
            # unique constraint (the silent re-add bug, 2026-08-24).
            try:
                db.table("watchlist").update({
                    "status": "active",
                    "monitoring_active": True,
                    "dismissed_at": None,
                    "company_name": a.name,
                    "market_id": market_id,
                }).eq("id", removed[dom]).execute()
                accounts.append({"id": removed[dom], "name": a.name, "domain": dom})
            except Exception as e:  # noqa: BLE001
                logger.info("seed reactivate failed (%s): %s", dom, e)
            continue
        try:
            ins = db.table("watchlist").insert({
                "org_id": req.org_id,
                "company_name": a.name,
                "company_domain": dom,
                "status": "active",
                "monitoring_active": True,
                "market_id": market_id,
            }).execute()
        except Exception as e:  # noqa: BLE001
            logger.info("seed insert skipped (%s): %s", dom, e)
            continue
        row = (ins.data or [{}])[0]
        accounts.append({"id": row.get("id"), "name": a.name, "domain": dom})

    if not accounts:
        return {"accounts": [], "reason": "no_valid_accounts"}

    background_tasks.add_task(
        _seed_enrich_parallel, req.org_id, [a["domain"] for a in accounts]
    )
    return {"accounts": accounts}


class SuggestedAccountsRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    limit: int = Field(default=6, ge=1, le=10)


@router.post("/solo/suggested-accounts")
def solo_suggested_accounts(
    req: SuggestedAccountsRequest, ctx: ApiContext = Depends(verify_api_key),
):
    """Read-only ICP-matched cards for the picker — a nudge for users who
    can't think of 5 off the top of their head. Creates NOTHING; the user
    still chooses. Demoting the matcher from decider to helper is what makes
    its occasional bad match harmless ('didn't tap that one')."""
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    criteria = _get_icp_criteria(req.org_id)
    if not any(criteria.values()):
        return {"accounts": []}

    skip: set[str] = set()
    for row in (db.table("watchlist").select("company_domain, status")
                .eq("org_id", req.org_id).execute().data or []):
        dom = (row.get("company_domain") or "").lower().strip()
        if dom and row.get("status") in ("active", "pending"):
            skip.add(dom)

    out: List[Dict[str, Any]] = []
    from app.services.internal_match import search_internal_companies
    for c in search_internal_companies(
        db, industries=criteria["industries"], locations=criteria["locations"],
        employee_ranges=criteria["employee_ranges"], exclude_domains=skip, limit=req.limit,
    ):
        dom = (c.get("domain") or "").lower().strip()
        if dom and dom not in skip:
            skip.add(dom)
            out.append({"name": c.get("name"), "domain": dom,
                        "reason": (c.get("matched_reasons") or [None])[0]})

    if len(out) < req.limit:
        cfg = (db.table("organisations").select("icp_config")
               .eq("id", req.org_id).maybe_single().execute().data or {})
        seller_profile = build_seller_profile(cfg.get("icp_config") or {})
        found = search_companies(
            industries=canonical_industries(criteria["industries"]) or None,
            locations=criteria["locations"] or None,
            employee_ranges=criteria["employee_ranges"] or None,
            per_page=50, page=1,
        )
        found = [c for c in found if (c.get("domain") or "").lower().strip() not in skip]
        for c in classify_candidates(found, seller_profile=seller_profile):
            if len(out) >= req.limit:
                break
            dom = (c.get("domain") or "").lower().strip()
            if dom and dom not in skip:
                skip.add(dom)
                out.append({"name": c.get("name"), "domain": dom,
                            "reason": (c.get("matched_reasons") or [None])[0]})

    return {"accounts": out[:req.limit]}


class OnboardingStatusRequest(BaseModel):
    org_id: str = Field(..., min_length=1)


@router.post("/solo/onboarding-status")
def solo_onboarding_status(
    req: OnboardingStatusRequest, ctx: ApiContext = Depends(verify_api_key),
):
    """Live state of the seed for the wizard's discovery feed + gate.

    Per account: enriched (has a profile score = the first pass finished) and
    a single real headline finding. The frontend releases its hold when
    `enriched_count` >= 5 (or its own timeout). Honest by construction —
    'enriched' means the monitor pass actually completed, nothing inferred.
    """
    ctx.enforce_org(req.org_id)
    db = get_supabase()

    wl = (db.table("watchlist").select("company_name, company_domain")
          .eq("org_id", req.org_id).eq("status", "active").execute().data or [])
    domains = [(w.get("company_domain") or "").lower() for w in wl if w.get("company_domain")]
    if not domains:
        return {"accounts": [], "enriched_count": 0, "total": 0}

    companies = (db.table("companies")
                 .select("id, domain, name, industry, size_band, profile_score, credential_exposure")
                 .in_("domain", domains).execute().data or [])
    by_dom = {(c.get("domain") or "").lower(): c for c in companies}

    # Open security roles per company (the strongest day-one hook).
    id_to_dom = {c["id"]: (c.get("domain") or "").lower() for c in companies if c.get("id")}
    sec_by_company: Dict[str, int] = {}
    if id_to_dom:
        jobs = (db.table("company_job_postings").select("company_id")
                .in_("company_id", list(id_to_dom.keys()))
                .eq("is_security", True).is_("closed_at", "null").execute().data or [])
        for j in jobs:
            dom = id_to_dom.get(j["company_id"])
            if dom:
                sec_by_company[dom] = sec_by_company.get(dom, 0) + 1

    accounts, enriched, basic_ready = [], 0, 0
    for w in wl:
        dom = (w.get("company_domain") or "").lower()
        c = by_dom.get(dom) or {}
        score = c.get("profile_score") if isinstance(c.get("profile_score"), dict) else None
        is_enriched = bool(score)
        if is_enriched:
            enriched += 1
        # Firmographics landed (fast pass) even before the deep score — enough
        # to paint a real card and release the user into the dashboard.
        is_basic = bool(c.get("industry") or c.get("size_band"))
        if is_basic:
            basic_ready += 1
        # One real headline, in priority order. Never fabricated.
        headline = None
        sec = sec_by_company.get(dom, 0)
        cred = c.get("credential_exposure") if isinstance(c.get("credential_exposure"), dict) else None
        if sec:
            headline = f"{sec} open security role{'s' if sec != 1 else ''}"
        elif cred and cred.get("recent_90d"):
            headline = "Credentials exposed"
        elif isinstance(score, dict) and isinstance(score.get("composite"), (int, float)):
            headline = f"Profile score {int(score['composite'])}"
        accounts.append({
            "name": w.get("company_name") or dom, "domain": dom,
            "enriched": is_enriched, "basic_ready": is_basic, "headline": headline,
            "industry": c.get("industry"), "size_band": c.get("size_band"),
        })

    return {
        "accounts": accounts,
        "enriched_count": enriched,
        "basic_ready_count": basic_ready,
        "total": len(accounts),
    }


class ExportRequest(BaseModel):
    org_id: str = Field(..., min_length=1)


@router.post("/export")
def export_accounts(
    req: ExportRequest, ctx: ApiContext = Depends(verify_api_key),
):
    """ABM-ready export of the org's active accounts: the target-account
    list + intelligence + buying committee, so marketing can push it into
    their ad platform / CRM. Read-only; reads watchlist + companies
    (global facts) only — no cross-tenant data.
    """
    ctx.enforce_org(req.org_id)
    db = get_supabase()

    wl = (
        db.table("watchlist")
        .select("company_name, company_domain")
        .eq("org_id", req.org_id)
        .eq("status", "active")
        .execute()
        .data
    ) or []

    domains = list({(w.get("company_domain") or "").lower() for w in wl if w.get("company_domain")})
    comps: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(domains), 100):  # chunk to keep the .in_ query sane
        chunk = domains[i:i + 100]
        if not chunk:
            continue
        rows = (
            db.table("companies")
            .select("domain, industry, size_band, hq_country, profile_score, why_this_matters, key_contacts")
            .in_("domain", chunk)
            .execute()
            .data
        ) or []
        for c in rows:
            comps[(c.get("domain") or "").lower()] = c

    accounts: List[Dict[str, Any]] = []
    for w in wl:
        dom = (w.get("company_domain") or "").lower()
        c = comps.get(dom) or {}
        ps = c.get("profile_score") if isinstance(c.get("profile_score"), dict) else {}
        wtm = c.get("why_this_matters") if isinstance(c.get("why_this_matters"), dict) else {}
        contacts = c.get("key_contacts") if isinstance(c.get("key_contacts"), list) else []
        peer = ps.get("peer_band") if isinstance(ps.get("peer_band"), dict) else {}
        committee = "; ".join(
            f"{p.get('name')} ({p.get('title')})"
            for p in contacts
            if isinstance(p, dict) and p.get("name")
        )
        para = wtm.get("paragraph")
        accounts.append({
            "company": w.get("company_name") or "",
            "domain": dom,
            "industry": c.get("industry") or "",
            "employees": c.get("size_band") or "",
            "country": c.get("hq_country") or "",
            "profile_score": ps.get("composite"),
            "risk": ps.get("risk"),
            "posture": ps.get("posture"),
            "maturity": ps.get("maturity"),
            "commercial": ps.get("commercial"),
            "peer_percentile": peer.get("percentile"),
            "why_this_matters": para.strip() if isinstance(para, str) else "",
            "buying_committee": committee,
            "num_contacts": len(contacts),
        })

    # Highest profile score first — the accounts to prioritise in a campaign.
    accounts.sort(key=lambda a: (a.get("profile_score") or -1), reverse=True)
    return {"accounts": accounts, "count": len(accounts)}


class AccountPotentialRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    domain: str = Field(..., min_length=1)


@router.post("/potential")
def account_potential(
    req: AccountPotentialRequest, ctx: ApiContext = Depends(verify_api_key),
):
    """Deterministic deal-potential estimate for an account, scaled per-org.

    Pure maths (no LLM, no vendor calls): the org's average deal size,
    multiplied by account-fit factors — company size, annual revenue,
    industry, live signals, cyber relevance — via the shared
    estimate_revenue_potential model. Computed on read because the
    company row is global but the baseline is per-org.
    """
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    from app.services.revenue_potential import estimate_revenue_potential, _unknown_result
    from app.services.client_context import get_client_context

    client_ctx = get_client_context(db, req.org_id)
    currency = (client_ctx.get("currency") or "GBP").upper()

    domain = req.domain.strip().lower()
    co = (
        db.table("companies")
        .select("id, industry, size_band, apollo_data")
        .eq("domain", domain).limit(1).execute()
    )
    if not co.data:
        return _unknown_result(currency, "Account not enriched yet.")
    company = co.data[0]
    apollo = company.get("apollo_data") or {}
    company_size = apollo.get("estimated_num_employees") or company.get("size_band")
    annual_revenue = (
        apollo.get("annual_revenue")
        or apollo.get("organization_revenue")
    )
    industry = company.get("industry") or apollo.get("industry") or ""

    # Live signals for the urgency + cyber-relevance multipliers (best-effort).
    signals_map: Dict[str, bool] = {}
    findings: List[Dict[str, Any]] = []
    try:
        sig = (
            db.table("signals").select("signal_type")
            .eq("company_id", company["id"])
            .order("detected_at", desc=True).limit(40).execute()
        )
        for s in (sig.data or []):
            st = s.get("signal_type")
            if st:
                signals_map[st] = True
                findings.append({"signal_type": st})
    except Exception as e:
        logger.warning("account_potential: signal fetch failed for %s: %s", domain, e)

    return estimate_revenue_potential(
        lead={
            "company_size": company_size,
            "company_industry": industry,
            "annual_revenue": annual_revenue,
        },
        client_ctx=client_ctx,
        signals=signals_map,
        findings=findings,
    )


def _auto_promote_monitoring(db, org_id: str, domain: str) -> None:
    """Committed-action promotion (2026-06-11): when a rep unlocks a
    contact or enables person intel on a Research account, they're
    clearly working it — turn monitoring on. Only affects
    decoupled-monitoring orgs (others are always monitored, so this is a
    no-op for them and we skip the write). Best-effort; never raises."""
    from app.services.org_features import org_feature_enabled
    if not org_feature_enabled(org_id, "decoupled_monitoring", db):
        return
    dom = (domain or "").strip().lower()
    if not dom:
        return
    # Caps tiers: don't auto-promote past the watched cap. The committed
    # action (unlock / person intel) still succeeds; the account just
    # stays Research until the user frees a watch slot.
    from app.services.plan_limits import _org_is_capped, current_usage, effective_limits
    if _org_is_capped(org_id, db):
        if current_usage(org_id, db)["watched"] >= effective_limits(org_id, db)["watched"]:
            return
    try:
        db.table("watchlist").update(
            {"monitoring_active": True, "updated_at": datetime.now(timezone.utc).isoformat()}
        ).eq("org_id", org_id).ilike("company_domain", dom).eq("status", "active").execute()
    except Exception as e:
        logger.warning("auto-promote monitoring failed for %s/%s: %s", org_id, dom, e)


class MonitoringToggleRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    watchlist_id: str = Field(..., min_length=1)
    active: bool


@router.post("/monitoring")
def set_monitoring(
    req: MonitoringToggleRequest, ctx: ApiContext = Depends(verify_api_key),
):
    """Pause or resume weekly monitoring for one watched account.
    Scoped to the org so a watchlist id alone can't toggle another
    org's account."""
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    # Solo plan: turning monitoring ON counts against the 15-watched cap.
    if req.active:
        from app.services.plan_limits import enforce_watch_cap
        enforce_watch_cap(req.org_id, db)
    res = (
        db.table("watchlist")
        .update({
            "monitoring_active": req.active,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        .eq("id", req.watchlist_id)
        .eq("org_id", req.org_id)
        .eq("status", "active")
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="account not found")
    return {
        "ok": True,
        "watchlist_id": req.watchlist_id,
        "monitoring_active": req.active,
    }


# ---------------------------------------------------------------------------
# Per-person monitoring — the Solo "watch N people" lever. A lead with
# person_monitoring=true is scraped on the weekly person-monitor pass
# (role change, posts, competitor engagement → person_* signals),
# independent of whether their company is a watched account. Distinct from
# the account-level person-intel toggle below, which is the discovery
# mechanism. The standing 'monitored_people' cap is enforced on turn-ON
# for Solo orgs; the action is free (no per-use charge).
# ---------------------------------------------------------------------------
class PersonMonitoringRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    lead_id: str = Field(..., min_length=1)
    active: bool


def _kick_person_monitoring_initial_run(org_id: str, lead_id: str) -> None:
    """Background — run the first person-monitor pass right after the rep
    hits Watch, so signals appear without waiting for the weekly cron.
    Soft-fails: logged, never raises out of the background task."""
    try:
        from app.services.watchlist.person_monitor import monitor_one_person
        monitor_one_person(get_supabase(), org_id=org_id, lead_id=lead_id, force=True)
    except Exception as e:
        logger.warning(
            "person-monitoring kick failed for lead %s: %s", lead_id, e
        )


@router.post("/people/monitoring")
def set_person_monitoring(
    req: PersonMonitoringRequest,
    background_tasks: BackgroundTasks,
    ctx: ApiContext = Depends(verify_api_key),
):
    """Start/stop watching a specific person (a lead). Scoped to the org so
    a lead id alone can't toggle another org's person. Turning ON counts
    against the Solo 'people watched' standing cap and fires an immediate
    first monitor pass so signals don't wait for the weekly cron."""
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    if req.active:
        from app.services.plan_limits import enforce_monitored_people_cap
        enforce_monitored_people_cap(req.org_id, db)
    res = (
        db.table("leads")
        .update({
            "person_monitoring": req.active,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        .eq("id", req.lead_id)
        .eq("org_id", req.org_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="person not found")
    if req.active:
        background_tasks.add_task(
            _kick_person_monitoring_initial_run, req.org_id, req.lead_id
        )
    return {
        "ok": True,
        "lead_id": req.lead_id,
        "person_monitoring": req.active,
    }


# ---------------------------------------------------------------------------
# Person-intel toggle — per-account opt-in for Phase D person scraping.
# Lives here (rather than in its own router) because it's small and shares
# the same /accounts prefix.
# ---------------------------------------------------------------------------
class PersonIntelRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    mode: str = Field(..., pattern="^(off|top1|deep)$")


def _kick_person_intel_initial_run(org_id: str, company_id: str) -> None:
    """Background helper — fires the first person-intel pass immediately
    after a rep flips the toggle on, instead of making them wait for the
    next watchlist cron (spec 2026-06-10).

    Two stages, fastest-visible first:
      1. discover_key_contacts: Apollo title search, ~2-5s, FREE
         (search-only, no credits). Writes companies.key_contacts so
         the "People at this account" list populates within seconds.
      2. _run_phase_d_person_intel: the full Apify scrape + Haiku
         classification. Takes 30-90s depending on contact count.
         Costs the per-pass credits the rep already agreed to via
         slide-to-confirm (1 for top1, 2 for deep).

    Soft-failures: each stage is logged and continues; never raises
    out of the background task.
    """
    from app.database import get_supabase as _get_db
    from app.services.key_contacts import discover_key_contacts

    db = _get_db()

    # 1. Discovery (fast, free) — names + titles visible immediately
    try:
        # Pick max contacts based on the just-set mode.
        co = (
            db.table("companies")
            .select("person_intel_enabled")
            .eq("id", company_id)
            .maybe_single()
            .execute()
            .data
            or {}
        )
        mode = (co.get("person_intel_enabled") or "off").lower()
        if mode == "off":
            return  # someone flipped it back to off before we ran
        max_contacts = 1 if mode == "top1" else 3
        discover_key_contacts(
            db, company_id=company_id, org_id=org_id,
            max_contacts=max_contacts, force_refresh=True,
        )
    except Exception as e:
        logger.warning(
            "Person-intel kick: discovery failed for company %s: %s",
            company_id, e,
        )
        # Continue to Phase D anyway — it'll do its own discovery.

    # 2. Full Phase D pass (Apify scrape + classify + signal write)
    try:
        from app.services.watchlist.monitor import _run_phase_d_person_intel
        _run_phase_d_person_intel(db, company_id=company_id, org_id=org_id)
    except Exception as e:
        logger.error(
            "Person-intel kick: Phase D failed for company %s: %s",
            company_id, e,
        )


@router.post("/{company_id}/person-intel")
def set_person_intel(
    company_id: str,
    req: PersonIntelRequest,
    background_tasks: BackgroundTasks,
    ctx: ApiContext = Depends(verify_api_key),
):
    """Flip person_intel_enabled for a single account (off|top1|deep).
    Real-money commit — the dashboard guards this behind a slide-to-
    confirm. Returns the updated company row's relevant fields.

    When toggling ON (top1 or deep), kicks the first person-intel
    pass as a BackgroundTask so the rep sees contacts within seconds
    instead of waiting for the next watchlist monitor pass (spec
    2026-06-10).
    """
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    # Solo plan: turning person intel ON counts against the 5-account cap.
    if req.mode in ("top1", "deep"):
        from app.services.plan_limits import enforce_person_intel_cap
        enforce_person_intel_cap(req.org_id, db)
    # Verify the company belongs to this org (don't let one org poke
    # another org's accounts). The watchlist row is the source of truth.
    wl = (
        db.table("watchlist")
        .select("id")
        .eq("org_id", req.org_id)
        .ilike(
            "company_domain",
            (
                db.table("companies")
                .select("domain")
                .eq("id", company_id)
                .maybe_single()
                .execute()
                .data
                or {}
            ).get("domain")
            or "__none__",
        )
        .limit(1)
        .execute()
    )
    if not wl.data:
        raise HTTPException(
            status_code=403,
            detail="company is not on this org's watchlist",
        )

    res = (
        db.table("companies")
        .update({"person_intel_enabled": req.mode})
        .eq("id", company_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="company not found")

    # Kick the initial run on toggle-ON only; turning off does nothing
    # in the background (no contacts to clear; future Phase D passes
    # will simply skip because mode='off').
    if req.mode in ("top1", "deep"):
        background_tasks.add_task(
            _kick_person_intel_initial_run, req.org_id, company_id,
        )
        # Enabling person intel is a committed action → auto-promote the
        # account to monitored (decoupled-monitoring orgs only).
        _co = (
            db.table("companies").select("domain").eq("id", company_id)
            .maybe_single().execute()
        )
        _dom = ((_co.data or {}) if _co else {}).get("domain")
        if _dom:
            _auto_promote_monitoring(db, req.org_id, _dom)

    return {
        "ok": True,
        "company_id": company_id,
        "person_intel_enabled": req.mode,
    }


@router.post("/suggestions/{suggestion_id}/dismiss")
def dismiss_suggestion(
    suggestion_id: str,
    req: SuggestionActionRequest,
    background_tasks: BackgroundTasks,
    ctx: ApiContext = Depends(verify_api_key),
):
    """Mark a pending suggestion dismissed. Won't re-suggest for 90 days.
    Top-up fires in the background. org_id required + update scoped to
    it (same isolation rule as accept)."""
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    now_iso = datetime.now(timezone.utc).isoformat()
    res = (
        db.table("watchlist")
        .update(
            {
                "status": "dismissed",
                "dismissed_at": now_iso,
                "monitoring_active": False,
                "updated_at": now_iso,
            }
        )
        .eq("id", suggestion_id)
        .eq("org_id", req.org_id)
        .eq("status", "pending")
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="suggestion not found or already actioned")
    background_tasks.add_task(_topup_pool, req.org_id)
    return {"ok": True, "id": suggestion_id}


# ---------------------------------------------------------------------------
# Why-this-matters refresh — spec 2026-06-08
#
# Called by:
#   - an optional weekly safety-net cron (POST with {org_id, all: true})
#   - Manual refresh by the operator / debug (POST with {company_id})
#
# Engine-side Phase E of the monitor pass also calls
# refresh_why_this_matters internally — this endpoint is the EXTERNAL
# trigger surface.
# ---------------------------------------------------------------------------
class WhyMattersRefreshRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    company_id: Optional[str] = Field(default=None)
    all: Optional[bool] = Field(default=False)


@router.post("/why-this-matters/refresh")
def refresh_why_this_matters_endpoint(
    req: WhyMattersRefreshRequest, ctx: ApiContext = Depends(verify_api_key),
):
    """Refresh the cached 'why this account matters' paragraph(s).

    If `company_id` is provided → refresh that one account.
    If `all=true` → refresh every active watchlist account for the org
    (the weekly cron path; force=True so cron always rewrites to keep
    relative time language fresh).
    """
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    if req.company_id:
        record = refresh_why_this_matters(
            db,
            company_id=req.company_id,
            org_id=req.org_id,
            trigger="manual",
            force=True,
        )
        return {"ok": True, "refreshed": 1, "company_id": req.company_id, "record": record}

    if not req.all:
        raise HTTPException(
            status_code=400,
            detail="provide company_id, or set all=true to refresh every watchlist account",
        )

    # Cron path — refresh every account on this org's watchlist.
    return {"ok": True, "refreshed": _refresh_why_all_for_org(db, req.org_id)}


def _refresh_why_all_for_org(db, org_id: str) -> int:
    """Refresh the WTM paragraph for every active watchlist account of one
    org (force=True so relative-time language stays fresh). Returns the
    number refreshed."""
    from app.services.plan_limits import org_lapsed
    if org_lapsed(org_id, db):
        return 0
    wl = (
        db.table("watchlist")
        .select("company_domain")
        .eq("org_id", org_id)
        .eq("status", "active")
        .execute()
    )
    domains = [
        (r.get("company_domain") or "").lower().strip()
        for r in (wl.data or [])
        if r.get("company_domain")
    ]
    if not domains:
        return 0

    co_res = (
        db.table("companies")
        .select("id, domain")
        .in_("domain", domains)
        .execute()
    )
    refreshed = 0
    for c in co_res.data or []:
        try:
            refresh_why_this_matters(
                db,
                company_id=c["id"],
                org_id=org_id,
                trigger="cron",
                force=True,
            )
            refreshed += 1
        except Exception as e:
            logger.warning("Cron refresh failed for company %s: %s", c.get("id"), e)
    return refreshed


# In-process debounce for the public refresh-solo trigger — same idea as the
# topup debounce: repeat hits inside the window are no-ops so the endpoint
# can stay unauthenticated (the /watchlist/monitor-solo precedent) without
# letting anyone burn Haiku spend by hammering it.
_WHY_SOLO_LOCK = threading.Lock()
_WHY_SOLO_LAST_RUN = {"ts": 0.0}
_WHY_SOLO_DEBOUNCE_SECONDS = 6 * 3600


def _run_why_refresh_solo_bg() -> None:
    """Background sweep: WTM refresh for every capped-tier org with live
    billing. Mirrors /watchlist/monitor-solo — one global cron covers every
    self-serve org, so research accounts (which never get a monitor pass)
    keep a fresh paragraph too."""
    try:
        db = get_supabase()
        from app.services.plan_limits import CAPPED_TIERS, REVOKED_STATUSES
        orgs = (
            db.table("organisations")
            .select("id, subscription_status")
            .in_("tier", list(CAPPED_TIERS))
            .execute()
        )
        total = 0
        for row in orgs.data or []:
            if (row.get("subscription_status") or "").lower() in REVOKED_STATUSES:
                continue
            try:
                total += _refresh_why_all_for_org(db, row["id"])
            except Exception as e:
                logger.warning("why-refresh-solo failed for org %s: %s", row.get("id"), e)
        logger.info("why-refresh-solo: refreshed %d account paragraphs", total)
    except Exception as e:
        logger.error("why-refresh-solo sweep failed: %s", e)


@router.post("/why-this-matters/refresh-solo")
def refresh_why_this_matters_solo(background_tasks: BackgroundTasks):
    """Weekly cron trigger: refresh the WTM paragraph for every capped-tier
    (self-serve) org's active accounts. No body, no org input — the engine
    iterates internally, so new signups are covered without per-org cron config."""
    now = time.time()
    with _WHY_SOLO_LOCK:
        if now - _WHY_SOLO_LAST_RUN["ts"] < _WHY_SOLO_DEBOUNCE_SECONDS:
            return {"status": "debounced"}
        _WHY_SOLO_LAST_RUN["ts"] = now
    background_tasks.add_task(_run_why_refresh_solo_bg)
    return {"status": "started", "scope": "solo"}


# ---------------------------------------------------------------------------
# Profile score refresh — spec 2026-06-09.
#
# Two endpoints, mirroring the WTM refresh pattern:
#   POST /accounts/profile/refresh         — one company OR all for an org
#   POST /accounts/security-team/refresh   — same for the headcount profile
#
# Profile recomputation is cheap (no AI calls); team profile is one
# Apollo search per account. Used for backfills + on-demand refresh
# when the rep wants the freshest read on the dossier.
# ---------------------------------------------------------------------------
class ProfileRefreshRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    company_id: Optional[str] = Field(default=None)
    all: Optional[bool] = Field(default=False)


def _watchlist_company_ids(db, org_id: str) -> List[str]:
    """Resolve the org's active watchlist domains -> companies.id list."""
    wl = (
        db.table("watchlist")
        .select("company_domain")
        .eq("org_id", org_id)
        .eq("status", "active")
        .execute()
    )
    domains = [
        (r.get("company_domain") or "").lower().strip()
        for r in (wl.data or [])
        if r.get("company_domain")
    ]
    if not domains:
        return []
    co_res = (
        db.table("companies").select("id").in_("domain", domains).execute()
    )
    return [c["id"] for c in (co_res.data or []) if c.get("id")]


@router.post("/profile/refresh")
def refresh_profile_endpoint(
    req: ProfileRefreshRequest, ctx: ApiContext = Depends(verify_api_key),
):
    """Recompute the account Profile score (Risk / Posture / Maturity /
    Commercial + composite + peer band) for one company or every active
    watchlist account for an org. Cheap — no AI calls, just signal
    aggregation."""
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    if req.company_id:
        record = refresh_profile_score(
            db, company_id=req.company_id, org_id=req.org_id,
        )
        return {
            "ok": True, "refreshed": 1 if record else 0,
            "company_id": req.company_id, "record": record,
        }
    if not req.all:
        raise HTTPException(
            status_code=400,
            detail="provide company_id, or set all=true to refresh every watchlist account",
        )
    company_ids = _watchlist_company_ids(db, req.org_id)
    refreshed = 0
    for cid in company_ids:
        try:
            if refresh_profile_score(db, company_id=cid, org_id=req.org_id):
                refreshed += 1
        except Exception as e:
            logger.warning("Profile refresh failed for company %s: %s", cid, e)
    return {"ok": True, "refreshed": refreshed, "total": len(company_ids)}


@router.post("/security-team/refresh")
def refresh_security_team_endpoint(
    req: ProfileRefreshRequest, ctx: ApiContext = Depends(verify_api_key),
):
    """Refresh the cached security team headcount profile. Force=True
    bypasses the 30-day freshness gate (one Apollo search per account,
    free — search-only, no reveals)."""
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    if req.company_id:
        record = refresh_security_team_profile(
            db, company_id=req.company_id, force=True,
        )
        return {
            "ok": True, "refreshed": 1 if record else 0,
            "company_id": req.company_id, "record": record,
        }
    if not req.all:
        raise HTTPException(
            status_code=400,
            detail="provide company_id, or set all=true to refresh every watchlist account",
        )
    company_ids = _watchlist_company_ids(db, req.org_id)
    refreshed = 0
    for cid in company_ids:
        try:
            if refresh_security_team_profile(db, company_id=cid, force=True):
                refreshed += 1
        except Exception as e:
            logger.warning(
                "Security team refresh failed for company %s: %s", cid, e,
            )
    return {"ok": True, "refreshed": refreshed, "total": len(company_ids)}


# ---------------------------------------------------------------------------
# Website posture backfill — spec 2026-06-09.
#
# The website posture detector (security_page/trust_center/bug_bounty/
# privacy/etc + security.txt probe + status subdomain probe) historically
# only ran during per-lead enrichment, so account-keyed Posture coverage
# is patchy — the first rollout had 0 posture on 11 of 18 accounts because
# they were watchlist-only (no lead present in the org).
#
# This endpoint runs the same projector keyed on company_id, using the
# already-cached companies.deep_research_data as input (no extra
# Perplexity spend). The two live probes (security.txt, status subdomain)
# are cheap HTTP calls.
#
# Wipes stale company-keyed posture rows before each refresh so repeat
# runs don't accumulate duplicates.
# ---------------------------------------------------------------------------
_POSTURE_SIGNAL_TYPES = [
    "trust_center_present",
    "security_page_present",
    "privacy_page_present",
    "responsible_disclosure_present",
    "bug_bounty_present",
    "certification_claim_detected",
    "security_contact_present",
    "status_page_present",
    "public_proof_not_visible",
]


@router.post("/website-posture/refresh")
def refresh_website_posture_endpoint(
    req: ProfileRefreshRequest, ctx: ApiContext = Depends(verify_api_key),
):
    """Re-project website posture signals for one company OR every active
    watchlist account in an org. Writes company_id-keyed signals.
    Returns {refreshed: int, signals_written: int, total: int}.
    """
    ctx.enforce_org(req.org_id)
    db = get_supabase()

    if req.company_id:
        n = _backfill_posture_for_company(db, req.org_id, req.company_id)
        return {
            "ok": True, "refreshed": 1 if n else 0,
            "signals_written": n, "company_id": req.company_id,
        }

    if not req.all:
        raise HTTPException(
            status_code=400,
            detail="provide company_id, or set all=true to refresh every watchlist account",
        )

    # Pull every active watchlist company for this org
    wl = (
        db.table("watchlist")
        .select("company_domain")
        .eq("org_id", req.org_id)
        .eq("status", "active")
        .execute()
    )
    domains = [
        (r.get("company_domain") or "").lower().strip()
        for r in (wl.data or [])
        if r.get("company_domain")
    ]
    if not domains:
        return {"ok": True, "refreshed": 0, "signals_written": 0, "total": 0}

    co_res = (
        db.table("companies")
        .select("id")
        .in_("domain", domains)
        .execute()
    )
    company_ids = [c["id"] for c in (co_res.data or []) if c.get("id")]

    refreshed = 0
    total_written = 0
    for cid in company_ids:
        try:
            n = _backfill_posture_for_company(db, req.org_id, cid)
            total_written += n
            if n > 0:
                refreshed += 1
        except Exception as e:
            logger.warning(
                "Posture refresh failed for company %s: %s", cid, e,
            )
    return {
        "ok": True,
        "refreshed": refreshed,
        "signals_written": total_written,
        "total": len(company_ids),
    }


def _backfill_posture_for_company(db, org_id: str, company_id: str) -> int:
    """Wipe + re-project website posture signals for one company.
    Returns the number of new rows written."""
    co_res = (
        db.table("companies")
        .select("id, name, domain, industry, deep_research_data")
        .eq("id", company_id)
        .limit(1)
        .execute()
    )
    rows_meta = co_res.data or []
    if not rows_meta:
        return 0
    company = rows_meta[0]
    domain = (company.get("domain") or "").strip().lower()
    if not domain:
        return 0

    # 1. Wipe any existing company-keyed posture rows for this org/company
    try:
        db.table("signals").delete().eq(
            "org_id", org_id,
        ).eq("company_id", company_id).in_(
            "signal_type", _POSTURE_SIGNAL_TYPES,
        ).execute()
    except Exception as e:
        logger.warning(
            "Posture wipe failed for company %s: %s", company_id, e,
        )

    # 2. Project + write new rows (axis is auto-derived in signal_writer)
    rows = create_website_posture_signals_for_company(
        org_id=org_id,
        company_id=company_id,
        domain=domain,
        company_name=company.get("name"),
        deep_research_data=company.get("deep_research_data") or {},
        industry=company.get("industry"),
    )
    if not rows:
        return 0
    return insert_signals(db, rows)


# ---------------------------------------------------------------------------
# Contact unlock — spec 2026-06-10.
#
# When a rep wants to act on a person surfaced in companies.key_contacts
# (whether from cyber-targeted Apollo, broader-leadership Apollo fallback,
# or future sources), they need:
#   1. Full name + email + phone (Apollo people-match)
#   2. A row in `leads` so the existing /dashboard/leads/[id] page
#      with all the enrichment pipeline kicks in
#   3. A way to navigate there immediately
#
# Costs 1 credit (credits.COST["person_unlock"] = 1). Slide-to-confirm
# on the frontend gates the spend.
# ---------------------------------------------------------------------------
class UnlockContactRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    # Optional person hints. The accounts People tab can surface contacts from
    # a LIVE Apollo search whose apollo_id isn't yet in companies.key_contacts;
    # passing the name/title lets us match + unlock + upsert them anyway.
    name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    title: Optional[str] = None
    linkedin_url: Optional[str] = None


def _upsert_people_cache(
    db,
    org_id: str,
    apollo_id: str,
    domain: str,
    person_data: Dict[str, Any],
    lead_id: Optional[str] = None,
) -> None:
    """Write the revealed person into apollo_people_cache.

    apollo_people_cache (org_id, apollo_id) is THE per-org unlock record:
    the apollo-people edge function (find_people in chat + MCP, and the
    dashboard live people search) marks contacts unlocked and surfaces
    revealed emails ONLY from here, and lead_id powers the per-org
    "View lead" link. Unlock state must never be written to the global
    companies.key_contacts row — that leaked one org's paid reveals to
    every other org watching the same domain. Non-fatal on failure: the
    lead still exists.
    """
    try:
        row: Dict[str, Any] = {
            "org_id": org_id,
            "apollo_id": str(apollo_id),
            "company_domain": (domain or "").lower(),
            "person_data": person_data,
        }
        if lead_id:
            row["lead_id"] = lead_id
        db.table("apollo_people_cache").upsert(
            row,
            on_conflict="org_id,apollo_id",
        ).execute()
    except Exception as e:
        logger.warning(
            "apollo_people_cache upsert failed for %s: %s", apollo_id, e
        )


@router.post("/{company_id}/people/{apollo_id}/unlock")
def unlock_contact(
    company_id: str,
    apollo_id: str,
    req: UnlockContactRequest,
    background_tasks: BackgroundTasks,
    ctx: ApiContext = Depends(verify_api_key),
):
    """Reveal contact details for one key_contacts entry and create
    a `leads` row so the rep can navigate to the full lead page.

    Verifies the org owns the company via the watchlist before any
    credit is charged or any Apollo call is made.

    Returns: {ok: bool, lead_id: str, already_unlocked: bool}
    """
    ctx.enforce_org(req.org_id)
    db = get_supabase()

    # 1. Verify org ownership
    company_res = (
        db.table("companies")
        .select("id, domain, name, key_contacts")
        .eq("id", company_id)
        .maybe_single()
        .execute()
    )
    company = (company_res.data or {}) if company_res else {}
    if not company:
        raise HTTPException(status_code=404, detail="company not found")
    domain = (company.get("domain") or "").lower().strip()
    if not domain:
        raise HTTPException(status_code=400, detail="company has no domain")

    wl = (
        db.table("watchlist")
        .select("id")
        .eq("org_id", req.org_id)
        .ilike("company_domain", domain)
        .limit(1)
        .execute()
    )
    if not wl.data:
        raise HTTPException(
            status_code=403,
            detail="company is not on this org's watchlist",
        )

    # 2. Already unlocked BY THIS ORG? The per-org unlock record is
    #    apollo_people_cache (org_id, apollo_id) — never key_contacts,
    #    which lives on the global companies row and may carry another
    #    org's state. Checked before target resolution so a retry without
    #    name hints still short-circuits instead of 404ing.
    try:
        cache_res = (
            db.table("apollo_people_cache")
            .select("lead_id, person_data")
            .eq("org_id", req.org_id)
            .eq("apollo_id", str(apollo_id))
            .maybe_single()
            .execute()
        )
        cached_unlock = (cache_res.data or {}) if cache_res else {}
    except Exception:
        cached_unlock = {}
    if cached_unlock:
        cached_lead_id = cached_unlock.get("lead_id")
        if not cached_lead_id:
            # Pre-lead_id cache rows: resolve the org's lead via the
            # revealed email so the response still links somewhere useful.
            cached_email = (cached_unlock.get("person_data") or {}).get("email")
            if cached_email:
                try:
                    r = (
                        db.table("leads")
                        .select("id")
                        .eq("org_id", req.org_id)
                        .eq("email", cached_email)
                        .limit(1)
                        .execute()
                    )
                    cached_lead_id = r.data[0]["id"] if r.data else None
                except Exception:
                    cached_lead_id = None
        if cached_lead_id:
            return {
                "ok": True,
                "lead_id": cached_lead_id,
                "already_unlocked": True,
            }
        # Cache row with no resolvable lead (legacy edge-function row) —
        # fall through to the fresh path; the email dedupe below prevents
        # a duplicate lead and the charge guard prevents double-charging.

    # 2a. Find the contact in key_contacts by apollo_id
    contacts = company.get("key_contacts") or []
    if not isinstance(contacts, list):
        contacts = []
    target_idx = None
    target = None
    for i, c in enumerate(contacts):
        if isinstance(c, dict) and str(c.get("apollo_id")) == str(apollo_id):
            target_idx = i
            target = c
            break
    if target is None:
        # Not in the cached people — the People tab and find_people can
        # offer contacts from a live Apollo search. Accept the request
        # hints; with NO hints, resolve the person live from Apollo before
        # failing (2026-08-24: agents don't reliably re-send
        # name/title from find_people results, so unlocks 409'd on ids
        # find_people itself had just returned — for five hours).
        hint_name = (
            req.name or " ".join(p for p in [req.first_name, req.last_name] if p)
        ).strip()
        if hint_name:
            target = {
                "apollo_id": apollo_id,
                "name": hint_name,
                "title": req.title or "",
                "linkedin_url": req.linkedin_url or "",
            }
        else:
            from app.services.key_contacts import resolve_person_by_id
            live = resolve_person_by_id(
                db, org_id=req.org_id,
                domain=(company.get("domain") or ""),
                apollo_id=str(apollo_id),
            )
            if live and live.get("name"):
                target = {"apollo_id": apollo_id, **live}
            elif not contacts:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Vera couldn't match this contact_id at this account "
                        "on a live check, and hasn't discovered its people "
                        "yet. Re-run find_people for current contact ids and "
                        "pass name + title from its results with the unlock."
                    ),
                )
            else:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"apollo_id {apollo_id} isn't among this account's "
                        "known contacts and didn't match on a live check — "
                        "run find_people for current ids and pass name + "
                        "title with the unlock."
                    ),
                )
        # target_idx stays None → append (not replace) on success.

    # 2c. Solo plan: 30 contact unlocks/month. Check BEFORE the paid Apollo
    #     reveal so we never spend vendor money past the cap. This only COUNTS
    #     existing unlocks (person_unlock leads this month) — the in-flight one
    #     isn't recorded until success below, so a failed unlock never consumes
    #     a Solo cap slot.
    from app.services.plan_limits import enforce_unlock_cap
    enforce_unlock_cap(req.org_id, db)

    # 3. Apollo people-match for the reveal. Pulls name + email + phone +
    #    fully-formed organisation/location/industry data. NB: we deliberately
    #    do NOT charge a credit until the reveal + lead creation succeed (see
    #    below) — a failed reveal must never burn a credit or a Solo cap slot.
    from app.routers.leads import _apollo_people_match
    name_parts = (target.get("name") or "").split()
    first_guess = req.first_name or (name_parts[0] if name_parts else "")
    last_guess = req.last_name or (
        name_parts[1].rstrip(".") if len(name_parts) > 1 else ""
    )
    match = _apollo_people_match(
        first_name=first_guess,
        last_name=last_guess,
        linkedin_url=(target.get("linkedin_url") or ""),
        company_domain=domain,
        company_name=(company.get("name") or ""),
        apollo_id=apollo_id,  # strongest matcher → exact person + real name + photo
    )
    if not match:
        raise HTTPException(
            status_code=502,
            detail="Apollo reveal failed — couldn't match this person.",
        )

    # 5. Create the leads row. Mirrors add_manual_lead but uses the
    #    Apollo-revealed fields directly. status='new' so the rep sees
    #    it at the top of their pipeline.
    email = (match.get("email") or "").strip()
    if not email:
        # Apollo returned a person but no email — still create the lead
        # with whatever fields we have so the rep can use it. Email may
        # show up on a future re-enrichment pass.
        email = ""

    # 4. Verify the revealed email before handing it over (2026-08-19:
    #    a hard bounce on an unlocked contact). One Findymail
    #    verify per unlock; fail-open — an unreachable verifier never
    #    blocks the reveal, it just leaves confidence at Apollo's level.
    #    Two domain checks on top (2026-08-25, a contact revealed with an
    #    address on an unrelated company's domain): a dead email domain is a
    #    certain bounce, and an address on a different apex than the
    #    watched account is the legacy/pre-merger-domain trap.
    from app.services.email_finder import (
        check_pattern_consistency,
        domain_has_mail_dns,
        find_email,
        mail_domains_match,
        verify_email,
    )
    email_verified: Optional[bool] = None
    email_confidence = 0
    email_domain_mismatch = False
    email_pattern_conflict = False
    email_source = "apollo" if email else None
    if email and "@" in email:
        email_domain = email.rsplit("@", 1)[1].lower()
        email_domain_mismatch = not mail_domains_match(email_domain, domain)
        if domain_has_mail_dns(email_domain) is False:
            email_verified = False
            email_confidence = 5  # DNS says the domain can't receive mail
        else:
            email_verified = verify_email(email)
            if email_verified is True:
                email_confidence = 99
            elif email_verified is False:
                email_confidence = 30
            elif (match.get("email_status") or "") == "verified":
                # Apollo-verified, our verifier couldn't judge. 85, not 95:
                # 95 is retired outright — after the blanket-95 era it reads
                # as "trust me" while meaning "unverified"
                # (2026-08-28). ≥90 now always means verifier-confirmed.
                email_confidence = 85
            else:
                # Apollo's own status is 'unverified' (most are) and the
                # verifier couldn't judge — don't oversell it.
                email_confidence = 80
            if email_domain_mismatch and email_verified is not True:
                email_confidence = min(email_confidence, 55)
            if email_verified is None and check_pattern_consistency(
                email,
                match.get("first_name") or first_guess,
                match.get("last_name") or "",
                domain,
            ) is False:
                # Unverifiable AND conflicts with the company's verified
                # convention (e.g. jsmith@ vs smithj@, 2026-08-28).
                email_pattern_conflict = True
                email_confidence = min(email_confidence, 40)
    if email_verified is False:
        # The revealed address hard-bounces (or its domain is dead) — hunt
        # an alternative down the waterfall before handing over a known-bad
        # address. Only a VERIFIED alternative replaces the reveal; anything
        # weaker keeps Apollo's address with its honest low confidence.
        alt = find_email(
            linkedin_url=match.get("linkedin_url") or target.get("linkedin_url") or "",
            first_name=match.get("first_name") or first_guess,
            last_name=match.get("last_name") or "",
            company_domain=domain,
            company_name=company.get("name") or "",
        )
        if (
            alt.get("email")
            and alt["email"].lower() != email.lower()
            and alt.get("email_verified") is True
        ):
            logger.info(
                "Unlock: bounced %s replaced by verified %s (%s)",
                email, alt["email"], alt.get("source"),
            )
            email = alt["email"]
            email_verified = True
            email_confidence = alt.get("confidence") or 85
            email_source = alt.get("source")
            email_domain_mismatch = not mail_domains_match(
                email.rsplit("@", 1)[1], domain
            )

    lead_row = {
        "email_confidence": email_confidence,
        # Verdict columns — read by search_people et al.
        "email_verified": email_verified,
        "email_domain_mismatch": email_domain_mismatch,
        "email_source": email_source if email else None,
        "org_id": req.org_id,
        "email": email,
        "first_name": match.get("first_name") or first_guess,
        "last_name": match.get("last_name") or "",
        "title": match.get("title") or target.get("title") or "",
        "company_name": match.get("company_name") or company.get("name") or "",
        # File the lead under the WATCHED account's domain (not Apollo's org
        # primary_domain, which may be a subsidiary/variant like us.gt.com).
        # The accounts table links leads to accounts by domain, so this keeps
        # the unlocked contact attached to the account the rep is viewing.
        "company_domain": (domain or match.get("company_domain") or "").lower(),
        "company_size": match.get("company_size") or "",
        "company_industry": match.get("company_industry") or "",
        "company_location": match.get("company_location") or "",
        "linkedin_url": match.get("linkedin_url") or target.get("linkedin_url") or "",
        "photo_url": match.get("photo_url") or target.get("photo_url") or "",
        "phone": match.get("phone") or "",
        "source": "person_unlock",
        "status": "new",
    }

    # Dedupe — if a lead with this email already exists in the org,
    # don't create a duplicate; reuse it.
    existing_lead_id: Optional[str] = None
    if email:
        try:
            existing = (
                db.table("leads")
                .select("id")
                .eq("org_id", req.org_id)
                .eq("email", email)
                .limit(1)
                .execute()
            )
            if existing.data:
                existing_lead_id = existing.data[0]["id"]
        except Exception as e:
            logger.warning("Lead-dedupe lookup failed: %s", e)

    if existing_lead_id:
        lead_id = existing_lead_id
        # The lead pre-existed (deduped by email — likely from lead-gen), but the
        # user has now deliberately unlocked this person. Mark it person_unlock +
        # refresh the revealed identity so it surfaces in the People tab as an
        # acquired contact (not buried as generic lead-gen).
        try:
            db.table("leads").update(
                {
                    "source": "person_unlock",
                    "first_name": match.get("first_name") or lead_row["first_name"],
                    "last_name": match.get("last_name") or lead_row["last_name"],
                    "photo_url": lead_row["photo_url"],
                }
            ).eq("id", lead_id).execute()
        except Exception as e:
            logger.warning("Failed to mark deduped lead %s as unlocked: %s", lead_id, e)
    else:
        try:
            inserted = db.table("leads").insert(lead_row).execute()
            lead_id = inserted.data[0]["id"]
        except Exception as e:
            logger.error("Failed to create lead from unlock: %s", e)
            raise HTTPException(
                status_code=500,
                detail="Reveal succeeded but lead creation failed.",
            )

    # 5b. NOW charge — the reveal + lead succeeded, so the allowance is
    #     genuinely consumed. credits.charge is a no-op for Solo orgs (their
    #     monthly cap is counted from the person_unlock lead we just created).
    #     Only charge for a fresh unlock; a deduped (already-revealed) lead
    #     was paid for the first time round. Failures above raise before here,
    #     so a failed unlock never burns a credit or a Solo cap slot.
    if not existing_lead_id:
        from app.services import credits as _credits
        _credits.charge(
            db,
            org_id=req.org_id,
            action="person_unlock",
            related_id=apollo_id,
            metadata={
                "company_id": company_id,
                "company_name": company.get("name") or "",
                "domain": domain,
            },
        )

    # 6. key_contacts lives on the GLOBAL companies row, shared by every
    #    org watching this domain — so it must stay per-org-neutral:
    #    discovery fields only, NEVER unlock state or revealed identity
    #    (unlocked/lead_id/email/full name leaked one org's paid reveals
    #    to every other org until 2026-07-09). If the contact came from a
    #    live search, append the free-search fields so the committee list
    #    stays complete; otherwise leave the entry untouched.
    if target_idx is None:
        contacts.append(
            {
                "apollo_id": apollo_id,
                "name": target.get("name") or "",
                "title": target.get("title") or "",
                "linkedin_url": target.get("linkedin_url") or "",
            }
        )
        try:
            db.table("companies").update(
                {"key_contacts": contacts},
            ).eq("id", company_id).execute()
        except Exception as e:
            logger.warning("Failed to append live-search contact: %s", e)

    # 6b. The per-org unlock record: revealed identity + email + lead link
    #     live in apollo_people_cache, which find_people / search_people
    #     (chat + MCP) and the dashboard People surfaces read org-scoped.
    _upsert_people_cache(
        db, req.org_id, apollo_id, domain,
        {
            "apollo_id": apollo_id,
            "first_name": match.get("first_name") or first_guess,
            "last_name": match.get("last_name") or "",
            "title": match.get("title") or target.get("title") or "",
            "linkedin_url": lead_row["linkedin_url"],
            "email": email or None,
            "phone_numbers": (
                [{"raw_number": match["phone"]}] if match.get("phone") else None
            ),
        },
        lead_id=lead_id,
    )

    # 7. Fire the enrichment pipeline in the background. The rep lands on
    #    the lead page immediately and the page's polling picks up the
    #    results as they arrive. Deduped (pre-existing) leads were already
    #    enriched by whichever flow created them, so only new rows run.
    if not existing_lead_id:
        from app.jobs import enqueue_enrichment
        enqueue_enrichment(
            background_tasks, db, req.org_id, [lead_id],
            ["website", "tech_stack", "news", "jobs"], "cyber_company_gtm",
        )

    # Unlocking a contact is a committed action → auto-promote the
    # account to monitored (decoupled-monitoring orgs only).
    _auto_promote_monitoring(db, req.org_id, domain)

    return {
        "ok": True,
        "lead_id": lead_id,
        "already_unlocked": False,
        # The revealed address + verification result, so the caller can
        # judge deliverability without a second lookup (2026-08-25:
        # a sequencer's bounce protection tripped on an address with no
        # confidence score). true = deliverable, false =
        # likely bounce (treat as unusable), null = couldn't check.
        "email": email or None,
        "email_verified": email_verified,
        "email_confidence": email_confidence,
        # true = the address lives on a different apex than the watched
        # account's domain (legacy/pre-merger domain, subsidiary, etc.) —
        # deliverable or not, treat it with suspicion before outbound.
        "email_domain_mismatch": email_domain_mismatch,
        # true = unverifiable address that doesn't fit the company's
        # verified email convention — likely wrong, hold before sending.
        "email_pattern_conflict": email_pattern_conflict,
    }
