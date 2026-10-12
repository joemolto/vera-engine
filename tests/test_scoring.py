"""Tests for lead scoring.

Real coverage of the scorer's deterministic surface area + a smoke test
of the Claude-driven path with a mocked LLM. The previous prompt-shape
and single-lead tests pinned the legacy SCORING_PROMPT contract; they
have been replaced with tests that match the current code.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.prompts.templates import parse_claude_json
from app.services.scorer import (
    _check_data_quality,
    _check_icp_prefilter,
)


# ---------------------------------------------------------------------
# JSON parsing — still useful coverage
# ---------------------------------------------------------------------
class TestParseClaudeJSON:
    def test_clean_json(self):
        result = parse_claude_json('{"lead_rating": "hot"}')
        assert result["lead_rating"] == "hot"

    def test_json_with_markdown_fences(self):
        result = parse_claude_json('```json\n{"lead_rating": "warm"}\n```')
        assert result["lead_rating"] == "warm"

    def test_json_with_leading_text(self):
        result = parse_claude_json(
            'Here is the result:\n{"lead_rating": "hot"}'
        )
        assert result["lead_rating"] == "hot"

    def test_malformed_json_returns_error(self):
        assert "error" in parse_claude_json("this is not json at all")

    def test_empty_string_returns_error(self):
        assert "error" in parse_claude_json("")


# ---------------------------------------------------------------------
# Data-quality gate — deterministic, runs before any Claude call
# ---------------------------------------------------------------------
class TestDataQuality:
    def test_complete_lead_passes(self):
        lead = {
            "first_name": "Jane",
            "last_name": "Smith",
            "email": "jane@acme.com",
            "email_confidence": 95,
            "title": "VP Security",
        }
        assert _check_data_quality(lead) is None

    def test_hidden_last_name_alone_is_one_issue(self):
        # One issue is allowed — the gate fires at 2+
        lead = {
            "first_name": "Jane",
            "last_name": "S",
            "email": "jane@acme.com",
            "email_confidence": 95,
            "title": "VP Security",
        }
        assert _check_data_quality(lead) is None

    def test_two_issues_is_disqualifying(self):
        lead = {
            "first_name": "Jane",
            "last_name": "S",        # incomplete (issue 1)
            "email": "",              # missing (issue 2)
            "title": "VP Security",
        }
        issue = _check_data_quality(lead)
        assert issue is not None
        assert "last name" in issue.lower()
        assert "email" in issue.lower()

    def test_linkedin_headline_as_title(self):
        long_headline = "x" * 90
        lead = {
            "first_name": "Jane",
            "last_name": "Smith",
            "email": "",                  # issue 1
            "title": long_headline,       # issue 2 — too long
        }
        issue = _check_data_quality(lead)
        assert issue is not None
        assert "linkedin headline" in issue.lower()


# ---------------------------------------------------------------------
# ICP pre-filter — the biggest cost-saver in the pipeline
# ---------------------------------------------------------------------
class TestIcpPrefilter:
    def _lead(self, **over):
        base = {
            "id": "L1",
            "first_name": "Jane",
            "last_name": "Smith",
            "email": "jane@acme.com",
            "email_confidence": 95,
            "title": "VP Security",
            "data_status": "verified",
            "company_name": "Acme Bank",
            "company_industry": "banking",
            "enrichment_data": {"website": {}},
        }
        base.update(over)
        return base

    def test_stale_data_blocks(self):
        out = _check_icp_prefilter(self._lead(data_status="stale"), {})
        assert out is not None and "left the company" in out

    def test_disqualify_signal_blocks(self):
        client_ctx = {"disqualify_signals": "casino, crypto"}
        lead = self._lead(company_industry="crypto exchange")
        out = _check_icp_prefilter(lead, {}, client_ctx)
        assert out is not None and "disqualify signal" in out

    def test_competitor_rule_blocks_cyber_industries(self):
        client_ctx = {
            "critical_rules": (
                "Cybersecurity companies are competitors. Do not prospect."
            ),
        }
        lead = self._lead(company_industry="cybersecurity", company_name="Sentinel Co")
        out = _check_icp_prefilter(lead, {}, client_ctx)
        assert out is not None and "competitor industry" in out

    def test_competitor_rule_inactive_without_phrase_match(self):
        # No matching rule phrase → competitor filter does not engage.
        client_ctx = {"critical_rules": "Lead with growth angles, never fear."}
        lead = self._lead(company_industry="cybersecurity", company_name="Sentinel Co")
        out = _check_icp_prefilter(lead, {}, client_ctx)
        assert out is None or "competitor industry" not in (out or "")

    def test_cyber_keyword_in_name_overrides_industry(self):
        # Cyber keyword in name auto-passes to Claude regardless of label
        lead = self._lead(company_industry="other", company_name="SentinelOne Inc")
        out = _check_icp_prefilter(lead, {})
        assert out is None

    def test_no_enrichment_blocks(self):
        lead = self._lead(enrichment_data={})
        out = _check_icp_prefilter(lead, {})
        assert out is not None and "no enrichment data" in out.lower()


# ---------------------------------------------------------------------
# End-to-end smoke (mocked Claude) — covers the rating-coercion +
# malformed-JSON-fallback path that previously had no real assertion.
# ---------------------------------------------------------------------
class TestScoreSingleLead:
    def _lead(self, **over):
        base = {
            "id": "L1",
            "first_name": "Jane",
            "last_name": "Smith",
            "email": "jane@acme.com",
            "email_confidence": 95,
            "title": "VP Security",
            "data_status": "verified",
            "company_name": "Acme Bank",
            "company_industry": "banking",
            "company_size": "201-500",
            "company_location": "London",
            "enrichment_data": {"website": {"summary": "Acme is a UK bank."}},
        }
        base.update(over)
        return base

    def _ctx(self, **over):
        base = {
            "client_name": "TestCo",
            "what_they_sell": "Cyber GRC platform",
        }
        base.update(over)
        return base

    @patch("app.services.scorer.call_claude")
    @patch("app.services.scorer.log_activity")
    @patch("app.services.outcomes.get_outcome_block", return_value="(none yet)")
    def test_hot_rating_flows_through(self, _outcome, _log, mock_claude):
        from app.services.scorer import _score_single_lead

        mock_claude.return_value = {
            "lead_rating": "hot",
            "rating_reason": "Recent funding + security hiring + ICP fit.",
            "buying_signals": ["recent_funding", "hiring_security"],
            "icp_fit": {"score": 90},
        }

        db = MagicMock()
        db.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock()

        result = _score_single_lead(
            db, "org-1", self._lead(),
            icp_config={"phase": 1},
            client_ctx=self._ctx(),
        )

        assert result.get("lead_rating") == "hot"
        # Should have updated the lead row
        assert db.table.return_value.update.called

    @patch("app.services.scorer.call_claude")
    @patch("app.services.scorer.log_activity")
    @patch("app.services.outcomes.get_outcome_block", return_value="(none yet)")
    def test_cold_from_claude_coerced_to_warm(self, _outcome, _log, mock_claude):
        # Pre-filter is responsible for cold; if Claude still returns cold
        # we coerce up to warm to preserve the LLM-path contract.
        from app.services.scorer import _score_single_lead

        mock_claude.return_value = {
            "lead_rating": "cold",
            "rating_reason": "Wrong vertical.",
        }
        db = MagicMock()
        db.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock()

        result = _score_single_lead(
            db, "org-1", self._lead(),
            icp_config={}, client_ctx=self._ctx(),
        )
        assert result.get("lead_rating") == "warm"

    @patch("app.services.scorer.call_claude")
    @patch("app.services.scorer.log_activity")
    @patch("app.services.outcomes.get_outcome_block", return_value="(none yet)")
    def test_malformed_claude_response_falls_back_to_warm(
        self, _outcome, _log, mock_claude,
    ):
        from app.services.scorer import _score_single_lead

        # No lead_rating at all → llm_responses.ScoringResponse rejects;
        # scorer defaults to "warm".
        mock_claude.return_value = {"random": "garbage"}
        db = MagicMock()
        db.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock()

        result = _score_single_lead(
            db, "org-1", self._lead(),
            icp_config={}, client_ctx=self._ctx(),
        )
        assert result.get("lead_rating") == "warm"
