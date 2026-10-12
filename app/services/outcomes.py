"""Outcome capture + read-back for the scoring/briefing prompts.

This module does two jobs:

  1. `record_lead_outcome` writes to the `lead_outcomes` table
     when the dashboard, classifier, or webhook
     observes a state transition. The row captures both decision-time
     context (scored_signals, matched_play, score_at_time) and
     outcome-time facts (reply_classification, final_status, outcome).
     Aggregated rows across clients are the feedback loop that lets
     the scorer eventually learn which signal patterns actually
     predict conversion.

  2. `fetch_recent_outcome_exemplars` + `get_outcome_block` read the
     pre-existing `action_outcomes` table (older shape) and produce
     a prompt block the scorer + briefing reuse. Kept untouched —
     the two tables are intentionally parallel until the next
     consolidation pass.

Every send/reply/thumbs-up/thumbs-down/meeting/close becomes an
exemplar. The engine doesn't filter on outcomes — Claude is asked to
use them as reference, not as filter rules.
"""

import logging
from typing import Any, Dict, List, Optional

from supabase import Client

logger = logging.getLogger(__name__)


# Reply classifications map to lead_outcomes.outcome values.
# AUTO_REPLY produces no outcome row (it's noise).
CLASSIFICATION_TO_OUTCOME: Dict[str, str] = {
    "INTERESTED": "replied_positive",
    "CURIOUS": "replied_positive",
    "REFERRAL": "replied_positive",
    "NOT_NOW": "replied_neutral",
    "OBJECTION": "replied_neutral",
    "UNSUBSCRIBE": "dead",
    "DEAD": "dead",
}

# Allowed values for lead_outcomes.outcome — pin them for callers so a
# UI button or webhook can't send a free-text typo. Mirrors the CHECK
# constraint on lead_outcomes.outcome.
VALID_OUTCOMES = frozenset({
    "meeting_booked",
    "replied_positive",
    "replied_neutral",
    "no_reply",
    "not_contacted",
    "unqualified",
    "dead",
})


def record_lead_outcome(
    db: Client,
    *,
    org_id: str,
    lead_id: str,
    outcome: str,
    company_id: Optional[str] = None,
    reply_classification: Optional[str] = None,
    final_status: Optional[str] = None,
    matched_play: Optional[str] = None,
    scored_signals: Optional[List[Any]] = None,
    score_at_time: Optional[float] = None,
) -> None:
    """Insert one row into ``lead_outcomes``.

    Best-effort: a DB error is logged at WARN and swallowed. Outcome
    capture is observability/learning fuel; a single missed row is
    less bad than failing a status transition the user already
    committed to.

    `outcome` must be one of VALID_OUTCOMES — otherwise the row is
    skipped (with a log) rather than written. The migration's CHECK
    constraint is a second line of defence.
    """
    if outcome not in VALID_OUTCOMES:
        logger.warning(
            "record_lead_outcome called with unknown outcome %r (org=%s, lead=%s)",
            outcome, org_id, lead_id,
        )
        return

    row: Dict[str, Any] = {
        "org_id": org_id,
        "lead_id": lead_id,
        "outcome": outcome,
    }
    if company_id:
        row["company_id"] = company_id
    if reply_classification:
        row["reply_classification"] = reply_classification
    if final_status:
        row["final_status"] = final_status
    if matched_play:
        row["matched_play"] = matched_play
    if scored_signals is not None:
        row["scored_signals"] = scored_signals
    if score_at_time is not None:
        row["score_at_time"] = score_at_time

    try:
        db.table("lead_outcomes").insert(row).execute()
    except Exception as e:
        logger.warning(
            "lead_outcomes insert failed (org=%s, lead=%s, outcome=%s): %s",
            org_id, lead_id, outcome, e,
        )


def record_from_lead(
    db: Client,
    lead: Dict[str, Any],
    outcome: str,
    *,
    reply_classification: Optional[str] = None,
    final_status: Optional[str] = None,
) -> None:
    """Convenience: pull decision-time context off a lead row and call
    ``record_lead_outcome``.

    Used by classifier + status-transition flows. The lead row should
    already carry total_score + matched persona + (optionally) the
    finding payload captured at scoring time.
    """
    if not lead:
        return

    # Score at decision time (best available — total_score updated
    # by the scorer; if a more granular snapshot is ever stored we can
    # prefer that here).
    score = lead.get("total_score")
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None

    # The play name selected for this lead is on the persisted briefing
    # if there is one; otherwise None and the row still carries the
    # outcome + classification.
    matched_play = None
    enrichment = lead.get("enrichment_data") or {}
    briefing = enrichment.get("gtm_briefing") or {}
    rp = briefing.get("recommended_play") or {}
    if isinstance(rp, dict):
        matched_play = rp.get("play_name")

    # Anonymisable list of signal types that fired at scoring time —
    # tracks "what we knew when we decided".
    scored_signals = lead.get("signals") or []

    record_lead_outcome(
        db,
        org_id=lead.get("org_id"),
        lead_id=lead.get("id"),
        outcome=outcome,
        company_id=lead.get("company_id"),
        reply_classification=reply_classification,
        final_status=final_status,
        matched_play=matched_play,
        scored_signals=scored_signals,
        score_at_time=score,
    )

