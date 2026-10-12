"""Caps plans — caps, not credits.

The caps family: Starter (fenced, no smart groups) and Individual
(internal tier id "solo"). Instead of a credit balance, they set hard
ceilings on
the actions that matter, so the buyer knows exactly what they get and the
marginal-cost actions (Apollo unlocks, Apify person-intel) are bounded.

Two kinds of cap:
  - MONTHLY (refills on the billing cycle): accounts researched, contact
    unlocks. The throughput levers — what the rep spends each month. The
    results pile up; the allowance resets.
  - STANDING (a ceiling at any moment, never refills): accounts watched,
    people watched. The live-monitoring stock — what Vera is actively
    tracking right now. Stop watching one to free a slot.

Solo orgs do NOT charge credits — the cap IS the cost ceiling. Other
tiers (scout/growth/scale and hand-configured pilot orgs) are untouched.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import HTTPException

from app.config import settings
from app.database import get_supabase

logger = logging.getLogger(__name__)

SOLO_TIER = "solo"
STARTER_TIER = "starter"

# Tiers billed on caps (not credits). Every cap door and the credit-charge
# skip use this — a new caps tier only needs its LIMITS dict + this tuple.
CAPPED_TIERS = (SOLO_TIER, STARTER_TIER)

# Stripe subscription statuses that REVOKE paid access. Deliberately a
# tight allow-nothing set of terminal states — a lapsed payer, not a
# transient one. Must NEVER include null/None ('active'-with-no-Stripe-sub
# demo and manually provisioned orgs read null or 'active'),
# nor 'past_due' (dunning grace), nor 'pending_payment' (pre-first-payment,
# has no user yet). Revoking here zeroes caps + stops monitoring.
# Self-hosted orgs have no subscription, so nothing revokes them (this also
# covers orgs imported from the hosted service with a lapsed status).
REVOKED_STATUSES = (
    frozenset() if settings.SELF_HOSTED
    else frozenset({"canceled", "unpaid", "incomplete_expired"})
)


def org_lapsed(org_id: str, db=None) -> bool:
    """True when the org's subscription_status is revoked. Spend paths that
    don't sit behind a cap door call this so lapsed orgs get zero compute
    (2026-08-23 lapsed-org audit). Fail-open: a lookup error must never
    break a paying customer's request."""
    db = db or get_supabase()
    try:
        row = (db.table("organisations").select("subscription_status")
               .eq("id", org_id).maybe_single().execute().data or {})
        return (row.get("subscription_status") or "").lower() in REVOKED_STATUSES
    except Exception as e:  # noqa: BLE001
        logger.warning("org_lapsed check failed for %s: %s", org_id, e)
        return False

_ZERO_LIMITS: Dict[str, int] = {
    "accounts": 0, "watched": 0, "person_intel_accounts": 0,
    "monitored_people": 0, "unlocks_per_month": 0,
}


# Standing + monthly caps for the Solo plan (marketed as "Individual" since
# 2026-07-14). Tune here; nowhere else should hardcode these numbers.
SOLO_LIMITS: Dict[str, int] = {
    "accounts": 150,           # monthly — accounts researched this billing cycle (refills; research ~15¢ each, the cheap lever)
    "watched": 25,             # standing — accounts Vera monitors weekly
    "person_intel_accounts": 5,  # standing — accounts with person intel on (discovery mechanism)
    "monitored_people": 25,    # standing — specific PEOPLE watched (the Solo people lever)
    "unlocks_per_month": 500,  # monthly — contact reveals (Apollo→Findymail waterfall ~3¢ each). 2026-07-19:
                               # raised 50→500. The workflow (25 watched, 150 researched) can't
                               # legitimately generate more than ~750, so real usage is far lower.
}

