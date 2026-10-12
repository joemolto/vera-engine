"""SELF_HOSTED lifts every plan cap; off, Solo caps apply unchanged."""

from unittest.mock import MagicMock, patch

from app.config import settings
from app.services.plan_limits import SELF_HOSTED_LIMITS, SOLO_LIMITS, effective_limits


def _db_with_org(row):
    db = MagicMock()
    (db.table.return_value.select.return_value.eq.return_value
     .maybe_single.return_value.execute.return_value.data) = row
    return db


def test_self_hosted_limits_are_unlimited_and_skip_the_db():
    db = _db_with_org({"tier": "solo", "subscription_status": "canceled"})
    with patch.object(settings, "SELF_HOSTED", True):
        assert effective_limits("org-1", db) == SELF_HOSTED_LIMITS
    db.table.assert_not_called()


def test_hosted_solo_limits_unchanged():
    db = _db_with_org({"tier": "solo", "capacity_packs": 0, "subscription_status": "active"})
    with patch.object(settings, "SELF_HOSTED", False):
        assert effective_limits("org-1", db) == SOLO_LIMITS


def test_scheduler_waits_until_next_run_hour():
    from datetime import datetime, timezone
    from app.services.scheduler import seconds_until

    at = lambda h, m=0: datetime(2026, 10, 3, h, m, tzinfo=timezone.utc)  # noqa: E731
    assert seconds_until(4, at(3)) == 3600            # later today
    assert seconds_until(4, at(4)) == 24 * 3600       # exactly on the hour: tomorrow, never 0
    assert seconds_until(4, at(4, 30)) == 23.5 * 3600  # just missed: tomorrow


def test_hosted_only_routes_404_when_self_hosted():
    import pytest
    from fastapi import HTTPException
    from app.services.api_auth import hosted_only

    with patch.object(settings, "SELF_HOSTED", False):
        hosted_only()  # hosted: route stays available
    with patch.object(settings, "SELF_HOSTED", True), pytest.raises(HTTPException) as exc:
        hosted_only()
    assert exc.value.status_code == 404


def test_brain_add_to_watchlist_reaches_the_tier_check():
    """Regression: the brain imported TIER_LIMITS from a module that doesn't
    define it, so every add_to_watchlist call raised ImportError."""
    from app.services.vera_brain import _exec_add_to_watchlist

    db = MagicMock()
    db.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = []
    db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [{"tier": "scout"}]
    db.table.return_value.select.return_value.eq.return_value.execute.return_value.count = 999
    out = _exec_add_to_watchlist(db, "org-1", "Example Corp", "example.com")
    assert "watchlist limit reached" in out
