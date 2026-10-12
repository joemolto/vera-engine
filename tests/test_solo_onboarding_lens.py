"""Solo onboarding auto-ICP + lens — the self-serve wizard build.

Covers the pure mappers in onboarding_intel (expansion tables, personas,
signal priorities, client-context builder) and the /accounts/solo/onboarding
endpoint wiring (personas seeded, priorities merged, client_context column
written once). Vendor calls (Claude) are mocked throughout.
"""

from unittest.mock import MagicMock, patch

from app.services.onboarding_intel import (
    GEO_EXPANSIONS,
    INDUSTRY_EXPANSIONS,
    TITLE_EXPANSIONS,
    build_client_context,
    build_solo_seller_profile,
    build_wizard_personas,
    map_answers_to_icp,
    signal_priorities_from_answers,
)

WIZARD_ANSWERS = {
    "website": "https://acmesec.com",
    "sells_what": "We run a 24/7 SOC for mid-market financial firms.",
    "value_prop": "We give lean security teams enterprise-grade detection without the headcount.",
    "sub_sectors": ["MDR / SOC"],
    "ambulance": "defend",
    "titles": ["CISO", "Head of Security"],
    "industries": ["Financial services", "Legal"],
    "geos": ["UK", "Middle East"],
    "target_company_size": ["201-1000", "1001-5000"],
    "deal_band": "$10k–50k",
    "signals": ["recently_funded", "security_leadership_hire"],
    "competitors": "Arctic Wolf, eSentire",
}


# ---------------------------------------------------------------------------
# map_answers_to_icp — Apollo expansion
# ---------------------------------------------------------------------------

class TestMapAnswersToIcp:
    def test_titles_expand_to_apollo_variants(self):
        icp = map_answers_to_icp(WIZARD_ANSWERS)
        assert "Chief Information Security Officer" in icp["target_titles"]
        assert "Director of Information Security" in icp["target_titles"]

    def test_industries_expand_to_keyword_tags(self):
        icp = map_answers_to_icp(WIZARD_ANSWERS)
        assert "banking" in icp["target_industries"]
        assert "law practice" in icp["target_industries"]

    def test_middle_east_expands_to_countries(self):
        # Apollo has no "Middle East" region — the raw label silently
        # matches nothing, so it must become country names.
        icp = map_answers_to_icp(WIZARD_ANSWERS)
        assert "Middle East" not in icp["target_locations"]
        assert "United Arab Emirates" in icp["target_locations"]
        assert "United Kingdom" in icp["target_locations"]

    def test_global_means_no_location_filter(self):
        answers = {**WIZARD_ANSWERS, "geos": ["Global", "UK"]}
        icp = map_answers_to_icp(answers)
        assert "target_locations" not in icp

    def test_unknown_option_passes_through(self):
        answers = {**WIZARD_ANSWERS, "titles": ["Head of Widgets"]}
        icp = map_answers_to_icp(answers)
        assert icp["target_titles"] == ["Head of Widgets"]

    def test_no_duplicate_expansions(self):
        icp = map_answers_to_icp(WIZARD_ANSWERS)
        assert len(icp["target_titles"]) == len(set(icp["target_titles"]))

    def test_empty_answers_map_to_empty_icp(self):
        assert map_answers_to_icp({}) == {}

    def test_expansion_tables_cover_all_wizard_options(self):
        # Every chip the wizard offers must expand deliberately — an
        # uncurated option would pass through raw and silently no-op
        # against Apollo.
        wizard_titles = [
            "CISO", "Head of Security", "SOC Manager", "CIO", "CTO",
            "IT Director", "DPO / Data Protection Officer",
            "Head of Risk / Compliance", "VP Engineering", "Founder / CEO",
        ]
        wizard_industries = [
            "Financial services", "Healthcare", "Retail / e-commerce",
            "Energy / utilities", "Public sector / government",
            "Software / SaaS", "Manufacturing", "Insurance", "Legal", "Education",
        ]
        wizard_geos = ["UK", "United States", "Europe (EU)", "Canada", "APAC", "Middle East"]
        assert set(wizard_titles) <= set(TITLE_EXPANSIONS)
        assert set(wizard_industries) <= set(INDUSTRY_EXPANSIONS)
        assert set(wizard_geos) <= set(GEO_EXPANSIONS)


# ---------------------------------------------------------------------------
# build_wizard_personas
# ---------------------------------------------------------------------------

