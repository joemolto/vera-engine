"""Credit accounting — charge() helper + balance().

v1 (Phase 3, 2026-05): soft metering. Every chargeable action logs to
credit_ledger and increments org_credits.used. We do NOT yet enforce a
hard balance ceiling — that arrives with Stripe top-up. Until then, the
ledger lets us:
  - see real burn rate per org
  - surface the meter on the dashboard
  - validate pricing assumptions before flipping enforcement on

Costs:
  monitor_pass    1
  deep_enrich     5
  person_unlock   1
  search          0   (don't call charge for these)

Helpers are tolerant: a failed charge() logs and continues. We never block
the underlying operation on a credit-ledger error — that would mean a
Supabase blip blocks the whole pipeline. The ledger is metering, not gating.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from supabase import Client

logger = logging.getLogger(__name__)


# Action → credit cost. Edit here when pricing changes; nowhere else
# should hardcode the cost.
COST: Dict[str, int] = {
    "monitor_pass": 1,
    "deep_enrich": 5,
    "person_unlock": 1,
    # Per-account person intel scan (Apify profile + posts + activity →
    # Haiku classifier). Base unit = top1 mode (1 contact monitored).
    # Deep mode (3 contacts) calls charge() with override_units=2.
    "person_intel_pass": 1,
    # Flat price includes the baseline monitor sweep that runs on add —
    # monitor_account skips its own charge when is_first_run=True.
    "add_to_watchlist": 5,
    # Apollo discovery scan (user-initiated /leads/discover and the
    # Vera Brain auto path — same product action, same price).
    "discovery_scan": 2,
    # Free actions deliberately absent — call sites must NOT call charge()
    # for them. Listed here as reference:
    #   search_company   0
    #   search_people    0
}


def current_month() -> str:
    """YYYY-MM bucket key."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def charge(
    db: Client,
    *,
    org_id: str,
    action: str,
    related_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
    override_units: Optional[int] = None,
) -> None:
    """Charge credits for an action.

    Writes a ledger row + increments org_credits.used for the current
    month. Failures are logged, not raised — credit metering should never
    block the actual operation.

    Args:
        action: must be a key in COST (or pass override_units explicitly).
        related_id: optional FK-ish reference (watchlist_id, apollo_id...).
        metadata: free-form context for the ledger UI.
        user_id: who triggered (omit for system actions like the monitor cron).
        override_units: bypass COST table. Use for grants / refunds / topups
            (pass negative numbers for those).
    """
    # Caps-billed orgs (solo/starter) never touch credits — the cap IS the
    # cost ceiling. Never write a ledger row or touch the credit bucket for
    # them, so the two systems stay cleanly separate (no phantom caps-tier
    # rows in credit_ledger / org_credits). Local import avoids a cycle.
    try:
        from app.services.plan_limits import _org_is_capped
        if _org_is_capped(org_id, db):
            return
    except Exception as e:  # never let the plan check block the actual op
        logger.debug("credits.charge: plan check failed for %s (%s) — proceeding", org_id, e)

    units = override_units if override_units is not None else COST.get(action)
    if units is None:
        logger.warning("credits.charge: unknown action %s, skipping", action)
        return

    # 1) Append to ledger
    try:
        db.table("credit_ledger").insert({
            "org_id": org_id,
            "action": action,
            "units": units,
            "related_id": related_id,
            "metadata": metadata or {},
            "created_by": user_id,
        }).execute()
    except Exception as e:
        logger.warning("credit_ledger insert failed for %s/%s: %s", org_id, action, e)
        # Don't bail — still try to update the bucket so the meter doesn't drift.

    # 2) Upsert this month's bucket and increment `used`.
    #    Supabase Python client doesn't have an atomic increment, so we
    #    do read-modify-write. Concurrent passes could race but the worst
    #    case is a tiny undercount which the ledger reconciles. The
    #    ledger is the source of truth; org_credits.used is a cached view.
    if units <= 0:
        return  # Grants/refunds don't burn from used; they sit in the ledger only.

    month = current_month()
    try:
        existing = (
            db.table("org_credits")
            .select("id,used")
            .eq("org_id", org_id)
            .eq("month", month)
            .maybe_single()
            .execute()
        )
        # maybe_single() returns None (not a response object) when no row
        # exists — accessing .data on it raised, the except below swallowed
        # it, and the month's bucket was never created. That's why June
        # 2026 charges had ledger rows but no org_credits row.
        existing_data = existing.data if existing else None
        if existing_data:
            new_used = (existing_data.get("used") or 0) + units
            db.table("org_credits").update({
                "used": new_used,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", existing_data["id"]).execute()
        else:
            db.table("org_credits").insert({
                "org_id": org_id,
                "month": month,
                "included": 0,  # plan grant set by seed / billing flow
                "purchased": 0,
                "used": units,
            }).execute()
    except Exception as e:
        logger.warning("org_credits bucket update failed for %s/%s: %s", org_id, action, e)


def get_balance(db: Client, org_id: str) -> Dict[str, Any]:
    """Read the current month's balance for an org.

    Returns: {month, included, purchased, used, remaining}.
    A fresh org with no row yet returns zeros — caller decides what to
    display in that case.
    """
    month = current_month()
    try:
        r = (
            db.table("org_credits")
            .select("month,included,purchased,used")
            .eq("org_id", org_id)
            .eq("month", month)
            .maybe_single()
            .execute()
        )
        r_data = r.data if r else None  # maybe_single() → None when no row
        if r_data:
            included = int(r_data.get("included") or 0)
            purchased = int(r_data.get("purchased") or 0)
            used = int(r_data.get("used") or 0)
            return {
                "month": month,
                "included": included,
                "purchased": purchased,
                "used": used,
                "remaining": (included + purchased) - used,
            }
    except Exception as e:
        logger.warning("get_balance failed for %s: %s", org_id, e)
    return {
        "month": month,
        "included": 0,
        "purchased": 0,
        "used": 0,
        "remaining": 0,
    }
