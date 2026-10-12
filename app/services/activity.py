"""Activity logging helper — used by all services."""

import logging

from supabase import Client
from typing import Optional, Any

logger = logging.getLogger(__name__)


def log_activity(
    db: Client,
    org_id: str,
    lead_id: Optional[str],
    activity_type: str,
    activity_data: Optional[dict] = None,
) -> None:
    """Log an activity to the activity_log table."""
    try:
        row = {
            "org_id": org_id,
            "activity_type": activity_type,
            "activity_data": activity_data or {},
        }
        if lead_id:
            row["lead_id"] = lead_id
        db.table("activity_log").insert(row).execute()
    except Exception as e:
        # Activity logging should never crash the caller
        logger.error("Failed to log activity (%s): %s", activity_type, e)
