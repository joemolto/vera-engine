"""Unit tests for the billing service — covers the deterministic helpers
that don't hit Stripe's API. End-to-end checkout flow is tested manually
in Stripe test mode (using test card 4242 4242 4242 4242)."""

import os
import pytest


class TestTopupPacks:
    """The 3 credit-pack options surfaced to the dashboard top-up UI."""

    def test_three_packs_defined(self):
        from app.services.billing import TOPUP_PACKS
        assert set(TOPUP_PACKS.keys()) == {"small", "medium", "large"}

    def test_pack_credits_increase_with_size(self):
        from app.services.billing import TOPUP_PACKS
        assert TOPUP_PACKS["small"]["credits"] < TOPUP_PACKS["medium"]["credits"]
        assert TOPUP_PACKS["medium"]["credits"] < TOPUP_PACKS["large"]["credits"]

    def test_per_credit_price_decreases_with_pack_size(self):
        """Larger packs offer better per-credit value — small $0.50, large $0.40."""
        from app.services.billing import TOPUP_PACKS
        per_credit = lambda p: TOPUP_PACKS[p]["amount_cents"] / TOPUP_PACKS[p]["credits"]
        assert per_credit("small") > per_credit("medium")
        assert per_credit("medium") >= per_credit("large")

    def test_pack_amounts_match_documented_prices(self):
        """Cross-check vs the documented prices in app/services/billing.py
        and the FAQ — guards against accidental drift."""
        from app.services.billing import TOPUP_PACKS
        assert TOPUP_PACKS["small"]["amount_cents"] == 49_00
        assert TOPUP_PACKS["medium"]["amount_cents"] == 159_00
        assert TOPUP_PACKS["large"]["amount_cents"] == 349_00


class TestPriceConstants:
    """Source-of-truth pricing values used by the seed script + checkout."""

    def test_integration_fee_is_3000_dollars(self):
        from app.services.billing import INTEGRATION_FEE_CENTS
        assert INTEGRATION_FEE_CENTS == 3_000_00

    def test_monthly_tier_prices(self):
        from app.services.billing import TIER_MONTHLY_CENTS
        assert TIER_MONTHLY_CENTS == {
            "scout": 399_00,
            "growth": 599_00,
            "scale": 999_00,
        }

    def test_annual_tier_prices_are_about_10_percent_off(self):
        """Annual prices target ~10% off but are rounded to a clean
        monthly-equivalent ($359/mo etc.) for nicer customer-facing numbers.
        That introduces a small discrepancy vs strict 10% — Scout is
        actually 10.03% off, etc. Tolerance allows up to $2 deviation."""
        from app.services.billing import TIER_MONTHLY_CENTS, TIER_ANNUAL_CENTS
        for tier, monthly in TIER_MONTHLY_CENTS.items():
            expected_annual = round(monthly * 12 * 0.90)
            actual_annual = TIER_ANNUAL_CENTS[tier]
            assert abs(actual_annual - expected_annual) <= 200, (
                f"{tier} annual is {actual_annual} cents but ~10% off "
                f"monthly × 12 = {expected_annual}"
            )

    def test_annual_tier_prices_match_clean_monthly_equivalents(self):
        """Annual prices encode a round monthly-equivalent rate. Cross-checks
        the documented per-month values customers see on the Pricing page."""
        from app.services.billing import TIER_ANNUAL_CENTS
        # Scout: $359/mo × 12, Growth: $539/mo × 12, Scale: $899/mo × 12
        assert TIER_ANNUAL_CENTS == {
            "scout": 359_00 * 12,
            "growth": 539_00 * 12,
            "scale": 899_00 * 12,
        }


class TestPriceIdLookup:
    """The seed script writes Stripe Price IDs to env vars; the runtime
    reads them back via get_*_price_id helpers."""

    def test_get_subscription_price_id_rejects_unknown_tier(self):
        from app.services.billing import get_subscription_price_id
        with pytest.raises(ValueError, match="Unknown tier"):
            get_subscription_price_id("enterprise", "monthly")

    def test_get_subscription_price_id_rejects_unknown_cycle(self):
        from app.services.billing import get_subscription_price_id
        with pytest.raises(ValueError, match="Unknown cycle"):
            get_subscription_price_id("scout", "weekly")

    def test_get_topup_price_id_rejects_unknown_pack(self):
        from app.services.billing import get_topup_price_id
        with pytest.raises(ValueError, match="Unknown top-up pack"):
            get_topup_price_id("xlarge")

    def test_get_subscription_price_id_returns_env_value(self, monkeypatch):
        from app.services.billing import get_subscription_price_id
        monkeypatch.setenv("STRIPE_PRICE_SCOUT_MONTHLY", "price_test_scout_monthly")
        assert get_subscription_price_id("scout", "monthly") == "price_test_scout_monthly"

    def test_get_subscription_price_id_raises_when_env_missing(self, monkeypatch):
        from app.services.billing import get_subscription_price_id
        monkeypatch.delenv("STRIPE_PRICE_SCOUT_MONTHLY", raising=False)
        with pytest.raises(RuntimeError, match="Stripe Price ID not configured"):
            get_subscription_price_id("scout", "monthly")

    def test_get_integration_fee_price_id_returns_env_value(self, monkeypatch):
        from app.services.billing import get_integration_fee_price_id
        monkeypatch.setenv("STRIPE_PRICE_INTEGRATION_FEE", "price_test_integration")
        assert get_integration_fee_price_id() == "price_test_integration"

    def test_get_topup_price_id_returns_env_value(self, monkeypatch):
        from app.services.billing import get_topup_price_id
        monkeypatch.setenv("STRIPE_PRICE_TOPUP_MEDIUM", "price_test_topup_medium")
        assert get_topup_price_id("medium") == "price_test_topup_medium"