# Starter (2026-07-15): the fenced entry tier. Caps sit at
# 40-60% of Individual on purpose — credible numbers. The one
# feature fence is smart groups (API/webhooks/MCP ship on every paid
# tier since 2026-07-15). No capacity packs: Starter's upgrade path is Individual.
STARTER_LIMITS: Dict[str, int] = {
    "accounts": 75,
    "watched": 15,
    "person_intel_accounts": 2,
    "monitored_people": 10,
    "unlocks_per_month": 200,  # 2026-07-19: raised 25→200 (see SOLO note)
}

# In-dashboard capacity upgrade: each pack bumps the caps. Increments
# chosen to land on clean numbers and scale every actionable lever — research,
# watch, unlocks, people. Approximate provider cost per unit: research ~15¢,
# watch ~43¢/mo, unlock ~3¢, person ~4¢/mo. Max 3 packs → 300 research/month,
# 70 watched, 950 unlocks, 70 people; beyond that the user is steered to
# the Team pack.
CAPACITY_PER_PACK: Dict[str, int] = {
    "accounts": 50,
    "watched": 15,
    "unlocks_per_month": 150,  # 2026-07-19: raised 25→150 with the base-cap raise
    "person_intel_accounts": 3,
    "monitored_people": 15,
}
MAX_CAPACITY_PACKS = 3

# Self-hosted (settings.SELF_HOSTED): no plan, so no ceilings. A large finite
# number rather than None keeps every cap door's arithmetic unchanged.
SELF_HOSTED_LIMITS: Dict[str, int] = {
    "accounts": 1_000_000,
    "watched": 1_000_000,
    "person_intel_accounts": 1_000_000,
    "monitored_people": 1_000_000,
    "unlocks_per_month": 1_000_000,
}


def effective_limits(org_id: str, db=None) -> Dict[str, int]:
    """The org's caps: STARTER_LIMITS flat, or SOLO_LIMITS plus packs.
    Self-hosted: unlimited (every cap door reads its numbers from here)."""
    from app.config import settings
    if settings.SELF_HOSTED:
        return dict(SELF_HOSTED_LIMITS)
    db = db or get_supabase()
    packs = 0
    tier = ""
    status = ""
    try:
        res = (
            db.table("organisations").select("capacity_packs, tier, subscription_tier, subscription_status")
            .eq("id", org_id).maybe_single().execute()
        )
        row = (res.data or {}) if res and res.data else {}
        packs = int(row.get("capacity_packs") or 0)
        tier = (row.get("tier") or row.get("subscription_tier") or "").lower()
        status = (row.get("subscription_status") or "").lower()
    except Exception:
        packs = 0
    # Lapsed billing → zero caps, so every cost door (research/watch/unlock/
    # person-intel) closes. Only terminal Stripe states revoke; null/active/
    # past_due keep working (see REVOKED_STATUSES).
    if tier in CAPPED_TIERS and status in REVOKED_STATUSES:
        return dict(_ZERO_LIMITS)
    if tier == STARTER_TIER:
        return dict(STARTER_LIMITS)
    packs = max(0, min(MAX_CAPACITY_PACKS, packs))
    return {
        "accounts": SOLO_LIMITS["accounts"] + CAPACITY_PER_PACK["accounts"] * packs,
        "watched": SOLO_LIMITS["watched"] + CAPACITY_PER_PACK["watched"] * packs,
        "person_intel_accounts": SOLO_LIMITS["person_intel_accounts"]
        + CAPACITY_PER_PACK["person_intel_accounts"] * packs,
        "monitored_people": SOLO_LIMITS["monitored_people"]
        + CAPACITY_PER_PACK["monitored_people"] * packs,
        "unlocks_per_month": SOLO_LIMITS["unlocks_per_month"]
        + CAPACITY_PER_PACK["unlocks_per_month"] * packs,
    }


