"""Phase 7 tests — revenue potential engine.

Covers the brief's required scenarios: baseline-only, larger company,
weak data, high cyber relevance, min/max caps, no-baseline safety, and
that the output always carries assumptions. Pure/deterministic — no LLM.
"""

from app.services.revenue_potential import estimate_revenue_potential


CTX = {
    "average_deal_size": 35000,
    "currency": "GBP",
    "pricing_model": "platform",
    "min_deal_size": 10000,
    "max_deal_size": 150000,
    "enterprise_threshold_employee_count": 5000,
}


class TestNoBaseline:
    def test_no_average_deal_size_returns_unknown(self):
        r = estimate_revenue_potential(lead={"company_size": "500"}, client_ctx={})
        assert r["estimated_revenue_potential"] is None
        assert r["range_low"] is None and r["range_high"] is None
        assert r["confidence"] == "unknown"
        # Safe result still explains itself.
        assert r["assumptions"] and len(r["assumptions"]) >= 1

    def test_no_inputs_at_all_is_safe(self):
        r = estimate_revenue_potential()
        assert r["estimated_revenue_potential"] is None
        assert r["confidence"] == "unknown"


class TestBaseline:
    def test_baseline_with_no_signals_lands_near_baseline(self):
        r = estimate_revenue_potential(
            lead={"company_size": "120", "company_industry": "software",
                  "title": "Manager"},
            client_ctx=CTX,
        )
        est = r["estimated_revenue_potential"]
        # Mid-size, neutral fit, no signals → within a sane band of baseline.
        assert 15000 <= est <= 60000
        assert r["currency"] == "GBP"
        assert r["assumptions"]  # explanation always includes assumptions

    def test_output_shape_complete(self):
        r = estimate_revenue_potential(
            lead={"company_size": "300"}, client_ctx=CTX
        )
        for k in (
            "estimated_revenue_potential", "range_low", "range_high",
            "currency", "confidence", "deal_size_band",
            "expansion_potential", "sales_cycle_complexity",
            "why_this_value", "assumptions", "recommended_commercial_angle",
        ):
            assert k in r


class TestSizeAndCyberLift:
    def test_larger_company_increases_estimate_and_range(self):
        small = estimate_revenue_potential(
            lead={"company_size": "30", "company_industry": "software",
                  "title": "CISO"},
            client_ctx=CTX, signals={"recent_funding": True},
            findings=[{"signal_type": "cve_in_stack"}],
        )
        big = estimate_revenue_potential(
            lead={"company_size": "4000", "company_industry": "banking",
                  "title": "CISO"},
            client_ctx=CTX, signals={"recent_funding": True},
            findings=[{"signal_type": "cve_in_stack"}],
        )
        assert big["estimated_revenue_potential"] > small["estimated_revenue_potential"]
        big_width = big["range_high"] - big["range_low"]
        small_width = small["range_high"] - small["range_low"]
        assert big_width >= small_width

    def test_high_cyber_relevance_increases_estimate(self):
        base = estimate_revenue_potential(
            lead={"company_size": "500", "company_industry": "banking",
                  "title": "CISO"},
            client_ctx=CTX,
        )
        cyber = estimate_revenue_potential(
            lead={"company_size": "500", "company_industry": "banking",
                  "title": "CISO"},
            client_ctx=CTX,
            findings=[{"signal_type": "cve_in_stack"},
                      {"signal_type": "compliance_gap"},
                      {"signal_type": "breach_proximity"}],
        )
        assert cyber["estimated_revenue_potential"] >= base["estimated_revenue_potential"]


class TestConfidenceAndCaps:
    def test_weak_data_lowers_confidence_and_widens_range(self):
        weak = estimate_revenue_potential(lead={}, client_ctx=CTX)
        strong = estimate_revenue_potential(
            lead={"company_size": "800", "company_industry": "banking",
                  "title": "CISO"},
            client_ctx=CTX, signals={"recent_funding": True},
            findings=[{"signal_type": "cve_in_stack"}],
        )
        assert weak["confidence"] == "low"
        assert strong["confidence"] in ("medium", "high")
        weak_width = weak["range_high"] - weak["range_low"]
        strong_width = strong["range_high"] - strong["range_low"]
        # Lower confidence ⇒ proportionally broader band.
        assert weak_width / max(weak["estimated_revenue_potential"], 1) >= \
            strong_width / max(strong["estimated_revenue_potential"], 1)

    def test_min_max_caps_respected(self):
        capped = estimate_revenue_potential(
            lead={"company_size": "50000", "company_industry": "banking",
                  "title": "CEO"},
            client_ctx=CTX,
            signals={"recent_funding": True, "recent_acquisition": True,
                     "expansion": True},
            findings=[{"signal_type": "cve_in_stack"},
                      {"signal_type": "compliance_gap"},
                      {"signal_type": "breach_proximity"},
                      {"signal_type": "security_team_expansion"}],
        )
        assert capped["estimated_revenue_potential"] <= CTX["max_deal_size"]
        assert capped["range_high"] <= CTX["max_deal_size"]
        assert capped["range_low"] >= 0

    def test_never_implies_guaranteed_revenue(self):
        r = estimate_revenue_potential(
            lead={"company_size": "300"}, client_ctx=CTX
        )
        blob = " ".join([
            r["recommended_commercial_angle"], *r["assumptions"]
        ]).lower()
        assert "guarantee" not in blob and "guaranteed" not in blob

    def test_estimates_are_rounded_no_fake_precision(self):
        r = estimate_revenue_potential(
            lead={"company_size": "742", "company_industry": "fintech",
                  "title": "Director"},
            client_ctx=CTX, signals={"recent_funding": True},
        )
        # Sensible rounding — never an unrounded float-looking figure.
        assert r["estimated_revenue_potential"] % 500 == 0
