"""Pydantic schemas + validator for LLM JSON responses.

Anthropic / OpenRouter can return malformed JSON on edge cases or
silently drift their response shape. Without validation, missing fields
cascade as ``None`` / ``KeyError`` through the briefing and scoring
pipelines and briefings silently degrade.

This module provides:

  • One Pydantic model per LLM call site.
  • ``parse_llm_response`` — a wrapper that validates and either returns
    the parsed model OR ``None`` (with a WARN log naming the failed
    fields). Never raises. Callers fall back to their existing default.

Integration pattern at each call site is one line:

    raw = call_claude(prompt)
    parsed = parse_llm_response(NarrativeResponse, raw, context="briefing")
    if parsed is None:
        return _fallback()
    # use raw / parsed as before
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Type, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def parse_llm_response(
    model_cls: Type[T],
    raw: Any,
    *,
    context: str = "",
) -> Optional[T]:
    """Validate raw LLM output against ``model_cls``.

    Returns the parsed model on success, ``None`` on failure (with a
    WARN log naming the failed fields). Never raises — the upstream
    pipeline is responsible for its own fallback behaviour.
    """
    # Engine wraps Claude errors as {"error": "..."} dicts. Treat as a
    # validation failure so the caller falls back uniformly.
    if isinstance(raw, dict) and "error" in raw and len(raw) <= 2:
        logger.warning(
            "LLM call returned error [%s]: %s",
            context or model_cls.__name__,
            raw.get("error"),
        )
        return None
    if not isinstance(raw, dict):
        logger.warning(
            "LLM response not a dict [%s]: got %s",
            context or model_cls.__name__,
            type(raw).__name__,
        )
        return None
    try:
        return model_cls.model_validate(raw)
    except ValidationError as e:
        logger.warning(
            "LLM response failed validation [%s]: %s",
            context or model_cls.__name__,
            e.errors(include_url=False, include_input=False),
        )
        return None


# ---------------------------------------------------------------------
# Concrete response shapes
# ---------------------------------------------------------------------


class NarrativeResponse(BaseModel):
    """``gtm_briefing._generate_narrative`` output."""

    model_config = ConfigDict(extra="allow")

    intelligence_summary: str = Field(..., min_length=1)
    play_pitch: str = ""
    opening_line: str = ""
    timing_assessment: str = ""

    @field_validator("intelligence_summary")
    @classmethod
    def _summary_not_blank(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("intelligence_summary must not be blank")
        return s


class CustomSignalEvaluation(BaseModel):
    """``gtm_briefing._evaluate_custom_signals`` output.

    Keys are caller-defined (one entry per custom signal name the
    client configured), so we just pin that the response is a dict.
    The downstream coercion to bool already lives in the caller.
    """

    model_config = ConfigDict(extra="allow")


class TechCategoryItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""
    relevance: str = ""


class TechFilterResponse(BaseModel):
    """``predictleads.filter_technologies_with_ai`` output."""

    model_config = ConfigDict(extra="ignore")

    security_tools: List[TechCategoryItem] = []
    compliance: List[TechCategoryItem] = []
    infrastructure: List[TechCategoryItem] = []
    development: List[TechCategoryItem] = []
    data_monitoring: List[TechCategoryItem] = []
    total_relevant: int = 0
    total_removed: int = 0


class ScoringResponse(BaseModel):
    """``scorer._score_single_lead`` output.

    Permissive on the long tail of optional fields (buying_signals,
    icp_fit, signal_strength, etc.) so the model can evolve without
    breaking. Strict on ``lead_rating`` because that's the field the
    rest of the pipeline keys off.
    """

    model_config = ConfigDict(extra="allow")

    lead_rating: str
    rating_reason: str = ""

    @field_validator("lead_rating")
    @classmethod
    def _rating_in_range(cls, v: str) -> str:
        s = (v or "").strip().lower()
        if s not in {"hot", "warm", "cold"}:
            raise ValueError(
                f"lead_rating must be one of hot/warm/cold, got {v!r}"
            )
        return s
