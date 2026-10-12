"""Re-enrichment of a skeletal lead must hydrate its contact fields.

The brain-side fix (commit 35a8700) stops new skeletal leads from
landing — but the skeletal rows already in the database still need
a repair path. Re-enrich is the natural one: every time a user clicks
"re-enrich", the pipeline tries to backfill missing last_name /
linkedin_url / email via Apollo people/match before running the rest
of the phases.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _skeletal_lead():
    return {
        "id": "lead-1",
        "org_id": "org-1",
        "first_name": "Stu",
        "last_name": "",
        "email": "",
        "linkedin_url": "",
        "title": "CISO",
        "company_name": "Contoso",
        "company_domain": "contoso.com",
        "enrichment_data": {},
        "enrichment_sources_completed": [],
    }


def _full_lead():
    return {
        **_skeletal_lead(),
        "last_name": "Smith",
        "email": "stu@contoso.com",
        "linkedin_url": "https://linkedin.com/in/stu-s",
    }


class TestSkeletalRepairOnReenrich:
    def test_skeletal_lead_calls_apollo_match_and_patches_db(self):
        """Re-enriching a skeletal lead hits Apollo people/match and writes the
        verified contact fields back to the leads row before any of the
        enrichment phases kick off."""
        from app.services import enricher

        db = MagicMock()
        # Make every downstream call into the enrichment phases a no-op
        # by short-circuiting at the first place that needs a real client.
        # We're only verifying the repair-on-read step; phase logic has its
        # own dedicated tests.
        lead = _skeletal_lead()

        with patch.object(
            enricher, "_build_client_context", return_value={}
        ), patch(
            "app.services.vera_brain._hydrate_from_apollo_match",
            return_value={
                **lead,
                "last_name": "Smith",
                "linkedin_url": "https://linkedin.com/in/stu-s",
                "email": "stu@contoso.com",
                "apollo_id": "apollo-uuid",
            },
        ) as mock_hydrate, patch.object(
            enricher, "enrich_website", return_value=None
        ), patch.object(
            enricher, "enrich_news", return_value=None
        ), patch.object(
            enricher, "enrich_jobs", return_value=None
        ), patch.object(
            enricher, "enrich_tech_stack", return_value=None
        ), patch.object(
            enricher, "find_email",
            return_value={"email": None, "source": None, "confidence": 0},
        ):
            try:
                enricher._enrich_single_lead(
                    db, "org-1", "lead-1", [],
                    "cyber_company_gtm",
                    lead=lead,
                    client_context={},
                )
            except Exception:
                # Phases beyond the repair step may raise on the mocked db;
                # we don't care for this test — we only assert the repair
                # ran first.
                pass

        # Apollo people/match was called once
        mock_hydrate.assert_called_once()
        # The DB row was patched with the verified contact fields
        update_calls = [
            c for c in db.table.return_value.update.call_args_list
            if c.args and isinstance(c.args[0], dict)
        ]
        patch_calls = [c.args[0] for c in update_calls]
        # At least one update carried the contact fields
        assert any(
            "last_name" in p and "email" in p and "linkedin_url" in p
            for p in patch_calls
        ), f"Expected an update with the hydrated fields, got: {patch_calls}"

    def test_full_lead_skips_hydration(self):
        """A lead that already has all three contact fields populated must NOT
        cost an Apollo people/match call on every re-enrichment."""
        from app.services import enricher

        db = MagicMock()
        lead = _full_lead()

        with patch.object(
            enricher, "_build_client_context", return_value={}
        ), patch(
            "app.services.vera_brain._hydrate_from_apollo_match",
        ) as mock_hydrate, patch.object(
            enricher, "enrich_website", return_value=None
        ), patch.object(
            enricher, "enrich_news", return_value=None
        ), patch.object(
            enricher, "enrich_jobs", return_value=None
        ), patch.object(
            enricher, "enrich_tech_stack", return_value=None
        ), patch.object(
            enricher, "find_email",
            return_value={"email": lead["email"], "source": "apollo", "confidence": 95},
        ):
            try:
                enricher._enrich_single_lead(
                    db, "org-1", "lead-1", [],
                    "cyber_company_gtm",
                    lead=lead,
                    client_context={},
                )
            except Exception:
                pass

        mock_hydrate.assert_not_called()

    def test_lead_without_first_name_or_domain_skips_hydration(self):
        """No identifiers = no point calling people/match."""
        from app.services import enricher

        db = MagicMock()
        lead = {
            **_skeletal_lead(),
            "first_name": "",
            "company_domain": "",
        }

        with patch.object(
            enricher, "_build_client_context", return_value={}
        ), patch(
            "app.services.vera_brain._hydrate_from_apollo_match",
        ) as mock_hydrate, patch.object(
            enricher, "enrich_website", return_value=None
        ), patch.object(
            enricher, "enrich_news", return_value=None
        ), patch.object(
            enricher, "enrich_jobs", return_value=None
        ), patch.object(
            enricher, "enrich_tech_stack", return_value=None
        ), patch.object(
            enricher, "find_email",
            return_value={"email": None, "source": None, "confidence": 0},
        ):
            try:
                enricher._enrich_single_lead(
                    db, "org-1", "lead-1", [],
                    "cyber_company_gtm",
                    lead=lead,
                    client_context={},
                )
            except Exception:
                pass

        mock_hydrate.assert_not_called()
