import logging

from fastapi import APIRouter

from app.database import get_supabase
from app.models.schemas import PushLeadsRequest, PushLeadsResponse, SyncStatsRequest
from app.services.instantly import push_leads, sync_campaign_stats
from typing import Optional, Any

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/instantly", tags=["instantly"])


@router.post("/push-leads", response_model=PushLeadsResponse)
def push(request: PushLeadsRequest):
    """Push qualified leads to an Instantly campaign."""
    db = get_supabase()
    org_id = str(request.org_id)
    lead_ids = [str(lid) for lid in request.lead_ids] if request.lead_ids else None

    result = push_leads(
        db=db,
        org_id=org_id,
        campaign_id=request.campaign_id,
        lead_ids=lead_ids,
        min_score=request.min_score,
    )

    return PushLeadsResponse(
        pushed_count=result["pushed_count"],
        skipped_count=result["skipped_count"],
        pushed_lead_ids=result["pushed_lead_ids"],
        errors=result["errors"],
    )


@router.post("/sync-stats")
def sync_stats(request: SyncStatsRequest):
    """Pull campaign stats from Instantly and update local records."""
    db = get_supabase()
    org_id = str(request.org_id)

    result = sync_campaign_stats(db, org_id)
    return result
