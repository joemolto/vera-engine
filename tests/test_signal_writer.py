"""Tests for the validated signal-write boundary.

The goal of this layer is to keep empty / malformed signal rows out of
the dashboard. These tests pin the rejection rules + the side effects
(log entry, skipped insert) so we don't regress.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.signal_writer import (
    KNOWN_SIGNAL_TYPES,
    insert_signal,
    insert_signals,
)


def _valid_row(**over):
    base = {
        "org_id": "org-1",
        "lead_id": "lead-1",
        "signal_type": "recently_funded",
        "signal_source": "predictleads",
        "signal_data": {
            "headline": "Acme raised a Series B last week",
            "detail": "$25M, lead investor Atlas Ventures",
            "entity": "Acme Corp",
            "occurred_at": "2026-05-10",
        },
        "signal_strength": 0.9,
    }
    base.update(over)
    return base


class TestInsertSignal:
    def test_valid_payload_writes(self):
        db = MagicMock()
        ok = insert_signal(db, _valid_row())
        assert ok is True
        # Dedupe reads (leads/signals lookups) precede the write — assert
        # the signals write happened, not that it was the only table call.
        db.table.assert_any_call("signals")
        db.table.return_value.insert.assert_called_once()

    def test_missing_headline_rejected(self, caplog):
        db = MagicMock()
        row = _valid_row(signal_data={"detail": "no headline"})
        with caplog.at_level("WARNING"):
            ok = insert_signal(db, row)
        assert ok is False
        db.table.return_value.insert.assert_not_called()
        assert any("malformed signal" in r.getMessage() for r in caplog.records)

    def test_blank_headline_rejected(self):
        db = MagicMock()
        row = _valid_row(signal_data={"headline": "   "})
        assert insert_signal(db, row) is False
        db.table.return_value.insert.assert_not_called()

    def test_missing_signal_type_rejected(self):
        db = MagicMock()
        row = _valid_row()
        row.pop("signal_type")
        assert insert_signal(db, row) is False
        db.table.return_value.insert.assert_not_called()

    def test_missing_org_id_rejected(self):
        db = MagicMock()
        row = _valid_row(org_id="")
        assert insert_signal(db, row) is False

    def test_missing_lead_id_rejected(self):
        db = MagicMock()
        row = _valid_row(lead_id="")
        assert insert_signal(db, row) is False

    def test_strength_out_of_range_rejected(self):
        db = MagicMock()
        assert insert_signal(db, _valid_row(signal_strength=1.5)) is False
        assert insert_signal(db, _valid_row(signal_strength=-0.1)) is False

    def test_unknown_signal_type_accepted_with_info_log(self, caplog):
        db = MagicMock()
        row = _valid_row(signal_type="never_seen_before_signal")
        with caplog.at_level("INFO"):
            ok = insert_signal(db, row)
        assert ok is True
        assert any(
            "Unknown signal_type" in r.getMessage() for r in caplog.records
        )

    def test_known_signal_types_complete(self):
        """Pin the canonical set so accidental removals are visible."""
        assert "cve_in_stack" in KNOWN_SIGNAL_TYPES
        assert "compliance_gap" in KNOWN_SIGNAL_TYPES
        assert "recently_funded" in KNOWN_SIGNAL_TYPES
        assert "security_team_expansion" in KNOWN_SIGNAL_TYPES
        assert "breach_proximity" in KNOWN_SIGNAL_TYPES
        assert "security_leadership_hire" in KNOWN_SIGNAL_TYPES

    def test_db_error_does_not_raise(self):
        db = MagicMock()
        db.table.return_value.insert.return_value.execute.side_effect = (
            RuntimeError("supabase 500")
        )
        # Caller (enrichment pipeline) must not see an exception even
        # if the DB write fails — enrichment_data is the source of truth.
        assert insert_signal(db, _valid_row()) is False


class TestInsertSignals:
    def test_bulk_writes_only_valid_rows(self):
        db = MagicMock()
        good = _valid_row()
        bad = _valid_row(signal_data={"headline": ""})
        another_good = _valid_row(signal_type="compliance_gap")
        written = insert_signals(db, [good, bad, another_good])
        assert written == 2
        # The single bulk insert call should contain only the 2 valid rows
        call = db.table.return_value.insert.call_args
        args = call.args[0] if call.args else call.kwargs.get("rows")
        assert len(args) == 2

    def test_all_invalid_short_circuits_db(self):
        db = MagicMock()
        rows = [_valid_row(signal_data={"headline": ""}) for _ in range(3)]
        assert insert_signals(db, rows) == 0
        db.table.return_value.insert.assert_not_called()

    def test_empty_input(self):
        db = MagicMock()
        assert insert_signals(db, []) == 0
        db.table.return_value.insert.assert_not_called()
