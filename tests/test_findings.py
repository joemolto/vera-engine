"""Phase 7 tests — normalized findings + CVE/compliance no-overclaim guardrails.

Asserts the decisive architecture guarantees:
  - scorer/briefing have NO hard dependency on the live `signals` table:
    canonical findings in enrichment_data resolve without ever querying it;
  - findings are an additive projection, not a replacement for the
    deterministic signal extraction;
  - CVE wording never overclaims (non-KEV critical ≠ "actively exploited");
  - compliance gap always carries the "absence is not proof" caveat.
"""

from datetime import datetime, timedelta, timezone

from app.services import signal_payload as sp


def _iso(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _row(stype, strength, days_exp=30):
    return {
        "signal_type": stype, "signal_source": "x", "signal_strength": strength,
        "signal_data": sp.build_signal_payload(
            headline=f"{stype} headline", entity="Acme", detail="d"),
        "expires_at": _iso(days_exp),
    }


class TestSignalProminenceLever:
    """Per-client lever: deprioritise threat/fear findings in ordering."""

    def _set(self):
        # cve (0.95, threat) strongest; funding (0.9, opportunity) next.
        return sp.collect_findings([
            _row("cve_in_stack", 0.95),
            _row("recently_funded", 0.9),
            _row("security_leadership_hire", 0.8),
        ])

    def test_default_off_threat_leads_by_strength(self):
        out = sp.load_findings(
            None, org_id=None, lead_id=None,
            enrichment_data={"findings": self._set()},
        )
        assert out[0]["signal_type"] == "cve_in_stack"  # unchanged behaviour

    def test_lever_on_opportunity_leads_threat_demoted_not_dropped(self):
        out = sp.load_findings(
            None, org_id=None, lead_id=None,
            enrichment_data={"findings": self._set()},
            deprioritise_threats=True,
        )
        assert out[0]["signal_type"] == "recently_funded"
        assert out[1]["signal_type"] == "security_leadership_hire"
        # CVE still present — supporting context, just last.
        assert out[-1]["signal_type"] == "cve_in_stack"
        assert any(f["signal_type"] == "cve_in_stack" for f in out)

    def test_wants_threats_deprioritised_parsing(self):
        # Universal hard rule: ON by default for EVERY client, incl.
        # unconfigured / future orgs (absent or None -> True).
        assert sp.wants_threats_deprioritised({})
        assert sp.wants_threats_deprioritised(None)
        assert sp.wants_threats_deprioritised({"deprioritise_threat_findings": True})
        assert sp.wants_threats_deprioritised({"deprioritise_threat_findings": "true"})
        assert sp.wants_threats_deprioritised({"deprioritise_threat_findings": "1"})
        # Only an explicit false-y escape hatch turns it off.
        assert not sp.wants_threats_deprioritised({"deprioritise_threat_findings": False})
        assert not sp.wants_threats_deprioritised({"deprioritise_threat_findings": "false"})
        assert not sp.wants_threats_deprioritised({"deprioritise_threat_findings": "off"})
        assert not sp.wants_threats_deprioritised({"deprioritise_threat_findings": "no"})

    def test_strength_and_confidence_unchanged_by_lever(self):
        """Lever only reorders — underlying strength/confidence untouched."""
        s = self._set()
        cve = next(f for f in s if f["signal_type"] == "cve_in_stack")
        assert cve["strength"] == 0.95 and cve["confidence"] == "high"


class _ExplodingDB:
    """Any access to the signals table raises — proves decoupling."""

    def table(self, *a, **k):
        raise AssertionError(
            "signals table must NOT be queried when canonical findings exist"
        )


class TestFindingsDecoupling:
    def test_canonical_findings_resolve_without_signals_table(self):
        row = {
            "signal_type": "cve_in_stack", "signal_source": "threat_intel",
            "signal_strength": 0.95,
            "signal_data": sp.build_signal_payload(
                headline="2 CVEs in Acme's stack", entity="Acme",
                detail="context."),
            "expires_at": _iso(30),
        }
        canonical = sp.collect_findings([row])
        ed = {"findings": canonical}
        # DB would raise if touched.
        out = sp.load_findings(
            _ExplodingDB(), org_id="o", lead_id="l", enrichment_data=ed
        )
        assert [f["headline"] for f in out] == ["2 CVEs in Acme's stack"]

    def test_no_rows_no_canonical_returns_empty_not_error(self):
        # Pre-canonical lead with no rows: graceful [] (never raises).
        assert sp.load_findings(
            None, org_id=None, lead_id=None, enrichment_data={}
        ) == []

    def test_expired_canonical_findings_filtered_at_read(self):
        row = {
            "signal_type": "recently_funded", "signal_source": "predictleads",
            "signal_strength": 0.9,
            "signal_data": sp.build_signal_payload(
                headline="Acme raised", entity="Acme", detail="x"),
            "expires_at": _iso(-1),  # already expired
        }
        ed = {"findings": sp.collect_findings([row])}
        assert sp.load_findings(
            None, org_id=None, lead_id=None, enrichment_data=ed
        ) == []

    def test_findings_are_superset_not_replacement(self):
        """v2 keeps every v1 key (additive) AND adds the richer schema."""
        row = {
            "signal_type": "compliance_gap", "signal_source": "deep_research",
            "signal_strength": 0.75,
            "signal_data": sp.build_signal_payload(
                headline="h", entity="Acme", detail="d", industry="fintech"),
            "expires_at": _iso(30),
        }
        f = sp.finding_from_row(row)
        for v1 in ("signal_type", "signal_source", "strength", "headline",
                   "detail", "entity", "occurred_at", "detected_at",
                   "expires_at"):
            assert v1 in f
        for v2 in ("type", "category", "summary", "source_name",
                   "source_url", "observed_at", "freshness", "confidence",
                   "evidence", "sales_relevance", "recommended_use",
                   "risk_note", "related_persona", "related_service"):
            assert v2 in f

    def test_category_taxonomy_complete(self):
        assert len(sp.FINDING_CATEGORIES) == 10
        # Mapped cyber types resolve to a canonical category.
        assert sp.finding_from_row({
            "signal_type": "cve_in_stack", "signal_strength": 0.9,
            "signal_data": {"headline": "h"},
        })["category"] == "exposed_technology_relevance"


class TestCveGuardrails:
    def test_non_kev_critical_not_called_actively_exploited(self):
        headline, _ = sp.cve_in_stack_line(
            company="Acme",
            matches=[{"cve_title": "CVE-2026-9 Foo", "tech": "nginx",
                      "is_kev": False, "severity": "critical"}],
        )
        assert "actively-exploited" not in headline.lower()
        assert "critical" in headline.lower()

    def test_kev_match_is_actively_exploited(self):
        headline, _ = sp.cve_in_stack_line(
            company="Acme",
            matches=[{"cve_title": "CVE-2026-1 Bar", "tech": "struts",
                      "is_kev": True, "severity": "critical"}],
        )
        assert "actively-exploited" in headline.lower()

    def test_mixed_kev_and_critical_does_not_inflate_exploited_count(self):
        """1 KEV + 1 critical-non-KEV must NOT read '2 actively-exploited'.
        Regression: the headline count must equal what the wording claims.
        """
        headline, detail = sp.cve_in_stack_line(
            company="NorthBank",
            matches=[
                {"cve_title": "CVE-A Struts", "tech": "apache",
                 "is_kev": True, "severity": "critical"},
                {"cve_title": "CVE-B nginx", "tech": "nginx",
                 "is_kev": False, "severity": "critical"},
            ],
        )
        assert "2 actively-exploited" not in headline.lower()
        assert "1 actively-exploited cve" in headline.lower()
        # The non-KEV critical is acknowledged, just not as exploited.
        assert "other critical" in detail.lower()

    def test_confidence_ladder_orders_kev_above_critical(self):
        kev = sp.cve_confidence([{"is_kev": True, "severity": "critical"}])
        crit = sp.cve_confidence([{"is_kev": False, "severity": "critical"}])
        high = sp.cve_confidence([{"is_kev": False, "severity": "high"}])
        assert kev > crit > high

    def test_cve_detail_keeps_sector_context_framing(self):
        _, detail = sp.cve_in_stack_line(
            company="Acme",
            matches=[{"cve_title": "CVE-X", "tech": "nginx",
                      "is_kev": True, "severity": "critical"}],
        )
        assert "sector context" in detail.lower()


class TestComplianceGuardrails:
    def test_absence_is_not_proof_caveat_present(self):
        _, detail = sp.compliance_gap_line(company="Acme", industry="fintech")
        assert "not proof of" in detail.lower()

    def test_compliance_finding_carries_risk_note_caveat(self):
        row = {
            "signal_type": "compliance_gap", "signal_source": "deep_research",
            "signal_strength": 0.75,
            "signal_data": sp.build_signal_payload(
                headline="h", entity="Acme",
                detail=sp.compliance_gap_line(
                    company="Acme", industry="fintech")[1],
                industry="fintech"),
        }
        f = sp.finding_from_row(row)
        assert "not proof of" in f["risk_note"].lower()


class TestReframeRatingReason:
    """No-fear clients: rating reason must never LEAD with the prospect's
    own incident/exposure. Deterministic; no-op for normal clients."""

    NOFEAR = {"deprioritise_threat_findings": True}
    NORMAL = {}
    OPP = [{"category": "budget_or_growth_signal",
            "headline": "Contoso closed a $40M funding round"}]
    THREATONLY = [{"category": "exposed_technology_relevance",
                   "headline": "2 actively-exploited CVEs in the stack"}]

    FEAR = ("Northwind suffered three DDoS incidents this week, a live "
            "security crisis. It also builds NorthOS, a strong ICP fit.")
    CLEAN = ("Fabrikam is a UK fintech building proprietary payment "
             "infrastructure — a strong ICP fit.")

    def test_fear_led_replaced_with_opportunity_for_nofear_client(self):
        out = sp.reframe_rating_reason(
            self.FEAR, client_context=self.NOFEAR, findings=self.OPP)
        assert out.startswith("Contoso closed a $40M funding round")
        assert "It also builds NorthOS" in out          # rest preserved
        assert not out.lower().startswith("northwind suffered")

    def test_default_client_reframed_no_fear_is_universal(self):
        # bba99cd made no-fear the code-level default for EVERY org.
        out = sp.reframe_rating_reason(
            self.FEAR, client_context=self.NORMAL, findings=self.OPP)
        assert out.startswith("Contoso closed a $40M funding round")

    def test_escape_hatch_client_untouched_even_if_fear_led(self):
        hatch = {"deprioritise_threat_findings": False}
        assert sp.reframe_rating_reason(
            self.FEAR, client_context=hatch, findings=self.OPP) == self.FEAR

    def test_nofear_client_clean_reason_untouched(self):
        assert sp.reframe_rating_reason(
            self.CLEAN, client_context=self.NOFEAR, findings=self.OPP) == self.CLEAN

    def test_fear_led_no_opportunity_finding_drops_fear_opener(self):
        out = sp.reframe_rating_reason(
            self.FEAR, client_context=self.NOFEAR, findings=self.THREATONLY)
        assert not out.lower().startswith("northwind suffered")
        assert out == "It also builds NorthOS, a strong ICP fit."

    def test_empty_reason(self):
        assert sp.reframe_rating_reason(
            "", client_context=self.NOFEAR, findings=self.OPP) == ""

    def test_no_findings_still_demotes_fear(self):
        out = sp.reframe_rating_reason(
            self.FEAR, client_context=self.NOFEAR, findings=None)
        assert not out.lower().startswith("northwind suffered")
