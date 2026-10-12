"""Tests for lead deduplication logic."""

from unittest.mock import MagicMock

from app.models.schemas import LeadInput
from app.services.dedup import deduplicate_leads


class TestDeduplication:
    """Test deduplication logic."""

    def test_all_new_leads(self):
        """All leads are new when DB has no matches."""
        db = MagicMock()
        db.table.return_value.select.return_value.eq.return_value.in_.return_value.execute.return_value.data = []

        leads = [
            LeadInput(email="a@test.com"),
            LeadInput(email="b@test.com"),
        ]
        new, dupes = deduplicate_leads(db, "org-1", leads)
        assert len(new) == 2
        assert dupes == 0

    def test_all_duplicates(self):
        """All leads are duplicates when DB has all emails."""
        db = MagicMock()
        db.table.return_value.select.return_value.eq.return_value.in_.return_value.execute.return_value.data = [
            {"email": "a@test.com"},
            {"email": "b@test.com"},
        ]

        leads = [
            LeadInput(email="a@test.com"),
            LeadInput(email="b@test.com"),
        ]
        new, dupes = deduplicate_leads(db, "org-1", leads)
        assert len(new) == 0
        assert dupes == 2

    def test_mixed_new_and_duplicate(self):
        """Some leads are new, some are duplicates."""
        db = MagicMock()
        db.table.return_value.select.return_value.eq.return_value.in_.return_value.execute.return_value.data = [
            {"email": "a@test.com"},
        ]

        leads = [
            LeadInput(email="a@test.com"),
            LeadInput(email="b@test.com"),
        ]
        new, dupes = deduplicate_leads(db, "org-1", leads)
        assert len(new) == 1
        assert new[0].email == "b@test.com"
        assert dupes == 1

    def test_empty_leads_list(self):
        """Empty input returns empty output."""
        db = MagicMock()
        new, dupes = deduplicate_leads(db, "org-1", [])
        assert len(new) == 0
        assert dupes == 0

    def test_case_insensitive_dedup(self):
        """Dedup should be case-insensitive on email."""
        db = MagicMock()
        db.table.return_value.select.return_value.eq.return_value.in_.return_value.execute.return_value.data = [
            {"email": "A@Test.com"},
        ]

        leads = [LeadInput(email="a@test.com")]
        new, dupes = deduplicate_leads(db, "org-1", leads)
        assert len(new) == 0
        assert dupes == 1
