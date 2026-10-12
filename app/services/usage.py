"""Usage tracking and enrichment guardrails."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from supabase import Client

from app.database import get_supabase

logger = logging.getLogger(__name__)


def _current_month() -> str:
    """Return current month as 'YYYY-MM'."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _first_of_next_month() -> str:
    """Return the first day of next month as 'YYYY-MM-DD'."""
    now = datetime.now(timezone.utc)
    if now.month == 12:
        return f"{now.year + 1}-01-01"
    return f"{now.year}-{now.month + 1:02d}-01"


def get_or_create_usage(db: Client, org_id: str) -> Dict[str, Any]:
    """Get the current month's usage record, creating one if needed.

    New month = automatic reset. The old month's record stays for history.
    """
    month = _current_month()

    # Try to fetch existing
    result = (
        db.table("usage_tracking")
        .select("*")
        .eq("org_id", org_id)
        .eq("month_year", month)
        .execute()
    )

    if result.data:
        row = result.data[0]
    else:
        # Fetch org's enrichment limit
        org_result = (
            db.table("organisations")
            .select("monthly_enrichment_limit, tier")
            .eq("id", org_id)
            .execute()
        )
        limit = 100
        if org_result.data:
            limit = org_result.data[0].get("monthly_enrichment_limit", 100) or 100

        # Create fresh record for this month.
        # `csv_uploads_used` column remains in the table for historical data
        # but is no longer written or surfaced — CSV upload was removed.
        insert_result = (
            db.table("usage_tracking")
            .insert({
                "org_id": org_id,
                "month_year": month,
                "enrichments_used": 0,
                "enrichments_limit": limit,
                "discovery_scans_used": 0,
            })
            .execute()
        )
        row = insert_result.data[0] if insert_result.data else {
            "enrichments_used": 0,
            "enrichments_limit": limit,
        }

    used = row.get("enrichments_used", 0)
    limit = row.get("enrichments_limit", 100)
    remaining = max(0, limit - used)

    return {
        "enrichments_used": used,
        "enrichments_limit": limit,
        "remaining": remaining,
        "is_at_limit": used >= limit,
        "discovery_scans_used": row.get("discovery_scans_used", 0),
        "notified_at_80_pct": row.get("notified_at_80_pct", False),
        "notified_at_100_pct": row.get("notified_at_100_pct", False),
        "month_year": row.get("month_year", month),
    }


def check_enrichment_allowed(db: Client, org_id: str, count: int = 1) -> Tuple[bool, Dict[str, Any]]:
    """Check if the org can run `count` more enrichments this month.

    Returns (allowed, usage_info).
    """
    usage = get_or_create_usage(db, org_id)
    used = usage["enrichments_used"]
    limit = usage["enrichments_limit"]
    remaining = usage["remaining"]
    pct_used = round((used / limit) * 100) if limit > 0 else 100

    usage_info = {
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "pct_used": pct_used,
        "month": usage["month_year"],
        "resets_on": _first_of_next_month(),
    }

    allowed = (used + count) <= limit
    return allowed, usage_info


def record_enrichment(db: Client, org_id: str, count: int = 1) -> None:
    """Increment enrichments_used and handle threshold notifications."""
    month = _current_month()
    now = datetime.now(timezone.utc).isoformat()

    # Fetch current record
    result = (
        db.table("usage_tracking")
        .select("id, enrichments_used, enrichments_limit, notified_at_80_pct, notified_at_100_pct")
        .eq("org_id", org_id)
        .eq("month_year", month)
        .execute()
    )

    if not result.data:
        # Ensure record exists
        get_or_create_usage(db, org_id)
        result = (
            db.table("usage_tracking")
            .select("id, enrichments_used, enrichments_limit, notified_at_80_pct, notified_at_100_pct")
            .eq("org_id", org_id)
            .eq("month_year", month)
            .execute()
        )

    if not result.data:
        logger.error("Could not find or create usage record for org %s", org_id)
        return

    row = result.data[0]
    new_used = row["enrichments_used"] + count
    limit = row["enrichments_limit"]

    update: Dict[str, Any] = {
        "enrichments_used": new_used,
        "last_enrichment_at": now,
        "updated_at": now,
    }

    # 80% threshold
    if limit > 0 and new_used >= (limit * 0.8) and not row.get("notified_at_80_pct"):
        update["notified_at_80_pct"] = True
        logger.warning("Org %s has used 80%% of enrichment limit (%d/%d)", org_id, new_used, limit)

    # 100% threshold
    if limit > 0 and new_used >= limit and not row.get("notified_at_100_pct"):
        update["notified_at_100_pct"] = True
        update["limit_hit_at"] = now
        logger.warning("Org %s has hit enrichment limit (%d/%d)", org_id, new_used, limit)

    db.table("usage_tracking").update(update).eq("id", row["id"]).execute()


def record_discovery_scan(db: Client, org_id: str) -> None:
    """Increment discovery_scans_used for the current month."""
    month = _current_month()
    now = datetime.now(timezone.utc).isoformat()

    # Ensure record exists
    get_or_create_usage(db, org_id)

    result = (
        db.table("usage_tracking")
        .select("id, discovery_scans_used")
        .eq("org_id", org_id)
        .eq("month_year", month)
        .execute()
    )
    if result.data:
        row = result.data[0]
        db.table("usage_tracking").update({
            "discovery_scans_used": row["discovery_scans_used"] + 1,
            "updated_at": now,
        }).eq("id", row["id"]).execute()


# record_csv_upload removed — CSV upload is no longer a Vera feature.
# The csv_uploads_used column on usage_tracking remains for historical data
# but is not written by any code path.
