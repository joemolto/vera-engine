"""Regression tests for the Apollo brain-suggestion hydration waterfall.

Bug being pinned: ``search_apollo`` returned a text summary of people
to Claude, then Claude reconstructed structured dicts for
``store_suggestions``. Apollo's mixed_people/api_search redacts
verified emails and often returns sparse last_name / linkedin_url, and
the LLM round-trip lost the rest. Result: 42% of brain-sourced leads
landed with empty last_name + linkedin_url + email and scored cold
under "insufficient data quality".

The fix hydrates each row before insert through two layers:
  1. In-memory cache of the raw Apollo search response (free).
  2. Apollo people/match API (one credit per missing row).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.services import vera_brain
from app.services.vera_brain import (
    _apollo_unlock_by_id,
    _cache_apollo_people,
    _hydrate_from_apollo_match,
    _hydrate_lead_from_apollo_cache,
    _apollo_search_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts with an empty in-memory Apollo cache."""
    _apollo_search_cache.clear()
    yield
    _apollo_search_cache.clear()


class TestApolloCacheHydration:
    def test_cache_hit_fills_missing_fields(self):
        _cache_apollo_people("org-1", [
            {
                "first_name": "Alex",
                "last_name": "Example",
                "linkedin_url": "https://linkedin.com/in/alex-e",
                "email": "alex@northwind.co.uk",
                "organization": {
                    "name": "Northwind Software",
                    "primary_domain": "northwind.co.uk",
                },
            },
        ])
        skeletal = {
            "first_name": "Alex",
            "title": "CISO",
            "company_name": "Northwind Software",
            "company_domain": "northwind.co.uk",
        }
        out = _hydrate_lead_from_apollo_cache("org-1", skeletal)
        assert out["last_name"] == "Example"
        assert out["linkedin_url"] == "https://linkedin.com/in/alex-e"
        assert out["email"] == "alex@northwind.co.uk"
        # Original input is not mutated
        assert skeletal.get("last_name") is None

    def test_cache_miss_returns_input_unchanged(self):
        _cache_apollo_people("org-1", [
            {
                "first_name": "Someone Else",
                "last_name": "X",
                "organization": {"primary_domain": "other.com"},
            },
        ])
        skeletal = {
            "first_name": "Alex",
            "company_domain": "northwind.co.uk",
        }
        out = _hydrate_lead_from_apollo_cache("org-1", skeletal)
        assert out == skeletal  # nothing to merge

    def test_cache_match_by_company_name_when_domain_missing(self):
        _cache_apollo_people("org-1", [
            {
                "first_name": "Alex",
                "last_name": "B",
                "organization": {"name": "Northwind Software"},
            },
        ])
        out = _hydrate_lead_from_apollo_cache("org-1", {
            "first_name": "Alex",
            "company_name": "Northwind Software",
        })
        assert out["last_name"] == "B"

    def test_existing_field_not_overwritten(self):
        """If Claude already populated last_name, the cache must not stomp it."""
        _cache_apollo_people("org-1", [
            {
                "first_name": "Alex",
                "last_name": "WRONG",
                "organization": {"primary_domain": "northwind.co.uk"},
            },
        ])
        out = _hydrate_lead_from_apollo_cache("org-1", {
            "first_name": "Alex",
            "last_name": "Example",
            "company_domain": "northwind.co.uk",
        })
        assert out["last_name"] == "Example"  # Claude's value wins

    def test_expired_cache_is_not_used(self):
        _cache_apollo_people("org-1", [
            {
                "first_name": "Alex",
                "last_name": "B",
                "organization": {"primary_domain": "northwind.co.uk"},
            },
        ])
        # Force the cache row to look stale
        _apollo_search_cache["org-1"]["stored_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=3600)
        )
        out = _hydrate_lead_from_apollo_cache("org-1", {
            "first_name": "Alex",
            "company_domain": "northwind.co.uk",
        })
        assert "last_name" not in out  # cache expired, no merge


class TestApolloPeopleMatchFallback:
    def test_skips_if_all_three_already_present(self):
        """Idempotency: a fully-populated lead never costs an API call."""
        with patch.object(vera_brain, "_apollo_match_person") as mock_match:
            full = {
                "first_name": "A",
                "last_name": "B",
                "linkedin_url": "https://linkedin.com/in/a-b",
                "email": "a@b.com",
                "company_domain": "b.com",
            }
            out = _hydrate_from_apollo_match(full)
        mock_match.assert_not_called()
        assert out == full

    def test_calls_match_when_last_name_missing(self):
        with patch.object(vera_brain, "_apollo_match_person") as mock_match:
            mock_match.return_value = {
                "last_name": "Example",
                "linkedin_url": "https://linkedin.com/in/alex-e",
                "email": "alex@northwind.co.uk",
                "id": "apollo-person-uuid",
                "title": "Chief Information Security Officer",
            }
            out = _hydrate_from_apollo_match({
                "first_name": "Alex",
                "title": "",
                "company_domain": "northwind.co.uk",
            })
        mock_match.assert_called_once()
        assert out["last_name"] == "Example"
        assert out["linkedin_url"].endswith("alex-e")
        assert out["email"] == "alex@northwind.co.uk"
        assert out["apollo_id"] == "apollo-person-uuid"
        # Empty title gets backfilled
        assert out["title"].startswith("Chief")

    def test_apollo_failure_returns_input(self):
        """A people/match failure must not abort the store_suggestions loop."""
        with patch.object(vera_brain, "_apollo_match_person", return_value=None):
            skeletal = {"first_name": "Alex", "company_domain": "x.com"}
            out = _hydrate_from_apollo_match(skeletal)
        assert out == skeletal


