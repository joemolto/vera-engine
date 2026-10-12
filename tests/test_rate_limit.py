"""Tests for the table-backed rate limiter."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.services.rate_limit import (
    check_and_record,
    current_count,
    record_events,
    sweep_old_events,
)


def _mk_db(count_value: int = 0):
    """Build a mock supabase client that returns `count_value` from a
    select(count='exact') call."""
    db = MagicMock()
    # Mock the chain: db.table().select(count='exact').eq(...).eq(...).gte(...).execute()
    chain = db.table.return_value
    # select/eq/gte all return the same chain
    chain.select.return_value = chain
    chain.eq.return_value = chain
    chain.gte.return_value = chain
    chain.lt.return_value = chain
    chain.delete.return_value = chain
    # The select chain's execute returns a result with .count
    result = MagicMock()
    result.count = count_value
    result.data = []
    chain.execute.return_value = result
    return db


class TestCurrentCount:
    def test_returns_zero_when_count_is_none(self):
        db = _mk_db(count_value=None)
        assert current_count(db, "org-1", bucket_key="scoring") == 0

    def test_returns_supabase_count(self):
        db = _mk_db(count_value=42)
        assert current_count(db, "org-1", bucket_key="scoring") == 42


class TestRecordEvents:
    def test_zero_count_is_a_noop(self):
        db = _mk_db()
        record_events(db, "org-1", bucket_key="scoring", count=0)
        db.table.assert_not_called()

    def test_negative_count_is_a_noop(self):
        db = _mk_db()
        record_events(db, "org-1", bucket_key="scoring", count=-1)
        db.table.assert_not_called()

    def test_single_event_writes_one_row(self):
        db = _mk_db()
        record_events(db, "org-1", bucket_key="scoring", count=1)
        db.table.return_value.insert.assert_called_once()
        call = db.table.return_value.insert.call_args
        rows = call.args[0]
        assert len(rows) == 1
        assert rows[0]["org_id"] == "org-1"
        assert rows[0]["bucket_key"] == "scoring"

    def test_db_failure_does_not_raise(self):
        db = _mk_db()
        db.table.return_value.insert.return_value.execute.side_effect = (
            RuntimeError("boom")
        )
        # Caller (paying request) must not fail because the audit write failed
        record_events(db, "org-1", bucket_key="scoring", count=3)


class TestCheckAndRecord:
    def test_within_limit_records_events(self):
        db = _mk_db(count_value=10)
        check_and_record(
            db, "org-1",
            bucket_key="scoring",
            max_per_minute=100,
            count=5,
        )
        # Both the count query and the insert should have fired
        assert db.table.return_value.insert.called

    def test_at_limit_raises_429(self):
        db = _mk_db(count_value=99)
        # +5 would push to 104, over the limit of 100
        with pytest.raises(HTTPException) as exc:
            check_and_record(
                db, "org-1",
                bucket_key="scoring",
                max_per_minute=100,
                count=5,
            )
        assert exc.value.status_code == 429
        # Critically: no events were recorded on rejection
        db.table.return_value.insert.assert_not_called()

    def test_exact_limit_allowed(self):
        db = _mk_db(count_value=95)
        # 95 + 5 = 100 exactly, should pass
        check_and_record(
            db, "org-1",
            bucket_key="scoring",
            max_per_minute=100,
            count=5,
        )
        assert db.table.return_value.insert.called

    def test_per_bucket_isolation(self):
        # If counting "scoring" returns 99, hitting "brain" with a
        # different key should not be blocked. (Verified by passing the
        # bucket through unchanged — the mock returns 99 for everything,
        # so the test is structural: the count call uses the bucket_key
        # arg, not a hardcoded value.)
        db = _mk_db(count_value=50)
        check_and_record(
            db, "org-1",
            bucket_key="brain",
            max_per_minute=100,
            count=10,
        )
        # The .eq() chain should have been called with bucket_key="brain"
        eq_calls = db.table.return_value.eq.call_args_list
        bucket_keys = [c.args[1] for c in eq_calls if c.args[0] == "bucket_key"]
        assert "brain" in bucket_keys


class TestSweep:
    def test_returns_zero_on_db_error(self):
        db = _mk_db()
        db.table.return_value.delete.return_value.lt.return_value.execute.side_effect = (
            RuntimeError("boom")
        )
        assert sweep_old_events(db) == 0
