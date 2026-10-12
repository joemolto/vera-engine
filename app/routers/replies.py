import logging

from fastapi import APIRouter

from app.database import get_supabase
from app.models.schemas import ClassifyRequest, ClassifyResponse, ExtractedIntent
from app.services.classifier import classify_reply
from typing import Optional, Any

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/replies", tags=["replies"])


@router.post("/classify", response_model=ClassifyResponse)
def classify(request: ClassifyRequest):
    """Classify a reply using Claude API with intent extraction."""
    db = get_supabase()
    org_id = str(request.org_id)

    result = classify_reply(
        db=db,
        org_id=org_id,
        lead_email=request.lead_email,
        reply_text=request.reply_text,
        campaign_id=request.campaign_id,
        campaign_name=request.campaign_name,
        reply_subject=request.reply_subject,
        reply_html=request.reply_html,
        email_account=request.email_account,
        webhook_data=request.webhook_data,
    )

    intent_data = result.get("extracted_intent", {})
    extracted_intent = ExtractedIntent(
        buying_timeline=intent_data.get("buying_timeline"),
        competitor_mentioned=intent_data.get("competitor_mentioned"),
        objection=intent_data.get("objection"),
        referral_to=intent_data.get("referral_to"),
        follow_up_date=intent_data.get("follow_up_date"),
        sentiment=intent_data.get("sentiment"),
        key_quote=intent_data.get("key_quote"),
    )

    return ClassifyResponse(
        classification=result["classification"],
        confidence=result["confidence"],
        extracted_intent=extracted_intent,
        suggested_next_action=result.get("suggested_next_action"),
        lead_id=result.get("lead_id"),
        lead_status_updated=result.get("lead_status_updated"),
    )
