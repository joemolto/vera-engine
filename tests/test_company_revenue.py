"""Deterministic multi-provider company-revenue reconciliation.

Locks the no-hallucination contract: real provider numbers only,
disagreement -> range + low confidence, Companies House is a guardrail
that never becomes the number (decision 1A), zero sources -> None.
"""

from app.services.company_revenue import (
    is_uk_company,
    parse_money_text,
    reconcile,
)

M = 1_000_000
B = 1_000_000_000


class TestParseMoneyText:
    def test_point_with_suffix(self):
        assert parse_money_text("$70M") == (70 * M, 70 * M)
        assert parse_money_text("£20 million") == (20 * M, 20 * M)
        assert parse_money_text("$1.5B") == (1.5 * B, 1.5 * B)

    def test_range(self):
        assert parse_money_text("$50-70m") == (50 * M, 70 * M)
        assert parse_money_text("approx £50 to £70 million") == (50 * M, 70 * M)

    def test_bare_big_number(self):
        assert parse_money_text("70,000,000") == (70 * M, 70 * M)

    def test_no_value_tokens_return_none(self):
        for s in ("Not found", "", "unknown", "N/A", "private", "undisclosed"):
            assert parse_money_text(s) is None

    def test_unscaled_small_number_is_not_invented(self):
        # "70" with no scale word is not a credible revenue — don't guess.
        assert parse_money_text("70") is None
        assert parse_money_text("founded 2015") is None

    def test_non_string(self):
        assert parse_money_text(None) is None
        assert parse_money_text(40 * M) is None


class TestIsUkCompany:
    def test_country(self):
        assert is_uk_company(country="United Kingdom")
        assert is_uk_company(country="GB")
        assert not is_uk_company(country="United States")

    def test_location_and_domain(self):
        assert is_uk_company(location="London, England, United Kingdom")
        assert is_uk_company(domain="acme.co.uk")
        assert not is_uk_company(location="Boston, Massachusetts, United States")
        assert not is_uk_company(domain="acme.com")


class TestReconcile:
    def test_apollo_only_point_high_confidence(self):
        r = reconcile(apollo_amount=40 * M)
        assert r["amount"] == 40 * M
        assert r["printed"] == "$40M"
        assert r["confidence"] == "high"
        assert r["sources"] == [{"provider": "apollo", "value": 40 * M}]

    def test_two_providers_agree_average_point(self):
        r = reconcile(apollo_amount=40 * M, perplexity_text="$45M")
        # 45/40 = 1.125 <= 1.30 -> agree, point = median(40, 45) = 42.5M
        assert r["confidence"] == "high"
        assert r["amount"] == 42.5 * M
        assert r["printed"] == "$42.5M"
        assert len(r["sources"]) == 2

    def test_two_providers_disagree_show_range_low_confidence(self):
        r = reconcile(apollo_amount=40 * M, perplexity_text="$70M")
        # 70/40 = 1.75 -> disagree -> range, no confident point
        assert r["confidence"] == "low"
        assert r["amount"] is None
        assert r["printed"] == "$40M–$70M"
        assert r["low"] == 40 * M and r["high"] == 70 * M

    def test_no_numeric_source_returns_none(self):
        assert reconcile(apollo_amount=None, perplexity_text="Not found") is None

    def test_companies_house_alone_never_a_number(self):
        # Decision 1A: CH is a guardrail only.
        assert reconcile(companies_house_bracket="£10.2m - £36m") is None

    def test_companies_house_corroborates(self):
        r = reconcile(apollo_amount=20 * M, companies_house_bracket="£10.2m - £36m")
        assert r["corroborated_by_companies_house"] is True
        assert r["companies_house_conflict"] is False
        assert r["confidence"] == "high"
        assert r["amount"] == 20 * M

    def test_companies_house_conflict_forces_caution(self):
        # Apollo says $500M, filed accounts say "Under £632k" -> conflict.
        r = reconcile(apollo_amount=500 * M, companies_house_bracket="Under £632k")
        assert r["companies_house_conflict"] is True
        assert r["confidence"] == "low"
        assert r["amount"] is None  # no confident point shown
        assert r["companies_house_band"] == "Under £632k"

    def test_perplexity_wide_self_range_is_low_confidence(self):
        r = reconcile(perplexity_text="$20-70M")
        assert r["confidence"] == "low"
        assert r["amount"] is None
        assert r["printed"] == "$20M–$70M"

    def test_sources_retain_raw_values(self):
        r = reconcile(apollo_amount=30 * M, perplexity_text="around $32 million")
        provs = {s["provider"]: s for s in r["sources"]}
        assert provs["apollo"]["value"] == 30 * M
        assert provs["perplexity"]["text"] == "around $32 million"
        assert provs["perplexity"]["parsed_low"] == 32 * M
