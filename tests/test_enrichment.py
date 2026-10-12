"""Tests for enrichment pipeline and caching."""

from unittest.mock import MagicMock, patch

from app.services.enrichers.tech_stack import analyse_tech_stack


class TestTechStackAnalysis:
    """Test GTM maturity analysis from tech stack."""

    def test_no_tools_detected(self):
        result = analyse_tech_stack([])
        assert result["gtm_maturity_from_stack"] == "none"
        assert not result["has_outbound_tools"]
        assert not result["has_crm"]

    def test_basic_maturity(self):
        """CRM only = basic."""
        result = analyse_tech_stack(["hubspot"])
        assert result["gtm_maturity_from_stack"] == "basic"
        assert result["has_crm"]
        assert not result["has_outbound_tools"]

    def test_intermediate_maturity(self):
        """CRM + outbound = intermediate."""
        result = analyse_tech_stack(["hubspot", "instantly"])
        assert result["gtm_maturity_from_stack"] == "intermediate"
        assert result["has_crm"]
        assert result["has_outbound_tools"]

    def test_advanced_maturity(self):
        """CRM + outbound + sales intel = advanced."""
        result = analyse_tech_stack(["salesforce", "outreach", "zoominfo"])
        assert result["gtm_maturity_from_stack"] == "advanced"

    def test_gap_identification(self):
        """No outbound tools = gap identified."""
        result = analyse_tech_stack(["hubspot"])
        gaps = result["gtm_gaps"]
        assert any("outbound" in g.lower() for g in gaps)

    def test_case_insensitive(self):
        """Tech names should be matched case-insensitively."""
        result = analyse_tech_stack(["HubSpot", "Instantly"])
        assert result["has_crm"]
        assert result["has_outbound_tools"]


class TestEnrichmentCache:
    """Test cache hit/miss logic."""

    def test_cache_returns_none_on_miss(self):
        """Cache miss returns None."""
        from app.services.enrichers.cache import get_cached

        db = MagicMock()
        db.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = []

        result = get_cached(db, "test.com", "website", "cyber_company_gtm")
        assert result is None

    def test_cache_returns_data_on_hit(self):
        """Cache hit returns stored data."""
        from app.services.enrichers.cache import get_cached

        db = MagicMock()
        query_mock = MagicMock()
        query_mock.execute.return_value.data = [{
            "result_data": {"company_type": "MSSP"},
            "expires_at": "2099-01-01T00:00:00Z",
        }]
        db.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value = query_mock

        result = get_cached(db, "test.com", "website", "cyber_company_gtm")
        assert result == {"company_type": "MSSP"}