def reconcile_solo_caps(org_id: str, target_packs: int, db=None) -> Dict[str, int]:
    """Bring live monitoring in line with a new capacity tier.

    Accounts are now a MONTHLY research allowance (a flow, not a stock), so a
    capacity change never archives existing accounts — they're already the
    user's, and next month's allowance simply resets to the new number. We only
    reconcile the standing WATCH cap: a downgrade pauses monitoring on the
    newest watched accounts over the new cap (data kept; re-enable any time, an
    upgrade lifts the ceiling again).

    Returns {archived, paused} — archived is always 0 now, kept for the
    billing API's response shape.
    """
    db = db or get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    packs = max(0, min(MAX_CAPACITY_PACKS, int(target_packs)))
    watch_cap = SOLO_LIMITS["watched"] + CAPACITY_PER_PACK["watched"] * packs

    watched = (
        db.table("watchlist").select("id")
        .eq("org_id", org_id).eq("status", "active").eq("monitoring_active", True)
        .order("created_at", desc=False).execute().data or []  # oldest first
    )
    paused_count = 0
    if len(watched) > watch_cap:
        for a in watched[watch_cap:]:  # newest excess
            db.table("watchlist").update(
                {"monitoring_active": False, "updated_at": now}
            ).eq("id", a["id"]).execute()
            paused_count += 1

    return {"archived": 0, "paused": paused_count}


def reconcile_standing_caps(org_id: str, watched_cap: int, people_cap: int, db=None) -> Dict[str, int]:
    """Pause the newest over-cap STANDING items after a tier downgrade.

    Standing stocks are watched accounts (watchlist.monitoring_active) and
    monitored people (leads.person_monitoring). On a downgrade the caller
    lowers these ceilings; we pause monitoring on the newest items over the
    new cap — data is kept, monitoring simply stops, and it's reversible
    (an upgrade lifts the ceiling; the user re-enables what they want).
    Monthly flows (research, unlocks) need no reconcile — they just reset
    lower next cycle. Returns the counts paused.
    """
    db = db or get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    paused_accounts = 0
    paused_people = 0
    try:
        watched = (
            db.table("watchlist").select("id")
            .eq("org_id", org_id).eq("status", "active").eq("monitoring_active", True)
            .order("created_at", desc=False).execute().data or []  # oldest first
        )
        for a in watched[max(0, watched_cap):]:  # newest excess
            db.table("watchlist").update(
                {"monitoring_active": False, "updated_at": now}
            ).eq("id", a["id"]).execute()
            paused_accounts += 1
    except Exception as e:  # noqa: BLE001
        logger.error("reconcile_standing_caps watched failed for %s: %s", org_id, e)
    try:
        people = (
            db.table("leads").select("id")
            .eq("org_id", org_id).eq("person_monitoring", True)
            .order("created_at", desc=False).execute().data or []
        )
        for p in people[max(0, people_cap):]:
            db.table("leads").update({"person_monitoring": False}).eq("id", p["id"]).execute()
            paused_people += 1
    except Exception as e:  # noqa: BLE001
        logger.error("reconcile_standing_caps people failed for %s: %s", org_id, e)
    if paused_accounts or paused_people:
        logger.info(
            "Downgrade reconcile for org %s: paused %d account(s), %d person(s)",
            org_id, paused_accounts, paused_people,
        )
    return {"paused_accounts": paused_accounts, "paused_people": paused_people}


# The feature-flag preset a Solo org gets. Everything on (the product is
# the whole product); the caps + rails do the gating, not feature locks.
SOLO_FEATURES: Dict[str, bool] = {
    "accounts_dashboard": True,
    "accounts_dashboard_tabs": True,
    "intelligence_map": True,
    "social_listening": True,
    "account_suggestions": True,
    "home_v2": True,
    "analytics_v2": True,
    "cyber_lens_v1": True,
    "intelligence_only": True,      # no AI-drafted outreach copy
    "decoupled_monitoring": True,   # research vs watch split + flat weekly
    "solo_plan": True,              # the marker the UI reads for cap chrome
    "groups": True,                 # smart groups — part of the Individual
                                    # plan; was missing from the preset
                                    # so new signups lacked the nav (2026-07-15)
    "solo_digest": True,            # daily ranked-moves email (the homepage
                                    # "morning worklist"); on for every
                                    # Solo org.
}


