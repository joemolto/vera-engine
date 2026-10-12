"""Batch monitoring — run daily by the built-in scheduler (services/scheduler.py).

v3 (2026-05): heat-based cadence + smart Phase A/B monitor.
  - No tier caps. Watchlist size is now bounded by credit spend, not
    arbitrary plan limits.
  - Only fetches accounts where next_monitor_at <= now() — accounts
    self-pace via cadence written back by monitor_account.
  - HARD_SAFETY_CAP on a single org pass to prevent a runaway watchlist
    from saturating the cron — accounts past the cap just roll to the
    next pass.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from supabase import Client

from app.services.activity import log_activity
from app.services.watchlist.monitor import monitor_account

logger = logging.getLogger(__name__)

# Safety bound on a single org's pass — not a plan cap, just a sane ceiling
# to keep the daily cron from drowning if someone has a 5000-account list and
# all of them happen to be due at once. Accounts past this just defer to the
# next pass naturally (next_monitor_at stays in the past until they're picked).
HARD_SAFETY_CAP = 200


def run_daily_monitoring(db: Client, org_id: str) -> Dict[str, Any]:
    """Monitor all DUE watchlist entries for a single org.

    Called via the /watchlist/monitor endpoint and by run_monitoring_all_orgs.
    Each entry's next monitor time is set by monitor_account based on heat;
    we only fetch accounts whose next_monitor_at has elapsed.
    """
    # Fetch org config
    org_resp = db.table("organisations").select("*").eq("id", org_id).execute()
    if not org_resp.data:
        return {"error": "Organisation not found", "org_id": org_id}
    org_config = org_resp.data[0]

    # Lapsed orgs get zero monitoring spend. The all-orgs loop filters
    # further down, but per-org triggers (per-org cron calls, manual
    # calls, the dossier refresh path) land here directly — without this
    # check a cancelled org kept its full PredictLeads/Apify/Claude pass
    # (2026-08-23 lapsed-org audit).
    from app.services.plan_limits import REVOKED_STATUSES
    if (org_config.get("subscription_status") or "").lower() in REVOKED_STATUSES:
        return {"org_id": org_id, "skipped": "subscription_lapsed",
                "accounts_checked": 0, "alerts_generated": 0}

    # Pick accounts that are DUE (next_monitor_at <= now, OR null = first pass).
    # No tier cap — watchlist size scales with what the org pays for in credits.
    now_iso = datetime.now(timezone.utc).isoformat()
    wl_resp = (
        db.table("watchlist")
        .select("*")
        .eq("org_id", org_id)
        .eq("monitoring_active", True)
        # Guard against pending suggestions nobody accepted — legacy topup
        # inserts left monitoring_active at its default (true).
        .neq("status", "pending")
        .or_(f"next_monitor_at.is.null,next_monitor_at.lte.{now_iso}")
        .order("next_monitor_at", desc=False, nullsfirst=True)
        .limit(HARD_SAFETY_CAP)
        .execute()
    )
    entries = wl_resp.data or []

    total_alerts: List[Dict[str, Any]] = []

    for entry in entries:
        try:
            is_first = not entry.get("last_monitored_at")
            alerts = monitor_account(db, entry, org_config, is_first_run=is_first)
            total_alerts.extend(alerts)
        except Exception as e:
            logger.error(
                "Monitoring failed for watchlist %s (%s): %s",
                entry.get("id"), entry.get("company_name"), e,
            )
        # Rate limit between accounts
        time.sleep(2)

    # Per-person monitoring — watch N specific people. Runs in the SAME per-org
    # pass as accounts (cadence-gated ~weekly inside monitor_one_person), so it
    # fires wherever this org is monitored: the per-client /monitor workflows
    # AND the global batch. Runs even when no accounts were due.
    people_summary: Dict[str, Any]
    try:
        from app.services.watchlist.person_monitor import monitor_watched_people
        people_summary = monitor_watched_people(db, org_id=org_id)
    except Exception as e:
        logger.error("Person-monitoring failed for org %s: %s", org_id, e)
        people_summary = {"error": str(e)}

    # Log activity
    log_activity(
        db,
        org_id,
        None,
        "watchlist_monitoring_run",
        {
            "accounts_checked": len(entries),
            "alerts_generated": len(total_alerts),
            "people_watched": people_summary.get("watched", 0)
            if isinstance(people_summary, dict) else 0,
        },
    )

    return {
        "org_id": org_id,
        "accounts_checked": len(entries),
        "alerts_generated": len(total_alerts),
        "people": people_summary,
        "alerts": [
            {
                "type": a.get("alert_type"),
                "title": a.get("alert_title"),
                "urgency": a.get("urgency"),
            }
            for a in total_alerts
        ],
    }


def run_monitoring_all_orgs(db: Client, tier: Optional[str] = None) -> Dict[str, Any]:
    """Monitor orgs' watchlists. Called daily by the built-in scheduler with tier="solo".

    If `tier` is given (e.g. "solo"), only orgs of that tier are
    monitored. This lets a single global cron cover every Vera Solo
    org without overlapping orgs that run their own per-org workflow.
    """
    # Orgs with at least one active watched ACCOUNT. Pending suggestions
    # don't count — same guard as the per-org query above.
    acc_resp = (
        db.table("watchlist")
        .select("org_id")
        .eq("monitoring_active", True)
        .neq("status", "pending")
        .execute()
    )
    org_ids_set = set(row["org_id"] for row in (acc_resp.data or []))

    # Orgs with at least one watched PERSON — an org can watch people without
    # watching any account, so union them in (else a people-only org is missed).
    # Best-effort: tolerate the column not existing yet.
    try:
        ppl_resp = (
            db.table("leads").select("org_id").eq("person_monitoring", True).execute()
        )
        org_ids_set |= set(r["org_id"] for r in (ppl_resp.data or []))
    except Exception as e:
        logger.warning("Watched-people org scan failed: %s", e)

    org_ids = list(org_ids_set)

    # Tier filter — keep only orgs on the requested plan. "solo" means the
    # caps-plan family (solo + starter share the same daily monitoring
    # shape), so the existing daily call keeps covering new starter orgs.
    if tier and org_ids:
        from app.services.plan_limits import CAPPED_TIERS
        tiers = list(CAPPED_TIERS) if tier == "solo" else [tier]
        tier_resp = (
            db.table("organisations")
            .select("id")
            .in_("tier", tiers)
            .in_("id", org_ids)
            .execute()
        )
        org_ids = [r["id"] for r in (tier_resp.data or [])]

    # Never do paid monitoring work for an org whose billing has lapsed —
    # a canceled/unpaid org keeps its watched accounts but must stop being
    # processed (the ongoing provider spend the audit flagged). Runs regardless
    # of the tier filter; null/active/past_due orgs are untouched.
    if org_ids:
        from app.services.plan_limits import REVOKED_STATUSES
        status_resp = (
            db.table("organisations").select("id, subscription_status")
            .in_("id", org_ids).execute()
        )
        revoked = {
            r["id"] for r in (status_resp.data or [])
            if (r.get("subscription_status") or "").lower() in REVOKED_STATUSES
        }
        if revoked:
            org_ids = [o for o in org_ids if o not in revoked]
            logger.info("Monitor: skipping %d org(s) with lapsed billing", len(revoked))

    # Each org goes through run_daily_monitoring once — which now covers BOTH
    # accounts and watched people — so nothing is processed twice.
    results: Dict[str, Any] = {}
    for org_id in org_ids:
        try:
            results[org_id] = run_daily_monitoring(db, org_id)
        except Exception as e:
            logger.error("Monitoring failed for org %s: %s", org_id, e)
            results[org_id] = {"error": str(e)}
        # Daily digest — the rule-based brain emails the owner the most
        # valuable moves. Per-org flag-gated (solo_digest, on for all caps
        # orgs since 2026-07-15); never breaks monitoring.
        if tier == "solo":
            try:
                from app.services.solo_digest import run_for_org
                run_for_org(db, org_id)
            except Exception as e:
                logger.error("Solo digest failed for org %s: %s", org_id, e)
        time.sleep(2)

    # Liveness stamp — the daily heartbeat check emails the operator if this stops.
    if tier == "solo":
        try:
            from app.services.heartbeat import stamp
            stamp(db, detail={"orgs_monitored": len(org_ids)})
        except Exception as e:
            logger.warning("heartbeat stamp failed: %s", e)

    return {
        "orgs_monitored": len(org_ids),
        "results": results,
    }
