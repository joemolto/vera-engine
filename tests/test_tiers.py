"""Per-tier suggestion cap — single source of truth + Brain enforcement.

Regression: a Scout org reached 20 suggestions because /discover capped
at 5 but the Brain's store_suggestions did not. These tests lock (a) one
shared module both paths use, and (b) the Brain hard-caps at the tier
ceiling regardless of what the LLM asks for.
"""

from app.services import tiers


class _Resp:
    def __init__(self, data=None, count=None):
        self.data = data if data is not None else []
        self.count = count


class _Q:
    """Minimal chainable Supabase stub configured per test."""

    def __init__(self, fake):
        self.fake = fake
        self._table = None
        self._count = None
        self._filters = {}
        self._insert = None

    def table(self, name):
        self._table = name
        return self

    def select(self, *a, **k):
        self._count = k.get("count")
        return self

    def insert(self, row):
        self._insert = row
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        if self._insert is not None:
            self.fake.inserted.append(self._insert)
            return _Resp(data=[{"id": "new"}])
        if self._table == "organisations":
            return _Resp(data=[{"tier": self.fake.tier}])
        if self._table == "leads" and self._count == "exact":
            return _Resp(count=self.fake.existing_suggested)
        if self._table == "leads":  # per-lead dedup lookups
            return _Resp(data=[])
        return _Resp()


class _FakeDB:
    def __init__(self, tier, existing_suggested=0):
        self.tier = tier
        self.existing_suggested = existing_suggested
        self.inserted = []

    def table(self, name):
        return _Q(self).table(name)


class TestTierTargets:
    def test_mapping(self):
        assert tiers.SUGGESTION_TARGETS == {
            "intel": 0, "scout": 5, "growth": 20, "scale": 75}
        assert tiers.DEFAULT_SUGGESTION_TARGET == 5

    def test_get_suggestion_target_by_tier(self):
        assert tiers.get_suggestion_target(_FakeDB("scout"), "o") == 5
        assert tiers.get_suggestion_target(_FakeDB("growth"), "o") == 20
        assert tiers.get_suggestion_target(_FakeDB("scale"), "o") == 75

    def test_unknown_or_missing_tier_defaults_to_5(self):
        assert tiers.get_suggestion_target(_FakeDB(None), "o") == 5
        assert tiers.get_suggestion_target(_FakeDB("enterprise_typo"), "o") == 5

    def test_discover_and_brain_share_one_function(self):
        # The literal anti-bug assertion: both ingestion paths must call
        # the SAME callable, so they can never diverge again.
        from app.routers.leads import _get_suggestion_target
        assert _get_suggestion_target is tiers.get_suggestion_target


class TestBrainHardCap:
    def _store(self, db, n):
        from app.services.vera_brain import _exec_store_suggestions
        leads = [{"email": f"p{i}@x{i}.com", "first_name": f"P{i}"}
                 for i in range(n)]
        return _exec_store_suggestions(db, "org-1", leads)

    def test_scout_pool_already_full_stores_nothing(self):
        db = _FakeDB("scout", existing_suggested=5)
        msg = self._store(db, 8)
        assert db.inserted == []
        assert "tier cap" in msg.lower()
        assert "enrich" in msg.lower()  # nudged to enrich, not source

    def test_scout_partial_room_caps_to_remaining(self):
        db = _FakeDB("scout", existing_suggested=2)  # room for 3
        msg = self._store(db, 10)
        assert len(db.inserted) == 3
        assert "3/5" in msg or "5/5" in msg
        assert "cap reached" in msg.lower() or "not stored" in msg.lower()

    def test_scale_tier_allows_more(self):
        db = _FakeDB("scale", existing_suggested=0)  # cap 75
        self._store(db, 10)
        assert len(db.inserted) == 10  # well under 75, all stored


# ---------------------------------------------------------------------------
# First-pass enrichment of the cold suggestion pool — the missing wire.
# Deterministic: enforced in code, bounded by tier target AND credits.
# ---------------------------------------------------------------------------

from unittest.mock import patch  # noqa: E402


def _sugg_rows(n):
    return [{"id": f"L{i}", "first_name": "A", "last_name": "B",
             "company_name": "C"} for i in range(n)]


class _FPDB:
    """Fake db supporting only the one suggestion-list select chain."""

    def __init__(self, rows):
        self.rows = rows
        self._lim = None

    def table(self, _):
        return self

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def is_(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        self._lim = n
        return self

    def execute(self):
        class _R:
            pass
        r = _R()
        r.data = self.rows[: (self._lim if self._lim is not None else len(self.rows))]
        return r


class TestFirstPassEnrich:
    def _run(self, db, limit, credits, allowed=True, enrich_ok=True):
        from app.services.vera_brain import _first_pass_enrich_pool
        ret = [{"sources_completed": ["website"]}] if enrich_ok else [{"error": "x"}]
        with patch("app.services.enricher.run_full_enrichment",
                   return_value=ret) as rfe, \
             patch("app.services.scorer.score_leads",
                   return_value={"total_scored": 1}) as sl, \
             patch("app.services.usage.check_enrichment_allowed",
                   return_value=(allowed, {"used": 0, "limit": 100})), \
             patch("app.services.usage.record_enrichment"), \
             patch("app.services.vera_brain.log_activity"):
            res = _first_pass_enrich_pool(db, "org", limit, credits)
        return res, rfe, sl

    def test_zero_credits_is_noop(self):
        res, rfe, sl = self._run(_FPDB(_sugg_rows(5)), 5, 0)
        assert res["attempted"] == 0 and res["reason"] == "no_capacity"
        rfe.assert_not_called()
        sl.assert_not_called()

    def test_capped_by_min_of_limit_and_credits(self):
        res, rfe, sl = self._run(_FPDB(_sugg_rows(10)), 5, 2)  # cap=2
        assert res == {"attempted": 2, "enriched": 2, "scored": 2}
        assert rfe.call_count == 2 and sl.call_count == 2

    def test_scout_limit_caps_to_five(self):
        res, rfe, sl = self._run(_FPDB(_sugg_rows(20)), 5, 100)  # cap=5
        assert res["enriched"] == 5 and rfe.call_count == 5

    def test_none_pending(self):
        res, rfe, sl = self._run(_FPDB([]), 5, 5)
        assert res["enriched"] == 0 and res["reason"] == "none_pending"
        rfe.assert_not_called()

    def test_enrich_failure_is_not_scored_but_continues(self):
        res, rfe, sl = self._run(_FPDB(_sugg_rows(3)), 5, 5, enrich_ok=False)
        assert res["enriched"] == 0 and res["scored"] == 0
        assert rfe.call_count == 3 and sl.call_count == 0  # tried all, none scored

    def test_credit_guard_stops_before_spending(self):
        res, rfe, sl = self._run(_FPDB(_sugg_rows(5)), 5, 5, allowed=False)
        assert res["enriched"] == 0
        rfe.assert_not_called()  # never spent a credit