class TestCyberSignals:
    """Phase A — cyber-specific signal extraction in gtm_briefing._extract_signals."""

    def _baseline_enrichment(self):
        return {
            "threat_alerts": [],
            "predictleads_jobs": {"jobs": []},
            "predictleads_news": {"events": []},
            "tech_stack": {"total_detected": 50},
            "linkedin_verification": {"primary_role": {"title": "Director of Engineering"}},
            "deep_research": {"compliance": {}},
            "news": {},
        }

    def test_cve_in_stack_fires_on_actively_exploited(self):
        from app.services.gtm_briefing import _extract_signals
        e = self._baseline_enrichment()
        e["threat_alerts"] = [
            {"title": "CVE-2024-21762", "threat_type": "vulnerability", "is_actively_exploited": True}
        ]
        signals = _extract_signals(e, {})
        assert signals["cve_in_stack"] is True
        assert signals["breach_proximity"] is False

    def test_cve_in_stack_does_not_fire_without_alerts(self):
        from app.services.gtm_briefing import _extract_signals
        signals = _extract_signals(self._baseline_enrichment(), {})
        assert signals["cve_in_stack"] is False

    def test_breach_proximity_fires(self):
        from app.services.gtm_briefing import _extract_signals
        e = self._baseline_enrichment()
        e["threat_alerts"] = [
            {"title": "Acme breach exposes 2M records", "threat_type": "breach"}
        ]
        signals = _extract_signals(e, {})
        assert signals["breach_proximity"] is True

    def test_compliance_gap_fires_for_regulated_industry_no_certs(self):
        from app.services.gtm_briefing import _extract_signals
        e = self._baseline_enrichment()
        e["deep_research"]["compliance"] = {"iso_27001": "Not found", "soc_2": "Not found"}
        signals = _extract_signals(e, {"company_industry": "Banking"})
        assert signals["regulated_industry"] is True
        assert signals["compliance_gap"] is True

    def test_compliance_gap_does_not_fire_when_iso_present(self):
        from app.services.gtm_briefing import _extract_signals
        e = self._baseline_enrichment()
        e["deep_research"]["compliance"] = {"iso_27001": "Type II", "soc_2": "Not found"}
        signals = _extract_signals(e, {"company_industry": "Banking"})
        assert signals["compliance_gap"] is False

    def test_compliance_gap_does_not_fire_for_non_regulated(self):
        from app.services.gtm_briefing import _extract_signals
        signals = _extract_signals(self._baseline_enrichment(), {"company_industry": "marketing"})
        assert signals["compliance_gap"] is False

    def test_security_leadership_fires_on_ciso_title(self):
        from app.services.gtm_briefing import _extract_signals
        e = self._baseline_enrichment()
        e["linkedin_verification"]["primary_role"]["title"] = "CISO"
        signals = _extract_signals(e, {"title": "CISO"})
        assert signals["security_leadership_hire"] is True

    def test_security_leadership_fires_on_head_of_security(self):
        from app.services.gtm_briefing import _extract_signals
        e = self._baseline_enrichment()
        e["linkedin_verification"]["primary_role"]["title"] = "Head of Security"
        signals = _extract_signals(e, {})
        assert signals["security_leadership_hire"] is True

    def test_security_team_expansion_fires_on_three_security_roles(self):
        from app.services.gtm_briefing import _extract_signals
        e = self._baseline_enrichment()
        e["predictleads_jobs"]["jobs"] = [
            {"title": "Senior Security Engineer"},
            {"title": "SOC Analyst"},
            {"title": "Security Architect"},
        ]
        signals = _extract_signals(e, {})
        assert signals["security_team_expansion"] is True

    def test_security_team_expansion_does_not_fire_below_threshold(self):
        from app.services.gtm_briefing import _extract_signals
        e = self._baseline_enrichment()
        e["predictleads_jobs"]["jobs"] = [
            {"title": "Security Engineer"},
            {"title": "DevOps Engineer"},  # not a security role
        ]
        signals = _extract_signals(e, {})
        assert signals["security_team_expansion"] is False


class TestCyberPlayMatching:
    """Phase A — _match_plays should pick cyber-specific plays over generic ones."""

    def _signals_with(self, **overrides):
        base = {
            "recent_funding": False, "recent_acquisition": False,
            "product_launch_or_partnership": False, "new_leadership": False,
            "hiring": False, "hiring_sales_bd": False, "hiring_security": False,
            "no_security_tooling": False, "growing_tech_stack": False,
            "competitor_tool_detected": False, "regulated_industry": False,
            "missing_compliance_tools": False,
            "cve_in_stack": False, "breach_proximity": False,
            "security_leadership_hire": False, "security_team_expansion": False,
            "compliance_gap": False,
        }
        base.update(overrides)
        return base

    def test_cve_urgency_wins_over_funding_surge(self):
        """Both CVE in stack AND recent funding — CVE urgency must win."""
        from app.services.gtm_briefing import _match_plays
        signals = self._signals_with(cve_in_stack=True, recent_funding=True, hiring=True)
        best, _ = _match_plays(signals)
        assert best is not None
        assert best["name"] == "CVE Urgency"

    def test_breach_adjacent_picked_when_only_breach_signal(self):
        from app.services.gtm_briefing import _match_plays
        signals = self._signals_with(breach_proximity=True, regulated_industry=True)
        best, _ = _match_plays(signals)
        assert best is not None
        assert best["name"] == "Breach Adjacent"

    def test_compliance_window_picked(self):
        from app.services.gtm_briefing import _match_plays
        signals = self._signals_with(compliance_gap=True, regulated_industry=True)
        best, _ = _match_plays(signals)
        assert best is not None
        assert best["name"] == "Compliance Window"

    def test_security_leadership_picked(self):
        from app.services.gtm_briefing import _match_plays
        signals = self._signals_with(security_leadership_hire=True)
        best, _ = _match_plays(signals)
        assert best is not None
        assert best["name"] == "Security Leadership"

    def test_falls_back_to_generic_when_no_cyber_signals(self):
        """When only generic B2B signals fire, the existing plays still match."""
        from app.services.gtm_briefing import _match_plays
        signals = self._signals_with(recent_acquisition=True, new_leadership=True)
        best, _ = _match_plays(signals)
        assert best is not None
        assert best["name"] == "Recent Acquisition"


