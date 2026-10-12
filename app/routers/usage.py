"""Usage tracking endpoint."""

import logging

from fastapi import APIRouter

from app.database import get_supabase
from app.services.usage import check_enrichment_allowed, _first_of_next_month

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("/status")
def get_usage_status(org_id: str):
    """Return current usage stats for an org. Used by external workflows to gate enrichment."""
    db = get_supabase()

    # Fetch org tier
    org_result = (
        db.table("organisations")
        .select("tier")
        .eq("id", org_id)
        .execute()
    )
    tier = "scout"
    if org_result.data:
        tier = org_result.data[0].get("tier", "scout") or "scout"

    allowed, usage_info = check_enrichment_allowed(db, org_id)

    return {
        "org_id": org_id,
        "tier": tier,
        "month": usage_info["month"],
        "enrichments_used": usage_info["used"],
        "enrichments_limit": usage_info["limit"],
        "remaining": usage_info["remaining"],
        "pct_used": usage_info["pct_used"],
        "is_at_limit": not allowed,
        "resets_on": usage_info["resets_on"],
    }