# Starter preset: the full Solo experience (cap chrome, wizard, daily
# digest all ride solo_plan) with the smart-groups fence on top. The
# dashboard reads starter_plan for the upgrade-gate wall on Groups.
STARTER_FEATURES: Dict[str, bool] = {
    **SOLO_FEATURES,
    "starter_plan": True,
    "groups": False,
}


def is_solo(org_row_or_tier: Any) -> bool:
    """Accepts an org row dict or a raw tier string."""
    if isinstance(org_row_or_tier, str):
        return org_row_or_tier.lower() == SOLO_TIER
    if isinstance(org_row_or_tier, dict):
        tier = (org_row_or_tier.get("tier") or org_row_or_tier.get("subscription_tier") or "").lower()
        return tier == SOLO_TIER
    return False


def is_capped(org_row_or_tier: Any) -> bool:
    """True for any caps-billed tier (solo/starter). Accepts row or string."""
    if isinstance(org_row_or_tier, str):
        return org_row_or_tier.lower() in CAPPED_TIERS
    if isinstance(org_row_or_tier, dict):
        tier = (org_row_or_tier.get("tier") or org_row_or_tier.get("subscription_tier") or "").lower()
        return tier in CAPPED_TIERS
    return False


def _org_is_solo(org_id: str, db) -> bool:
    try:
        res = (
            db.table("organisations").select("tier, subscription_tier")
            .eq("id", org_id).maybe_single().execute()
        )
        return is_solo(res.data) if res and res.data else False
    except Exception as e:
        logger.warning("plan_limits: tier lookup failed for %s: %s", org_id, e)
        return False


def _org_is_capped(org_id: str, db) -> bool:
    """Caps-billed check (solo OR starter) — the door most call sites want."""
    try:
        res = (
            db.table("organisations").select("tier, subscription_tier")
            .eq("id", org_id).maybe_single().execute()
        )
        return is_capped(res.data) if res and res.data else False
    except Exception as e:
        logger.warning("plan_limits: tier lookup failed for %s: %s", org_id, e)
        return False


# ---------------------------------------------------------------------------
# Usage counts
# ---------------------------------------------------------------------------

def _count(db, table: str, **filters) -> int:
    q = db.table(table).select("id", count="exact")
    for k, v in filters.items():
        q = q.eq(k, v)
    res = q.execute()
    return res.count if res.count is not None else len(res.data or [])


def _month_start_iso() -> str:
    """First instant of the current UTC month — the Solo billing-cycle
    boundary for the monthly allowances (research, unlocks)."""
    return (
        datetime.now(timezone.utc)
        .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        .isoformat()
    )


def _accounts_this_month(org_id: str, db) -> int:
    """Accounts the org researched this billing month — the Solo monthly
    research allowance (refills each cycle). Counts watchlist rows the user
    committed to (status active/archived/deleted) created this month; excludes
    the system suggestion candidates (status pending/dismissed) so the topup
    pool never burns the allowance. NOTE: 'deleted' is counted on purpose — a
    user deleting an account (e.g. an accidental research) must NOT get the
    research allowance back. Best-effort → 0 on error."""
    try:
        res = (
            db.table("watchlist").select("id", count="exact")
            .eq("org_id", org_id)
            .in_("status", ["active", "archived", "deleted"])
            .gte("created_at", _month_start_iso())
            .execute()
        )
        return res.count or 0
    except Exception as e:
        logger.warning("accounts-this-month count failed for %s: %s", org_id, e)
        return 0