class TestApolloMatchSearchStrategy:
    """The two-stage strategy: search-by-domain-and-title (returns full
    candidate records), then pick the one whose first_name matches.
    """

    def _mk_search_response(self, people):
        resp = MagicMock()
        resp.is_success = True
        resp.json.return_value = {"people": people}
        return resp

    def test_search_picks_first_name_match(self):
        from app.services.vera_brain import _apollo_match_person

        with patch("app.services.vera_brain.settings") as mock_settings, \
             patch("app.services.vera_brain.httpx.Client") as mock_client:
            mock_settings.APOLLO_API_KEY = "test-key"
            mock_client.return_value.__enter__.return_value.post.return_value = (
                self._mk_search_response([
                    {
                        "first_name": "Adam",
                        "last_name": "Smith",
                        "linkedin_url": "https://linkedin.com/in/adam-s",
                        "email": "adam@contoso.com",
                        "id": "wrong-person",
                        "title": "CTO",
                    },
                    {
                        "first_name": "Dan",
                        "last_name": "Jones",
                        "linkedin_url": "https://linkedin.com/in/dan-j",
                        "email": "dan@contoso.com",
                        "id": "right-person",
                        "title": "Chief Technology Officer",
                    },
                ])
            )
            out = _apollo_match_person(
                first_name="Dan",
                last_name="",
                company_domain="contoso.com",
                company_name="Contoso",
                title="Chief Technology Officer",
            )
        assert out is not None
        assert out["id"] == "right-person"
        assert out["last_name"] == "Jones"
        assert out["email"] == "dan@contoso.com"

    def test_search_with_title_and_single_candidate_accepts_it(self):
        """When the title + domain narrows the result to one candidate
        and no first_name matches exactly, accept that single candidate."""
        from app.services.vera_brain import _apollo_match_person

        with patch("app.services.vera_brain.settings") as mock_settings, \
             patch("app.services.vera_brain.httpx.Client") as mock_client:
            mock_settings.APOLLO_API_KEY = "test-key"
            mock_client.return_value.__enter__.return_value.post.return_value = (
                self._mk_search_response([
                    {
                        # Apollo may have "Daniel" while we have "Dan"
                        "first_name": "Daniel",
                        "last_name": "Jones",
                        "linkedin_url": "https://linkedin.com/in/dan-j",
                        "id": "only-candidate",
                        "title": "CTO",
                    },
                ])
            )
            out = _apollo_match_person(
                first_name="Dan",
                last_name="",
                company_domain="contoso.com",
                company_name="Contoso",
                title="Chief Technology Officer",
            )
        assert out is not None
        assert out["id"] == "only-candidate"

    def test_search_empty_falls_through_to_match(self):
        """Empty search results trigger people/match fallback ONLY if we
        have last_name. With no last_name, no fallback — return None."""
        from app.services.vera_brain import _apollo_match_person

        with patch("app.services.vera_brain.settings") as mock_settings, \
             patch("app.services.vera_brain.httpx.Client") as mock_client:
            mock_settings.APOLLO_API_KEY = "test-key"
            empty = self._mk_search_response([])
            mock_client.return_value.__enter__.return_value.post.return_value = empty
            out = _apollo_match_person(
                first_name="Dan",
                last_name="",            # no last name = no people/match fallback
                company_domain="contoso.com",
                company_name="Contoso",
                title="CTO",
            )
        assert out is None


