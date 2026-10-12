"""Apify + Apollo shared-cache behavior.

Pay-per-result Apify actors and Apollo org enrichment now read/write
enrichment_cache so one org's fetch serves everyone within the TTL.
Raw payloads are cached and re-normalised on read.
"""

from unittest.mock import MagicMock, patch

from app.config import settings


class TestApifyPersonScrapeCache:
    """Person profile/posts/activity scrapes reuse the shared cache."""

    def test_activity_cache_hit_skips_actor(self):
        from app.services.linkedin_person import scrape_person_activity

        with patch.object(settings, "APIFY_API_KEY", "test-key"), \
                patch("app.services.linkedin_person.get_cached",
                      return_value={"items": []}) as mock_get, \
                patch("app.services.linkedin_person.httpx.Client") as mock_client:
            result = scrape_person_activity(
                "https://www.linkedin.com/in/joeblogs/", db=MagicMock(),
            )

        assert result == []
        mock_client.assert_not_called()
        assert mock_get.call_args.args[2] == "linkedin_person_activity"

    def test_activity_fetch_writes_raw_items(self):
        from app.services.linkedin_person import scrape_person_activity

        raw = [{"action": "commented", "commentText": "great post"}]
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = raw
        client = MagicMock()
        client.__enter__.return_value.post.return_value = resp

        with patch.object(settings, "APIFY_API_KEY", "test-key"), \
                patch("app.services.linkedin_person.get_cached", return_value=None), \
                patch("app.services.linkedin_person.httpx.Client", return_value=client), \
                patch("app.services.linkedin_person.set_cache") as mock_set:
            scrape_person_activity(
                "https://www.linkedin.com/in/joeblogs/", db=MagicMock(),
            )

        mock_set.assert_called_once()
        assert mock_set.call_args.args[2] == "linkedin_person_activity"
        assert mock_set.call_args.args[4] == {"items": raw}

    def test_posts_cache_hit_skips_actor(self):
        from app.services.linkedin_person import scrape_person_posts

        with patch.object(settings, "APIFY_API_KEY", "test-key"), \
                patch("app.services.linkedin_person.get_cached",
                      return_value={"items": []}), \
                patch("app.services.linkedin_person.httpx.Client") as mock_client:
            result = scrape_person_posts(
                "https://www.linkedin.com/in/joeblogs/", db=MagicMock(),
            )

        assert result == []
        mock_client.assert_not_called()

    def test_profile_cache_hit_normalises_cached_item(self):
        from app.services.linkedin_person import scrape_profile

        cached_item = {"headline": "CISO at Tesla", "positions": []}
        with patch.object(settings, "APIFY_API_KEY", "test-key"), \
                patch("app.services.linkedin_person.get_cached",
                      return_value={"item": cached_item}), \
                patch("app.services.linkedin_person.httpx.Client") as mock_client:
            result = scrape_profile(
                "https://www.linkedin.com/in/joeblogs/", db=MagicMock(),
            )

        assert result["current_title"] == "CISO at Tesla"
        mock_client.assert_not_called()

    def test_profile_failure_not_cached(self):
        from app.services.linkedin_person import scrape_profile

        client = MagicMock()
        client.__enter__.return_value.post.side_effect = RuntimeError("boom")

        with patch.object(settings, "APIFY_API_KEY", "test-key"), \
                patch("app.services.linkedin_person.get_cached", return_value=None), \
                patch("app.services.linkedin_person.httpx.Client", return_value=client), \
                patch("app.services.linkedin_person.set_cache") as mock_set:
            result = scrape_profile(
                "https://www.linkedin.com/in/joeblogs/", db=MagicMock(),
            )

        assert result is None
        mock_set.assert_not_called()

    def test_no_db_no_cache_calls(self):
        from app.services.linkedin_person import scrape_person_posts

        client = MagicMock()
        client.__enter__.return_value.post.side_effect = RuntimeError("boom")

        with patch.object(settings, "APIFY_API_KEY", "test-key"), \
                patch("app.services.linkedin_person.get_cached") as mock_get, \
                patch("app.services.linkedin_person.httpx.Client", return_value=client):
            scrape_person_posts("https://www.linkedin.com/in/joeblogs/")

        mock_get.assert_not_called()


class TestCompanyPostsCache:
    """scrape_linkedin_posts caches per URL + max_posts for 24h."""

    def test_cache_hit_skips_actor_and_keys_on_max(self):
        from app.services.apify_client import scrape_linkedin_posts

        with patch.object(settings, "APIFY_API_KEY", "test-key"), \
                patch("app.services.enrichers.cache.get_cached",
                      return_value={"items": []}) as mock_get, \
                patch("app.services.apify_client.httpx.Client") as mock_client:
            result = scrape_linkedin_posts(
                "https://www.linkedin.com/company/tesla/", max_posts=10,
                db=MagicMock(),
            )

        assert result == []
        mock_client.assert_not_called()
        assert mock_get.call_args.args[2] == "linkedin_posts"
        assert mock_get.call_args.args[3] == "max10"


class TestApolloOrgCache:
    """Apollo org enrichment reuses the shared 7-day cache."""

    def test_cache_hit_skips_apollo_call(self):
        from app.services.enrichers.tech_stack import _fetch_apollo_org_data

        cached = {"organization": {"technologies": ["Python"], "name": "Tesla"}}
        with patch("app.database.get_supabase", return_value=MagicMock()), \
                patch("app.services.enrichers.tech_stack.get_cached",
                      return_value=cached), \
                patch("app.services.enrichers.tech_stack.httpx.Client") as mock_client:
            technologies, _jobs, intel = _fetch_apollo_org_data("tesla.com")

        assert technologies == ["python"]
        assert intel["name"] == "Tesla"
        mock_client.assert_not_called()

    def test_cache_miss_fetches_and_writes(self):
        from app.services.enrichers.tech_stack import _fetch_apollo_org_data

        data = {"organization": {"technologies": [], "name": "Tesla"}}
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = data
        client = MagicMock()
        client.__enter__.return_value.post.return_value = resp

        with patch("app.database.get_supabase", return_value=MagicMock()), \
                patch("app.services.enrichers.tech_stack.get_cached",
                      return_value=None), \
                patch("app.services.enrichers.tech_stack.httpx.Client",
                      return_value=client), \
                patch("app.services.enrichers.tech_stack.set_cache") as mock_set:
            _fetch_apollo_org_data("tesla.com")

        mock_set.assert_called_once()
        assert mock_set.call_args.args[1] == "tesla.com"
        assert mock_set.call_args.args[2] == "apollo_org"
        assert mock_set.call_args.args[4] == data
