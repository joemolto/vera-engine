"""Public-facing Vera API.

Endpoints under /api/v1/public/* are authenticated via Bearer token
(per-tenant API keys, see services/api_keys.py). Everything is
org-scoped automatically — the api_key resolves to an org_id and we
filter every query by it.

Phase 1 (read-only): leads, briefing, signals, usage.
Phase 2 (write + rate limit): enrich, create lead, add to watchlist;
        per-tier rate limit applied to all endpoints; credit deduction
        on enrich.
"""

import logging
import time
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.config import settings
from app.database import get_supabase
from app.services.api_auth import ApiContext, require_api_key
from app.services.domain_resolver import canonicalise_domain
from app.services.api_keys import (
    count_recent_requests,
    get_rate_limit,
    log_request,
)
from app.services.plan_limits import (
    _org_is_solo,
    current_usage,
    effective_limits,
    enforce_account_cap,
    enforce_watch_cap,
)
from app.services.usage import check_enrichment_allowed, record_enrichment

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/public", tags=["public-api"])


# ---------------------------------------------------------------
# Middleware-style helper to log every public-API call
# ---------------------------------------------------------------

def _log_after(request: Request, status_code: int, started: float) -> None:
    """Best-effort logging of the request to api_request_log. Called
    from each handler at the end. Cheap insert — no performance impact
    on the response."""
    api_key_id = getattr(request.state, "api_key_id", None)
    org_id = getattr(request.state, "org_id", None)
    if not api_key_id or not org_id:
        return
    duration_ms = int((time.time() - started) * 1000)
    db = get_supabase()
    log_request(
        db,
        api_key_id=api_key_id,
        org_id=org_id,
        method=request.method,
        path=request.url.path,
        status_code=status_code,
        duration_ms=duration_ms,
    )


# ---------------------------------------------------------------
# Rate limiting — per-key, per-minute, tier-driven
# ---------------------------------------------------------------

def enforce_rate_limit(ctx: ApiContext = Depends(require_api_key)) -> ApiContext:
    """Dependency that runs after auth and before the handler. Counts
    the key's requests in the last 60s; if over the org's tier limit,
    raises 429 with a Retry-After header.

    Lookup is one indexed COUNT against api_request_log. Cheap enough
    not to need Redis or a dedicated bucket table at our current scale;
    upgrade later if the COUNT becomes a hot path.
    """
    db = get_supabase()
    limit_per_min = get_rate_limit(db, ctx.org_id)
    recent = count_recent_requests(db, ctx.api_key_id, seconds=60)
    if recent >= limit_per_min:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "rate_limit_exceeded",
                "message": f"Limit is {limit_per_min} requests/min on your tier. {recent} requests in the last 60 seconds.",
                "retry_after_seconds": 60,
            },
            headers={
                "Retry-After": "60",
                "X-RateLimit-Limit": str(limit_per_min),
                "X-RateLimit-Remaining": str(max(0, limit_per_min - recent)),
            },
        )
    return ctx


# ---------------------------------------------------------------
# GET /v1/public/leads — list leads (paginated, filterable)
# ---------------------------------------------------------------

