"""Lead scoring router with rate limiting."""

import logging

from fastapi import APIRouter

from app.database import get_supabase
from app.models.schemas import ScoreRequest, ScoreResponse, ScoreResult
from app.services.rate_limit import check_and_record
from app.services.scorer import score_leads
from typing import Optional  # noqa: F401  (kept for downstream imports)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scoring", tags=["scoring"])

# Per-org cap on leads scored per minute. The limit lives in the
# scoring router (not the engine-wide rate limit) because Claude calls
# are the expensive resource here.
MAX_PER_MINUTE = 100


@router.post("/score", response_model=ScoreResponse)
def score(request: ScoreRequest):
    """Score leads using Claude API against the org's ICP config."""
    org_id = str(request.org_id)
    lead_ids = [str(lid) for lid in request.lead_ids] if request.lead_ids else None

    db = get_supabase()

    # Table-backed rate-limit check. Counts how many scoring events this
    # org logged in the last 60s; rejects with 429 if this request would
    # push the count over MAX_PER_MINUTE. Survives process restarts +
    # works across multiple engine instances (the old in-memory deque
    # did neither).
    requested = len(lead_ids) if lead_ids else 1
    check_and_record(
        db, org_id,
        bucket_key="scoring",
        max_per_minute=MAX_PER_MINUTE,
        count=requested,
    )
    raw_results = score_leads(db, org_id, lead_ids)

    results = [
        ScoreResult(
            lead_id=r["lead_id"],
            lead_rating=r.get("lead_rating", ""),
            rating_reason=r.get("rating_reason", ""),
            buying_signals=r.get("buying_signals", []),
            icp_fit=r.get("icp_fit", {}),
            recommended_action=r.get("recommended_action"),
            best_approach_angle=r.get("best_approach_angle", ""),
            icp_score=r.get("icp_score", 0),
            timing_score=r.get("timing_score", 0),
            total_score=r.get("total_score", 0),
            reasoning=r.get("reasoning", ""),
        )
        for r in raw_results["results"]
    ]

    return ScoreResponse(
        results=results,
        total_scored=raw_results["total_scored"],
        errors=raw_results["errors"],
    )