class TestThreatCorrelation:
    """Phase A — correlate_threats_for_lead pulls threat_intel matches for a lead."""

    def _mock_db_with_threats(self, rows):
        db = MagicMock()
        db.table.return_value.select.return_value.gte.return_value.order.return_value.limit.return_value.execute.return_value.data = rows
        return db

    def test_no_match_when_tech_and_industry_dont_overlap(self):
        from app.services.threat_intel.analyser import correlate_threats_for_lead
        db = self._mock_db_with_threats([
            {
                "id": "t1", "source": "cisa_kev", "title": "CVE in Apache",
                "severity": "critical",
                "affected_technologies": ["apache", "tomcat"],
                "affected_industries": ["financial services"],
                "affected_vendors": ["apache"],
                "published_at": "2026-04-01T00:00:00Z",
                "ai_summary": "...", "sales_relevance": {},
            }
        ])
        result = correlate_threats_for_lead(
            db,
            tech_stack=[{"name": "Stripe"}, {"name": "HubSpot"}],
            industry="marketing",
        )
        assert result["threat_alerts"] == []
        assert result["cve_in_stack_matches"] == []

    def test_kev_match_on_tech_overlap(self):
        from app.services.threat_intel.analyser import correlate_threats_for_lead
        db = self._mock_db_with_threats([
            {
                "id": "t1", "source": "cisa_kev", "title": "Critical Apache CVE",
                "severity": "critical",
                "affected_technologies": ["apache"],
                "affected_industries": [],
                "affected_vendors": [],
                "published_at": "2026-04-01T00:00:00Z",
                "ai_summary": "Apache RCE", "sales_relevance": {},
            }
        ])
        result = correlate_threats_for_lead(
            db,
            tech_stack=[{"name": "Apache"}, {"name": "Stripe"}],
            industry="marketing",
        )
        assert len(result["threat_alerts"]) == 1
        alert = result["threat_alerts"][0]
        assert alert["is_actively_exploited"] is True
        assert "apache" in alert["matched_technologies"]
        assert any(m["tech"] == "apache" for m in result["cve_in_stack_matches"])

    def test_breach_classification_populates_breach_matches(self):
        from app.services.threat_intel.analyser import correlate_threats_for_lead
        db = self._mock_db_with_threats([
            {
                "id": "t1", "source": "tavily_news",
                "title": "Acme suffers ransomware attack",
                "severity": "high",
                "affected_technologies": ["acme-saas"],
                "affected_industries": [],
                "affected_vendors": [],
                "published_at": "2026-04-15T00:00:00Z",
                "ai_summary": "...", "sales_relevance": {"threat_type": "ransomware"},
            }
        ])
        result = correlate_threats_for_lead(
            db,
            tech_stack=[{"name": "Acme-SaaS"}],
            industry="",
        )
        assert len(result["breach_proximity_matches"]) == 1