class TestApolloUnlockByID:
    """The bulk_match unlock step that spends a credit and returns full
    contact data (last_name, email, linkedin_url) for a person whose
    apollo_id came from a fresh search."""

    def _mk_unlock_response(
        self, *, match=None, credits_consumed=1, ok=True,
    ):
        resp = MagicMock()
        resp.is_success = ok
        resp.status_code = 200 if ok else 500
        resp.json.return_value = {
            "matches": [match] if match else [None],
            "credits_consumed": credits_consumed,
        }
        resp.text = "ok"
        return resp

    def test_unlock_returns_full_person(self):
        with patch.object(vera_brain, "settings") as mock_settings, \
             patch.object(vera_brain.httpx, "Client") as mock_client:
            mock_settings.APOLLO_API_KEY = "test-key"
            mock_client.return_value.__enter__.return_value.post.return_value = (
                self._mk_unlock_response(match={
                    "first_name": "Dan",
                    "last_name": "Walker",
                    "email": "danw@contoso.com",
                    "email_status": "verified",
                    "linkedin_url": "http://www.linkedin.com/in/example-danw",
                    "id": "5f00000000000000000000a1",
                })
            )
            out = _apollo_unlock_by_id("5f00000000000000000000a1")
        assert out is not None
        assert out["last_name"] == "Walker"
        assert out["email"] == "danw@contoso.com"
        assert out["linkedin_url"].endswith("example-danw")

    def test_unlock_no_match_returns_none(self):
        """Stale apollo_id case — bulk_match returns matches=[None] and
        credits_consumed=0; our code must NOT confuse the None-match with
        a successful return."""
        with patch.object(vera_brain, "settings") as mock_settings, \
             patch.object(vera_brain.httpx, "Client") as mock_client:
            mock_settings.APOLLO_API_KEY = "test-key"
            mock_client.return_value.__enter__.return_value.post.return_value = (
                self._mk_unlock_response(match=None, credits_consumed=0)
            )
            out = _apollo_unlock_by_id("stale-id-6a0dae39")
        assert out is None

    def test_missing_id_or_key_returns_none(self):
        assert _apollo_unlock_by_id("") is None

    def test_apollo_http_error_returns_none(self):
        with patch.object(vera_brain, "settings") as mock_settings, \
             patch.object(vera_brain.httpx, "Client") as mock_client:
            mock_settings.APOLLO_API_KEY = "test-key"
            mock_client.return_value.__enter__.return_value.post.return_value = (
                self._mk_unlock_response(ok=False)
            )
            out = _apollo_unlock_by_id("5f00000000000000000000a1")
        assert out is None


class TestApolloMatchUnlocksAtStage2:
    """End-to-end: search returns obfuscated candidate, _apollo_match_person
    then unlocks via bulk_match and returns the full record."""

    def _mk_search(self, people):
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 200
        resp.json.return_value = {"people": people}
        resp.text = "ok"
        return resp

    def _mk_unlock(self, person):
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 200
        resp.json.return_value = {
            "matches": [person] if person else [None],
            "credits_consumed": 1 if person else 0,
        }
        resp.text = "ok"
        return resp

    def test_search_then_unlock_returns_full_data(self):
        from app.services.vera_brain import _apollo_match_person

        # Search returns the obfuscated candidate; bulk_match returns full.
        obfuscated = {
            "id": "5f00000000000000000000a1",
            "first_name": "Dan",
            "last_name_obfuscated": "Wa***r",
            "title": "Chief Technology Officer (CTO)",
            "organization": {"name": "Contoso", "primary_domain": "contoso.com"},
        }
        unlocked = {
            **obfuscated,
            "last_name": "Walker",
            "email": "danw@contoso.com",
            "email_status": "verified",
            "linkedin_url": "http://www.linkedin.com/in/example-danw",
        }
        with patch.object(vera_brain, "settings") as mock_settings, \
             patch.object(vera_brain.httpx, "Client") as mock_client:
            mock_settings.APOLLO_API_KEY = "test-key"
            post = mock_client.return_value.__enter__.return_value.post
            # First call → search (obfuscated), second call → bulk_match (full)
            post.side_effect = [
                self._mk_search([obfuscated]),
                self._mk_unlock(unlocked),
            ]
            out = _apollo_match_person(
                first_name="Dan",
                last_name="",
                company_domain="contoso.com",
                company_name="Contoso",
                title="CTO",
            )
        assert out is not None
        assert out["last_name"] == "Walker"
        assert out["email"] == "danw@contoso.com"
        assert out["linkedin_url"].endswith("example-danw")

    def test_unlock_failure_falls_back_to_obfuscated(self):
        """If credits are exhausted or bulk_match fails, we still return the
        obfuscated search hit — last_name_obfuscated + apollo_id are
        better than nothing for the dashboard."""
        from app.services.vera_brain import _apollo_match_person

        obfuscated = {
            "id": "5f00000000000000000000a1",
            "first_name": "Dan",
            "last_name_obfuscated": "Wa***r",
            "organization": {"name": "Contoso", "primary_domain": "contoso.com"},
        }
        with patch.object(vera_brain, "settings") as mock_settings, \
             patch.object(vera_brain.httpx, "Client") as mock_client:
            mock_settings.APOLLO_API_KEY = "test-key"
            post = mock_client.return_value.__enter__.return_value.post
            post.side_effect = [
                self._mk_search([obfuscated]),
                self._mk_unlock(None),  # unlock failed
            ]
            out = _apollo_match_person(
                first_name="Dan",
                last_name="",
                company_domain="contoso.com",
                company_name="Contoso",
                title="CTO",
            )
        # We got the obfuscated candidate back as a fallback
        assert out is not None
        assert out["id"] == "5f00000000000000000000a1"
        assert out.get("last_name_obfuscated") == "Wa***r"