@router.get("/leads")
def list_leads(
    request: Request,
    ctx: ApiContext = Depends(enforce_rate_limit),
    rating: Optional[str] = Query(None, description="hot | warm | cold"),
    status_: Optional[str] = Query(None, alias="status", description="Lead status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List leads in your pipeline.

    Filters: rating, status. Pagination: limit (max 200) + offset.
    """
    started = time.time()
    db = get_supabase()
    q = (
        db.table("leads")
        .select(
            "id, first_name, last_name, email, title, company_name, "
            "company_domain, company_industry, company_size, total_score, "
            "lead_rating, status, recommended_action, best_approach_angle, "
            "linkedin_url, source, suggestion_reason, scored_at, enriched_at, created_at"
        )
        .eq("org_id", ctx.org_id)
    )
    if rating:
        q = q.eq("lead_rating", rating)
    if status_:
        q = q.eq("status", status_)
    q = q.order("total_score", desc=True).range(offset, offset + limit - 1)

    resp = q.execute()
    leads = resp.data or []

    payload = {
        "data": leads,
        "count": len(leads),
        "limit": limit,
        "offset": offset,
    }
    _log_after(request, 200, started)
    return payload


# ---------------------------------------------------------------
# GET /v1/public/leads/{lead_id} — full lead with enrichment
# ---------------------------------------------------------------

@router.get("/leads/{lead_id}")
def get_lead(
    request: Request,
    lead_id: str,
    ctx: ApiContext = Depends(enforce_rate_limit),
):
    """Get a single lead by id, including the full enrichment_data blob
    (gtm_briefing, tech_stack, threat_alerts, website intel, etc.).
    """
    started = time.time()
    db = get_supabase()
    resp = (
        db.table("leads")
        .select("*")
        .eq("id", lead_id)
        .eq("org_id", ctx.org_id)
        .limit(1)
        .execute()
    )
    if not resp.data:
        _log_after(request, 404, started)
        raise HTTPException(status_code=404, detail="Lead not found")

    _log_after(request, 200, started)
    return resp.data[0]


# ---------------------------------------------------------------
# GET /v1/public/leads/{lead_id}/briefing — just the GTM briefing
# ---------------------------------------------------------------

@router.get("/leads/{lead_id}/briefing")
def get_lead_briefing(
    request: Request,
    lead_id: str,
    ctx: ApiContext = Depends(enforce_rate_limit),
):
    """Get just the GTM briefing for a lead — useful for piping into a
    CRM activity, Slack message, or sequencer without pulling the
    full enrichment blob."""
    started = time.time()
    db = get_supabase()
    resp = (
        db.table("leads")
        .select("id, first_name, last_name, email, company_name, enrichment_data")
        .eq("id", lead_id)
        .eq("org_id", ctx.org_id)
        .limit(1)
        .execute()
    )
    if not resp.data:
        _log_after(request, 404, started)
        raise HTTPException(status_code=404, detail="Lead not found")

    lead = resp.data[0]
    enrichment = lead.get("enrichment_data") or {}
    briefing = enrichment.get("gtm_briefing")

    payload = {
        "lead_id": lead["id"],
        "lead_name": f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip(),
        "lead_email": lead.get("email"),
        "company": lead.get("company_name"),
        "briefing": briefing,
    }
    _log_after(request, 200, started)
    return payload


# ---------------------------------------------------------------
# GET /v1/public/signals — recent signals across the pipeline
# ---------------------------------------------------------------

@router.get("/signals")
def list_signals(
    request: Request,
    ctx: ApiContext = Depends(enforce_rate_limit),
    signal_type: Optional[str] = Query(None, description="Filter by signal_type"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List signals detected across your prospects."""
    started = time.time()
    db = get_supabase()
    q = (
        db.table("signals")
        .select(
            "id, lead_id, company_id, signal_type, signal_strength, "
            "signal_data, detected_at, expires_at"
        )
        .eq("org_id", ctx.org_id)
    )
    if signal_type:
        q = q.eq("signal_type", signal_type)
    q = q.order("detected_at", desc=True).range(offset, offset + limit - 1)

    resp = q.execute()
    rows = resp.data or []
    _attach_account_fields(db, ctx.org_id, rows)
    payload = {
        "data": rows,
        "count": len(rows),
        "limit": limit,
        "offset": offset,
    }
    _log_after(request, 200, started)
    return payload


# ---------------------------------------------------------------
# GET /v1/public/usage — credits used / remaining
# ---------------------------------------------------------------

@router.get("/usage")
def get_usage(
    request: Request,
    ctx: ApiContext = Depends(enforce_rate_limit),
):
    """Current month's usage and remaining balance.

    Solo orgs are caps-based, not credit-based, so they get the plan's
    five levers (used/limit each, capacity packs included); other tiers
    keep the credit shape.
    """
    started = time.time()
    db = get_supabase()

    if _org_is_solo(ctx.org_id, db):
        from app.services.usage import _first_of_next_month
        used = current_usage(ctx.org_id, db)
        limits = effective_limits(ctx.org_id, db)
        caps = {
            "accounts_researched": {"used": used["accounts"], "limit": limits["accounts"], "period": "monthly"},
            "accounts_watched": {"used": used["watched"], "limit": limits["watched"], "period": "standing"},
            "contact_unlocks": {"used": used["unlocks_this_month"], "limit": limits["unlocks_per_month"], "period": "monthly"},
            "person_intel_accounts": {"used": used["person_intel_accounts"], "limit": limits["person_intel_accounts"], "period": "standing"},
            "people_watched": {"used": used["monitored_people"], "limit": limits["monitored_people"], "period": "standing"},
        }
        payload = {
            "plan": "solo",
            "caps": caps,
            "monthly_allowances_reset_on": _first_of_next_month(),
        }
        _log_after(request, 200, started)
        return payload

    from app.services.usage import get_or_create_usage
    usage = get_or_create_usage(db, ctx.org_id)
    org_resp = (
        db.table("organisations")
        .select("monthly_enrichment_limit")
        .eq("id", ctx.org_id)
        .limit(1)
        .execute()
    )
    limit = (org_resp.data or [{}])[0].get("monthly_enrichment_limit") or 0
    used = usage.get("enrichments_used") or 0
    payload = {
        "credits_used": used,
        "credits_limit": limit,
        "credits_remaining": max(0, limit - used),
        "period_start": usage.get("period_start"),
        "period_end": usage.get("period_end"),
    }
    _log_after(request, 200, started)
    return payload


# ===============================================================
# Phase 2 — Write endpoints
# ===============================================================

class CreateLeadRequest(BaseModel):
    """Create a new lead in the org's pool. At least one of email or
    linkedin_url is required."""
    email: Optional[str] = None
    linkedin_url: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    title: Optional[str] = None
    company_name: Optional[str] = None
    company_domain: Optional[str] = None
    # Free-form note explaining where this lead came from (optional).
    suggestion_reason: Optional[str] = None
    # If True, immediately enrich the new lead — costs 1 credit.
    enrich: bool = False


class EnrichRequest(BaseModel):
    lead_ids: List[UUID] = Field(..., min_length=1, max_length=10)


class WatchlistAddRequest(BaseModel):
    company_name: str = Field(..., min_length=1, max_length=200)
    company_domain: Optional[str] = None
    target_contact_name: Optional[str] = None
    target_contact_title: Optional[str] = None
    target_contact_linkedin: Optional[str] = None
    notes: Optional[str] = None
    priority: int = Field(default=0, ge=0, le=10)


# ---------------------------------------------------------------
# POST /v1/public/leads — create a lead (optionally enriching it)
# ---------------------------------------------------------------

@router.post("/leads", status_code=201)
def create_lead(
    payload: CreateLeadRequest,
    request: Request,
    ctx: ApiContext = Depends(enforce_rate_limit),
):
    """Create a new lead in your pool. Returns the inserted row.

    Provide at least one of `email` or `linkedin_url`. If `enrich=true`,
    the lead is enriched + scored synchronously before returning (consumes
    1 credit; returns 402 if you're over your monthly limit).
    """
    started = time.time()
    if not payload.email and not payload.linkedin_url:
        _log_after(request, 422, started)
        raise HTTPException(
            status_code=422,
            detail="At least one of `email` or `linkedin_url` is required",
        )

    db = get_supabase()

    # Solo is caps-based and account-led; the credit-model lead enrichment
    # below would be an unpriced side allowance (orgs carry a default
    # monthly_enrichment_limit that Solo never bought). Creating the lead
    # row is fine — enriching it is not.
    if payload.enrich and not settings.SELF_HOSTED and _org_is_solo(ctx.org_id, db):
        _log_after(request, 403, started)
        raise HTTPException(
            status_code=403,
            detail={
                "error": "not_available_on_solo",
                "message": "Lead enrichment is account-led on your plan: add the company via POST /v1/public/watchlist and Vera researches and monitors it within your plan caps. Create the lead without `enrich`, or unlock contacts in the dashboard.",
            },
        )

    insert_row = {
        "org_id": ctx.org_id,
        "email": payload.email,
        "linkedin_url": payload.linkedin_url,
        "first_name": payload.first_name,
        "last_name": payload.last_name,
        "title": payload.title,
        "company_name": payload.company_name,
        "company_domain": payload.company_domain,
        "source": "api",
        "suggestion_reason": payload.suggestion_reason or "Created via Vera API",
        "enrichment_status": "pending",
        "status": "new",
    }
    # Drop None values so DB defaults apply instead of overwriting with NULL
    insert_row = {k: v for k, v in insert_row.items() if v is not None}

    insert_resp = db.table("leads").insert(insert_row).execute()
    if not insert_resp.data:
        _log_after(request, 500, started)
        raise HTTPException(status_code=500, detail="Lead insert failed")
    lead = insert_resp.data[0]

    # Fire lead.created webhook — best-effort
    try:
        from app.services.webhooks import enqueue_event
        enqueue_event(db, ctx.org_id, "lead.created", {
            "lead_id": lead["id"],
            "email": lead.get("email"),
            "linkedin_url": lead.get("linkedin_url"),
            "company_name": lead.get("company_name"),
            "source": lead.get("source"),
        })
    except Exception as e:
        logger.warning("Webhook lead.created enqueue failed: %s", e)

    # Optional inline enrich. Reuses the same code path as the dashboard
    # /enrichment/full endpoint so credit accounting + scoring + brain
    # review all run identically.
    if payload.enrich:
        allowed, usage_info = check_enrichment_allowed(db, ctx.org_id, 1)
        if not allowed:
            # Lead was created; surface the credit error so the caller
            # knows enrichment didn't run.
            _log_after(request, 402, started)
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "credit_limit_reached",
                    "message": f"Lead created, but enrichment skipped: monthly limit hit ({usage_info['used']}/{usage_info['limit']}).",
                    "lead_id": lead["id"],
                    "usage": usage_info,
                },
            )
        from app.services.enricher import run_full_enrichment
        from app.services.scorer import score_leads
        results = run_full_enrichment(
            db=db,
            org_id=ctx.org_id,
            lead_ids=[lead["id"]],
            enrichment_sources=["website", "tech_stack", "news", "jobs"],
            enrichment_mode="cyber_company_gtm",
        )
        enriched_ids = [r["lead_id"] for r in results if "error" not in r]
        if enriched_ids:
            score_leads(db, ctx.org_id, enriched_ids)
            record_enrichment(db, ctx.org_id, len(enriched_ids))
        # Re-fetch the now-enriched lead so the response carries it
        lead_resp = db.table("leads").select("*").eq("id", lead["id"]).limit(1).execute()
        if lead_resp.data:
            lead = lead_resp.data[0]

    _log_after(request, 201, started)
    return lead