class TestIcpConfigDrivenSignals:
    """Per-client signal gating: icp_config.signals_enabled controls which
    signals can drive plays, regardless of what's detectable in enrichment_data.
    """

    def _baseline_enrichment(self):
        return {
            "threat_alerts": [{"title": "CVE-X", "threat_type": "vulnerability", "is_actively_exploited": True}],
            "predictleads_jobs": {"jobs": []},
            "predictleads_news": {"events": []},
            "tech_stack": {"total_detected": 50},
            "linkedin_verification": {"primary_role": {"title": "CISO"}},
            "deep_research": {"compliance": {"iso_27001": "Not found", "soc_2": "Not found"}},
            "news": {},
        }

    def test_no_icp_config_returns_all_signals(self):
        from app.services.gtm_briefing import _extract_signals
        signals = _extract_signals(self._baseline_enrichment(), {"company_industry": "Banking"})
        assert signals["cve_in_stack"] is True
        assert signals["security_leadership_hire"] is True
        assert signals["compliance_gap"] is True

    def test_signals_enabled_filters_to_listed_only(self):
        from app.services.gtm_briefing import _extract_signals
        signals = _extract_signals(
            self._baseline_enrichment(),
            {"company_industry": "Banking"},
            icp_config={"signals_enabled": ["compliance_gap"]},
        )
        assert signals["compliance_gap"] is True
        assert signals["cve_in_stack"] is False
        assert signals["security_leadership_hire"] is False

    def test_empty_signals_enabled_falls_back_to_all(self):
        """An empty list (vs missing key) should NOT silence everything —
        treat empty as 'not configured' and pass everything through."""
        from app.services.gtm_briefing import _extract_signals
        signals = _extract_signals(
            self._baseline_enrichment(),
            {"company_industry": "Banking"},
            icp_config={"signals_enabled": []},
        )
        assert signals["cve_in_stack"] is True


class TestIcpConfigDrivenPlays:
    """Per-client play library: icp_config.plays replaces the global _PLAYS list."""

    def test_custom_play_matches_when_signals_fire(self):
        from app.services.gtm_briefing import _match_plays
        custom = [{
            "name": "Example Hiring Alternative", "tier": "cyber",
            "description": "Saas hiring security",
            "required_signals": ["hiring_security"],
            "boost_signals": ["recent_funding"],
            "min_score": 1,
            "framing": "Embedded team is half the cost of one hire.",
        }]
        signals = {k: False for k in (
            "recent_funding", "hiring_security", "cve_in_stack", "compliance_gap",
            "security_leadership_hire", "breach_proximity",
        )}
        signals["hiring_security"] = True
        best, _ = _match_plays(signals, icp_config={"plays": custom})
        assert best is not None
        assert best["name"] == "Example Hiring Alternative"
        assert "embedded team" in best["framing"].lower()

    def test_global_plays_used_when_no_custom_plays(self):
        from app.services.gtm_briefing import _match_plays, _PLAYS
        signals = {k: False for k in (
            "recent_acquisition", "cve_in_stack", "compliance_gap",
            "breach_proximity", "security_leadership_hire", "hiring_security",
            "recent_funding", "hiring", "hiring_sales_bd", "new_leadership",
            "growing_tech_stack", "no_security_tooling", "regulated_industry",
            "missing_compliance_tools", "competitor_tool_detected",
            "product_launch_or_partnership", "security_team_expansion",
        )}
        signals["recent_acquisition"] = True
        best, _ = _match_plays(signals, client_context={"company_name": "TestOrg"})
        assert best is not None
        # Should still find a match in the fallback global _PLAYS
        assert best["name"] in {p["name"] for p in _PLAYS}

    def test_custom_plays_with_disabled_signals_match_correctly(self):
        """End-to-end: signals_enabled gates upstream, custom plays match downstream.
        A config where security_leadership_hire is detected but not enabled,
        so the cyber-vendor play that requires it should never fire even if the
        custom play library happened to reference it."""
        from app.services.gtm_briefing import _extract_signals, _match_plays

        enrichment = {
            "threat_alerts": [],
            "predictleads_jobs": {"jobs": [{"title": "Security Engineer"}]},
            "predictleads_news": {"events": []},
            "tech_stack": {"total_detected": 50},
            "linkedin_verification": {"primary_role": {"title": "CISO"}},  # would fire security_leadership_hire
            "deep_research": {"compliance": {}},
            "news": {},
        }
        lead = {"company_industry": "saas", "title": "CISO"}
        config = {
            "signals_enabled": ["hiring_security", "recent_funding"],
            "plays": [{
                "name": "Hiring Alt", "tier": "cyber",
                "description": "...",
                "required_signals": ["hiring_security"],
                "boost_signals": [],
                "min_score": 1,
                "framing": "Embedded team angle",
            }],
        }
        signals = _extract_signals(enrichment, lead, icp_config=config)
        # security_leadership_hire is detectable (CISO title) but NOT in
        # signals_enabled — must be forced to False
        assert signals["security_leadership_hire"] is False
        # hiring_security IS enabled and a security-titled role is in jobs — should fire
        assert signals["hiring_security"] is True
        # Match should pick the custom play, not anything from the global _PLAYS
        best, _ = _match_plays(signals, icp_config=config)
        assert best is not None
        assert best["name"] == "Hiring Alt"