def current_usage(org_id: str, db=None) -> Dict[str, int]:
    """Current usage for an org — monthly research + unlocks (this cycle),
    standing watched accounts + watched people."""
    db = db or get_supabase()
    accounts = _accounts_this_month(org_id, db)  # monthly research allowance
    watched = _count(db, "watchlist", org_id=org_id, status="active", monitoring_active=True)
    # person-intel accounts: companies in this org's watchlist with intel on.
    try:
        domains = (
            db.table("watchlist").select("company_domain")
            .eq("org_id", org_id).eq("status", "active").execute().data or []
        )
        doms = [d.get("company_domain", "").lower() for d in domains if d.get("company_domain")]
        pi = 0
        if doms:
            co = (
                db.table("companies").select("person_intel_enabled, domain")
                .in_("domain", doms).execute().data or []
            )
            pi = sum(1 for c in co if (c.get("person_intel_enabled") or "off") != "off")
    except Exception:
        pi = 0
    # People actively WATCHED — leads flagged person_monitoring. This is the
    # Solo people lever: it counts specific INDIVIDUALS, not accounts. Best
    # effort — if the column isn't present yet, fall back to 0 rather than 500.
    try:
        mp = (
            db.table("leads").select("id", count="exact")
            .eq("org_id", org_id).eq("person_monitoring", True).execute()
        )
        monitored_people = mp.count or 0
    except Exception as e:
        logger.warning("monitored_people count failed for org %s: %s", org_id, e)
        monitored_people = 0
    # Unlocks this month = person_unlock LEADS created this month. We count the
    # leads, not credit_ledger rows: Solo orgs skip the credit ledger entirely
    # (credits.charge no-ops for Solo), so a ledger-based count was always 0 for
    # them — the monthly unlock cap never actually fired. Counting the
    # person_unlock leads makes the cap work for Solo AND only counts SUCCESSFUL
    # unlocks (a lead exists), so a failed/aborted reveal never burns a cap slot.
    month_start = _month_start_iso()
    try:
        unlocks = (
            db.table("leads").select("id", count="exact")
            .eq("org_id", org_id).eq("source", "person_unlock")
            .gte("created_at", month_start).execute()
        )
        unlocks_count = unlocks.count or 0
    except Exception:
        unlocks_count = 0
    return {
        "accounts": accounts,
        "watched": watched,
        "person_intel_accounts": pi,
        "monitored_people": monitored_people,
        "unlocks_this_month": unlocks_count,
    }


def limits_status(org_id: str, db=None) -> Dict[str, Any]:
    """Usage + caps + remaining, for the dashboard cap chrome."""
    db = db or get_supabase()
    usage = current_usage(org_id, db)
    limits = effective_limits(org_id, db)
    capacity_packs = (limits["accounts"] - SOLO_LIMITS["accounts"]) // CAPACITY_PER_PACK["accounts"]
    return {
        "tier": SOLO_TIER,
        "limits": limits,
        "usage": usage,
        "capacity_packs": capacity_packs,
        "max_capacity_packs": MAX_CAPACITY_PACKS,
        "pack_accounts": CAPACITY_PER_PACK["accounts"],
        "pack_watched": CAPACITY_PER_PACK["watched"],
        "pack_unlocks": CAPACITY_PER_PACK["unlocks_per_month"],
        "pack_person_intel": CAPACITY_PER_PACK["person_intel_accounts"],
        "pack_monitored_people": CAPACITY_PER_PACK["monitored_people"],
        "remaining": {
            "accounts": max(0, limits["accounts"] - usage["accounts"]),
            "watched": max(0, limits["watched"] - usage["watched"]),
            "person_intel_accounts": max(0, limits["person_intel_accounts"] - usage["person_intel_accounts"]),
            "monitored_people": max(0, limits["monitored_people"] - usage["monitored_people"]),
            "unlocks_this_month": max(0, limits["unlocks_per_month"] - usage["unlocks_this_month"]),
        },
    }


# ---------------------------------------------------------------------------
# Enforcement — call BEFORE the action. No-op for non-Solo orgs.
# ---------------------------------------------------------------------------