# ---------------------------------------------------------------
# POST /v1/public/leads/enrich — enrich existing leads (consumes credits)
# ---------------------------------------------------------------

@router.post("/leads/enrich")
def enrich_leads(
    payload: EnrichRequest,
    request: Request,
    ctx: ApiContext = Depends(enforce_rate_limit),
):
    """Trigger full enrichment + scoring for one or more existing leads.
    Consumes 1 credit per lead. Returns 402 if you'd exceed your limit.
    """
    started = time.time()
    db = get_supabase()

    # Same Solo gate as create_lead(enrich=True): no credit-model
    # enrichment on the caps-based plan.
    if not settings.SELF_HOSTED and _org_is_solo(ctx.org_id, db):
        _log_after(request, 403, started)
        raise HTTPException(
            status_code=403,
            detail={
                "error": "not_available_on_solo",
                "message": "Lead enrichment is account-led on your plan: add the company via POST /v1/public/watchlist and Vera researches and monitors it within your plan caps.",
            },
        )

    lead_ids = [str(lid) for lid in payload.lead_ids]

    # Verify all lead_ids belong to this org — never enrich another tenant's leads
    own = (
        db.table("leads")
        .select("id")
        .eq("org_id", ctx.org_id)
        .in_("id", lead_ids)
        .execute()
    )
    own_ids = {row["id"] for row in (own.data or [])}
    if not own_ids:
        _log_after(request, 404, started)
        raise HTTPException(status_code=404, detail="No matching leads in your org")
    valid_ids = [lid for lid in lead_ids if lid in own_ids]

    allowed, usage_info = check_enrichment_allowed(db, ctx.org_id, len(valid_ids))
    if not allowed:
        _log_after(request, 402, started)
        raise HTTPException(
            status_code=402,
            detail={
                "error": "credit_limit_reached",
                "message": f"Would use {len(valid_ids)} credits but only {usage_info['remaining']} remaining.",
                "usage": usage_info,
            },
        )

    from app.services.enricher import run_full_enrichment
    from app.services.scorer import score_leads
    results = run_full_enrichment(
        db=db,
        org_id=ctx.org_id,
        lead_ids=valid_ids,
        enrichment_sources=["website", "tech_stack", "news", "jobs"],
        enrichment_mode="cyber_company_gtm",
    )
    enriched_ids = [r["lead_id"] for r in results if "error" not in r]
    if enriched_ids:
        score_leads(db, ctx.org_id, enriched_ids)
        record_enrichment(db, ctx.org_id, len(enriched_ids))

    _log_after(request, 200, started)
    return {
        "results": results,
        "leads_enriched": len(enriched_ids),
        "credits_consumed": len(enriched_ids),
        "skipped_not_in_org": [lid for lid in lead_ids if lid not in own_ids],
    }