class TestCustomSignals:
    """Per-client custom signals defined in icp_config.custom_signals.

    Different cyber sub-sectors (brand protection, threat intel, vCISO, MSSP,
    IAM) care about different signals. Each client's icp_config.custom_signals
    declares its own — Claude evaluates them per-prospect and the result merges
    into the regular signals dict so the play matcher picks them up by name.
    """

    def test_returns_empty_when_no_custom_signals_defined(self):
        from app.services.gtm_briefing import _evaluate_custom_signals
        result = _evaluate_custom_signals({}, {}, icp_config={})
        assert result == {}

    def test_returns_empty_when_icp_config_is_none(self):
        from app.services.gtm_briefing import _evaluate_custom_signals
        result = _evaluate_custom_signals({}, {}, icp_config=None)
        assert result == {}

    def test_skips_malformed_signal_entries(self):
        """A malformed entry (missing name or evaluation_prompt) is silently
        dropped — should NOT cause the LLM call. Confirms validation gate."""
        from app.services.gtm_briefing import _evaluate_custom_signals
        with patch("app.services.gtm_briefing.call_claude") as mock_call:
            result = _evaluate_custom_signals(
                {}, {"first_name": "A", "company_name": "X"},
                icp_config={"custom_signals": [
                    {"name": "", "evaluation_prompt": "foo"},     # blank name
                    {"name": "valid_one"},                          # missing prompt
                    "not a dict",                                    # wrong type
                ]},
            )
        assert result == {}
        mock_call.assert_not_called()

    def test_evaluates_and_merges_into_signals_dict(self):
        """End-to-end-ish: a brand-protection client has a custom signal
        defined; Claude returns true; signal lands in the merged dict and is
        matchable by a play that requires it."""
        from app.services.gtm_briefing import _evaluate_custom_signals, _match_plays

        icp_config = {
            "custom_signals": [{
                "name": "brand_abuse_high",
                "description": "Prospect has detected typosquats / phishing infra",
                "evaluation_prompt": "Fire when enrichment_data.webamon.typosquat_count >= 5.",
            }],
            "plays": [{
                "name": "Brand Abuse Outreach", "tier": "cyber",
                "description": "Client sells brand protection — hot prospect detected",
                "required_signals": ["brand_abuse_high"],
                "boost_signals": [],
                "min_score": 1,
                "framing": "Lead with the typosquat count.",
            }],
        }
        lead = {"first_name": "Sam", "last_name": "Lee", "company_name": "AcmeCo"}
        enrichment = {"webamon": {"typosquat_count": 12}}

        with patch("app.services.gtm_briefing.call_claude") as mock_call:
            mock_call.return_value = {"brand_abuse_high": True}
            custom = _evaluate_custom_signals(enrichment, lead, icp_config=icp_config)

        assert custom == {"brand_abuse_high": True}

        # Merge into signals dict and confirm play matcher finds it by name
        signals = {"brand_abuse_high": False}
        signals.update(custom)
        best, _ = _match_plays(signals, icp_config=icp_config)
        assert best is not None
        assert best["name"] == "Brand Abuse Outreach"

    def test_coerces_string_truthy_values(self):
        """Some models return 'true'/'yes' as strings — coerce to bool rather
        than reject the response."""
        from app.services.gtm_briefing import _evaluate_custom_signals

        icp_config = {"custom_signals": [
            {"name": "sig_a", "evaluation_prompt": "test"},
            {"name": "sig_b", "evaluation_prompt": "test"},
            {"name": "sig_c", "evaluation_prompt": "test"},
        ]}
        with patch("app.services.gtm_briefing.call_claude") as mock_call:
            mock_call.return_value = {"sig_a": "true", "sig_b": "no", "sig_c": False}
            result = _evaluate_custom_signals({}, {"first_name": "A"}, icp_config=icp_config)

        assert result == {"sig_a": True, "sig_b": False, "sig_c": False}

    def test_returns_empty_on_llm_failure(self):
        """If Claude raises, we fail closed — no signals fire. Briefing
        continues with built-in signals only rather than crashing."""
        from app.services.gtm_briefing import _evaluate_custom_signals

        icp_config = {"custom_signals": [
            {"name": "sig", "evaluation_prompt": "test"},
        ]}
        with patch("app.services.gtm_briefing.call_claude", side_effect=RuntimeError("boom")):
            result = _evaluate_custom_signals({}, {"first_name": "A"}, icp_config=icp_config)
        assert result == {}

    def test_unknown_signal_keys_in_response_are_dropped(self):
        """Defensive: model adds extra keys to its JSON — only signals we
        asked about flow through."""
        from app.services.gtm_briefing import _evaluate_custom_signals

        icp_config = {"custom_signals": [
            {"name": "sig_only", "evaluation_prompt": "test"},
        ]}
        with patch("app.services.gtm_briefing.call_claude") as mock_call:
            mock_call.return_value = {
                "sig_only": True,
                "made_up_signal": True,
                "another_invented_one": False,
            }
            result = _evaluate_custom_signals({}, {"first_name": "A"}, icp_config=icp_config)
        assert result == {"sig_only": True}


