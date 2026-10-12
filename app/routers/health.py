import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from app.database import get_supabase
from app.services.api_auth import require_superadmin
from app.models.schemas import HealthResponse
from typing import Optional, Any

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health_check():
    """Health check endpoint — no auth required."""
    db_status = "unknown"
    last_scoring = None
    last_classification = None

    try:
        db = get_supabase()
        # Quick connectivity check
        db.table("organisations").select("id").limit(1).execute()
        db_status = "connected"

        # Last scoring run
        scoring = (
            db.table("activity_log")
            .select("created_at")
            .eq("activity_type", "lead_scored")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if scoring.data:
            last_scoring = scoring.data[0]["created_at"]

        # Last classification
        classification = (
            db.table("activity_log")
            .select("created_at")
            .eq("activity_type", "reply_classified")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if classification.data:
            last_classification = classification.data[0]["created_at"]

    except Exception as e:
        logger.error("Health check DB error: %s", e)
        db_status = f"error: {e}"

    last_beat = None
    try:
        beat = (
            db.table("system_heartbeat")
            .select("beat_at")
            .eq("key", "solo_monitor")
            .maybe_single()
            .execute()
        )
        if beat and beat.data:
            last_beat = beat.data.get("beat_at")
    except Exception:
        pass  # health must answer even when the ledger is unreadable

    return HealthResponse(
        status="healthy" if db_status == "connected" else "degraded",
        database=db_status,
        timestamp=datetime.now(timezone.utc).isoformat(),
        last_scoring_run=last_scoring,
        last_reply_classified=last_classification,
        last_monitor_beat=last_beat,
    )


@router.post("/health/heartbeat-check", dependencies=[Depends(require_superadmin)])
def heartbeat_check():
    """Daily guards, one call: (1) dead-man switch — if the monitor pass
    hasn't stamped in ~27h, email the operator; (2) churn guard — orgs with
    active monitoring but no sign-in for 14 days, digest email to the
    operator at most weekly per org. Needs the engine API key: every call
    can send email. The built-in scheduler runs the dead-man check directly."""
    from app.services.churn_guard import check as churn_check
    from app.services.heartbeat import check as beat_check

    db = get_supabase()
    result = {"heartbeat": beat_check(db)}
    try:
        result["churn_guard"] = churn_check(db)
    except Exception as e:
        logger.warning("churn-guard failed: %s", e)
        result["churn_guard"] = {"error": str(e)}
    return result
