"""Tests for the lead_outcomes write path."""

from __future__ import annotations

from unittest.mock import MagicMock

from app.services.outcomes import (
    CLASSIFICATION_TO_OUTCOME,
    VALID_OUTCOMES,
    record_from_lead,
    record_lead_outcome,
)


def _mk_db():
    db = MagicMock()
    db.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "row-1"}],
    )
    return db


class TestRecordLeadOutcome:
    def test_writes_minimal_row(self):
        db = _mk_db()
        record_lead_outcome(
            db, org_id="org-1", lead_id="L1", outcome="meeting_booked",
        )
        db.table.assert_called_once_with("lead_outcomes")
        call = db.table.return_value.insert.call_args
        row = call.args[0]
        assert row["org_id"] == "org-1"
        assert row["lead_id"] == "L1"
        assert row["outcome"] == "meeting_booked"
        # Optional fields not provided should not be in the row
        assert "reply_classification" not in row
        assert "scored_signals" not in row

    def test_includes_optional_fields(self):
        db = _mk_db()
        record_lead_outcome(
            db,
            org_id="org-1", lead_id="L1", outcome="replied_positive",
            company_id="c-1",
            reply_classification="INTERESTED",
            final_status="replied",
            matched_play="Recent Funding",
            scored_signals=[{"type": "recent_funding"}],
            score_at_time=87.5,
        )
        row = db.table.return_value.insert.call_args.args[0]
        assert row["company_id"] == "c-1"
        assert row["reply_classification"] == "INTERESTED"
        assert row["final_status"] == "replied"
        assert row["matched_play"] == "Recent Funding"
        assert row["scored_signals"] == [{"type": "recent_funding"}]
        assert row["score_at_time"] == 87.5

    def test_unknown_outcome_rejected_and_not_written(self, caplog):
        db = _mk_db()
        with caplog.at_level("WARNING"):
            record_lead_outcome(
                db, org_id="org-1", lead_id="L1", outcome="banana",
            )
        db.table.return_value.insert.assert_not_called()
        assert any("unknown outcome" in r.getMessage() for r in caplog.records)

    def test_swallows_db_errors(self):
        db = _mk_db()
        db.table.return_value.insert.return_value.execute.side_effect = (
            RuntimeError("supabase 500")
        )
        # Must not propagate
        record_lead_outcome(
            db, org_id="org-1", lead_id="L1", outcome="meeting_booked",
        )

    def test_valid_outcomes_pinned(self):
        # Lock the enum so removals are visible
        assert "meeting_booked" in VALID_OUTCOMES
        assert "no_reply" in VALID_OUTCOMES
        assert "not_contacted" in VALID_OUTCOMES
        assert "replied_positive" in VALID_OUTCOMES
        assert "replied_neutral" in VALID_OUTCOMES
        assert "dead" in VALID_OUTCOMES


class TestRecordFromLead:
    def test_pulls_decision_context_off_lead(self):
        db = _mk_db()
        lead = {
            "id": "L1",
            "org_id": "org-1",
            "company_id": "c-1",
            "total_score": "92.5",  # supabase often returns numerics as strings
            "signals": [{"type": "recent_funding"}],
            "status": "replied",
            "enrichment_data": {
                "gtm_briefing": {
                    "recommended_play": {"play_name": "Funding Surge"},
                },
            },
        }
        record_from_lead(
            db, lead, "replied_positive",
            reply_classification="INTERESTED",
            final_status="replied",
        )
        row = db.table.return_value.insert.call_args.args[0]
        assert row["org_id"] == "org-1"
        assert row["lead_id"] == "L1"
        assert row["company_id"] == "c-1"
        assert row["score_at_time"] == 92.5
        assert row["matched_play"] == "Funding Surge"
        assert row["scored_signals"] == [{"type": "recent_funding"}]
        assert row["reply_classification"] == "INTERESTED"

    def test_handles_missing_briefing(self):
        db = _mk_db()
        lead = {
            "id": "L1",
            "org_id": "org-1",
            "total_score": 50,
        }
        record_from_lead(db, lead, "no_reply")
        row = db.table.return_value.insert.call_args.args[0]
        assert row["outcome"] == "no_reply"
        assert row.get("matched_play") in (None, "")
        # If matched_play resolved to None, it should be omitted
        assert "matched_play" not in row or row["matched_play"] is None

    def test_none_lead_is_a_noop(self):
        db = _mk_db()
        record_from_lead(db, None, "no_reply")
        db.table.assert_not_called()


class TestClassificationMapping:
    def test_positive_classifications_map_to_positive_outcome(self):
        assert CLASSIFICATION_TO_OUTCOME["INTERESTED"] == "replied_positive"
        assert CLASSIFICATION_TO_OUTCOME["CURIOUS"] == "replied_positive"
        assert CLASSIFICATION_TO_OUTCOME["REFERRAL"] == "replied_positive"

    def test_neutral_classifications_map_to_neutral(self):
        assert CLASSIFICATION_TO_OUTCOME["NOT_NOW"] == "replied_neutral"
        assert CLASSIFICATION_TO_OUTCOME["OBJECTION"] == "replied_neutral"

    def test_dead_classifications_map_to_dead(self):
        assert CLASSIFICATION_TO_OUTCOME["UNSUBSCRIBE"] == "dead"
        assert CLASSIFICATION_TO_OUTCOME["DEAD"] == "dead"

    def test_auto_reply_unmapped(self):
        # AUTO_REPLY is noise — explicitly NOT in the map so it
        # produces no outcome row
        assert "AUTO_REPLY" not in CLASSIFICATION_TO_OUTCOME