class TestPhoneReveal:
    """Apollo phone-reveal helpers — extraction logic + key-not-set guard.

    The endpoint itself isn't tested end-to-end (DB orchestration is hard
    to mock cleanly and not worth the test burden); these focus on the
    pure-function helpers and the early-exit paths.
    """

    def test_extracts_phone_from_phone_number_field(self):
        from app.routers.leads import _extract_phone_from_person
        person = {"phone_number": "+447700900000"}
        assert _extract_phone_from_person(person) == "+447700900000"

    def test_extracts_phone_from_phone_numbers_array_sanitized(self):
        from app.routers.leads import _extract_phone_from_person
        person = {"phone_numbers": [{"sanitized_number": "+15551234567", "raw_number": "(555) 123-4567", "type": "mobile"}]}
        assert _extract_phone_from_person(person) == "+15551234567"

    def test_extracts_phone_from_phone_numbers_array_raw_fallback(self):
        """When sanitized_number is missing, fall back to raw_number."""
        from app.routers.leads import _extract_phone_from_person
        person = {"phone_numbers": [{"raw_number": "+44 20 7946 0958"}]}
        assert _extract_phone_from_person(person) == "+44 20 7946 0958"

    def test_extracts_phone_from_string_array(self):
        """Some Apollo payload variants ship phone_numbers as plain strings."""
        from app.routers.leads import _extract_phone_from_person
        person = {"phone_numbers": ["+15551234567"]}
        assert _extract_phone_from_person(person) == "+15551234567"

    def test_returns_none_when_no_phone(self):
        from app.routers.leads import _extract_phone_from_person
        assert _extract_phone_from_person({"name": "Adam"}) is None

    def test_returns_none_for_empty_string_phone(self):
        from app.routers.leads import _extract_phone_from_person
        assert _extract_phone_from_person({"phone_number": "  "}) is None

    def test_returns_none_for_non_dict_input(self):
        from app.routers.leads import _extract_phone_from_person
        assert _extract_phone_from_person(None) is None
        assert _extract_phone_from_person("string") is None

    def test_reveal_returns_none_when_no_apollo_key(self, monkeypatch):
        """Without the Apollo key, reveal short-circuits without HTTP call."""
        from app.routers.leads import _apollo_reveal_phone
        from app.config import settings
        monkeypatch.setattr(settings, "APOLLO_API_KEY", "")
        assert _apollo_reveal_phone(first_name="A", last_name="B") is None


