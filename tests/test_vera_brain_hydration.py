"""Tests for the Apollo people/match hydration fix.

The bug: vera_brain's search_apollo tool returns sparse human-readable
strings to Claude. Claude reconstructs structured rows for
store_suggestions but can only pass through what it saw — Apollo's
mixed_people/api_search redacts verified emails and often last_name +
linkedin_url, so leads landed skeletal and the data-quality pre-filter
scored them cold.

The fix is _hydrate_from_apollo_match: before insert, if any of
last_name / linkedin_url / email is blank, call Apollo's people/match
endpoint and merge in the verified fields.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.vera_brain import (
    _apollo_match_person,
    _hydrate_from_apollo_match,
)


class TestHydrateFromApolloMatch:
    def test_complete_lead_passes_through_untouched(self):
        lead = {
            "first_name": "Alex",
            "last_name": "Example",
            "email": "alex@northwind.co.uk",
            "linkedin_url": "https://linkedin.com/in/alex-e",
            "company_domain": "northwind.co.uk",
            "company_name": "Northwind Software",
            "title": "CISO",
        }
        with patch(
            "app.services.vera_brain._apollo_match_person"
        ) as mock_match:
            out = _hydrate_from_apollo_match(lead)
        # Skipped the Apollo call entirely
        mock_match.assert_not_called()
        assert out == lead

    def test_skeletal_lead_gets_hydrated(self):
        skeletal = {
            "first_name": "Alex",
            "last_name": "",
            "email": "",
            "linkedin_url": "",
            "company_domain": "northwind.co.uk",
            "company_name": "Northwind Software",
            "title": "CISO",
        }
        apollo_response = {
            "id": "apollo-123",
            "last_name": "Example",
            "linkedin_url": "https://linkedin.com/in/alex-example",
            "email": "a.example@northwind.co.uk",
            "title": "Chief Information Security Officer",
        }
        with patch(
            "app.services.vera_brain._apollo_match_person",
            return_value=apollo_response,
        ):
            out = _hydrate_from_apollo_match(skeletal)
        assert out["last_name"] == "Example"
        assert out["linkedin_url"] == "https://linkedin.com/in/alex-example"
        assert out["email"] == "a.example@northwind.co.uk"
        assert out["apollo_id"] == "apollo-123"
        # Existing fields are preserved
        assert out["first_name"] == "Alex"
        assert out["company_domain"] == "northwind.co.uk"

    def test_partial_lead_only_fills_blanks(self):
        """If the brain managed to capture last_name but not email or
        linkedin_url, hydration must NOT overwrite last_name with
        Apollo's version."""
        partial = {
            "first_name": "Stu",
            "last_name": "BrainGuessedName",
            "email": "",
            "linkedin_url": "",
            "company_domain": "contoso.com",
            "company_name": "Contoso",
        }
        apollo_response = {
            "id": "apollo-456",
            "last_name": "ApolloVerifiedName",  # different
            "linkedin_url": "https://linkedin.com/in/stu",
            "email": "stu@contoso.com",
        }
        with patch(
            "app.services.vera_brain._apollo_match_person",
            return_value=apollo_response,
        ):
            out = _hydrate_from_apollo_match(partial)
        # Brain's captured last_name is preserved (do-no-harm policy)
        assert out["last_name"] == "BrainGuessedName"
        # Blanks ARE filled
        assert out["email"] == "stu@contoso.com"
        assert out["linkedin_url"] == "https://linkedin.com/in/stu"

    def test_apollo_returns_nothing_lead_passes_through(self):
        skeletal = {
            "first_name": "Alex",
            "last_name": "",
            "email": "",
            "linkedin_url": "",
            "company_domain": "northwind.co.uk",
            "company_name": "Northwind Software",
        }
        with patch(
            "app.services.vera_brain._apollo_match_person",
            return_value=None,
        ):
            out = _hydrate_from_apollo_match(skeletal)
        # No crash — the lead just stays skeletal (caller still inserts
        # it; downstream enricher will mark data_status=no_linkedin)
        assert out["first_name"] == "Alex"
        assert out["last_name"] == ""

    def test_only_first_name_present_still_attempts(self):
        """The common case: brain has first_name + domain,
        nothing else. Should still attempt the match call."""
        lead = {
            "first_name": "Alex",
            "last_name": "",
            "email": "",
            "linkedin_url": "",
            "company_domain": "northwind.co.uk",
            "company_name": "Northwind Software",
        }
        with patch(
            "app.services.vera_brain._apollo_match_person"
        ) as mock_match:
            mock_match.return_value = None
            _hydrate_from_apollo_match(lead)
        assert mock_match.called
        kwargs = mock_match.call_args.kwargs
        assert kwargs["first_name"] == "Alex"
        assert kwargs["company_domain"] == "northwind.co.uk"


class TestApolloMatchPerson:
    def test_no_api_key_returns_none(self):
        with patch("app.services.vera_brain.settings") as mock_settings:
            mock_settings.APOLLO_API_KEY = ""
            out = _apollo_match_person(
                first_name="x", last_name="y",
                company_domain="z.com", company_name="Z",
            )
        assert out is None

    def test_no_identity_returns_none(self):
        with patch("app.services.vera_brain.settings") as mock_settings:
            mock_settings.APOLLO_API_KEY = "key"
            out = _apollo_match_person(
                first_name="", last_name="",
                company_domain="z.com", company_name="Z",
            )
        assert out is None

    def test_no_company_returns_none(self):
        with patch("app.services.vera_brain.settings") as mock_settings:
            mock_settings.APOLLO_API_KEY = "key"
            out = _apollo_match_person(
                first_name="x", last_name="y",
                company_domain="", company_name="",
            )
        assert out is None

    def test_http_failure_logs_and_returns_none(self):
        with patch("app.services.vera_brain.settings") as mock_settings, \
             patch("app.services.vera_brain.httpx.Client") as mock_client:
            mock_settings.APOLLO_API_KEY = "key"
            resp = MagicMock()
            resp.is_success = False
            resp.status_code = 429
            resp.text = "rate limited"
            mock_client.return_value.__enter__.return_value.post.return_value = resp
            out = _apollo_match_person(
                first_name="Alex", last_name="",
                company_domain="northwind.co.uk", company_name="Northwind",
            )
        assert out is None

    def test_success_returns_person(self):
        # Matches the staged flow: stage 1 search returns candidates
        # (obfuscated), stage 2 unlocks the picked candidate by id.
        with patch("app.services.vera_brain.settings") as mock_settings, \
             patch("app.services.vera_brain.httpx.Client") as mock_client, \
             patch("app.services.vera_brain._apollo_unlock_by_id") as mock_unlock:
            mock_settings.APOLLO_API_KEY = "key"
            resp = MagicMock()
            resp.is_success = True
            resp.json.return_value = {
                "people": [
                    {"id": "p1", "first_name": "Alex",
                     "last_name_obfuscated": "B."},
                ]
            }
            mock_client.return_value.__enter__.return_value.post.return_value = resp
            mock_unlock.return_value = {
                "id": "p1", "first_name": "Alex",
                "last_name": "Example",
                "email": "a@northwind.co.uk",
            }
            out = _apollo_match_person(
                first_name="Alex", last_name="",
                company_domain="northwind.co.uk", company_name="Northwind",
            )
        mock_unlock.assert_called_once_with("p1")
        assert out is not None
        assert out["last_name"] == "Example"
        assert out["email"] == "a@northwind.co.uk"