# ---------------------------------------------------------------
# POST /v1/public/watchlist — add a company to the watchlist
# ---------------------------------------------------------------

def _attach_account_fields(db, org_id: str, rows: list) -> None:
    """Stamp company_domain + company_name (and person_name when the signal
    captured one) onto each signal row, so integrations can route signals
    by account instead of parsing headline prose (demand-gen ask,
    2026-07-16 simulation). Resolution order: signal_data fields, then the
    companies row (company_id), then the lead (lead_id). Two batch
    lookups; best-effort, never raises."""
    try:
        cids, lids = set(), set()
        for r in rows:
            sd = r.get("signal_data") or {}
            if not (sd.get("company_domain") or sd.get("domain")):
                if r.get("company_id"):
                    cids.add(r["company_id"])
                elif r.get("lead_id"):
                    lids.add(r["lead_id"])
        cid_map: dict = {}
        if cids:
            res = (
                db.table("companies")
                .select("id, domain, name")
                .in_("id", list(cids))
                .execute()
            )
            cid_map = {c["id"]: c for c in (res.data or [])}
        lid_map: dict = {}
        if lids:
            res = (
                db.table("leads")
                .select("id, company_domain, company_name")
                .eq("org_id", org_id)
                .in_("id", list(lids))
                .execute()
            )
            lid_map = {l["id"]: l for l in (res.data or [])}
        for r in rows:
            sd = r.get("signal_data") or {}
            dom = str(sd.get("company_domain") or sd.get("domain") or "").lower() or None
            nam = str(sd.get("company_name") or sd.get("name") or "") or None
            if not dom and r.get("company_id") in cid_map:
                c = cid_map[r["company_id"]]
                dom = (c.get("domain") or "").lower() or None
                nam = nam or c.get("name")
            if not dom and r.get("lead_id") in lid_map:
                l = lid_map[r["lead_id"]]
                dom = (l.get("company_domain") or "").lower() or None
                nam = nam or l.get("company_name")
            r["company_domain"] = dom
            r["company_name"] = nam
            person = str(sd.get("person_name") or "").strip()
            r["person_name"] = person or None
    except Exception as e:
        logger.warning("attach_account_fields failed: %s", e)


