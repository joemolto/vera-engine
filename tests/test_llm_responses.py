"""Tests for the LLM response validator + concrete schemas."""

from __future__ import annotations

import pytest

from app.services.llm_responses import (
    CustomSignalEvaluation,
    NarrativeResponse,
    ScoringResponse,
    TechFilterResponse,
    parse_llm_response,
)


class TestParseLlmResponse:
    def test_non_dict_returns_none(self, caplog):
        with caplog.at_level("WARNING"):
            out = parse_llm_response(NarrativeResponse, "oops", context="t")
        assert out is None
        assert any("not a dict" in r.getMessage() for r in caplog.records)

    def test_none_returns_none(self):
        assert parse_llm_response(NarrativeResponse, None) is None

    def test_engine_error_dict_treated_as_failure(self, caplog):
        with caplog.at_level("WARNING"):
            out = parse_llm_response(
                NarrativeResponse, {"error": "rate limited"}, context="t"
            )
        assert out is None
        assert any("LLM call returned error" in r.getMessage() for r in caplog.records)

    def test_valid_payload_returns_model(self):
        raw = {
            "intelligence_summary": "Acme just raised a Series B.",
            "play_pitch": "Lead with growth angle.",
            "opening_line": "Hi Alex — saw the round.",
            "timing_assessment": "Now.",
        }
        out = parse_llm_response(NarrativeResponse, raw)
        assert out is not None
        assert out.intelligence_summary.startswith("Acme")


class TestNarrativeResponse:
    def test_requires_non_blank_summary(self):
        assert parse_llm_response(
            NarrativeResponse, {"intelligence_summary": ""}
        ) is None
        assert parse_llm_response(
            NarrativeResponse, {"intelligence_summary": "   "}
        ) is None
        assert parse_llm_response(
            NarrativeResponse, {"intelligence_summary": "x"}
        ) is not None

    def test_optional_fields_default_blank(self):
        out = parse_llm_response(
            NarrativeResponse, {"intelligence_summary": "ok"}
        )
        assert out is not None
        assert out.play_pitch == ""
        assert out.opening_line == ""

    def test_extra_fields_preserved_via_model_dump(self):
        raw = {
            "intelligence_summary": "ok",
            "rogue_field": "still here",
        }
        out = parse_llm_response(NarrativeResponse, raw)
        assert out is not None
        # extra="allow" keeps rogue fields accessible
        assert out.model_dump().get("rogue_field") == "still here"


class TestScoringResponse:
    def test_rating_must_be_in_range(self):
        assert parse_llm_response(
            ScoringResponse, {"lead_rating": "lukewarm"}
        ) is None
        assert parse_llm_response(
            ScoringResponse, {"lead_rating": "HOT"}
        ) is not None  # case-normalised

    def test_normalised_to_lower(self):
        out = parse_llm_response(ScoringResponse, {"lead_rating": "Warm"})
        assert out is not None
        assert out.lead_rating == "warm"

    def test_extra_fields_pass_through(self):
        raw = {
            "lead_rating": "hot",
            "rating_reason": "matches ICP",
            "buying_signals": ["recent_funding"],
            "icp_fit": {"score": 90},
        }
        out = parse_llm_response(ScoringResponse, raw)
        assert out is not None
        assert out.model_dump().get("buying_signals") == ["recent_funding"]


class TestTechFilterResponse:
    def test_empty_categories_default(self):
        out = parse_llm_response(TechFilterResponse, {})
        assert out is not None
        assert out.security_tools == []
        assert out.total_relevant == 0

    def test_categories_parse(self):
        raw = {
            "security_tools": [{"name": "CrowdStrike", "relevance": "EDR competitor"}],
            "total_relevant": 5,
        }
        out = parse_llm_response(TechFilterResponse, raw)
        assert out is not None
        assert out.security_tools[0].name == "CrowdStrike"
        assert out.total_relevant == 5

    def test_malformed_category_item_rejected(self):
        # name is required, missing dict items invalidate the list entry
        raw = {"security_tools": ["not-a-dict"]}
        out = parse_llm_response(TechFilterResponse, raw)
        assert out is None


class TestCustomSignalEvaluation:
    def test_arbitrary_keys_pass_through(self):
        raw = {"funding_5m_plus": True, "regulated_uk": False, "extra": 42}
        out = parse_llm_response(CustomSignalEvaluation, raw)
        assert out is not None
        d = out.model_dump()
        assert d.get("funding_5m_plus") is True
        assert d.get("regulated_uk") is False
