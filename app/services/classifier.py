"""Reply classification service using Claude API."""

import logging
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from supabase import Client

from app.prompts.classification import CLASSIFICATION_PROMPT
from app.services.activity import log_activity
from app.services.claude import call_claude
from app.services.outcomes import (
    CLASSIFICATION_TO_OUTCOME,
    record_from_lead,
)
from typing import Optional, Any

logger = logging.getLogger(__name__)

# Status mapping: classification -> lead status
CLASSIFICATION_STATUS_MAP = {
    "INTERESTED": "replied",
    "CURIOUS": "replied",
    "NOT_NOW": "replied",
    "OBJECTION": "replied",
    "REFERRAL": "replied",
    "UNSUBSCRIBE": "dead",
    "AUTO_REPLY": None,  # Don't change status
    "DEAD": "dead",
}

# Reply classification -> action_outcomes.outcome bucket
# This is what feeds the flywheel — every reply becomes a labelled
# outcome that the scorer + briefing prompts use as exemplars.
CLASSIFICATION_OUTCOME_MAP = {
    "INTERESTED": "positive",
    "CURIOUS": "positive",
    "REFERRAL": "positive",
    "NOT_NOW": "neutral",
    "OBJECTION": "neutral",
    "UNSUBSCRIBE": "negative",
    "DEAD": "negative",
    "AUTO_REPLY": "auto_reply",
}


def clean_reply_text(raw_text: str, raw_html: Optional[str] = None) -> str:
    """Clean reply text: strip HTML, remove signatures, extract fresh reply."""
    text = raw_text

    # If HTML provided and text is empty/messy, extract from HTML
    if raw_html and (not text or "<" in text):
        soup = BeautifulSoup(raw_html, "html.parser")
        text = soup.get_text(separator="\n")

    # Remove common signature separators and everything after them
    signature_patterns = [
        r"\n--\s*\n",           # Standard -- separator
        r"\n_{3,}\n",           # ___ separator
        r"\nSent from my ",    # Mobile signatures
        r"\nGet Outlook for ",
        r"\n\nOn .+ wrote:\s*\n",  # Quoted reply header
    ]
    for pattern in signature_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            text = text[:match.start()]
            break

    # Remove quoted lines (starting with >)
    lines = text.split("\n")
    fresh_lines = [line for line in lines if not line.strip().startswith(">")]
    text = "\n".join(fresh_lines)

    # Clean up whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    return text