class TestBuildWizardPersonas:
    def test_persona_matches_founder_configured_shape(self):
        personas = build_wizard_personas(WIZARD_ANSWERS)
        assert len(personas) == 1
        p = personas[0]
        assert p["active"] is True
        assert p["name"] == "Default profile"
        assert p["id"]
        assert "CISO" in p["target_titles"]
        assert "financial services" in p["target_industries"]
        assert p["target_company_size"] == ["201-1000", "1001-5000"]
        # Fields the matching/persona code expects to exist
        for key in (
            "target_locations", "target_tech_uses", "target_seniorities",
            "target_revenue_range", "target_tech_excludes", "target_funding_stages",
        ):
            assert key in p

    def test_no_targeting_answers_means_no_persona(self):
        assert build_wizard_personas({}) == []


# ---------------------------------------------------------------------------
# signal_priorities_from_answers
# ---------------------------------------------------------------------------

class TestSignalPriorities:
    def test_defend_excludes_ambulance_signals(self):
        pri = signal_priorities_from_answers({"ambulance": "defend", "signals": []})
        assert "cve_in_stack" not in pri
        assert "compliance_gap" not in pri
        assert "credential_leak" not in pri
        assert pri["breach_proximity"] == 1.0

    def test_expose_leads_with_exposure(self):
        pri = signal_priorities_from_answers({"ambulance": "expose", "signals": []})
        assert pri["cve_in_stack"] == 1.0
        assert pri["compliance_gap"] == 0.9
        assert pri["recently_funded"] == 0.9

    def test_picked_signals_boost_to_max(self):
        pri = signal_priorities_from_answers(WIZARD_ANSWERS)
        # recently_funded is 0.5 on the defend base; the user picked it.
        assert pri["recently_funded"] == 1.0

    def test_missing_angle_defaults_to_defend(self):
        pri = signal_priorities_from_answers({})
        assert "cve_in_stack" not in pri

    def test_every_wizard_signal_option_is_in_both_bases(self):
        wizard_signals = [
            "breach_proximity", "social_breach_mention",
            "security_leadership_hire", "new_leadership",
            "recently_funded", "recent_acquisition",
            "security_team_expansion", "hiring_security",
            "person_engaged_with_competitor", "person_posted_relevant_topic",
        ]
        for angle in ("defend", "expose"):
            pri = signal_priorities_from_answers({"ambulance": angle})
            for sig in wizard_signals:
                assert sig in pri, f"{sig} missing from {angle} base"


# ---------------------------------------------------------------------------
# build_solo_seller_profile — the classifier lens string
# ---------------------------------------------------------------------------

class TestSellerProfile:
    def test_profile_carries_value_prop_and_competitors(self):
        profile = build_solo_seller_profile(WIZARD_ANSWERS, {})
        assert "enterprise-grade detection" in profile
        assert "Arctic Wolf" in profile

    def test_profile_without_lens_fields_still_works(self):
        answers = {k: v for k, v in WIZARD_ANSWERS.items() if k not in ("value_prop", "competitors")}
        profile = build_solo_seller_profile(answers, {})
        assert "24/7 SOC" in profile


# ---------------------------------------------------------------------------
# build_client_context — the org-level lens document
# ---------------------------------------------------------------------------

class TestBuildClientContext:
    AI_FIELDS = {
        "key_differentiators": ["24/7 human-led SOC"],
        "pain_points_they_solve": ["No in-house night coverage"],
        "icp_guidance": "Targets security leaders at regulated mid-market firms. Other cyber vendors are competitors, not prospects.",
        "product_names": [],
    }

    def test_deterministic_and_ai_fields_combine(self):
        with patch("app.services.onboarding_intel.call_claude", return_value=self.AI_FIELDS):
            ctx = build_client_context("Acme Security", WIZARD_ANSWERS, {})
        assert ctx["company_name"] == "Acme Security"
        assert ctx["value_proposition"].startswith("We give lean security teams")
        assert ctx["competitors"] == ["Arctic Wolf", "eSentire"]
        assert ctx["average_deal_size"] == 30000  # $10k–50k band
        assert "ambulance-chasing" in ctx["critical_rules"]
        assert ctx["key_differentiators"] == ["24/7 human-led SOC"]
        assert ctx["context_source"] == "wizard_auto"

    def test_expose_angle_flips_critical_rules(self):
        answers = {**WIZARD_ANSWERS, "ambulance": "expose"}
        with patch("app.services.onboarding_intel.call_claude", return_value=self.AI_FIELDS):
            ctx = build_client_context("Acme Security", answers, {})
        assert "fair openers" in ctx["critical_rules"]

    def test_ai_failure_keeps_deterministic_lens(self):
        with patch("app.services.onboarding_intel.call_claude", return_value={"error": "boom"}):
            ctx = build_client_context("Acme Security", WIZARD_ANSWERS, {})
        assert ctx["value_proposition"]
        assert ctx["competitors"] == ["Arctic Wolf", "eSentire"]
        assert "key_differentiators" not in ctx

    def test_competitors_accept_list_or_string(self):
        with patch("app.services.onboarding_intel.call_claude", return_value=self.AI_FIELDS):
            as_list = build_client_context("A", {**WIZARD_ANSWERS, "competitors": ["X", " Y "]}, {})
            as_string = build_client_context("A", {**WIZARD_ANSWERS, "competitors": "X, Y"}, {})
        assert as_list["competitors"] == ["X", "Y"]
        assert as_string["competitors"] == ["X", "Y"]