class TestPreEnrichmentICPGate:
    """The cheap pre-enrichment ICP check — runs after Apollo, skips
    run_full_enrichment when the lead is clearly outside the org's ICP."""

    VERA_ICP = {
        "target_industries": [
            "cybersecurity", "information security", "MSSP",
            "penetration testing", "managed security",
            "threat intelligence", "GRC", "identity security",
        ],
        "target_keywords": [
            "cybersecurity", "infosec", "MSSP", "pen testing",
            "SOC", "MDR", "XDR", "threat detection", "GRC", "compliance",
        ],
    }

    TSC_ICP = {
        "target_industries": [
            "computer software", "internet", "information technology & services",
            "financial services", "fintech", "healthcare", "saas",
            "cloud computing", "artificial intelligence",
        ],
        "target_keywords": [],
    }

    def test_vera_misses_non_cyber_company(self):
        from app.routers.leads import _check_lead_in_icp
        # Merlin AI: AI productivity tool, not cyber — Vera should skip
        decision, _ = _check_lead_in_icp(
            {"company_industry": "computer software", "company_name": "Merlin AI"},
            self.VERA_ICP,
        )
        assert decision == "miss"

    def test_vera_fits_cyber_company_with_apollo_industry_string(self):
        from app.routers.leads import _check_lead_in_icp
        # Cybriant: Apollo returns "computer & network security" — should
        # match via the "security" token derived from Vera's target_industries
        decision, reason = _check_lead_in_icp(
            {"company_industry": "computer & network security", "company_name": "Cybriant"},
            self.VERA_ICP,
        )
        assert decision == "fit"
        assert "security" in reason.lower()

    def test_vera_fits_when_keyword_in_company_name_no_industry(self):
        from app.routers.leads import _check_lead_in_icp
        decision, _ = _check_lead_in_icp(
            {"company_industry": "", "company_name": "Apex MSSP Solutions"},
            self.VERA_ICP,
        )
        assert decision == "fit"

    def test_vera_misses_bank(self):
        from app.routers.leads import _check_lead_in_icp
        decision, _ = _check_lead_in_icp(
            {"company_industry": "banking", "company_name": "Bank of England"},
            self.VERA_ICP,
        )
        assert decision == "miss"

    def test_tsc_fits_saas_startup(self):
        from app.routers.leads import _check_lead_in_icp
        decision, _ = _check_lead_in_icp(
            {"company_industry": "computer software", "company_name": "Merlin AI"},
            self.TSC_ICP,
        )
        assert decision == "fit"

    def test_tsc_misses_bank(self):
        from app.routers.leads import _check_lead_in_icp
        decision, _ = _check_lead_in_icp(
            {"company_industry": "banking", "company_name": "Bank of England"},
            self.TSC_ICP,
        )
        assert decision == "miss"

    def test_miss_when_no_industry_no_keyword_match_no_profile(self):
        """Without LinkedIn context, a generic name + no industry → miss
        (org has needles configured, haystack has the company name, no match)."""
        from app.routers.leads import _check_lead_in_icp
        decision, _ = _check_lead_in_icp(
            {"company_industry": "", "company_name": "Random Co"},
            self.VERA_ICP,
        )
        assert decision == "miss"

    def test_linkedin_headline_provides_signal_when_industry_missing(self):
        """Apollo returns industry=null for ~30% of B2B contacts. LinkedIn
        headline + experience description usually rescue the classification.
        """
        from app.routers.leads import _check_lead_in_icp
        # Merlin-AI-shaped lead: name doesn't match cyber, no industry, but
        # LinkedIn headline talks about AI productivity — Vera should miss.
        profile = {
            "headline": "Co-Founder at Merlin AI | AI productivity assistant",
            "currentPosition": {
                "position": "Co-Founder",
                "companyName": "Merlin AI",
                "description": "Building an AI productivity tool for everyone",
            },
        }
        decision, _ = _check_lead_in_icp(
            {"company_industry": "", "company_name": "Merlin AI", "title": "Co-Founder"},
            self.VERA_ICP,
            profile=profile,
        )
        assert decision == "miss"

    def test_linkedin_headline_catches_cyber_company_with_no_apollo_industry(self):
        """Cyber company without Apollo industry — LinkedIn headline rescues."""
        from app.routers.leads import _check_lead_in_icp
        profile = {
            "headline": "Founder at Stealth Cyber Co | Threat intelligence for MSSPs",
            "currentPosition": {"position": "Founder", "companyName": "Stealth Cyber Co"},
        }
        decision, _ = _check_lead_in_icp(
            {"company_industry": "", "company_name": "Stealth Cyber Co"},
            self.VERA_ICP,
            profile=profile,
        )
        assert decision == "fit"

    def test_ambiguous_when_icp_not_configured(self):
        from app.routers.leads import _check_lead_in_icp
        decision, _ = _check_lead_in_icp(
            {"company_industry": "banking", "company_name": "Anyone"},
            {},
        )
        assert decision == "ambiguous"

    def test_generic_stopwords_dont_create_false_positives(self):
        """A target_industry of 'computer software' tokenises to 'computer'
        and 'software' — both stopwords. With filtering, the only real
        signals from Vera's list are cyber-specific, so a software-only
        company correctly misses."""
        from app.routers.leads import _check_lead_in_icp
        decision, _ = _check_lead_in_icp(
            {"company_industry": "computer software", "company_name": "Random SaaS"},
            self.VERA_ICP,
        )
        assert decision == "miss"


