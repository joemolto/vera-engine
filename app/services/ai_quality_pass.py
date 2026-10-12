"""AI quality pass — Phase 2 of the cyber-pressure work (2026-05-28).

Runs ONE Claude Haiku call (via the existing ``call_claude`` helper —
shares prompt caching + retry + telemetry with the rest of the engine)
after every enrichment to do four cleanup jobs on the signals + tech
the deterministic engine just produced:

  1. **Dedup**            — group signals that mean the same thing
  2. **Entity resolution** — group tech entries that name the same vendor
  3. **Quality gate**     — score each signal 0-1 for surface-worthiness
  4. **Market-context**   — for `inferred` signals, add the
                            "for a company of this profile, X is typical"
                            hedge the rep needs to avoid false-premise outreach

Strict design rules (2026-05-28):

  - AI **never** invents facts. Every claim must reference an input signal_id.
  - AI **never** writes outreach copy, openers, or email drafts. Data cleanup
    only.
  - Honour signal classification: `observed` = fact, `derived` = solid,
    `inferred` = hypothesis, `speculative` = ignore.
  - Visibility framing: absence of public evidence is NEVER asserted as
    absence of capability (absence of evidence is not evidence of absence).
  - Hedge market-context language ("likely", "typically", "almost certainly")
    — never absolute claims about internal state.

Output gets persisted to ``companies.ai_quality_pass`` (JSONB column added
in migration `companies_ai_quality_pass`). Dashboard reads it from there.

Cost: ~$0.001-0.002 per enrichment via Haiku 4.5 (Anthropic direct).
Started here for simplicity (Anthropic key already wired). Easy swap to
OpenRouter (Gemini Flash ~3x cheaper) once that key's populated — change
the model + replace call_claude with the OpenRouter shape in deep_research.py.

Fail-quiet — any error returns an empty pass and the dashboard renders
the raw deterministic output (system never falls below the deterministic baseline).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services.claude import call_claude

logger = logging.getLogger(__name__)

# Cap input size — sanity guard. A typical enrichment has 8-15 signals +
# 30-100 tech entries which fits comfortably under 8K tokens.
MAX_SIGNALS_IN = 40
MAX_TECH_IN = 120


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_quality_pass(
    *,
    company_name: Optional[str],
    company_domain: str,
    company_size_band: Optional[str],
    company_industry: Optional[str],
    company_revenue: Optional[str],
    is_public_company: bool,
    signals: List[Dict[str, Any]],
    technologies: List[Dict[str, Any]],
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the four-job cleanup pass and return the structured output.

    Returns an empty (but well-shaped) dict on any failure — the
    deterministic engine's output is always the baseline the UI falls
    back to.
    """
    if not settings.ANTHROPIC_API_KEY:
        logger.warning("AI quality pass skipped — ANTHROPIC_API_KEY not set")
        return _empty_pass()

    if not signals and not technologies:
        return _empty_pass()

    # Trim inputs to budget
    signals_in = (signals or [])[:MAX_SIGNALS_IN]
    tech_in = (technologies or [])[:MAX_TECH_IN]

    user_prompt = _build_user_prompt(
        company_name=company_name,
        company_domain=company_domain,
        company_size_band=company_size_band,
        company_industry=company_industry,
        company_revenue=company_revenue,
        is_public_company=is_public_company,
        signals=signals_in,
        technologies=tech_in,
    )

    chosen_model = model or settings.CLAUDE_FAST_MODEL
    # 8K output budget — quality pass over a 100-tech, 12-signal payload
    # routinely needs 5-7K tokens to fully express dedup + entity groups +
    # context notes. Default 4K causes mid-string truncation = unparseable.
    parsed = call_claude(
        prompt=user_prompt,
        system=_SYSTEM_RULES,
        model=chosen_model,
        cache_system=True,
        max_tokens=8192,
    )

    if "error" in parsed:
        logger.warning(
            "AI quality pass failed for %s: %s | raw_excerpt=%r",
            company_domain,
            parsed.get("error"),
            (parsed.get("raw_text") or "")[:300],
        )
        return _empty_pass()

    # Defensive: ensure all expected keys exist with safe defaults so the
    # dashboard never crashes on a partial response.
    normalised = {
        "dedup_groups": _safe_list(parsed.get("dedup_groups")),
        "entity_groups": _safe_list(parsed.get("entity_groups")),
        "quality_scores": _safe_dict(parsed.get("quality_scores")),
        "market_context_notes": _safe_dict(parsed.get("market_context_notes")),
        "model": chosen_model,
    }
    logger.info(
        "AI quality pass for %s: %d dedup groups, %d entity groups, %d quality scores, %d context notes",
        company_domain,
        len(normalised["dedup_groups"]),
        len(normalised["entity_groups"]),
        len(normalised["quality_scores"]),
        len(normalised["market_context_notes"]),
    )
    return normalised


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_SYSTEM_RULES = """\
You are Vera's data-cleanup layer for cyber sales intelligence. Your ONLY
job is to clean and contextualise structured signals the deterministic
engine produced. You do four things, and ONLY these four:

  1. dedup_groups       — group signals that mean the same thing
  2. entity_groups      — group tech entries that name the same vendor
  3. quality_scores     — score each signal 0.0–1.0 for surface-worthiness
  4. market_context_notes — for INFERRED signals, add a one-line hedged
                            context note grounded in known patterns

HARD RULES (violation = wrong output):

  R1. NEVER invent a fact. Every claim must reference a signal_id from input.
  R2. NEVER write outreach copy, openers, email drafts, or first-lines.
      You do not generate messaging. Only cleanup + context.
  R3. NEVER assert something the company "doesn't have" / "lacks" / "is
      missing" based on absence of public evidence. A regulated company
      almost certainly holds standard frameworks internally even if the
      website doesn't display them. Frame as visibility, not deficiency.
  R4. Inference handling:
      - observed   = fact (use as-is)
      - derived    = solid (use confidently)
      - inferred   = hypothesis (always hedge: "likely", "typically")
      - speculative = ignore (don't reference at all)
  R5. Market-context notes are ONE sentence. Use "almost certainly", "likely
      holds", "typical for", "kept behind procurement", etc. Never absolute.
  R6. Output is JSON ONLY. No prose preamble, no explanation, no markdown.

Output schema (every key required, empty arrays/objects allowed):

{
  "dedup_groups": [
    {
      "primary_signal_id": "<keep this one>",
      "folded_signal_ids": ["<id>", "<id>"],
      "label": "<short label, no prose, e.g. '3 overlapping public-proof signals'>"
    }
  ],
  "entity_groups": [
    {
      "canonical_name": "Microsoft 365",
      "merged_names": ["M365", "Microsoft Office 365"]
    }
  ],
  "quality_scores": {
    "<signal_id>": {"score": 0.85, "note": "<why low/high, max 80 chars>"}
  },
  "market_context_notes": {
    "<signal_id>": "<one hedged sentence using the company facts to soften an inferred signal>"
  }
}
"""


