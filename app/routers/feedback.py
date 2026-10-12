"""Dashboard feedback endpoints — thumbs up/down + structured outcomes.

Every interaction with a card (prospect / briefing / signal / threat
alert) writes a row to action_outcomes. The scorer + briefing prompts
read these as exemplars so the engine learns each org's taste.

Auth: same internal X-API-Key as the rest of the dashboard surface.
The dashboard passes the org_id and (optionally) the user_id from the
caller's authenticated session.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.database import get_supabase

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

ALLOWED_TARGET_TYPES = {"lead", "briefing", "signal", "threat_alert", "play_recommendation"}
ALLOWED_SENTIMENTS = {"up", "down"}


class ThumbRequest(BaseModel):
    org_id: str
    lead_id: str
    target_type: str
    target_id: Optional[str] = None  # defaults to lead_id when target_type='lead'
    sentiment: str  # 'up' | 'down'
    notes: Optional[str] = None
    given_by_user_id: Optional[str] = None
    signal_that_triggered: Optional[str] = None
    message_angle: Optional[str] = None


class MeetingBookedRequest(BaseModel):
    org_id: str
    lead_email: Optional[str] = None
    lead_id: Optional[str] = None
    notes: Optional[str] = None


class DealClosedRequest(BaseModel):
    org_id: str
    lead_id: str
    won: bool
    notes: Optional[str] = None
    given_by_user_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/feedback/thumb")
def thumb_feedback(req: ThumbRequest) -> dict:
    """Thumbs up/down on a prospect / briefing / signal card.

    Writes an action_outcomes row labelled 'positive' (up) or 'negative' (down)
    so the scorer + briefing prompts pick it up as an exemplar.
    """
    if req.target_type not in ALLOWED_TARGET_TYPES:
        raise HTTPException(400, f"target_type must be one of {sorted(ALLOWED_TARGET_TYPES)}")
    if req.sentiment not in ALLOWED_SENTIMENTS:
        raise HTTPException(400, "sentiment must be 'up' or 'down'")

    db = get_supabase()
    outcome = "positive" if req.sentiment == "up" else "negative"

    row = {
        "org_id": req.org_id,
        "lead_id": req.lead_id,
        "action_type": "dashboard_review",
        "channel": "dashboard",
        "outcome": outcome,
        "outcome_detail": {"sentiment": req.sentiment},
        "feedback_source": "dashboard_thumb",
        "target_type": req.target_type,
        "target_id": req.target_id or req.lead_id,
        "given_by_user_id": req.given_by_user_id,
        "notes": req.notes,
        "signal_that_triggered": req.signal_that_triggered,
        "message_angle": req.message_angle,
    }
    try:
        result = db.table("action_outcomes").insert(row).execute()
        return {"ok": True, "id": result.data[0]["id"] if result.data else None}
    except Exception as e:
        logger.exception("thumb_feedback insert failed: %s", e)
        raise HTTPException(500, f"Insert failed: {e}")


@router.post("/feedback/meeting-booked")
def meeting_booked(req: MeetingBookedRequest) -> dict:
    """Calendar booking webhook (relayed by your automation) → here. Records 'meeting_booked' as a strongly
    positive outcome and gives the scorer the loudest training signal."""
    db = get_supabase()

    lead_id = req.lead_id
    if not lead_id and req.lead_email:
        res = (
            db.table("leads")
            .select("id")
            .eq("org_id", req.org_id)
            .eq("email", req.lead_email)
            .execute()
        )
        if res.data:
            lead_id = res.data[0]["id"]

    if not lead_id:
        raise HTTPException(404, "Could not resolve lead_id from email")

    row = {
        "org_id": req.org_id,
        "lead_id": lead_id,
        "action_type": "meeting_booked",
        "channel": "calendar",
        "outcome": "meeting_booked",
        "feedback_source": "meeting_booked",
        "target_type": "lead",
        "target_id": lead_id,
        "notes": req.notes,
    }
    try:
        result = db.table("action_outcomes").insert(row).execute()
        return {"ok": True, "id": result.data[0]["id"] if result.data else None, "lead_id": lead_id}
    except Exception as e:
        logger.exception("meeting_booked insert failed: %s", e)
        raise HTTPException(500, f"Insert failed: {e}")


@router.post("/feedback/deal-closed")
def deal_closed(req: DealClosedRequest) -> dict:
    """Manual close button on the lead detail page."""
    db = get_supabase()
    outcome = "closed_won" if req.won else "closed_lost"
    row = {
        "org_id": req.org_id,
        "lead_id": req.lead_id,
        "action_type": "deal_close",
        "channel": "dashboard",
        "outcome": outcome,
        "feedback_source": "deal_closed",
        "target_type": "lead",
        "target_id": req.lead_id,
        "given_by_user_id": req.given_by_user_id,
        "notes": req.notes,
    }
    try:
        result = db.table("action_outcomes").insert(row).execute()
        return {"ok": True, "id": result.data[0]["id"] if result.data else None}
    except Exception as e:
        logger.exception("deal_closed insert failed: %s", e)
        raise HTTPException(500, f"Insert failed: {e}")


@router.get("/feedback/recent")
def recent_outcomes(org_id: str, limit: int = 40) -> dict:
    """Recent positive + negative outcomes for an org. Used by the scorer/briefing
    prompts as exemplars."""
    db = get_supabase()
    rows = (
        db.table("action_outcomes")
        .select("id, lead_id, action_type, outcome, outcome_detail, feedback_source, target_type, signal_that_triggered, message_angle, created_at")
        .eq("org_id", org_id)
        .in_("outcome", ["positive", "negative", "meeting_booked", "closed_won", "closed_lost"])
        .order("created_at", desc=True)
        .limit(min(limit, 200))
        .execute()
    )
    return {"outcomes": rows.data or []}