class TestLLMICPRescue:
    """gpt-4o-mini fallback that fires when deterministic check returns miss.
    Rescues false negatives where LinkedIn data was too sparse for token match."""

    def test_returns_none_when_no_api_key(self, monkeypatch):
        """Without OPENROUTER_API_KEY set, the rescue returns None and the
        caller keeps the deterministic miss."""
        from app.routers.leads import _classify_icp_via_llm
        from app.config import settings
        monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")
        result = _classify_icp_via_llm(
            {"company_name": "Merlin AI", "company_industry": "", "title": "CEO"},
            {"target_industries": ["saas"]},
            {"what_they_sell": "embedded security teams"},
            profile={"headline": "Co-Founder at Merlin AI"},
        )
        assert result is None

    def test_parses_fit_response(self, monkeypatch):
        from app.routers.leads import _classify_icp_via_llm
        import app.routers.leads as leads_module
        from app.config import settings
        monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "test-key")

        class FakeResponse:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": '{"decision": "fit", "reason": "AI startup matches target"}'}}]}

        def fake_post(*args, **kwargs):
            return FakeResponse()

        monkeypatch.setattr(leads_module.httpx, "post", fake_post)
        result = _classify_icp_via_llm(
            {"company_name": "Merlin AI", "company_industry": "", "title": "Co-Founder"},
            {"target_industries": ["saas", "artificial intelligence"]},
            {"what_they_sell": "embedded security teams"},
            profile={"headline": "Co-Founder at Merlin AI"},
        )
        assert result == ("fit", "AI startup matches target")

    def test_parses_miss_response(self, monkeypatch):
        from app.routers.leads import _classify_icp_via_llm
        import app.routers.leads as leads_module
        from app.config import settings
        monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "test-key")

        class FakeResponse:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": '```json\n{"decision": "miss", "reason": "Bank, not in target"}\n```'}}]}

        monkeypatch.setattr(leads_module.httpx, "post", lambda *a, **kw: FakeResponse())
        result = _classify_icp_via_llm(
            {"company_name": "Big Bank", "company_industry": "banking"},
            {"target_industries": ["cybersecurity"]},
            {"what_they_sell": "GTM automation"},
        )
        assert result == ("miss", "Bank, not in target")

    def test_returns_none_on_invalid_json(self, monkeypatch):
        from app.routers.leads import _classify_icp_via_llm
        import app.routers.leads as leads_module
        from app.config import settings
        monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "test-key")

        class FakeResponse:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": "not json at all"}}]}

        monkeypatch.setattr(leads_module.httpx, "post", lambda *a, **kw: FakeResponse())
        result = _classify_icp_via_llm(
            {"company_name": "X"}, {"target_industries": ["saas"]}, {},
        )
        assert result is None
