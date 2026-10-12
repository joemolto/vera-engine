"""Vera Brain endpoints — autonomous agent review and action."""

import logging
import traceback

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel
from uuid import UUID

from app.database import get_supabase
from app.services.activity import log_activity
from app.services.vera_brain import run_proactive_brain

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/brain", tags=["brain"])


class BrainRunRequest(BaseModel):
    org_id: UUID


@router.post("/think")
def trigger_brain(request: BrainRunRequest, background_tasks: BackgroundTasks):
    """Trigger the proactive brain for an org.

    Runs in background — returns immediately. The brain reviews the full
    pipeline state, decides what to do, and executes actions autonomously.
    Called from an external daily cron or manually for testing.
    """
    org_id = str(request.org_id)
    background_tasks.add_task(_run_brain_bg, org_id)
    return {"status": "started", "org_id": org_id}


def _run_brain_bg(org_id: str):
    """Background task wrapper for the proactive brain.

    Critical to log failures back to activity_log: the brain's only other
    log surface is mid-loop tool_call entries, so when the run dies before
    its first tool call (e.g. Anthropic 529 at 8am peak load) it would
    otherwise vanish completely from the dashboard. Pair with the
    vera_brain_started heartbeat at the top of run_proactive_brain so
    every invocation lands at least one row.
    """
    db = get_supabase()
    try:
        result = run_proactive_brain(db, org_id)
        logger.info(
            "Vera brain completed for org %s: %d actions taken",
            org_id, len(result.get("actions_taken", [])),
        )
    except Exception as e:
        tb = traceback.format_exc()
        logger.error("Vera brain failed for org %s: %s\n%s", org_id, e, tb)
        try:
            log_activity(db, org_id, None, "vera_brain_failed", {
                "error": str(e),
                "error_type": type(e).__name__,
                "traceback": tb[-2000:],
            })
        except Exception as log_err:
            # Don't let a logging failure shadow the original error.
            logger.error("Failed to record vera_brain_failed for %s: %s", org_id, log_err)
