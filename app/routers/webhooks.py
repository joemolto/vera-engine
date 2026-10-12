"""Instantly webhook receiver — must return 200 within 5 seconds."""

import hashlib
import hmac
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request

from app.config import settings
from app.database import get_supabase
from app.models.schemas import WebhookResponse
from app.services.activity import log_activity
from app.services.classifier import classify_reply
from typing import Optional, Any

logger = logging.getLogger(__name__)
router = APIRouter(tags=["webhooks"])


def _expected_instantly_secret(org_id: str) -> str:
    """Per-org webhook secret = HMAC-SHA256(global secret, org_id).

    Binds the secret to the org so a caller who knows ONE org's webhook
    secret can't reuse it against another org. The old code accepted a
    single global secret with an attacker-chosen ?org_id=, which allowed
    cross-tenant lead/campaign poisoning. The org-specific URL handed to
    Instantly uses this digest as ?secret=; the bare global secret is
    never placed in a URL.
    """
    return hmac.new(
        settings.WEBHOOK_SECRET.encode(),
        org_id.encode(),
        hashlib.sha256,
    ).hexdigest()


@router.post("/instantly", response_model=WebhookResponse)
async def instantly_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    secret: str = Query(...),
    org_id: str = Query(...),
):
    """Receive Instantly webhooks. Returns 200 immediately, processes in background."""
    # Validate the org-bound secret (HMAC of the global secret + org_id).
    # constant-time compare; reject if the global secret isn't configured.
    expected = _expected_instantly_secret(org_id)
    if not settings.WEBHOOK_SECRET or not hmac.compare_digest(secret, expected):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    payload = await request.json()
    event_type = payload.get("event_type", "unknown")

    logger.info("Webhook received: event_type=%s org_id=%s", event_type, org_id)

    # Route by event type — all heavy processing in background
    if event_type in ("reply_received", "auto_reply_received"):
        background_tasks.add_task(_process_reply, org_id, payload)
    elif event_type == "email_bounced":
        background_tasks.add_task(_process_bounce, org_id, payload)
    elif event_type == "lead_unsubscribed":
        background_tasks.add_task(_process_unsubscribe, org_id, payload)
    elif event_type == "email_sent":
        background_tasks.add_task(_process_email_sent, org_id, payload)
    else:
        logger.warning("Unknown webhook event_type: %s", event_type)

    return WebhookResponse(status="received", event_type=event_type)


def _process_reply(org_id: str, payload: dict) -> None:
    """Background task: classify reply and update lead."""
    try:
        db = get_supabase()
        classify_reply(
            db=db,
            org_id=org_id,
            lead_email=payload.get("lead_email", ""),
            reply_text=payload.get("reply_text", ""),
            campaign_id=payload.get("campaign_id"),
            campaign_name=payload.get("campaign_name"),
            reply_subject=payload.get("reply_subject"),
            reply_html=payload.get("reply_html"),
            email_account=payload.get("email_account"),
            webhook_data=payload,
        )
        logger.info("Reply processed for %s", payload.get("lead_email"))
    except Exception as e:
        logger.error("Webhook reply processing failed: %s", e)
        try:
            db = get_supabase()
            log_activity(db, org_id, None, "webhook_processing_error", {
                "event_type": "reply_received",
                "lead_email": payload.get("lead_email"),
                "error": str(e),
            })
        except Exception:
            pass


def _process_bounce(org_id: str, payload: dict) -> None:
    """Background task: mark bounced lead."""
    try:
        db = get_supabase()
        lead_email = payload.get("lead_email", "")
        now = datetime.now(timezone.utc).isoformat()

        lead_result = (
            db.table("leads")
            .select("id, status")
            .eq("org_id", org_id)
            .eq("email", lead_email)
            .execute()
        )
        if lead_result.data:
            lead = lead_result.data[0]
            db.table("leads").update({
                "status": "bounced",
                "updated_at": now,
            }).eq("id", lead["id"]).execute()
            log_activity(db, org_id, lead["id"], "email_bounced", {
                "email": lead_email,
                "campaign_id": payload.get("campaign_id"),
            })
        logger.info("Bounce processed for %s", lead_email)
    except Exception as e:
        logger.error("Webhook bounce processing failed: %s", e)


def _process_unsubscribe(org_id: str, payload: dict) -> None:
    """Background task: mark unsubscribed lead as dead."""
    try:
        db = get_supabase()
        lead_email = payload.get("lead_email", "")
        now = datetime.now(timezone.utc).isoformat()

        lead_result = (
            db.table("leads")
            .select("id")
            .eq("org_id", org_id)
            .eq("email", lead_email)
            .execute()
        )
        if lead_result.data:
            lead_id = lead_result.data[0]["id"]
            db.table("leads").update({
                "status": "dead",
                "updated_at": now,
            }).eq("id", lead_id).execute()
            log_activity(db, org_id, lead_id, "lead_unsubscribed", {
                "email": lead_email,
            })
        logger.info("Unsubscribe processed for %s", lead_email)
    except Exception as e:
        logger.error("Webhook unsubscribe processing failed: %s", e)


def _process_email_sent(org_id: str, payload: dict) -> None:
    """Background task: increment campaign email sent count."""
    try:
        db = get_supabase()
        campaign_id = payload.get("campaign_id")
        if not campaign_id:
            return

        campaign = (
            db.table("campaigns")
            .select("id, emails_sent")
            .eq("instantly_campaign_id", campaign_id)
            .eq("org_id", org_id)
            .execute()
        )
        if campaign.data:
            db.table("campaigns").update({
                "emails_sent": campaign.data[0]["emails_sent"] + 1,
                "updated_at": "now()",
            }).eq("id", campaign.data[0]["id"]).execute()
    except Exception as e:
        logger.error("Webhook email_sent processing failed: %s", e)
