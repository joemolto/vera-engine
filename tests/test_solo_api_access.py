"""Solo developer access (2026-07-09).

Every Solo org gets API/webhooks/MCP; the plan caps are the limit.
These tests pin the contract: the public API is not a side door around
the caps, and the caps it enforces are the same pack-aware functions the
dashboard calls.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from tests.test_sim_findings_fixes import RecordingSupabase

ORG = "org-solo"


class InsertReturningDb(RecordingSupabase):
    """Selects return the configured table data; inserts echo the row back
    (the shared recording mock returns the same data for every query, which
    can't distinguish the dedupe SELECT from the INSERT on one table)."""

    def table(self, name):
        q = super().table(name)
        original = q.execute

        def execute():
            insert_calls = [c for c in q._calls if c[0] == "insert"]
            res = original()
            if insert_calls:
                res.data = [dict(insert_calls[0][1][0], id="new-row")]
            return res

        q.__dict__["execute"] = execute
        return q


def make_ctx():
    from app.services.api_auth import ApiContext

    return ApiContext(api_key_id="key-1", org_id=ORG, key_name="test")


def make_request():
    req = MagicMock()
    req.method = "POST"
    req.url.path = "/api/v1/public/watchlist"
    return req


def watchlist_payload(**overrides):
    from app.routers.public_api import WatchlistAddRequest

    base = {"company_name": "Acme", "company_domain": "acme.com"}
    base.update(overrides)
    return WatchlistAddRequest(**base)


def _identity_canon(domain, company_name=""):
    """Network-free canonicalise_domain stub — these tests pin the cap
    contract, not domain resolution."""
    return {
        "domain": (domain or "").strip().lower(),
        "canonicalised_from": None,
        "suggestion": None,
    }


# ---------------------------------------------------------------------------
# Tier gate + rate limit
# ---------------------------------------------------------------------------

def test_solo_tier_has_api_access_and_rate_limit():
    from app.services.api_keys import RATE_LIMITS_PER_MINUTE, TIERS_WITH_API_ACCESS

    assert "solo" in TIERS_WITH_API_ACCESS
    assert RATE_LIMITS_PER_MINUTE["solo"] == 30


# ---------------------------------------------------------------------------
# POST /public/watchlist — the cap gate
# ---------------------------------------------------------------------------

def test_watchlist_add_checks_both_caps_and_inserts_active():
    from app.routers import public_api

    db = InsertReturningDb()
    db.set_table_data("watchlist", [])  # no existing row → new account path

    with patch.object(public_api, "get_supabase", return_value=db), \
         patch.object(public_api, "canonicalise_domain", side_effect=_identity_canon), \
         patch.object(public_api, "enforce_account_cap") as acct_cap, \
         patch.object(public_api, "enforce_watch_cap") as watch_cap:
        result = public_api.add_to_watchlist(
            watchlist_payload(), make_request(), make_ctx()
        )

    acct_cap.assert_called_once_with(ORG, db)
    watch_cap.assert_called_once_with(ORG, db)

    inserts = [
        c for q in db.executed if q["table"] == "watchlist"
        for c in q["calls"] if c[0] == "insert"
    ]
    assert len(inserts) == 1
    row = inserts[0][1][0]
    assert row["status"] == "active"
    assert row["monitoring_active"] is True
    assert row["org_id"] == ORG
    assert result is not None


def test_watchlist_add_429_blocks_insert_when_cap_hit():
    from app.routers import public_api
    from app.services.plan_limits import _raise_cap

    db = RecordingSupabase()
    db.set_table_data("watchlist", [])

    def hit_cap(org_id, _db):
        _raise_cap("watched", 25)

    with patch.object(public_api, "get_supabase", return_value=db), \
         patch.object(public_api, "canonicalise_domain", side_effect=_identity_canon), \
         patch.object(public_api, "enforce_account_cap"), \
         patch.object(public_api, "enforce_watch_cap", side_effect=hit_cap):
        with pytest.raises(HTTPException) as exc:
            public_api.add_to_watchlist(
                watchlist_payload(), make_request(), make_ctx()
            )

    assert exc.value.status_code == 429
    inserts = [
        c for q in db.executed if q["table"] == "watchlist"
        for c in q["calls"] if c[0] == "insert"
    ]
    assert not inserts, "cap hit must prevent the watchlist insert"


def test_watchlist_add_idempotent_by_domain_skips_caps():
    from app.routers import public_api

    existing_row = {"id": "w-1", "org_id": ORG, "company_domain": "acme.com", "status": "active"}
    db = RecordingSupabase()
    db.set_table_data("watchlist", [existing_row])

    with patch.object(public_api, "get_supabase", return_value=db), \
         patch.object(public_api, "canonicalise_domain", side_effect=_identity_canon), \
         patch.object(public_api, "enforce_account_cap") as acct_cap, \
         patch.object(public_api, "enforce_watch_cap") as watch_cap:
        result = public_api.add_to_watchlist(
            watchlist_payload(), make_request(), make_ctx()
        )

    assert result["already_active"] is True
    assert result["id"] == "w-1"
    acct_cap.assert_not_called()
    watch_cap.assert_not_called()


# ---------------------------------------------------------------------------
# Lead enrichment — credit model is not available on the caps-based plan
# ---------------------------------------------------------------------------

def test_enrich_leads_blocked_for_solo():
    from app.routers import public_api
    from app.routers.public_api import EnrichRequest

    db = RecordingSupabase()
    with patch.object(public_api, "get_supabase", return_value=db), \
         patch.object(public_api, "_org_is_solo", return_value=True):
        with pytest.raises(HTTPException) as exc:
            public_api.enrich_leads(
                EnrichRequest(lead_ids=["00000000-0000-0000-0000-000000000001"]),
                make_request(),
                make_ctx(),
            )
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "not_available_on_solo"


def test_create_lead_with_enrich_blocked_for_solo_but_plain_create_ok():
    from app.routers import public_api
    from app.routers.public_api import CreateLeadRequest

    db = RecordingSupabase()
    db.set_table_data("leads", [{"id": "lead-1", "email": "a@b.com"}])

    with patch.object(public_api, "get_supabase", return_value=db), \
         patch.object(public_api, "_org_is_solo", return_value=True):
        with pytest.raises(HTTPException) as exc:
            public_api.create_lead(
                CreateLeadRequest(email="a@b.com", enrich=True),
                make_request(),
                make_ctx(),
            )
        assert exc.value.status_code == 403

        lead = public_api.create_lead(
            CreateLeadRequest(email="a@b.com", enrich=False),
            make_request(),
            make_ctx(),
        )
        assert lead["id"] == "lead-1"


# ---------------------------------------------------------------------------
# GET /public/usage — Solo gets caps, not credits
# ---------------------------------------------------------------------------

def test_usage_returns_caps_shape_for_solo():
    from app.routers import public_api

    db = RecordingSupabase()
    usage = {
        "accounts": 10,
        "watched": 5,
        "person_intel_accounts": 1,
        "monitored_people": 2,
        "unlocks_this_month": 3,
    }
    limits = {
        "accounts": 150,
        "watched": 25,
        "person_intel_accounts": 5,
        "monitored_people": 25,
        "unlocks_per_month": 50,
    }
    with patch.object(public_api, "get_supabase", return_value=db), \
         patch.object(public_api, "_org_is_solo", return_value=True), \
         patch.object(public_api, "current_usage", return_value=usage), \
         patch.object(public_api, "effective_limits", return_value=limits):
        payload = public_api.get_usage(make_request(), make_ctx())

    assert payload["plan"] == "solo"
    caps = payload["caps"]
    assert caps["accounts_researched"] == {"used": 10, "limit": 150, "period": "monthly"}
    assert caps["accounts_watched"] == {"used": 5, "limit": 25, "period": "standing"}
    assert caps["contact_unlocks"] == {"used": 3, "limit": 50, "period": "monthly"}
    assert "monthly_allowances_reset_on" in payload
