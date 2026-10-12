"""Monitoring → shared enrichment_cache write-back.

Watched accounts are fetched fresh on every monitor pass; these tests
pin that the fresh data is written back into enrichment_cache (so any
org's later enrichment reuses it) without changing monitor semantics.
"""

import json
from unittest.mock import MagicMock, patch


class TestPredictleadsBundleWriteback:
    """_fetch_predictleads_bundle writes successful results to the cache."""

    def _patch_endpoints(self, news=None, jobs=None, tech=None, financing=None):
        return (
            patch("app.services.watchlist.monitor._fetch_news_events", news or MagicMock(return_value={"events": ["a"]})),
            patch("app.services.watchlist.monitor._fetch_jobs", jobs or MagicMock(return_value={"jobs": ["b"]})),
            patch("app.services.watchlist.monitor._fetch_technology", tech or MagicMock(return_value={"tech": ["c"]})),
            patch("app.services.watchlist.monitor._fetch_financing", financing or MagicMock(return_value={"rounds": ["d"]})),
        )

    def test_successful_endpoints_are_cached_with_enricher_keys(self):
        from app.services.watchlist.monitor import _fetch_predictleads_bundle

        db = MagicMock()
        p_news, p_jobs, p_tech, p_fin = self._patch_endpoints()
        with p_news, p_jobs, p_tech, p_fin, \
                patch("app.services.watchlist.monitor.set_cache") as mock_set:
            results = _fetch_predictleads_bundle("tesla.com", db=db)

        assert results["news_events"] == {"events": ["a"]}
        cached_types = {call.args[2] for call in mock_set.call_args_list}
        assert cached_types == {
            "predictleads_news_events",
            "predictleads_jobs",
            "predictleads_technology",
            "predictleads_financing",
        }
        for call in mock_set.call_args_list:
            assert call.args[0] is db
            assert call.args[1] == "tesla.com"
            assert call.args[3] is None  # enrichment_mode, matches enricher

    def test_empty_response_cached_as_none_sentinel(self):
        from app.services.watchlist.monitor import _fetch_predictleads_bundle

        p_news, p_jobs, p_tech, p_fin = self._patch_endpoints(
            news=MagicMock(return_value=None),
        )
        with p_news, p_jobs, p_tech, p_fin, \
                patch("app.services.watchlist.monitor.set_cache") as mock_set:
            results = _fetch_predictleads_bundle("tesla.com", db=MagicMock())

        assert results["news_events"] is None
        news_call = next(
            c for c in mock_set.call_args_list
            if c.args[2] == "predictleads_news_events"
        )
        assert news_call.args[4] == {"_none": True}

    def test_failed_endpoint_not_cached(self):
        from app.services.watchlist.monitor import _fetch_predictleads_bundle

        p_news, p_jobs, p_tech, p_fin = self._patch_endpoints(
            news=MagicMock(side_effect=RuntimeError("boom")),
        )
        with p_news, p_jobs, p_tech, p_fin, \
                patch("app.services.watchlist.monitor.set_cache") as mock_set:
            results = _fetch_predictleads_bundle("tesla.com", db=MagicMock())

        assert results["news_events"] is None
        cached_types = {call.args[2] for call in mock_set.call_args_list}
        assert "predictleads_news_events" not in cached_types
        assert len(cached_types) == 3

    def test_no_db_means_no_cache_writes(self):
        from app.services.watchlist.monitor import _fetch_predictleads_bundle

        p_news, p_jobs, p_tech, p_fin = self._patch_endpoints()
        with p_news, p_jobs, p_tech, p_fin, \
                patch("app.services.watchlist.monitor.set_cache") as mock_set:
            results = _fetch_predictleads_bundle("tesla.com")

        assert results["jobs"] == {"jobs": ["b"]}
        mock_set.assert_not_called()


class TestGetCachedMaxAge:
    """max_age_seconds tightens the read beyond the row's TTL."""

    def _db_with_row(self, fetched_at):
        db = MagicMock()
        query = MagicMock()
        query.execute.return_value.data = [{
            "result_data": {"compliance": {"iso_27001": True}},
            "expires_at": "2099-01-01T00:00:00+00:00",
            "fetched_at": fetched_at,
        }]
        db.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value = query
        return db

    def test_rejects_row_older_than_max_age(self):
        from datetime import datetime, timedelta, timezone
        from app.services.enrichers.cache import get_cached

        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        result = get_cached(
            self._db_with_row(old), "tesla.com", "deep_research",
            max_age_seconds=7 * 86400,
        )
        assert result is None

    def test_accepts_row_within_max_age(self):
        from datetime import datetime, timedelta, timezone
        from app.services.enrichers.cache import get_cached

        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        result = get_cached(
            self._db_with_row(recent), "tesla.com", "deep_research",
            max_age_seconds=7 * 86400,
        )
        assert result == {"compliance": {"iso_27001": True}}

    def test_no_max_age_ignores_fetched_at(self):
        from datetime import datetime, timedelta, timezone
        from app.services.enrichers.cache import get_cached

        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        result = get_cached(self._db_with_row(old), "tesla.com", "deep_research")
        assert result == {"compliance": {"iso_27001": True}}


class TestResearchFreshnessWindow:
    """Monitor reuses research within the window, refetches beyond it."""

    def _mock_perplexity_response(self):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "choices": [
                {"message": {"content": json.dumps({"compliance": {"iso_27001": True}})}}
            ]
        }
        return resp

    def test_fresh_cache_reused_no_api_call(self):
        from app.config import settings
        from app.services.enrichers.deep_research import enrich_deep_research

        cached = {"compliance": {"soc_2": "Type II"}}
        with patch.object(settings, "OPENROUTER_API_KEY", "test-key"), \
                patch("app.services.enrichers.deep_research.httpx.post") as mock_post, \
                patch("app.services.enrichers.deep_research.get_cached",
                      return_value=cached) as mock_get:
            result = enrich_deep_research(
                "Tesla", "tesla.com", db=MagicMock(),
                max_age_seconds=7 * 86400,
            )

        assert result == cached
        mock_post.assert_not_called()
        assert mock_get.call_args.kwargs["max_age_seconds"] == 7 * 86400

    def test_stale_cache_refetches_and_writes_back(self):
        from app.config import settings
        from app.services.enrichers.deep_research import enrich_deep_research

        db = MagicMock()
        with patch.object(settings, "OPENROUTER_API_KEY", "test-key"), \
                patch("app.services.enrichers.deep_research.httpx.post",
                      return_value=self._mock_perplexity_response()), \
                patch("app.services.enrichers.deep_research.get_cached",
                      return_value=None), \
                patch("app.services.enrichers.deep_research.set_cache") as mock_set:
            result = enrich_deep_research(
                "Tesla", "tesla.com", db=db, max_age_seconds=7 * 86400,
            )

        assert result["compliance"] == {"iso_27001": True}
        mock_set.assert_called_once()
        args = mock_set.call_args.args
        assert args[1] == "tesla.com"
        assert args[2] == "deep_research"
        assert args[3] == "universal-v1"
