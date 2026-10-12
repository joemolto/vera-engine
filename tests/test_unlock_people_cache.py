"""Contact unlock: per-org state in apollo_people_cache, nothing in key_contacts.

apollo_people_cache (org_id, apollo_id, lead_id) is THE per-org unlock
record — find_people / search_people (chat + MCP) and the dashboard People
surfaces read it org-scoped. companies.key_contacts is GLOBAL (shared by
every org watching a domain) so it must never carry unlock state, revealed
identity, or emails: doing so leaked one org's paid reveals to every other
org (found 2026-07-09).
"""

from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks


def _make_db(company, cache_row=None, dedupe_leads=None):
    """Table-routed Supabase mock for the unlock endpoint's queries."""
    db = MagicMock()
    companies = MagicMock()
    companies.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = company

    watchlist = MagicMock()
    watchlist.select.return_value.eq.return_value.ilike.return_value.limit.return_value.execute.return_value.data = [
        {"id": "wl-1"}
    ]

    leads = MagicMock()
    # Email-dedupe lookup (select().eq().eq().limit())
    leads.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data = (
        dedupe_leads or []
    )
    leads.insert.return_value.execute.return_value.data = [{"id": "lead-new"}]

    cache = MagicMock()
    # Per-org already-unlocked lookup (select().eq().eq().maybe_single())
    cache.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = cache_row

    tables = {
        "companies": companies,
        "watchlist": watchlist,
        "leads": leads,
        "apollo_people_cache": cache,
    }
    db.table.side_effect = lambda name: tables.get(name, MagicMock())
    return db, cache, companies


def _call(db, name="Sam Example"):
    from app.routers.account_suggestions import UnlockContactRequest, unlock_contact

    ctx = MagicMock()
    req = UnlockContactRequest(org_id="org-alpha", name=name)
    with patch("app.routers.account_suggestions.get_supabase", return_value=db), \
            patch("app.services.plan_limits.enforce_unlock_cap"), \
            patch("app.routers.leads._apollo_people_match", return_value={
                "email": "sam@fabrikam.com",
                "first_name": "Sam",
                "last_name": "Example",
                "title": "Chief Data and Technology Officer",
                "linkedin_url": "https://linkedin.com/in/sam-example",
                "phone": "",
            }) as mock_match, \
            patch("app.services.email_finder.verify_email", return_value=None), \
            patch("app.services.email_finder.domain_has_mail_dns", return_value=True), \
            patch("app.services.credits.charge"), \
            patch("app.routers.account_suggestions._auto_promote_monitoring"):
        result = unlock_contact(
            "comp-1", "apollo-123", req, BackgroundTasks(), ctx=ctx,
        )
    return result, mock_match


def _company(key_contacts):
    return {
        "id": "comp-1",
        "domain": "fabrikam.com",
        "name": "Fabrikam",
        "key_contacts": key_contacts,
    }


class TestUnlockPerOrgState:
    def test_fresh_unlock_upserts_cache_row_with_lead_id(self):
        contact = {"apollo_id": "apollo-123", "name": "Sam E.", "title": "CDTO"}
        db, cache, companies = _make_db(_company([contact]))

        result, _ = _call(db)

        assert result["ok"] is True
        assert result["lead_id"] == "lead-new"
        cache.upsert.assert_called_once()
        row, kwargs = cache.upsert.call_args.args[0], cache.upsert.call_args.kwargs
        assert kwargs["on_conflict"] == "org_id,apollo_id"
        assert row["org_id"] == "org-alpha"
        assert row["apollo_id"] == "apollo-123"
        assert row["lead_id"] == "lead-new"
        assert row["person_data"]["email"] == "sam@fabrikam.com"

    def test_fresh_unlock_writes_nothing_to_key_contacts(self):
        # Contact already in the committee: the global row must stay
        # byte-identical — no unlocked/lead_id/email/revealed-name write.
        contact = {"apollo_id": "apollo-123", "name": "Sam E.", "title": "CDTO"}
        db, _cache, companies = _make_db(_company([contact]))

        _call(db)

        companies.update.assert_not_called()

    def test_live_search_contact_appended_without_unlock_state(self):
        db, _cache, companies = _make_db(_company([]))

        _call(db, name="Sam Example")

        companies.update.assert_called_once()
        appended = companies.update.call_args.args[0]["key_contacts"][-1]
        assert appended["apollo_id"] == "apollo-123"
        for leaked in ("email", "unlocked", "lead_id", "unlocked_at"):
            assert leaked not in appended

    def test_already_unlocked_by_this_org_short_circuits(self):
        contact = {"apollo_id": "apollo-123", "name": "Sam E."}
        db, cache, _ = _make_db(
            _company([contact]),
            cache_row={"lead_id": "lead-77", "person_data": {"email": "sam@fabrikam.com"}},
        )

        result, mock_match = _call(db)

        assert result["already_unlocked"] is True
        assert result["lead_id"] == "lead-77"
        mock_match.assert_not_called()  # no second Apollo spend
        cache.upsert.assert_not_called()

    def test_other_orgs_key_contacts_marker_is_ignored(self):
        # The global entry carries ANOTHER org's unlock marker. This org has
        # no cache row, so it must get its own fresh reveal + lead — never
        # the other org's lead_id or a free copy of their paid data.
        contact = {
            "apollo_id": "apollo-123",
            "name": "Sam Example",
            "unlocked": True,
            "lead_id": "lead-other-org",
        }
        db, cache, _ = _make_db(_company([contact]), cache_row=None)

        result, mock_match = _call(db)

        assert result["already_unlocked"] is False
        assert result["lead_id"] == "lead-new"
        mock_match.assert_called_once()
        assert cache.upsert.call_args.args[0]["org_id"] == "org-alpha"

    def test_cache_failure_is_non_fatal(self):
        contact = {"apollo_id": "apollo-123", "name": "Sam E."}
        db, cache, _ = _make_db(_company([contact]))
        cache.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.side_effect = (
            RuntimeError("db down")
        )
        cache.upsert.return_value.execute.side_effect = RuntimeError("db down")

        result, _ = _call(db)

        assert result["ok"] is True
        assert result["lead_id"] == "lead-new"