# ---------------------------------------------------------------------------
# /accounts/solo/onboarding — endpoint wiring
# ---------------------------------------------------------------------------

class TestSoloOnboardingEndpoint:
    def _make_db(self, *, icp_config=None, client_context=None):
        """MagicMock Supabase whose organisations select returns the given
        row and which records update payloads on db.updates."""
        db = MagicMock()
        db.updates = []

        row = {"icp_config": icp_config or {}, "name": "Acme Security", "client_context": client_context}
        select_chain = MagicMock()
        select_chain.eq.return_value.maybe_single.return_value.execute.return_value = MagicMock(data=row)

        update_chain = MagicMock()

        def _capture_update(payload):
            db.updates.append(payload)
            chain = MagicMock()
            chain.eq.return_value.execute.return_value = MagicMock(data=[{}])
            return chain

        table = MagicMock()
        table.select.return_value = select_chain
        table.update.side_effect = _capture_update
        db.table.return_value = table
        return db

    def _post(self, client, db):
        summary = {"company_summary": "Acme runs a SOC.", "target_business_types": ["UK FS firms"]}
        # api_auth holds its own settings reference (import-time bind), so
        # patch it there — same pattern as test_api_auth.
        with patch("app.services.api_auth.settings") as auth_settings, \
             patch("app.routers.account_suggestions.get_supabase", return_value=db), \
             patch("app.services.onboarding_intel.call_claude", return_value={}), \
             patch("app.services.onboarding_intel.generate_company_summary", return_value=summary):
            auth_settings.API_SECRET_KEY = "test-key"
            auth_settings.API_SECRET_KEY_OLD = None
            from tests.conftest import API_KEY_HEADER
            return client.post(
                "/api/v1/accounts/solo/onboarding",
                json={"org_id": "org-1", "answers": WIZARD_ANSWERS},
                headers=API_KEY_HEADER,
            )

    def test_onboarding_builds_full_lens(self, client):
        db = self._make_db()
        res = self._post(client, db)
        assert res.status_code == 200
        assert res.json()["ok"] is True

        assert len(db.updates) == 1
        payload = db.updates[0]
        icp = payload["icp_config"]

        # Auto-ICP: expanded targets + personas + priorities
        assert "Chief Information Security Officer" in icp["target_titles"]
        assert icp["personas"][0]["target_industries"][0] == "financial services"
        assert icp["signal_priorities"]["recently_funded"] == 1.0
        assert icp["icp_source"] == "wizard_auto"
        assert icp["onboarding_completed"] is True

        # The classifier pitch + the org-level lens column
        assert "Arctic Wolf" in icp["client_context"]["product_pitch"]
        assert payload["client_context"]["company_name"] == "Acme Security"
        assert payload["client_context"]["context_source"] == "wizard_auto"

    def test_existing_client_context_never_overwritten(self, client):
        db = self._make_db(client_context={"what_they_sell": "Hand-written by the operator"})
        res = self._post(client, db)
        assert res.status_code == 200
        assert "client_context" not in db.updates[0]

    def test_wizard_rebuilds_personas_but_keeps_tuned_priorities(self, client):
        icp = {
            "personas": [{"id": "stale", "target_titles": ["CISO"]}],
            "signal_priorities": {"recently_funded": 0.1},
        }
        db = self._make_db(icp_config=icp)
        res = self._post(client, db)
        assert res.status_code == 200
        out = db.updates[0]["icp_config"]
        # The wizard's answers are the ICP source of truth: personas rebuilt
        assert out["personas"][0]["id"] != "stale"
        assert out["personas"][0]["target_industries"][0] == "financial services"
        # Hand-tuned weight wins over the wizard preset
        assert out["signal_priorities"]["recently_funded"] == 0.1
        # But preset fills the gaps
        assert out["signal_priorities"]["breach_proximity"] == 1.0