@router.get("/watchlist")
def list_watchlist(
    request: Request,
    ctx: ApiContext = Depends(enforce_rate_limit),
    status: Optional[str] = Query(
        "active", description="Filter by status; pass 'all' for every row"
    ),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List the org's watched accounts — the read half of POST /watchlist,
    so integrations can reconcile a CRM against what Vera watches without
    scraping the dashboard. Free and read-only."""
    started = time.time()
    db = get_supabase()
    q = (
        db.table("watchlist")
        .select(
            "id, company_name, company_domain, priority, status, "
            "monitoring_active, target_contact_name, target_contact_title, "
            "last_monitored_at, created_at"
        )
        .eq("org_id", ctx.org_id)
    )
    if status and status != "all":
        q = q.eq("status", status)
    q = q.order("created_at", desc=True).range(offset, offset + limit - 1)
    resp = q.execute()
    payload = {
        "data": resp.data or [],
        "count": len(resp.data or []),
        "limit": limit,
        "offset": offset,
    }
    _log_after(request, 200, started)
    return payload


@router.post("/watchlist", status_code=201)
def add_to_watchlist(
    payload: WatchlistAddRequest,
    request: Request,
    ctx: ApiContext = Depends(enforce_rate_limit),
):
    """Add a company to the org's watchlist. Vera's weekly monitor will
    poll the company for changes (new blog posts, leadership changes,
    breach exposure) and surface alerts via /v1/public/signals + the
    dashboard.

    Idempotent by domain: re-adding an already-active account returns the
    existing row (with already_active=true) instead of creating a
    duplicate row and a duplicate weekly monitor spend. Solo plan caps
    are enforced here exactly as in the dashboard — a new watched account
    consumes one monthly research slot and one standing watch slot, both
    capacity-pack aware. 429 with the cap details when a limit is hit.
    """
    started = time.time()
    db = get_supabase()

    # Canonicalise before anything is keyed on the domain: a same-brand
    # redirect target is adopted (getpostman.com → postman.com); an
    # Apollo-blind sibling domain comes back as domain_suggestion on the
    # response so the caller/agent can re-add under the domain that
    # actually has data (octopusenergy.com vs octopus.energy).
    canon = canonicalise_domain(
        payload.company_domain or "", payload.company_name or ""
    )
    domain = (canon["domain"] or "").strip().lower() or None
    canon_extras = {
        k: v
        for k, v in (
            ("domain_canonicalised_from", canon["canonicalised_from"]),
            ("domain_suggestion", canon["suggestion"]),
        )
        if v
    }
    if domain:
        existing = (
            db.table("watchlist")
            .select("*")
            .eq("org_id", ctx.org_id)
            .eq("company_domain", domain)
            .eq("status", "active")
            .limit(1)
            .execute()
        )
        if existing.data:
            _log_after(request, 200, started)
            return {**existing.data[0], "already_active": True, **canon_extras}

    # The API is not a side door around the plan: same cap functions the
    # dashboard buttons call (no-ops for credit-based tiers). Every watched
    # account costs a weekly monitor pass, so this is the cost gate.
    try:
        enforce_account_cap(ctx.org_id, db)
        enforce_watch_cap(ctx.org_id, db)
    except HTTPException as e:
        _log_after(request, e.status_code, started)
        raise

    # A soft-removed row (deleted/dismissed) must be REACTIVATED, not
    # collide with the unique org+domain constraint — re-adds after
    # delete_account failed silently (2026-08-24).
    if domain:
        removed = (
            db.table("watchlist").select("id")
            .eq("org_id", ctx.org_id).eq("company_domain", domain)
            .in_("status", ["deleted", "dismissed"]).limit(1).execute()
        )
        if removed.data:
            upd = (
                db.table("watchlist").update({
                    "status": "active",
                    "monitoring_active": True,
                    "dismissed_at": None,
                    **({"company_name": payload.company_name} if payload.company_name else {}),
                    **({"priority": payload.priority} if payload.priority else {}),
                }).eq("id", removed.data[0]["id"]).execute()
            )
            _log_after(request, 201, started)
            row = (upd.data or [{"id": removed.data[0]["id"]}])[0]
            return {**row, **canon_extras}

    insert_row = {
        "org_id": ctx.org_id,
        "company_name": payload.company_name,
        "company_domain": domain,
        "target_contact_name": payload.target_contact_name,
        "target_contact_title": payload.target_contact_title,
        "target_contact_linkedin": payload.target_contact_linkedin,
        "notes": payload.notes,
        "priority": payload.priority,
        # Explicit status so the row counts against the monthly research
        # allowance (_accounts_this_month counts active/archived/deleted).
        "status": "active",
        "monitoring_active": True,
    }
    insert_row = {k: v for k, v in insert_row.items() if v is not None}

    resp = db.table("watchlist").insert(insert_row).execute()
    if not resp.data:
        _log_after(request, 500, started)
        raise HTTPException(status_code=500, detail="Watchlist insert failed")

    _log_after(request, 201, started)
    return {**resp.data[0], **canon_extras}
