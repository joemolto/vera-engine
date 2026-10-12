import logging

from fastapi import APIRouter

from app.database import get_supabase
from app.models.schemas import SignalCheckRequest, SignalCheckResponse
from app.services.signal_checker import check_signals
from typing import Optional, Any

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/signals", tags=["signals"])


@router.post("/check", response_model=SignalCheckResponse)
def check(request: SignalCheckRequest):
    """Register signals against leads and recalculate signal scores."""
    db = get_supabase()
    org_id = str(request.org_id)

    signals_data = [
        {
            "lead_email": s.lead_email,
            "signal_type": s.signal_type,
            "signal_source": s.signal_source,
            "signal_data": s.signal_data,
            "signal_strength": s.signal_strength,
        }
        for s in request.signals
    ]

    result = check_signals(db, org_id, signals_data)

    return SignalCheckResponse(
        signals_registered=result["signals_registered"],
        leads_updated=result["leads_updated"],
        errors=result["errors"],
    )