def _build_user_prompt(
    *,
    company_name: Optional[str],
    company_domain: str,
    company_size_band: Optional[str],
    company_industry: Optional[str],
    company_revenue: Optional[str],
    is_public_company: bool,
    signals: List[Dict[str, Any]],
    technologies: List[Dict[str, Any]],
) -> str:
    """Build the user-message payload. System rules live separately so
    they share the call_claude prompt cache across enrichments."""
    company_facts = {
        "name": company_name or company_domain,
        "domain": company_domain,
        "size_band_employees": company_size_band,
        "industry": company_industry,
        "revenue_band": company_revenue,
        "is_public_company": is_public_company,
    }
    # Trim signals to just the fields the AI needs (keep payload small)
    sig_payload = [
        {
            "id": s.get("id"),
            "signal_type": s.get("signal_type"),
            "subject_type": s.get("signal_subject_type"),
            "inference_level": s.get("inference_level"),
            "source_quality": s.get("source_quality"),
            "signal_strength": s.get("signal_strength"),
            "headline": (s.get("signal_data") or {}).get("headline"),
            "detail": (s.get("signal_data") or {}).get("detail"),
        }
        for s in signals
        if s.get("id")
    ]
    tech_payload = [
        {
            "name": t.get("name"),
            "category": t.get("category"),
            "detection_methods": t.get("detection_methods", []),
        }
        for t in technologies
        if t.get("name")
    ]

    return (
        "--- COMPANY FACTS ---\n"
        + json.dumps(company_facts, indent=2)
        + "\n\n--- SIGNALS (just emitted this enrichment) ---\n"
        + json.dumps(sig_payload, indent=2)
        + "\n\n--- TECHNOLOGIES DETECTED ---\n"
        + json.dumps(tech_payload, indent=2)
        + "\n\n--- YOUR JSON OUTPUT ---\n"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_pass() -> Dict[str, Any]:
    return {
        "dedup_groups": [],
        "entity_groups": [],
        "quality_scores": {},
        "market_context_notes": {},
        "model": None,
    }


def _safe_list(v: Any) -> List[Any]:
    return v if isinstance(v, list) else []


def _safe_dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}
