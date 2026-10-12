"""Signal detection, stacking, and scoring logic."""

import logging
from datetime import datetime, timezone

from supabase import Client

from app.services.activity import log_activity
from app.services.signal_writer import insert_signal
from typing import Dict, List, Set

logger = logging.getLogger(__name__)


def check_signals(db: Client, org_id: str, signals: List[dict]) -> dict:
    """Register signals against leads and recalculate signal scores.

    Signal stacking formula:
    - 1 signal: sum of signal_strengths
    - 2 signals: sum * 1.3
    - 3+ signals: sum * 1.5

    Returns dict with signals_registered, leads_updated, errors.
    """
    signals_registered = 0
    leads_updated: List[dict] = []
    errors: List[str] = []
    updated_lead_ids: Set[str] = set()

    for signal in signals:
        try:
            lead_email = signal["lead_email"]
            signal_type = signal["signal_type"]
            signal_source = signal.get("signal_source")
            signal_data = signal.get("signal_data", {})
            signal_strength = float(signal.get("signal_strength", 0.5))

            # Find lead by email + org_id
            lead_result = (
                db.table("leads")
                .select("id, signals, signal_count")
                .eq("org_id", org_id)
                .eq("email", lead_email)
                .execute()
            )
            if not lead_result.data:
                errors.append(f"Lead not found: {lead_email}")
                continue

            lead = lead_result.data[0]
            lead_id = lead["id"]

            # Insert signal record (validated; malformed payloads are
            # logged + rejected rather than written as empty rows).
            written = insert_signal(db, {
                "org_id": org_id,
                "lead_id": lead_id,
                "signal_type": signal_type,
                "signal_source": signal_source,
                "signal_data": signal_data,
                "signal_strength": signal_strength,
            })
            if not written:
                errors.append(
                    f"Signal rejected (schema): {lead_email} / {signal_type}"
                )
                continue
            signals_registered += 1

            # Track which leads need score recalculation
            updated_lead_ids.add(lead_id)

            log_activity(db, org_id, lead_id, "signal_detected", {
                "signal_type": signal_type,
                "signal_source": signal_source,
                "signal_strength": signal_strength,
            })

        except Exception as e:
            error_msg = f"Error processing signal for {signal.get('lead_email')}: {e}"
            logger.error(error_msg)
            errors.append(error_msg)

    # Recalculate signal scores for all affected leads
    for lead_id in updated_lead_ids:
        try:
            updated = _recalculate_signal_score(db, org_id, lead_id)
            leads_updated.append(updated)
        except Exception as e:
            logger.error("Error recalculating signal score for %s: %s", lead_id, e)

    return {
        "signals_registered": signals_registered,
        "leads_updated": leads_updated,
        "errors": errors,
    }


def _recalculate_signal_score(db: Client, org_id: str, lead_id: str) -> dict:
    """Recalculate a lead's signal score based on all their signals."""
    # Fetch all signals for this lead
    signals_result = (
        db.table("signals")
        .select("signal_type, signal_strength, signal_data")
        .eq("lead_id", lead_id)
        .execute()
    )
    all_signals = signals_result.data
    signal_count = len(all_signals)

    # Calculate stacked score
    raw_sum = sum(float(s.get("signal_strength", 0.5)) for s in all_signals)

    if signal_count == 0:
        signal_score = 0
    elif signal_count == 1:
        signal_score = raw_sum
    elif signal_count == 2:
        signal_score = raw_sum * 1.3
    else:
        signal_score = raw_sum * 1.5

    # Cap at reasonable maximum
    signal_score = min(signal_score, 10.0)

    # Build signals summary array for the lead's JSONB field
    signals_summary = [
        {
            "type": s["signal_type"],
            "strength": float(s.get("signal_strength", 0.5)),
            "data": s.get("signal_data", {}),
        }
        for s in all_signals
    ]

    now = datetime.now(timezone.utc).isoformat()
    db.table("leads").update({
        "signals": signals_summary,
        "signal_count": signal_count,
        "signal_score": round(signal_score, 2),
        "updated_at": now,
    }).eq("id", lead_id).execute()

    return {
        "lead_id": lead_id,
        "signal_count": signal_count,
        "signal_score": round(signal_score, 2),
        "signals": [s["signal_type"] for s in all_signals],
    }
