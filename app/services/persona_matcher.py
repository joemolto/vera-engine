"""Pick the best-matching persona for a scored lead.

Runs after the main LLM scorer — the LLM produces a single total_score
considering the union of all personas' fields (dual-write makes this
cheap). This module then does a deterministic Python match to figure out
WHICH persona the lead best fits, and writes that persona's UUID to
leads.matched_persona_id.

No LLM calls. Pure field-overlap scoring across titles, industries,
locations, company size, and tech. Cheap; <1ms per lead.

Why a separate matcher rather than per-persona LLM scoring:
  - Per-persona LLM scoring would N× the token cost of the scorer
  - Deterministic matching is good enough for "tag the right persona"
  - The LLM score stays the source of truth for ranking; matcher only
    answers "of all the personas this org has, which one fits this lead?"
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.services.personas import Persona

logger = logging.getLogger(__name__)


# Weights — sum to 100. Adjust if a category turns out to dominate
# unfairly in production. Defaults reflect "title is the most signal-rich
# data point in cyber sales; tech overlap is the next-best indicator of
# fit".
W_TITLE = 30
W_INDUSTRY = 25
W_TECH = 15
W_LOCATION = 15
W_SIZE = 15


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def _any_substr_match(needles: List[str], haystack: str) -> bool:
    """True if any needle appears in haystack (case-insensitive substring)."""
    hay = _norm(haystack)
    if not hay:
        return False
    return any(_norm(n) and _norm(n) in hay for n in needles)


def _list_overlap_score(persona_values: List[str], lead_values: List[str], weight: int) -> int:
    """Substring-match between two lists. Returns the weight if any pair
    overlaps, else 0. Not the *number* of matches — that would let a
    persona with many entries dominate one with few."""
    if not persona_values:
        return weight  # No filter set = match anything (don't penalise)
    if not lead_values:
        return 0
    pn = {_norm(v) for v in persona_values if v}
    ln = {_norm(v) for v in lead_values if v}
    if pn & ln:
        return weight
    # Substring match — handles "VP Sales" persona vs "VP of Sales" lead
    for p in pn:
        for l in ln:
            if p and l and (p in l or l in p):
                return weight
    return 0


def _company_size_match(persona: Persona, lead_size: Any) -> int:
    """Lead company_size can be 'N' (an integer-ish string) or a bucket
    string. Map both forms to Apollo's bucket convention for comparison."""
    persona_sizes = persona.get("target_company_size") or []
    if not persona_sizes:
        return W_SIZE
    if not lead_size:
        return 0

    s = _norm(lead_size)
    # If already a bucket
    for bucket in persona_sizes:
        if _norm(bucket) == s:
            return W_SIZE

    # If numeric
    try:
        n = int(float(s.replace(",", "")))
    except (TypeError, ValueError):
        return 0

    for bucket in persona_sizes:
        try:
            lo, hi = bucket.split(",", 1)
            if int(lo.strip()) <= n <= int(hi.strip()):
                return W_SIZE
        except (ValueError, AttributeError):
            continue
    return 0


def score_persona_match(
    lead: Dict[str, Any],
    enrichment: Dict[str, Any],
    persona: Persona,
) -> int:
    """Deterministic 0-100 fit score for a (lead, persona) pair."""
    total = 0

    total += _list_overlap_score(
        persona.get("target_titles") or [],
        [lead.get("title") or ""],
        W_TITLE,
    )

    total += _list_overlap_score(
        persona.get("target_industries") or [],
        [lead.get("company_industry") or ""],
        W_INDUSTRY,
    )

    total += _list_overlap_score(
        persona.get("target_locations") or [],
        [lead.get("company_location") or ""],
        W_LOCATION,
    )

    total += _company_size_match(persona, lead.get("company_size"))

    detected_tech = []
    tech_block = (enrichment or {}).get("tech_stack") or {}
    if isinstance(tech_block, dict):
        detected_tech = (
            tech_block.get("technologies_detected")
            or tech_block.get("technologies")
            or []
        )
    total += _list_overlap_score(
        persona.get("target_tech_uses") or [],
        detected_tech,
        W_TECH,
    )

    return total


def pick_best_persona(
    lead: Dict[str, Any],
    enrichment: Dict[str, Any],
    personas: List[Persona],
) -> Optional[Dict[str, Any]]:
    """Return {persona_id, persona_name, match_score} for the best fit,
    or None when there are no active personas. Ties broken by first.

    Only considers active=True personas — muted profiles are skipped so
    they don't get attribution they didn't earn.
    """
    active = [p for p in personas if p.get("active", True)]
    if not active:
        return None

    best = None
    best_score = -1
    for p in active:
        s = score_persona_match(lead, enrichment, p)
        if s > best_score:
            best_score = s
            best = p

    if best is None:
        return None

    return {
        "persona_id": best.get("id"),
        "persona_name": best.get("name"),
        "match_score": best_score,
    }