# Below this threshold the prompts use no outcome exemplars at all.
# Cold-start guard: a tiny corpus would over-bias the scorer.
MIN_OUTCOMES_FOR_EXEMPLARS = 5
MAX_EXEMPLARS_PER_BUCKET = 6


def fetch_recent_outcome_exemplars(
    db: Client,
    org_id: str,
    *,
    limit_per_bucket: int = MAX_EXEMPLARS_PER_BUCKET,
) -> Dict[str, List[Dict[str, Any]]]:
    """Return a dict of {positive: [...], negative: [...]} exemplars.

    Each exemplar carries: industry, title, signal_that_triggered,
    message_angle, classification (when available). Used to format a
    prompt section like:
        "Past positive outcomes: CISO @ Acme (FinTech), INTERESTED reply to CVE Urgency play"
    """
    try:
        rows = (
            db.table("action_outcomes")
            .select(
                "id, lead_id, outcome, outcome_detail, signal_that_triggered, "
                "message_angle, feedback_source, created_at, "
                "leads(title, company_industry, company_name, company_size)"
            )
            .eq("org_id", org_id)
            .in_("outcome", ["positive", "negative", "meeting_booked", "closed_won", "closed_lost"])
            .order("created_at", desc=True)
            .limit(limit_per_bucket * 4)
            .execute()
        )
    except Exception as e:
        logger.warning("fetch_recent_outcome_exemplars failed: %s", e)
        return {"positive": [], "negative": []}

    positive: List[Dict] = []
    negative: List[Dict] = []
    for r in rows.data or []:
        bucket = r.get("outcome")
        normalised = "positive" if bucket in ("positive", "meeting_booked", "closed_won") else (
            "negative" if bucket in ("negative", "closed_lost") else None
        )
        if not normalised:
            continue
        target = positive if normalised == "positive" else negative
        if len(target) >= limit_per_bucket:
            continue
        target.append(r)

    return {"positive": positive, "negative": negative}


def format_outcomes_for_prompt(exemplars: Dict[str, List[Dict[str, Any]]]) -> str:
    """Render exemplars as a compact bulleted block for the scoring/briefing prompt.

    Returns empty string when below the cold-start threshold so the prompt
    template can render "(none yet)" as a fallback."""
    pos = exemplars.get("positive") or []
    neg = exemplars.get("negative") or []
    if len(pos) + len(neg) < MIN_OUTCOMES_FOR_EXEMPLARS:
        return "(none yet — engine is still building feedback corpus for this org)"

    def _line(row: Dict[str, Any]) -> str:
        lead = (row.get("leads") or {}) if isinstance(row.get("leads"), dict) else {}
        title = lead.get("title") or "?"
        company = lead.get("company_name") or "?"
        industry = lead.get("company_industry") or ""
        bits = [f"{title} @ {company}"]
        if industry:
            bits.append(f"({industry})")
        signal = row.get("signal_that_triggered")
        angle = row.get("message_angle")
        outcome = row.get("outcome")
        detail = row.get("outcome_detail") or {}
        classification = detail.get("classification") if isinstance(detail, dict) else None
        tail_bits = []
        if signal:
            tail_bits.append(f"signal={signal}")
        if angle:
            tail_bits.append(f"play={angle}")
        if classification:
            tail_bits.append(f"reply={classification}")
        else:
            tail_bits.append(f"outcome={outcome}")
        return f"- {' '.join(bits)} — {', '.join(tail_bits)}"

    parts: List[str] = []
    if pos:
        parts.append("POSITIVE EXEMPLARS:")
        parts.extend(_line(r) for r in pos)
    if neg:
        parts.append("NEGATIVE EXEMPLARS:")
        parts.extend(_line(r) for r in neg)
    return "\n".join(parts)


def get_outcome_block(db: Client, org_id: str) -> str:
    """One-call convenience for prompt builders."""
    exemplars = fetch_recent_outcome_exemplars(db, org_id)
    return format_outcomes_for_prompt(exemplars)