def classify_reply(
    db: Client,
    org_id: str,
    lead_email: str,
    reply_text: str,
    campaign_id: Optional[str] = None,
    campaign_name: Optional[str] = None,
    reply_subject: Optional[str] = None,
    reply_html: Optional[str] = None,
    email_account: Optional[str] = None,
    webhook_data: Optional[dict] = None,
) -> dict:
    """Classify a reply using Claude API. Store result and update lead status."""
    # Clean the reply text
    cleaned_text = clean_reply_text(reply_text, reply_html)

    if not cleaned_text:
        return {
            "classification": "AUTO_REPLY",
            "confidence": 0.5,
            "extracted_intent": {},
            "suggested_next_action": "suppress",
            "lead_id": None,
            "lead_status_updated": None,
        }

    # Look up lead
    lead = None
    lead_result = (
        db.table("leads")
        .select("id, title, company_name, status")
        .eq("org_id", org_id)
        .eq("email", lead_email)
        .execute()
    )
    if lead_result.data:
        lead = lead_result.data[0]

    lead_title = lead.get("title", "Unknown") if lead else "Unknown"
    lead_company = lead.get("company_name", "Unknown") if lead else "Unknown"

    # Format classification prompt
    prompt = CLASSIFICATION_PROMPT.format(
        reply_text=cleaned_text,
        campaign_name=campaign_name or "Unknown",
        lead_title=lead_title,
        lead_company=lead_company,
    )

    # Email reply classification (intent / sentiment) is a structured
    # short-form task. Haiku handles it just as well as Sonnet at ~10×
    # lower cost. cache_system=True because we use the same SYSTEM_INSTRUCTION
    # across calls — back-to-back classifications hit the cache.
    from app.config import settings as _s
    result = call_claude(prompt, model=_s.CLAUDE_FAST_MODEL)

    if "error" in result:
        logger.error("Classification failed: %s", result.get("error"))
        result = {
            "classification": "AUTO_REPLY",
            "confidence": 0.0,
            "extracted_intent": {},
            "suggested_next_action": "suppress",
        }

    classification = result.get("classification", "AUTO_REPLY")
    confidence = float(result.get("confidence", 0))
    extracted_intent = result.get("extracted_intent", {})
    suggested_action = result.get("suggested_next_action", "suppress")

    now = datetime.now(timezone.utc).isoformat()

    # Store the reply
    reply_row = {
        "org_id": org_id,
        "lead_email": lead_email,
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "reply_text": cleaned_text,
        "reply_subject": reply_subject,
        "reply_html": reply_html,
        "classification": classification,
        "confidence": confidence,
        "extracted_intent": extracted_intent,
        "classified_at": now,
        "instantly_webhook_data": webhook_data or {},
    }
    if lead:
        reply_row["lead_id"] = lead["id"]

    db.table("replies").insert(reply_row).execute()

    # Update lead status based on classification
    lead_status_updated = None
    if lead:
        new_status = CLASSIFICATION_STATUS_MAP.get(classification)
        if new_status and lead.get("status") != new_status:
            db.table("leads").update({
                "status": new_status,
                "updated_at": now,
            }).eq("id", lead["id"]).execute()
            lead_status_updated = new_status

        log_activity(db, org_id, lead["id"], "reply_classified", {
            "classification": classification,
            "confidence": confidence,
            "campaign": campaign_name,
            "key_quote": extracted_intent.get("key_quote"),
        })

        # Feed the outcomes flywheel. Skips AUTO_REPLY since it's noise.
        outcome_bucket = CLASSIFICATION_OUTCOME_MAP.get(classification)
        if outcome_bucket and outcome_bucket != "auto_reply":
            try:
                db.table("action_outcomes").insert({
                    "org_id": org_id,
                    "lead_id": lead["id"],
                    "action_type": "outbound_email",
                    "channel": "email",
                    "outcome": outcome_bucket,
                    "outcome_detail": {
                        "classification": classification,
                        "confidence": confidence,
                        "key_quote": extracted_intent.get("key_quote"),
                    },
                    "feedback_source": "reply_classification",
                    "target_type": "lead",
                    "target_id": lead["id"],
                }).execute()
            except Exception as e:
                logger.warning("action_outcomes insert failed for lead %s: %s", lead["id"], e)

        # Also write through to the new lead_outcomes table — this is
        # the feedback corpus the cross-client intelligence layer
        # learns from. Best-effort; classifier flow continues even if
        # the write fails.
        new_outcome = CLASSIFICATION_TO_OUTCOME.get(classification)
        if new_outcome:
            record_from_lead(
                db, lead, new_outcome,
                reply_classification=classification,
                final_status=lead_status_updated or lead.get("status"),
            )

    # Update campaign reply counters
    if campaign_id:
        _update_campaign_reply_counts(db, org_id, campaign_id, classification)

    return {
        "classification": classification,
        "confidence": confidence,
        "extracted_intent": extracted_intent,
        "suggested_next_action": suggested_action,
        "lead_id": lead["id"] if lead else None,
        "lead_status_updated": lead_status_updated,
    }


def _update_campaign_reply_counts(
    db: Client, org_id: str, campaign_id: str, classification: str
) -> None:
    """Increment reply counters on the campaign record."""
    try:
        campaign = (
            db.table("campaigns")
            .select("id, replies_received, positive_replies")
            .eq("instantly_campaign_id", campaign_id)
            .eq("org_id", org_id)
            .execute()
        )
        if campaign.data:
            updates = {
                "replies_received": campaign.data[0]["replies_received"] + 1,
                "updated_at": "now()",
            }
            if classification in ("INTERESTED", "CURIOUS"):
                updates["positive_replies"] = campaign.data[0]["positive_replies"] + 1
            db.table("campaigns").update(updates).eq("id", campaign.data[0]["id"]).execute()
    except Exception as e:
        logger.error("Failed to update campaign reply counts: %s", e)