# Plan-neutral — shown to every capped tier (Starter and Individual), so
# no plan names here. "Upgrade" resolves per-tier in the dashboard.
_CAP_MESSAGES = {
    "accounts": "You've researched all {limit} accounts in your plan this month. Your allowance refreshes next month, or upgrade for more.",
    "watched": "You're watching your maximum of {limit} accounts. Stop watching one, or upgrade to watch your whole territory.",
    "person_intel_accounts": "Person intel is on at your maximum of {limit} accounts. Turn it off somewhere, or upgrade.",
    "monitored_people": "You're watching your maximum of {limit} people. Stop watching someone, or upgrade to watch more.",
    "unlocks_per_month": "You've used all {limit} contact unlocks in your plan this month. They refresh next month, or upgrade for more.",
}


def _raise_cap(kind: str, limit: int) -> None:
    raise HTTPException(
        status_code=429,
        detail={
            "error": "solo_limit_reached",
            "cap": kind,
            "limit": limit,
            "message": _CAP_MESSAGES[kind].format(limit=limit),
        },
    )


def enforce_account_cap(org_id: str, db=None) -> None:
    """Researching/adding a NEW account. Monthly allowance (base 150 + packs),
    counted as accounts researched this billing cycle."""
    db = db or get_supabase()
    if not _org_is_capped(org_id, db):
        return
    cap = effective_limits(org_id, db)["accounts"]
    if _accounts_this_month(org_id, db) >= cap:
        _raise_cap("accounts", cap)


def can_add_account(org_id: str, domain: str, db=None) -> bool:
    """Non-raising account-cap check: can a NEW account for `domain` be added?

    True for non-Solo orgs, True if the domain is already an active account
    (a refresh, not a new research that spends the allowance), otherwise
    whether this month's research count is under the cap. Use where the
    caller must NOT raise (e.g. the person-enrich path that has already done
    its work) — enforce_account_cap is the raising version for action points
    that should 429.
    """
    db = db or get_supabase()
    if not _org_is_capped(org_id, db):
        return True
    if domain:
        existing = (
            db.table("watchlist").select("id")
            .eq("org_id", org_id).eq("company_domain", (domain or "").lower())
            .eq("status", "active").limit(1).execute()
        )
        if existing.data:
            return True
    cap = effective_limits(org_id, db)["accounts"]
    return _accounts_this_month(org_id, db) < cap


def enforce_watch_cap(org_id: str, db=None) -> None:
    """Turning monitoring on for an account. Standing cap (base 25 + packs)."""
    db = db or get_supabase()
    if not _org_is_capped(org_id, db):
        return
    cap = effective_limits(org_id, db)["watched"]
    watched = _count(db, "watchlist", org_id=org_id, status="active", monitoring_active=True)
    if watched >= cap:
        _raise_cap("watched", cap)


def enforce_person_intel_cap(org_id: str, db=None) -> None:
    """Enabling person intel on an account. Standing cap of 5."""
    db = db or get_supabase()
    if not _org_is_capped(org_id, db):
        return
    cap = effective_limits(org_id, db)["person_intel_accounts"]
    if current_usage(org_id, db)["person_intel_accounts"] >= cap:
        _raise_cap("person_intel_accounts", cap)


def enforce_monitored_people_cap(org_id: str, db=None) -> None:
    """Watching a specific person (a lead). Standing cap (base 25 + packs).

    The Solo people lever: bounds how many individuals Vera actively
    watches. No-op for non-Solo orgs.
    """
    db = db or get_supabase()
    if not _org_is_capped(org_id, db):
        return
    cap = effective_limits(org_id, db)["monitored_people"]
    if current_usage(org_id, db)["monitored_people"] >= cap:
        _raise_cap("monitored_people", cap)


def enforce_unlock_cap(org_id: str, db=None) -> None:
    """Unlocking a contact. Monthly cap of 30."""
    db = db or get_supabase()
    if not _org_is_capped(org_id, db):
        return
    cap = effective_limits(org_id, db)["unlocks_per_month"]
    if current_usage(org_id, db)["unlocks_this_month"] >= cap:
        _raise_cap("unlocks_per_month", cap)