class TestTopupStampedeGuard:
    """A burst of accepts (the wizard adds up to 10 back to back) fires
    concurrent background topups that each fill the pool independently —
    seen live 2026-07-08 (85 pending suggestions). One topup per org at a
    time, plus a cooldown."""

    def test_claim_then_running_then_cooldown(self):
        from app.routers import account_suggestions as mod
        org = "org-guard-1"
        mod._topup_running.discard(org)
        mod._topup_last_run.pop(org, None)
        assert mod._topup_permitted(org) is True
        assert mod._topup_permitted(org) is False  # already running
        mod._topup_release(org)
        assert mod._topup_permitted(org) is False  # cooling down

    def test_cooldown_expires(self):
        import time as _time
        from app.routers import account_suggestions as mod
        org = "org-guard-2"
        mod._topup_running.discard(org)
        mod._topup_last_run[org] = _time.monotonic() - mod._TOPUP_COOLDOWN_SECONDS - 1
        assert mod._topup_permitted(org) is True
        mod._topup_release(org)

    def test_topup_pool_debounces_second_call(self):
        from app.routers import account_suggestions as mod
        org = "org-guard-3"
        mod._topup_running.discard(org)
        mod._topup_last_run.pop(org, None)
        with patch.object(mod, "_suggestions_enabled", return_value=True), \
             patch.object(mod, "_topup_pool_inner", return_value={"added": 0}) as inner:
            first = mod._topup_pool(org)
            second = mod._topup_pool(org)
        assert first == {"added": 0}
        assert second == {"skipped": "debounced"}
        assert inner.call_count == 1


class TestAcceptSuggestionIdempotency:
    def test_accept_retry_after_proxy_502_is_idempotent(self, client):
        """The clara-api proxy can 502 after the engine completed the
        accept (seen live 2026-07-08: watchlist row went active, browser
        saw 502). A retry finds no status='pending' row to update — it
        must return ok, not 404, so the wizard doesn't report a phantom
        failure."""
        db = MagicMock()

        update_chain = MagicMock()
        update_chain.eq.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
        select_chain = MagicMock()
        select_chain.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value = MagicMock(
            data={"status": "active"}
        )
        table = MagicMock()
        table.update.return_value = update_chain
        table.select.return_value = select_chain
        db.table.return_value = table

        with patch("app.services.api_auth.settings") as auth_settings, \
             patch("app.routers.account_suggestions.get_supabase", return_value=db), \
             patch("app.services.plan_limits.get_supabase", return_value=db), \
             patch("app.services.org_features.get_supabase", return_value=db):
            auth_settings.API_SECRET_KEY = "test-key"
            auth_settings.API_SECRET_KEY_OLD = None
            from tests.conftest import API_KEY_HEADER
            res = client.post(
                "/api/v1/accounts/suggestions/sugg-1/accept",
                json={"org_id": "org-1"},
                headers=API_KEY_HEADER,
            )
        assert res.status_code == 200
        assert res.json() == {"ok": True, "id": "sugg-1", "already_active": True}

    def test_accept_of_unknown_suggestion_still_404s(self, client):
        db = MagicMock()
        update_chain = MagicMock()
        update_chain.eq.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
        select_chain = MagicMock()
        select_chain.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value = MagicMock(data=None)
        table = MagicMock()
        table.update.return_value = update_chain
        table.select.return_value = select_chain
        db.table.return_value = table

        with patch("app.services.api_auth.settings") as auth_settings, \
             patch("app.routers.account_suggestions.get_supabase", return_value=db), \
             patch("app.services.plan_limits.get_supabase", return_value=db), \
             patch("app.services.org_features.get_supabase", return_value=db):
            auth_settings.API_SECRET_KEY = "test-key"
            auth_settings.API_SECRET_KEY_OLD = None
            from tests.conftest import API_KEY_HEADER
            res = client.post(
                "/api/v1/accounts/suggestions/sugg-nope/accept",
                json={"org_id": "org-1"},
                headers=API_KEY_HEADER,
            )
        assert res.status_code == 404
