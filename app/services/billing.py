"""Stripe billing — checkout sessions, subscriptions, top-ups, and webhook
processing. Single source of truth for all Stripe-touching logic.

Billing model encoded here:
- Integration fee: one-time, charged at signup.
- Subscription: optional trial, then auto-bills monthly OR annually
  depending on the plan chosen (tiers: starter, solo, scout, growth, scale).
- Credit top-ups: 3 pack sizes (small / medium / large) for in-product
  purchases.

Prices live in your own Stripe account; their Price IDs are read from
STRIPE_PRICE_* env vars (see _price_id below).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, TYPE_CHECKING

from fastapi import HTTPException
from supabase import Client

from app.config import settings

logger = logging.getLogger(__name__)


# `stripe` is imported lazily so the engine still boots when the package
# isn't installed yet. Without this guard, a missing `stripe` package
# crashes the entire engine at startup (because main.py imports billing.py
# which imports stripe). With this guard, only billing endpoints fail
# (cleanly) and every other feature keeps working.
class _StripeNotInstalled(RuntimeError):
    """Raised when a billing endpoint is called but the stripe package
    isn't installed on the host. Surfaced as 503 by the router."""

    def __init__(self) -> None:
        super().__init__(
            "The `stripe` Python package is not installed on this host. "
            "Run `pip install stripe==11.4.1 --break-system-packages` "
            "(or install in your venv) and restart the engine."
        )


_stripe_module = None
_stripe_configured = False


def _get_stripe():
    """Return the stripe module, importing on first use and configuring the
    global API key. Raises _StripeNotInstalled if the package is missing."""
    global _stripe_module, _stripe_configured
    if _stripe_module is None:
        try:
            import stripe as _stripe  # noqa: WPS433 — intentional lazy import
        except ImportError as e:
            logger.error("Stripe package not installed: %s", e)
            raise _StripeNotInstalled() from e
        _stripe_module = _stripe

    if not _stripe_configured:
        if settings.STRIPE_SECRET_KEY:
            _stripe_module.api_key = settings.STRIPE_SECRET_KEY
            _stripe_configured = True
        else:
            logger.warning("STRIPE_SECRET_KEY not set — Stripe calls will fail")
    return _stripe_module


# Type-checking-only alias so the rest of this file can keep its type hints
# referring to `stripe.X` without forcing the runtime import.
if TYPE_CHECKING:
    import stripe  # noqa: F401


# ---------------------------------------------------------------------------
# Tier + cycle definitions
# ---------------------------------------------------------------------------
# Tier monthly prices (USD cents) — used by the seed script and as a
# canonical reference. The actual prices in Stripe come back as Price IDs
# stored in the environment after seeding.

TIER_MONTHLY_CENTS = {
    "scout": 399_00,
    "growth": 599_00,
    "scale": 999_00,
}

# Annual prices = monthly × 12 × 0.90 (10% discount). Ends up as the
# whole-year amount charged once per year.
TIER_ANNUAL_CENTS = {
    "scout": 4_308_00,   # 359 × 12
    "growth": 6_468_00,  # 539 × 12
    "scale": 10_788_00,  # 899 × 12
}

INTEGRATION_FEE_CENTS = 3_000_00


# Credit top-up pack definitions. The Stripe Price IDs for these get
# created by the seed script and stored in env vars (STRIPE_PRICE_TOPUP_*).
TOPUP_PACKS = {
    "small": {"credits": 50, "amount_cents": 49_00, "label": "50 credits"},
    "medium": {"credits": 200, "amount_cents": 159_00, "label": "200 credits"},
    "large": {"credits": 500, "amount_cents": 349_00, "label": "500 credits"},
}


# Stripe Price IDs are populated from environment variables. Create the
# products in your Stripe account and paste the Price IDs into your .env.
def _price_id(env_key: str) -> str:
    """Read a Stripe Price ID from settings via the env, or empty string."""
    import os
    return os.environ.get(env_key, "")


def get_subscription_price_id(tier: str, cycle: str) -> str:
    """Return the Stripe Price ID for a tier + cycle combination.

    tier: scout | growth | scale
    cycle: monthly | annual
    """
    tier = tier.lower()
    cycle = cycle.lower()
    # "solo" = Individual;
    # "starter" = the fenced entry tier (2026-07-15).
    # Both are monthly-only self-serve.
    if tier not in ("starter", "solo", "scout", "growth", "scale"):
        raise ValueError(f"Unknown tier: {tier}")
    if tier in ("starter", "solo") and cycle != "monthly":
        cycle = "monthly"  # self-serve caps tiers are monthly-only
    if cycle not in ("monthly", "annual"):
        raise ValueError(f"Unknown cycle: {cycle}")
    env_key = f"STRIPE_PRICE_{tier.upper()}_{cycle.upper()}"
    price_id = _price_id(env_key)
    if not price_id:
        raise RuntimeError(
            f"Stripe Price ID not configured for {tier}/{cycle}. "
            f"Set {env_key} in your .env to your Stripe Price ID."
        )
    return price_id


def get_integration_fee_price_id() -> str:
    price_id = _price_id("STRIPE_PRICE_INTEGRATION_FEE")
    if not price_id:
        raise RuntimeError(
            "STRIPE_PRICE_INTEGRATION_FEE not set in your .env."
        )
    return price_id


def get_topup_price_id(pack: str) -> str:
    pack = pack.lower()
    if pack not in TOPUP_PACKS:
        raise ValueError(f"Unknown top-up pack: {pack}. Must be one of {list(TOPUP_PACKS)}")
    env_key = f"STRIPE_PRICE_TOPUP_{pack.upper()}"
    price_id = _price_id(env_key)
    if not price_id:
        raise RuntimeError(
            f"Stripe Price ID not configured for top-up pack '{pack}'. "
            f"Set {env_key} in your .env to your Stripe Price ID."
        )
    return price_id


# ---------------------------------------------------------------------------
# Stripe Customer creation / retrieval
# ---------------------------------------------------------------------------

def get_or_create_stripe_customer(
    db: Client,
    org_id: str,
    email: str,
    org_name: str,
) -> str:
    """Return the Stripe Customer ID for an org, creating one if needed.

    Looks up `organisations.stripe_customer_id`. If unset, creates a new
    Stripe Customer and persists the ID. Idempotent — safe to call on every
    checkout.
    """
    org_resp = (
        db.table("organisations")
        .select("stripe_customer_id, name")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    if not org_resp.data:
        raise ValueError(f"Organisation {org_id} not found")

    existing = org_resp.data[0].get("stripe_customer_id")
    if existing:
        return existing

    customer = _get_stripe().Customer.create(
        email=email,
        name=org_name or org_resp.data[0].get("name") or "Vera customer",
        metadata={"org_id": org_id},
    )
    customer_id = customer["id"]

    db.table("organisations").update({
        "stripe_customer_id": customer_id,
    }).eq("id", org_id).execute()

    logger.info("Created Stripe customer %s for org %s", customer_id, org_id)
    return customer_id


# ---------------------------------------------------------------------------
# Checkout session creation
# ---------------------------------------------------------------------------

def create_signup_checkout_session(
    db: Client,
    org_id: str,
    email: str,
    org_name: str,
    tier: str,
    cycle: str,
    success_url: Optional[str] = None,
    cancel_url: Optional[str] = None,
    trial_period_days: int = 7,
    purpose: str = "signup",
) -> Dict[str, Any]:
    """Create a Stripe Checkout session for a NEW signup.

    Subscription flow with a 7-day free trial (2026-07-23). The
    customer enters a card (card-required, so day-8 conversion is
    automatic and tyre-kickers are filtered), but Stripe collects £0
    today and shows the free-trial messaging on its hosted page — no
    Price/Product config needed, the session's trial_period_days is
    enough. Sub lands in `trialing` status, which the caps engine treats
    as full access; Stripe auto-bills on day 8, moving it to `active`.

    `trial_period_days=0` + `purpose="activation"` is the EXISTING-org
    variant (2026-08-25): an established account
    starting its paid subscription — bills the first invoice immediately,
    no trial. The subscription.created webhook flips the org to active
    via _persist_subscription (metadata.org_id); the "activation" purpose
    deliberately skips the signup handler's login-provisioning side
    effects on checkout.session.completed.

    Returns: {url, session_id}
    """
    customer_id = get_or_create_stripe_customer(db, org_id, email, org_name)
    subscription_price = get_subscription_price_id(tier, cycle)

    return_root = success_url or f"{settings.STRIPE_CHECKOUT_RETURN_URL}/billing/success"
    cancel = cancel_url or f"{settings.STRIPE_CHECKOUT_RETURN_URL}/billing/cancel"

    # Single subscription line item, billed today. First recurring
    # invoice fires immediately on checkout completion; subsequent
    # invoices fire monthly or annually depending on the cycle.
    #
    # adjustable_quantity lets the customer pick their seat count on
    # Stripe's hosted checkout page. Stripe bills N × price up-front,
    # sets subscription quantity = N, and the customer.subscription.*
    # webhooks mirror that N into organisations.seat_count. Cap at 20
    # — above that they should be on Enterprise (call-only).
    session = _get_stripe().checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        line_items=[{
            # Exactly one seat. Teams are call-only (2026-07-16): a
            # quantity stepper here sold "seats" that scaled no caps and
            # couldn't self-provision, which is chargeback bait.
            "price": subscription_price,
            "quantity": 1,
        }],
        subscription_data={
            # 7-day free trial (signup default). Stripe collects the card
            # but bills £0 today and renders "no charge today" on its
            # hosted checkout. Activation checkouts pass 0 → first invoice
            # bills immediately.
            **({"trial_period_days": trial_period_days} if trial_period_days else {}),
            "metadata": {
                "org_id": org_id,
                "tier": tier,
                "cycle": cycle,
            },
        },
        success_url=f"{return_root}?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=cancel,
        metadata={
            "org_id": org_id,
            "purpose": purpose,
            "tier": tier,
            "cycle": cycle,
        },
        allow_promotion_codes=True,
        # Stripe Tax — calculates VAT based on customer billing address.
        # UK customer → +20%, EU B2B with VAT number → reverse charge,
        # US/RoW → no VAT. Requires the Stripe Tax toggle on the
        # dashboard + the seller's VAT number registered there.
        automatic_tax={"enabled": True},
        # Tell Stripe to write the billing address back onto the
        # customer record after checkout — needed for tax calculation
        # on the recurring renewal invoices and for compliant invoices.
        customer_update={"address": "auto", "name": "auto"},
        # Let B2B customers paste their VAT number for reverse charge.
        tax_id_collection={"enabled": True},
        # Required for Stripe Tax to determine residency reliably.
        billing_address_collection="required",
    )

    logger.info("Created %s checkout session %s for org %s (%s/%s)",
                purpose, session["id"], org_id, tier, cycle)
    return {"url": session["url"], "session_id": session["id"]}


def start_signup_for_new_customer(
    db: Client,
    email: str,
    company_name: str,
    tier: str,
    cycle: str,
    success_url: Optional[str] = None,
    cancel_url: Optional[str] = None,
    full_name: str = "",
) -> Dict[str, Any]:
    """Self-serve signup entry point for the marketing pricing page.

    Brand-new customer hits /pricing → slides on a plan → modal asks for
    email + company name → submits here. Creates an organisation row in
    `pending_payment` state, then initiates Stripe Checkout. The webhook
    flips the org to active when payment confirms and invites the user
    via Supabase (sends them a magic-link login email).

    Returns: {checkout_url, org_id, session_id}
    """
    tier = tier.lower()
    cycle = cycle.lower()
    monthly_limit = TIER_LIMITS.get(tier, 100)

    # An email with an existing login = an existing customer. Block HERE,
    # before Stripe — otherwise they pay first and only hit "user already
    # exists" at the set-password step (2026-07-15). Pending-payment
    # retries are unaffected: their auth user is only created at
    # complete-signup, after payment. Check is best-effort — an auth-API
    # blip must not block a genuinely new signup.
    email_lower = email.lower().strip()
    try:
        for user in (db.auth.admin.list_users() or []):
            if (getattr(user, "email", "") or "").lower() == email_lower:
                raise HTTPException(
                    status_code=409,
                    detail="This email address is already associated with an account. Please log in.",
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("start-signup: auth email check failed (%s) — continuing", e)

    # Vera Solo: seed the feature preset so the customer gets instant
    # access to the full dashboard the moment they pay. ICP config is
    # refined later (wizard / onboarding call); the preset is enough to
    # log in and research their first account via quick-enrich.
    # Every new customer is required to set up MFA on first login
    # (mfa_required gate in the dashboard). Existing orgs don't carry this
    # flag, so they're rolled out manually. Solo also gets its feature preset.
    new_icp_config: Dict[str, Any] = {"mfa_required": True}
    contact_name = (full_name or "").strip()
    if contact_name:
        # Stored so the welcome email + dashboard can greet the person by
        # name (we can't reliably derive it from their email address).
        new_icp_config["contact_name"] = contact_name
    if tier == "solo":
        from app.services.plan_limits import SOLO_FEATURES
        new_icp_config["features"] = dict(SOLO_FEATURES)
    elif tier == "starter":
        from app.services.plan_limits import STARTER_FEATURES
        new_icp_config["features"] = dict(STARTER_FEATURES)
    # Bind the pending org to the signer's email so a repeat submit by the
    # SAME person reuses it, but a DIFFERENT person entering the same
    # company name can never join their pending org (they'd both become
    # owners at complete-signup otherwise).
    new_icp_config["signup_email"] = email_lower

    # Reuse an existing pending_payment org only when BOTH the company name
    # and the signup email match — scoping to email closes the co-owner hole.
    existing = (
        db.table("organisations")
        .select("id, subscription_status")
        .eq("name", company_name)
        .eq("subscription_status", "pending_payment")
        .filter("icp_config->>signup_email", "eq", email_lower)
        .limit(1)
        .execute()
    )
    if existing.data:
        org_id = existing.data[0]["id"]
        logger.info("Reusing pending_payment org %s for repeat signup (%s)", org_id, email)
    else:
        insert_resp = (
            db.table("organisations")
            .insert({
                "name": company_name,
                "industry": "cybersecurity",  # default — customer adjusts in settings
                "tier": tier,
                "subscription_tier": tier,
                "subscription_cycle": cycle,
                "subscription_status": "pending_payment",
                "monthly_enrichment_limit": monthly_limit,
                "billing_cycle_start": 1,
                "icp_config": new_icp_config,
            })
            .execute()
        )
        if not insert_resp.data:
            raise RuntimeError("Failed to create organisation row for signup")
        org_id = insert_resp.data[0]["id"]
        logger.info("Created pending_payment org %s for %s (%s/%s)", org_id, email, tier, cycle)

    # Hand off to the existing checkout-session builder.
    checkout = create_signup_checkout_session(
        db=db,
        org_id=org_id,
        email=email,
        org_name=company_name,
        tier=tier,
        cycle=cycle,
        success_url=success_url,
        cancel_url=cancel_url,
    )

    return {
        "checkout_url": checkout["url"],
        "session_id": checkout["session_id"],
        "org_id": org_id,
    }


# Per-tier monthly enrichment caps. Mirrors the figures published on /pricing.
# (The caps tiers' real limits live in plan_limits.py; these keep the org
# row's monthly_limit column honest.)
TIER_LIMITS = {
    "starter": 75,
    "solo": 150,
    "scout": 100,
    "growth": 300,
    "scale": 10_000,  # "unlimited" in marketing — cap at 10k to detect runaway use
}


def create_topup_checkout_session(
    db: Client,
    org_id: str,
    email: str,
    org_name: str,
    pack: str,
    success_url: Optional[str] = None,
    cancel_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a one-time Stripe Checkout session to top up enrichment credits.

    pack: 'small' | 'medium' | 'large' (see TOPUP_PACKS)

    On successful payment, the webhook handler adds the credits to the org's
    usage_tracking row for the current month and writes a row to credit_topups.

    Returns: {url, session_id, pack, credits, amount_cents}
    """
    # Credit top-ups are a credit-tier feature. The caps tiers
    # (solo/starter) have no credit balance to top up, so refuse
    # server-side. Mirrors the Solo guard in set_solo_capacity; the
    # dashboard already hides this for caps tiers, this is the
    # defence-in-depth against a stale link or direct API call.
    from app.services.plan_limits import _org_is_capped
    if _org_is_capped(org_id, db):
        raise RuntimeError(
            "Credit top-ups aren't available on your plan — upgrade or "
            "adjust capacity from Settings instead."
        )

    if pack not in TOPUP_PACKS:
        raise ValueError(f"Unknown top-up pack: {pack}")

    customer_id = get_or_create_stripe_customer(db, org_id, email, org_name)
    price_id = get_topup_price_id(pack)
    pack_info = TOPUP_PACKS[pack]

    return_root = success_url or f"{settings.STRIPE_CHECKOUT_RETURN_URL}/billing/success"
    cancel = cancel_url or f"{settings.STRIPE_CHECKOUT_RETURN_URL}/billing/cancel"

    session = _get_stripe().checkout.Session.create(
        customer=customer_id,
        mode="payment",
        line_items=[
            {"price": price_id, "quantity": 1},
        ],
        success_url=f"{return_root}?session_id={{CHECKOUT_SESSION_ID}}&topup={pack}",
        cancel_url=cancel,
        metadata={
            "org_id": org_id,
            "purpose": "topup",
            "pack": pack,
            "credits": str(pack_info["credits"]),
        },
        # Same Stripe Tax setup as the signup checkout — top-ups for
        # UK customers get 20% VAT added, EU B2B reverse-charged, etc.
        automatic_tax={"enabled": True},
        customer_update={"address": "auto", "name": "auto"},
        tax_id_collection={"enabled": True},
        billing_address_collection="required",
    )

    # Pre-record the top-up as 'pending'. The webhook flips it to 'completed'
    # on payment success and adds the credits to usage_tracking. This gives
    # us a paper trail even if the customer abandons checkout.
    db.table("credit_topups").insert({
        "org_id": org_id,
        "stripe_checkout_session_id": session["id"],
        "credits": pack_info["credits"],
        "amount_cents": pack_info["amount_cents"],
        "currency": "usd",
        "status": "pending",
    }).execute()

    logger.info("Created top-up checkout session %s for org %s (%s)",
                session["id"], org_id, pack)
    return {
        "url": session["url"],
        "session_id": session["id"],
        "pack": pack,
        "credits": pack_info["credits"],
        "amount_cents": pack_info["amount_cents"],
    }


def create_customer_portal_session(
    db: Client,
    org_id: str,
    return_url: Optional[str] = None,
) -> str:
    """Create a Stripe Customer Portal session URL — the prebuilt page where
    customers manage their subscription, view invoices, update card on file,
    cancel, etc. Stripe-hosted, no UI work required on our side.

    Returns the URL the customer should be redirected to.
    """
    org_resp = (
        db.table("organisations")
        .select("stripe_customer_id")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    if not org_resp.data:
        raise ValueError(f"Organisation {org_id} not found")
    customer_id = org_resp.data[0].get("stripe_customer_id")
    if not customer_id:
        raise RuntimeError("This organisation has no Stripe customer yet — they haven't completed checkout.")

    portal = _get_stripe().billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url or settings.STRIPE_CHECKOUT_RETURN_URL,
    )
    return portal["url"]


def cancel_subscription_at_period_end(
    db: Client,
    org_id: str,
    reasons: Optional[list] = None,
    details: Optional[str] = None,
) -> Dict[str, Any]:
    """Cancel an org's subscription at the end of the current period (they
    keep access through what they paid for) and record the churn feedback.

    Feedback is stored first — even if the Stripe call fails we keep the
    reasons. Returns {ok, cancel_at_period_end, current_period_end}.
    """
    org_resp = (
        db.table("organisations")
        .select("stripe_subscription_id, tier")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    if not org_resp.data:
        raise ValueError(f"Organisation {org_id} not found")
    row = org_resp.data[0]

    # Record feedback first (best-effort) so we never lose the signal.
    try:
        db.table("cancellation_feedback").insert({
            "org_id": org_id,
            "reasons": reasons or [],
            "details": (details or "").strip() or None,
            "tier": row.get("tier"),
        }).execute()
    except Exception as e:
        logger.error("Failed to store cancellation feedback for %s: %s", org_id, e)

    sub_id = row.get("stripe_subscription_id")
    if not sub_id:
        raise RuntimeError("No active subscription to cancel.")

    sub = _get_stripe().Subscription.modify(sub_id, cancel_at_period_end=True)
    return {
        "ok": True,
        "cancel_at_period_end": True,
        "current_period_end": sub.get("current_period_end"),
    }


# One-time 50%-off coupon used as a churn save. Created on first use and
# reused thereafter (the id is stable, so Stripe only ever holds one).
RETENTION_COUPON_ID = "vera_retention_50_once"


def _ensure_retention_coupon() -> str:
    """Idempotently ensure the one-time 50%-off coupon exists. Returns its id."""
    stripe_mod = _get_stripe()
    try:
        stripe_mod.Coupon.retrieve(RETENTION_COUPON_ID)
    except Exception:
        stripe_mod.Coupon.create(
            id=RETENTION_COUPON_ID,
            percent_off=50,
            duration="once",
            name="Vera retention — 50% off next invoice",
        )
    return RETENTION_COUPON_ID


def apply_retention_discount(
    db: Client,
    org_id: str,
    reasons: Optional[list] = None,
    details: Optional[str] = None,
) -> Dict[str, Any]:
    """Churn save: apply a one-time 50%-off coupon to the org's next invoice,
    record the feedback, and mark the offer as redeemed so it can't be reused.

    Feedback is stored first (we want the "why" even when they stay). Raises
    RuntimeError if the offer was already used or there's no subscription.
    """
    org_resp = (
        db.table("organisations")
        .select("stripe_subscription_id, tier, retention_discount_redeemed_at")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    if not org_resp.data:
        raise ValueError(f"Organisation {org_id} not found")
    row = org_resp.data[0]

    # Record feedback first (best-effort) so we never lose the signal.
    try:
        db.table("cancellation_feedback").insert({
            "org_id": org_id,
            "reasons": reasons or [],
            "details": (details or "").strip() or None,
            "tier": row.get("tier"),
        }).execute()
    except Exception as e:
        logger.error("Failed to store retention feedback for %s: %s", org_id, e)

    if row.get("retention_discount_redeemed_at"):
        raise RuntimeError(
            "This retention offer has already been used on your account."
        )

    sub_id = row.get("stripe_subscription_id")
    if not sub_id:
        raise RuntimeError("No active subscription to apply the discount to.")

    coupon_id = _ensure_retention_coupon()
    _get_stripe().Subscription.modify(sub_id, coupon=coupon_id)

    db.table("organisations").update(
        {"retention_discount_redeemed_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", org_id).execute()

    return {"ok": True, "percent_off": 50, "duration": "once"}


def solo_price_packs() -> Dict[str, int]:
    """Map each Solo tier's Stripe price_id → its capacity level:
    0 = base (150 research/mo, 25 watched), 1/2/3 = +N capacity packs.
    Each pack adds +20 research/mo, +12 watched, +15 unlocks, +15 people (see
    plan_limits.CAPACITY_PER_PACK). Tiers a user can switch between, not stacked
    items. Pack prices live in Stripe (STRIPE_PRICE_SOLO_PLUS{n}).
    """
    out: Dict[str, int] = {}
    base = _price_id("STRIPE_PRICE_SOLO_MONTHLY")
    if base:
        out[base] = 0
    for n in (1, 2, 3):
        pid = _price_id(f"STRIPE_PRICE_SOLO_PLUS{n}")
        if pid:
            out[pid] = n
    return out


def set_solo_capacity(db: Client, org_id: str, target_packs: int) -> Dict[str, Any]:
    """Move a Vera Solo subscription to a capacity tier (0-3) by switching
    its price. One click can jump straight from 40 → 100. Stripe prorates
    onto the next invoice. Returns the new pack level.
    """
    from app.services.plan_limits import MAX_CAPACITY_PACKS

    target = max(0, min(MAX_CAPACITY_PACKS, int(target_packs)))
    price_map = solo_price_packs()
    target_price = next((pid for pid, n in price_map.items() if n == target), None)
    if not target_price:
        raise RuntimeError("That plan tier isn't configured in Stripe yet.")

    org_resp = (
        db.table("organisations")
        .select("stripe_subscription_id, capacity_packs, tier")
        .eq("id", org_id).limit(1).execute()
    )
    if not org_resp.data:
        raise ValueError(f"Organisation {org_id} not found")
    row = org_resp.data[0]
    if (row.get("tier") or "").lower() != "solo":
        raise RuntimeError("Capacity packs are an Individual plan feature.")
    sub_id = row.get("stripe_subscription_id")
    if not sub_id:
        raise RuntimeError("No active subscription to change.")

    current = max(0, int(row.get("capacity_packs") or 0))
    if target == current:
        return {"ok": True, "capacity_packs": current, "unchanged": True}

    stripe = _get_stripe()
    sub = stripe.Subscription.retrieve(sub_id)
    items = (sub.get("items") or {}).get("data") or []
    # The Solo line item is whichever item is on one of our Solo tier prices.
    solo_item = next(
        (it for it in items if (it.get("price") or {}).get("id") in price_map), None
    )
    if solo_item is None:
        solo_item = items[0] if items else None
    if solo_item is None:
        raise RuntimeError("Couldn't find the Solo line item on the subscription.")

    # Upgrades charge the prorated difference IMMEDIATELY (always_invoice) so
    # capacity can't be "rented" cheaply — upgrade, burn enrichment spend,
    # then downgrade before the next bill. Downgrades give no refund (none),
    # killing any upgrade→use→refund loop. They pay for what they take.
    proration = "always_invoice" if target > current else "none"
    stripe.SubscriptionItem.modify(
        solo_item["id"], price=target_price, proration_behavior=proration,
    )

    # Optimistic write — the customer.subscription.updated webhook confirms.
    db.table("organisations").update({"capacity_packs": target}).eq("id", org_id).execute()

    # Bring the workspace in line with the new tier: a downgrade archives
    # the newest over-cap accounts + pauses excess monitoring (data kept);
    # an upgrade restores archived accounts. Best-effort — never block the
    # billing change on a reconcile hiccup.
    reconciled = {"archived": 0, "paused": 0}
    try:
        from app.services.plan_limits import reconcile_solo_caps
        reconciled = reconcile_solo_caps(org_id, target, db)
    except Exception as e:
        logger.error("Solo cap reconcile failed for %s: %s", org_id, e)

    logger.info(
        "Solo capacity for org %s: tier %d -> %d (proration=%s, %s)",
        org_id, current, target, proration, reconciled,
    )
    return {"ok": True, "capacity_packs": target, **reconciled}


def delete_account(db: Client, org_id: str) -> Dict[str, Any]:
    """Permanently erase an organisation and everything personal attached
    to it — GDPR right-to-erasure, the self-serve "Delete account" flow.

    This is NOT cancellation (which keeps the data, just stops billing).
    It deletes the Stripe customer (cancelling any subscription), then
    atomically removes every org-scoped row + any auth users left with no
    other org, via the delete_org_account DB function.

    Stripe is best-effort: a billing-side hiccup must not block the
    customer's right to have their data erased.
    """
    org_resp = (
        db.table("organisations")
        .select("stripe_customer_id, name")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    if not org_resp.data:
        raise HTTPException(status_code=404, detail="Organisation not found.")
    customer_id = org_resp.data[0].get("stripe_customer_id")

    # 1. Delete the Stripe customer (cancels subscriptions, removes their
    #    card + billing details). Best-effort.
    stripe_result = "no_customer"
    if customer_id:
        try:
            _get_stripe().Customer.delete(customer_id)
            stripe_result = "deleted"
        except Exception as e:
            logger.error("delete-account: Stripe customer delete failed for %s (%s): %s", org_id, customer_id, e)
            stripe_result = "failed"

    # 2. Atomic data + auth erasure.
    rpc = db.rpc("delete_org_account", {"p_org_id": org_id}).execute()
    detail = getattr(rpc, "data", None)

    logger.info("delete-account: org %s erased (stripe=%s, detail=%s)", org_id, stripe_result, detail)
    return {"deleted": True, "stripe": stripe_result, "detail": detail}


# ---------------------------------------------------------------------------
# Webhook handlers
# ---------------------------------------------------------------------------

def verify_webhook_signature(payload: bytes, signature: str):
    """Verify a Stripe webhook signature and return the parsed event.

    Raises stripe.error.SignatureVerificationError if invalid.
    """
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET not set — webhook verification cannot proceed")
    stripe_mod = _get_stripe()
    return stripe_mod.Webhook.construct_event(
        payload=payload,
        sig_header=signature,
        secret=settings.STRIPE_WEBHOOK_SECRET,
    )


def handle_webhook_event(db: Client, event) -> Dict[str, Any]:
    """Dispatch a Stripe webhook event to the right handler. Idempotent —
    each event_type's handler tolerates being called multiple times for the
    same Stripe object (Stripe retries on non-2xx responses).
    """
    event_type = event["type"]
    data_object = event["data"]["object"]

    logger.info("Stripe webhook: type=%s id=%s", event_type, event.get("id"))

    if event_type == "checkout.session.completed":
        return _handle_checkout_completed(db, data_object)
    if event_type == "customer.subscription.created":
        return _handle_subscription_created(db, data_object)
    if event_type == "customer.subscription.updated":
        return _handle_subscription_updated(db, data_object)
    if event_type == "customer.subscription.trial_will_end":
        return _handle_trial_will_end(db, data_object)
    if event_type == "customer.subscription.deleted":
        return _handle_subscription_deleted(db, data_object)
    if event_type == "invoice.paid":
        return _handle_invoice_paid(db, data_object)
    if event_type == "invoice.payment_failed":
        return _handle_invoice_failed(db, data_object)

    logger.info("Stripe webhook: ignoring unhandled event type %s", event_type)
    return {"status": "ignored", "type": event_type}


def _handle_checkout_completed(db: Client, session: Dict[str, Any]) -> Dict[str, Any]:
    """A Checkout session completed — either signup or top-up. Branch on
    metadata.purpose."""
    metadata = session.get("metadata") or {}
    purpose = metadata.get("purpose")
    org_id = metadata.get("org_id")

    if not org_id:
        logger.warning("checkout.session.completed without org_id metadata: %s", session.get("id"))
        return {"status": "no_org_id"}

    if purpose == "signup":
        # Mark the integration fee as paid; subscription creation handled by
        # subsequent customer.subscription.created event
        now = datetime.now(timezone.utc).isoformat()
        db.table("organisations").update({
            "integration_fee_paid_at": now,
            "subscription_tier": metadata.get("tier"),
            "subscription_cycle": metadata.get("cycle"),
            "subscription_status": "trialing",  # 90-day calibration trial
            "updated_at": now,
        }).eq("id", org_id).execute()
        logger.info("Marked integration fee paid for org %s", org_id)

        # Provision the customer's login: invite their email via Supabase
        # admin (sends a magic-link / password-setup email) and link the
        # resulting auth user to the org via org_users. Best-effort —
        # failures here don't break the webhook response; Stripe still
        # gets a 200, customer can be manually onboarded if email bounces.
        try:
            customer_details = session.get("customer_details") or {}
            customer_email = customer_details.get("email") or session.get("customer_email")
            if customer_email:
                _provision_customer_login(db, org_id, customer_email, is_owner_signup=True)
        except Exception as e:
            logger.warning("Failed to provision login for org %s: %s", org_id, e)

        return {"status": "signup_completed", "org_id": org_id}

    if purpose == "topup":
        return _apply_topup_credits(db, session)

    if purpose == "activation":
        # Existing org starting its paid subscription (payment_required
        # gate). Optimistic flip so the gate lifts the moment checkout
        # completes; the subscription.created webhook confirms with the
        # real Stripe status via _persist_subscription. No login
        # provisioning — the org's users already exist.
        now = datetime.now(timezone.utc).isoformat()
        db.table("organisations").update({
            "subscription_status": "active",
            "subscription_tier": metadata.get("tier"),
            "subscription_cycle": metadata.get("cycle"),
            "updated_at": now,
        }).eq("id", org_id).execute()
        logger.info("Activation checkout completed for org %s", org_id)
        return {"status": "activation_completed", "org_id": org_id}

    logger.warning("checkout.session.completed with unknown purpose: %s", purpose)
    return {"status": "unknown_purpose", "purpose": purpose}


def _ensure_customer_provisioned(
    db: Client, org_id: str, email: str
) -> "tuple[Optional[str], bool, bool]":
    """Create the customer's auth user (unconfirmed) and link them to the
    org as owner. Sends NO email — callers decide that.

    Returns (auth_user_id, user_existed_before, already_linked). Idempotent:
    safe to call from the webhook AND from the synchronous /complete-signup
    path, in either order.
    """
    email_lower = email.lower().strip()

    # 1. Resolve or create the auth user.
    existing = db.auth.admin.list_users()
    auth_user_id: Optional[str] = None
    user_existed_before = False
    for user in (existing or []):
        if (getattr(user, "email", "") or "").lower() == email_lower:
            auth_user_id = user.id
            user_existed_before = True
            break

    if auth_user_id is None:
        # email_confirm=False so the user is created but not yet confirmed —
        # they confirm by setting a password (welcome page or magic link).
        try:
            created = db.auth.admin.create_user({
                "email": email_lower,
                "email_confirm": False,
            })
        except Exception as e:
            # Some versions of supabase-py expect positional args; retry
            logger.debug("create_user kwargs form failed (%s) — retrying alt shape", e)
            created = db.auth.admin.create_user({"email": email_lower})
        user_obj = getattr(created, "user", None) or (
            created.get("user") if isinstance(created, dict) else None
        )
        if user_obj:
            auth_user_id = getattr(user_obj, "id", None) or user_obj.get("id")
        logger.info("Created auth user %s for org %s", email_lower, org_id)

    if not auth_user_id:
        logger.warning("Could not resolve auth user for %s — skipping org_users link", email_lower)
        return None, user_existed_before, False

    # 2. Link user → org (idempotent).
    existing_link = (
        db.table("org_users")
        .select("id")
        .eq("user_id", auth_user_id)
        .eq("org_id", org_id)
        .limit(1)
        .execute()
    )
    already_linked = bool(existing_link.data)
    if not already_linked:
        db.table("org_users").insert({
            "user_id": auth_user_id,
            "org_id": org_id,
            "email": email_lower,
            "role": "owner",
        }).execute()
        logger.info("Linked %s as owner of org %s", email_lower, org_id)
    else:
        logger.info("org_users link already exists for %s → %s", email_lower, org_id)

    return auth_user_id, user_existed_before, already_linked


def complete_signup_set_password(
    db: Client, session_id: str, password: str
) -> Dict[str, Any]:
    """Set the customer's password immediately after checkout — no inbox detour.

    The /welcome page calls this with the Stripe Checkout session_id (proof of
    payment, carried in the success URL) and the password the customer just
    chose. We verify the session was genuinely paid, provision the org + auth
    user idempotently (covers the webhook not having landed yet), then set the
    password and confirm the account so the customer can sign straight in.

    Anti-replay: only works while the account is still unconfirmed. Once a
    password has been set, replaying the session_id returns 409 — a leaked
    success URL can't reset an active owner's password.
    """
    if not password or len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    # 1. Verify the session with Stripe — this is the proof of payment.
    try:
        session = _get_stripe().checkout.Session.retrieve(session_id)
    except Exception as e:
        logger.warning("complete-signup: could not retrieve session %s: %s", session_id, e)
        raise HTTPException(status_code=404, detail="Checkout session not found.")

    metadata = session.get("metadata") or {}
    if metadata.get("purpose") != "signup":
        raise HTTPException(status_code=400, detail="This isn't a signup session.")
    # 100%-off coupons settle as 'no_payment_required'; both mean "done".
    if session.get("payment_status") not in ("paid", "no_payment_required"):
        raise HTTPException(status_code=402, detail="Payment hasn't completed for this session yet.")

    org_id = metadata.get("org_id")
    customer_details = session.get("customer_details") or {}
    email = (customer_details.get("email") or session.get("customer_email") or "").lower().strip()
    if not org_id or not email:
        raise HTTPException(status_code=400, detail="Session is missing org or email.")

    # 2. Provision org + user idempotently (in case the webhook hasn't fired).
    auth_user_id, _existed, _linked = _ensure_customer_provisioned(db, org_id, email)
    if not auth_user_id:
        raise HTTPException(
            status_code=500,
            detail="Could not set up your account automatically — check your email for a sign-in link.",
        )

    # 3. Anti-replay guard: only set a password while the account is unconfirmed.
    user = db.auth.admin.get_user_by_id(auth_user_id)
    user_obj = getattr(user, "user", None) or (user.get("user") if isinstance(user, dict) else user)
    confirmed_at = (
        getattr(user_obj, "email_confirmed_at", None)
        or (user_obj.get("email_confirmed_at") if isinstance(user_obj, dict) else None)
    )
    if confirmed_at:
        raise HTTPException(status_code=409, detail="This account is already set up — please log in instead.")

    # 4. Set the password + confirm the account.
    db.auth.admin.update_user_by_id(auth_user_id, {"password": password, "email_confirm": True})

    # 5. Flip the org pending_payment → trialing (only if still pending, so a
    #    replay or a late webhook can never revert an active org).
    now = datetime.now(timezone.utc).isoformat()
    db.table("organisations").update({
        "subscription_status": "trialing",
        "integration_fee_paid_at": now,
        "updated_at": now,
    }).eq("id", org_id).eq("subscription_status", "pending_payment").execute()

    logger.info("complete-signup: password set + confirmed for %s (org %s)", email, org_id)
    return {"email": email, "org_id": org_id}


def _provision_customer_login(
    db: Client, org_id: str, email: str, *, is_owner_signup: bool = False
) -> None:
    """Create the customer's auth user silently, link them to the org, and
    fire our own branded welcome email via Resend.

    Two callers share this helper:
      - the Stripe signup webhook (is_owner_signup=True) — the org owner
      - the admin teammate-invite endpoint (is_owner_signup=False)

    The welcome email always carries the plan summary + setup-call CTA. Its
    PRIMARY CTA depends on whether a password is already set, not on tier:
      - password not set yet  → magic-link "set your password" button
        (owner closed the /welcome tab, webhook beat them to it, or a
        freshly-invited teammate)
      - password already set  → plain "sign in" link (Solo's happy path,
        where the password was chosen on /welcome)

    So a confirmed owner STILL gets their welcome (we no longer skip on
    confirmation) — that closes the gap where a fast /welcome password-set
    meant no welcome email at all.

    Idempotency:
      - owner signup is sent exactly once per org, guarded by the
        icp_config.welcome_email_sent_at marker (dedupes Stripe redeliveries
        and the /welcome-then-webdhook race)
      - teammate invites keep the prior confirmed / already-provisioned
        skips so re-invites don't re-mail an active user

    We deliberately do NOT use Supabase's invite_user_by_email on the happy
    path — it triggers Supabase's unbranded template. We only fall back to
    it if generate_link fails, so the customer is never stranded.
    """
    from app.config import settings  # local to keep import cycles clean
    from app.services.email_sender import send_welcome_email, send_founder_email

    email_lower = email.lower().strip()

    # Pull org context for the welcome email — company_name + tier + cycle
    # are used to render per-tier copy.
    org_resp = (
        db.table("organisations")
        .select("name, subscription_tier, subscription_cycle, icp_config")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    org_row = org_resp.data[0] if org_resp.data else {}
    company_name = (org_row.get("name") or "").strip() or "your team"
    # Solo is the only live self-serve tier — fall back to it (not the
    # retired "scout") if the column is somehow unset.
    tier = (org_row.get("subscription_tier") or "solo").lower()
    cycle = (org_row.get("subscription_cycle") or "monthly").lower()
    icp_config = org_row.get("icp_config") or {}
    contact_name = (icp_config.get("contact_name") or "").strip()

    # Owner welcome is sent exactly once per org. This marker is the single
    # source of truth: it dedupes Stripe webhook redeliveries AND the case
    # where the customer set their password on /welcome before the webhook
    # landed. Teammate invites skip this — they must send every time.
    if is_owner_signup and icp_config.get("welcome_email_sent_at"):
        logger.info("Skipping owner welcome for %s — already sent", email_lower)
        return

    # 1. Resolve or create the auth user + link to org (idempotent).
    auth_user_id, user_existed_before, already_linked = _ensure_customer_provisioned(
        db, org_id, email_lower
    )
    if not auth_user_id:
        logger.warning("Could not resolve auth user for %s — skipping welcome email", email_lower)
        return

    # 2. Has the customer already chosen a password (confirmed via /welcome)?
    #    Drives the email's primary CTA. We do NOT skip the owner email when
    #    confirmed — they still get their welcome + setup-call.
    password_already_set = False
    try:
        user = db.auth.admin.get_user_by_id(auth_user_id)
        user_obj = getattr(user, "user", None) or (user.get("user") if isinstance(user, dict) else user)
        confirmed_at = (
            getattr(user_obj, "email_confirmed_at", None)
            or (user_obj.get("email_confirmed_at") if isinstance(user_obj, dict) else None)
        )
        password_already_set = bool(confirmed_at)
    except Exception as e:
        logger.debug("Could not check confirmation state for %s (%s) — assuming password not set", email_lower, e)

    # 3. Teammate invites keep the prior skips so a re-invite never re-mails
    #    an already-active or already-provisioned user.
    if not is_owner_signup:
        if password_already_set:
            logger.info("Skipping invite for %s — already confirmed", email_lower)
            return
        if user_existed_before and already_linked:
            logger.info("Skipping invite for %s — already provisioned", email_lower)
            return

    # 4. Mint a password-setup magic URL ONLY when one is still needed. For an
    #    already-confirmed customer the invite link is useless (and can error),
    #    so the email falls back to a plain sign-in link instead.
    magic_link = ""
    if not password_already_set:
        try:
            link_response = db.auth.admin.generate_link({
                "type": "invite",
                "email": email_lower,
                "options": {
                    # /reset-password is the existing UX for "auth token →
                    # set a password → in" — same shape whether the user
                    # was invited or is resetting. Routes them to /dashboard
                    # on success.
                    "redirect_to": f"{settings.DASHBOARD_URL}/reset-password",
                },
            })
            # supabase-py returns properties.action_link on the response object
            properties = getattr(link_response, "properties", None) or (
                link_response.get("properties") if isinstance(link_response, dict) else None
            )
            if properties:
                magic_link = (
                    getattr(properties, "action_link", None)
                    or (properties.get("action_link") if isinstance(properties, dict) else None)
                ) or ""
        except Exception as e:
            logger.exception("generate_link failed for %s — falling back to invite_user_by_email so customer at least gets the default Supabase email: %s", email_lower, e)
            # Fallback path: don't strand the customer. Fire Supabase's
            # default invite email so they CAN get in, even if it's ugly.
            try:
                db.auth.admin.invite_user_by_email(email_lower)
            except Exception as e2:
                logger.exception("Fallback invite_user_by_email also failed for %s: %s", email_lower, e2)
            return

        if not magic_link:
            logger.warning("Magic link missing from generate_link response for %s — skipping welcome email", email_lower)
            return

    # 5. Send our branded welcome email via Resend, then stamp the marker so
    #    an owner welcome can never double-send on a webhook redelivery.
    try:
        send_welcome_email(
            to_email=email_lower,
            company_name=company_name,
            tier=tier,
            cycle=cycle,
            magic_link=magic_link,
            contact_name=contact_name,
            needs_password=not password_already_set,
        )
        if is_owner_signup:
            # Personal founder follow-up email, scheduled ~10 min out via
            # Resend. Best-effort — a scheduling failure must not block the
            # marker (the welcome already sent). Owner signups only.
            try:
                from datetime import timedelta
                scheduled_at = (
                    datetime.now(timezone.utc) + timedelta(minutes=10)
                ).isoformat()
                send_founder_email(
                    to_email=email_lower,
                    contact_name=contact_name,
                    company_name=company_name,
                    scheduled_at=scheduled_at,
                )
            except Exception as e:
                logger.warning("Founder follow-up schedule failed for %s: %s", email_lower, e)

            # Internal heads-up to the operator (OPERATOR_EMAIL) on every
            # paid owner signup. Best-effort — never
            # blocks the welcome flow.
            if settings.OPERATOR_EMAIL:
                try:
                    from app.services.email_sender import _send_via_resend
                    _send_via_resend(
                        to_email=settings.OPERATOR_EMAIL,
                        subject=f"New Vera signup: {company_name} ({tier})",
                        html=(
                            "<div style=\"font-family:Arial,sans-serif;font-size:14px\">"
                            f"<p>{company_name} just completed a paid signup.</p>"
                            f"<p>Tier: {tier} · Cycle: {cycle}<br>"
                            f"Owner: {contact_name or 'unknown'} · {email_lower}</p>"
                            "</div>"
                        ),
                        text=(
                            f"{company_name} just completed a paid signup.\n"
                            f"Tier: {tier} · Cycle: {cycle}\n"
                            f"Owner: {contact_name or 'unknown'} · {email_lower}"
                        ),
                    )
                except Exception as e:
                    logger.warning("Signup notification to operator failed: %s", e)

            db.table("organisations").update(
                {"icp_config": {**icp_config, "welcome_email_sent_at": datetime.now(timezone.utc).isoformat()}}
            ).eq("id", org_id).execute()
    except Exception as e:
        logger.exception("Welcome email failed to send for %s: %s", email_lower, e)
        # Customer is still provisioned in the DB — they just didn't get
        # the email. Manual recovery: re-run this function or call
        # admin.invite_user_by_email() to fall back to Supabase template.


def _apply_topup_credits(db: Client, session: Dict[str, Any]) -> Dict[str, Any]:
    """Apply credit top-up — bump enrichments_limit on usage_tracking and
    flip the credit_topups row to 'completed'. Idempotent: if the topup row
    is already completed, this is a no-op."""
    session_id = session.get("id")
    metadata = session.get("metadata") or {}
    org_id = metadata.get("org_id")
    credits = int(metadata.get("credits") or 0)

    if not org_id or credits <= 0:
        logger.warning("Top-up missing org_id or credits: %s", session_id)
        return {"status": "missing_data"}

    # Idempotency check — has this top-up already been applied?
    topup_resp = (
        db.table("credit_topups")
        .select("id, status, completed_at")
        .eq("stripe_checkout_session_id", session_id)
        .limit(1)
        .execute()
    )
    if not topup_resp.data:
        logger.warning("Top-up session %s has no credit_topups row", session_id)
        return {"status": "no_topup_row"}
    if topup_resp.data[0].get("status") == "completed":
        logger.info("Top-up %s already completed — idempotent skip", session_id)
        return {"status": "already_completed"}

    # Bump the org's enrichments_limit for the current month
    from app.services.usage import _current_month, get_or_create_usage
    get_or_create_usage(db, org_id)  # ensures the current month's row exists
    month = _current_month()
    current = (
        db.table("usage_tracking")
        .select("id, enrichments_limit")
        .eq("org_id", org_id)
        .eq("month_year", month)
        .limit(1)
        .execute()
    )
    if current.data:
        new_limit = (current.data[0].get("enrichments_limit") or 0) + credits
        db.table("usage_tracking").update({
            "enrichments_limit": new_limit,
        }).eq("id", current.data[0]["id"]).execute()

    # Flip the topup row to completed
    db.table("credit_topups").update({
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "stripe_payment_intent_id": session.get("payment_intent"),
    }).eq("stripe_checkout_session_id", session_id).execute()

    logger.info("Applied %d credits to org %s (session %s)", credits, org_id, session_id)
    return {"status": "applied", "credits": credits, "org_id": org_id}


def _handle_subscription_created(db: Client, sub: Dict[str, Any]) -> Dict[str, Any]:
    return _persist_subscription(db, sub)


def _handle_subscription_updated(db: Client, sub: Dict[str, Any]) -> Dict[str, Any]:
    return _persist_subscription(db, sub)


def _org_id_from_sub(db: Client, sub: Dict[str, Any]) -> Optional[str]:
    """org_id from subscription metadata, falling back to the customer id."""
    org_id = (sub.get("metadata") or {}).get("org_id")
    if org_id:
        return org_id
    customer_id = sub.get("customer")
    if customer_id:
        res = (db.table("organisations").select("id")
               .eq("stripe_customer_id", customer_id).limit(1).execute())
        if res.data:
            return res.data[0]["id"]
    return None


def _handle_trial_will_end(db: Client, sub: Dict[str, Any]) -> Dict[str, Any]:
    """Fires ~3 days before a trial ends. We send our own personal note via
    Resend (2026-07-23) instead of Stripe's transactional default — turn
    Stripe's trial reminder OFF in the dashboard so it isn't doubled up.
    Idempotent: guarded by icp_config.trial_reminder_sent_at.
    """
    org_id = _org_id_from_sub(db, sub)
    if not org_id:
        return {"status": "no_org_id"}

    # Stripe fires this 3 days out on the normal path, but ALSO immediately
    # when a trial ends early (portal change, trial_end=now, or a trial
    # shortened to <3 days) — possibly in the same transaction that collects
    # payment. Only send the "your trial's ending" note when the sub is
    # genuinely still trialing with time left; otherwise the customer may
    # already be a paying, converted user and the reminder would be wrong.
    if sub.get("status") != "trialing":
        return {"status": "not_trialing", "sub_status": sub.get("status")}
    trial_end = sub.get("trial_end")
    if not trial_end or trial_end <= datetime.now(timezone.utc).timestamp():
        return {"status": "trial_already_ended"}

    org = (db.table("organisations").select("icp_config")
           .eq("id", org_id).maybe_single().execute().data or {})
    icp = org.get("icp_config") or {}
    if icp.get("trial_reminder_sent_at"):
        return {"status": "already_sent"}

    # Owner email + first name (reuse the digest's resolver).
    try:
        from app.services.solo_digest import _owner
        to_email, name = _owner(db, org_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("trial reminder: owner lookup failed for %s: %s", org_id, e)
        to_email, name = None, None
    if not to_email:
        return {"status": "no_owner_email"}
    first_name = (name or "").split(" ")[0] if name else ""

    # Days left from the trial end.
    days_left = 3
    trial_end = sub.get("trial_end")
    if trial_end:
        secs = trial_end - datetime.now(timezone.utc).timestamp()
        days_left = max(1, round(secs / 86400))

    # The amount that'll be charged, straight off the subscription item.
    price_label = ""
    try:
        item = ((sub.get("items") or {}).get("data") or [{}])[0]
        price = item.get("price") or {}
        cents = price.get("unit_amount")
        cur = (price.get("currency") or "usd").lower()
        if isinstance(cents, int):
            sym = {"gbp": "£", "usd": "$", "eur": "€"}.get(cur, "")
            price_label = f"{sym}{cents // 100}"
    except Exception:  # noqa: BLE001
        pass

    try:
        from app.services.email_sender import send_trial_ending_email
        send_trial_ending_email(
            to_email=to_email, first_name=first_name,
            days_left=days_left, price_label=price_label,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("trial reminder send failed for %s: %s", org_id, e)
        return {"status": "send_failed", "reason": str(e)}

    try:
        icp["trial_reminder_sent_at"] = datetime.now(timezone.utc).isoformat()
        db.table("organisations").update({"icp_config": icp}).eq("id", org_id).execute()
    except Exception as e:  # noqa: BLE001
        logger.warning("trial reminder: state write failed for %s: %s", org_id, e)

    return {"status": "sent", "to": to_email}


def _persist_subscription(db: Client, sub: Dict[str, Any]) -> Dict[str, Any]:
    metadata = sub.get("metadata") or {}
    org_id = metadata.get("org_id")
    if not org_id:
        # Fallback: look up by customer_id
        customer_id = sub.get("customer")
        if customer_id:
            org_resp = (
                db.table("organisations")
                .select("id")
                .eq("stripe_customer_id", customer_id)
                .limit(1)
                .execute()
            )
            if org_resp.data:
                org_id = org_resp.data[0]["id"]

    if not org_id:
        logger.warning("Subscription event with no org_id (customer=%s)", sub.get("customer"))
        return {"status": "no_org_id"}

    period_end = sub.get("current_period_end")
    period_end_iso = None
    if period_end:
        period_end_iso = datetime.fromtimestamp(period_end, tz=timezone.utc).isoformat()

    # Read seat count from the first subscription item. Stripe stores
    # the customer-selected quantity here; multiple items only happen
    # for multi-price subs (we only have one). Default 1 if anything's
    # weird with the payload so we never NULL the column.
    seat_count = 1
    items = (sub.get("items") or {}).get("data") or []
    if items:
        seat_count = int(items[0].get("quantity") or 1)
    seat_count = max(1, seat_count)  # belt and braces

    # Vera Solo capacity tier — derived from which Solo price the
    # subscription is on (base / +1 / +2 / +3). Keeps the account cap in
    # sync with what they're actually paying for. 0 for non-Solo subs.
    capacity_packs = 0
    price_map = solo_price_packs()
    if price_map:
        for it in items:
            pid = (it.get("price") or {}).get("id")
            if pid in price_map:
                capacity_packs = price_map[pid]
                break

    update_payload: Dict[str, Any] = {
        "stripe_subscription_id": sub.get("id"),
        "subscription_status": sub.get("status"),
        "subscription_current_period_end": period_end_iso,
        "seat_count": seat_count,
        "capacity_packs": capacity_packs,
    }

    # Plan switches from the Stripe customer portal (Starter <-> Individual,
    # 2026-07-15): re-derive the tier from the subscription's price so
    # paying for Individual actually lifts the Starter fences (and a
    # downgrade re-applies them). Without this the portal changed billing
    # only — tier, fence flags and the monthly limit stayed frozen.
    item_price_ids = [(it.get("price") or {}).get("id") for it in items]
    starter_pid = _price_id("STRIPE_PRICE_STARTER_MONTHLY")
    tier_from_price = None
    upgraded_org: Optional[Dict[str, Any]] = None  # set on a starter -> solo flip
    downgraded_to_starter = False  # set on a solo -> starter flip
    if starter_pid and starter_pid in item_price_ids:
        tier_from_price = "starter"
    elif any(pid in price_map for pid in item_price_ids):
        tier_from_price = "solo"
    if tier_from_price:
        try:
            org_row = (
                db.table("organisations").select("name, tier, icp_config")
                .eq("id", org_id).limit(1).execute().data or [{}]
            )[0]
            old_tier = (org_row.get("tier") or "").lower()
            if old_tier != tier_from_price:
                update_payload["tier"] = tier_from_price
                update_payload["subscription_tier"] = tier_from_price
                update_payload["monthly_enrichment_limit"] = TIER_LIMITS[tier_from_price]
                # Flip ONLY the fence flags — the rest of icp_config
                # (wizard answers, personas, digest state) is the
                # customer's and must survive a plan change.
                icp = org_row.get("icp_config") or {}
                features = dict(icp.get("features") or {})
                features["starter_plan"] = tier_from_price == "starter"
                features["groups"] = tier_from_price == "solo"
                icp["features"] = features
                update_payload["icp_config"] = icp
                if old_tier == "starter" and tier_from_price == "solo":
                    upgraded_org = org_row
                if old_tier == "solo" and tier_from_price == "starter":
                    downgraded_to_starter = True
                logger.info(
                    "Plan switch for org %s: -> %s (price-derived)",
                    org_id, tier_from_price,
                )
        except Exception as e:
            logger.error("Plan-switch tier sync failed for %s: %s", org_id, e)

    db.table("organisations").update(update_payload).eq("id", org_id).execute()

    # Downgrade reconcile (solo -> starter), AFTER the tier write: pause the
    # newest over-cap watched accounts + monitored people so the org isn't
    # left monitoring 25 accounts on a 15-account plan. Reversible, best-
    # effort — never 500 the webhook over it.
    if downgraded_to_starter:
        try:
            from app.services.plan_limits import STARTER_LIMITS, reconcile_standing_caps
            reconcile_standing_caps(
                org_id,
                watched_cap=STARTER_LIMITS["watched"],
                people_cap=STARTER_LIMITS["monitored_people"],
                db=db,
            )
        except Exception as e:
            logger.error("Downgrade reconcile failed for org %s: %s", org_id, e)

    # Upgrade email (starter -> solo only), AFTER the DB write so we never
    # announce an upgrade that didn't land. Naturally idempotent: Stripe
    # redeliveries see tier already flipped, so upgraded_org stays None.
    # Best-effort — a mail failure must not 500 the webhook.
    if upgraded_org is not None:
        try:
            from app.services.email_sender import _send_via_resend
            from app.services.solo_digest import _owner
            from app.templates.emails.upgrade import (
                SUBJECT as UPGRADE_SUBJECT,
                build_upgrade_html,
                build_upgrade_text,
            )
            to_email, owner_first = _owner(db, org_id)
            if to_email:
                contact = ((upgraded_org.get("icp_config") or {}).get("contact_name") or owner_first or "")
                company = upgraded_org.get("name") or ""
                dash = settings.DASHBOARD_URL
                _send_via_resend(
                    to_email=to_email,
                    subject=UPGRADE_SUBJECT,
                    html=build_upgrade_html(company_name=company, dashboard_url=dash, contact_name=contact),
                    text=build_upgrade_text(company_name=company, dashboard_url=dash, contact_name=contact),
                )
                logger.info("Upgrade email sent to %s for org %s", to_email, org_id)
            else:
                logger.warning("Upgrade email skipped for org %s: no owner email", org_id)
        except Exception as e:
            logger.error("Upgrade email failed for org %s: %s", org_id, e)
    logger.info(
        "Persisted subscription %s for org %s (status=%s, seats=%d)",
        sub.get("id"), org_id, sub.get("status"), seat_count,
    )
    return {
        "status": "persisted",
        "org_id": org_id,
        "subscription_status": sub.get("status"),
        "seat_count": seat_count,
    }


def _handle_subscription_deleted(db: Client, sub: Dict[str, Any]) -> Dict[str, Any]:
    customer_id = sub.get("customer")
    if not customer_id:
        return {"status": "no_customer"}
    db.table("organisations").update({
        "subscription_status": "canceled",
        "stripe_subscription_id": None,
    }).eq("stripe_customer_id", customer_id).execute()
    logger.info("Subscription cancelled for customer %s", customer_id)
    return {"status": "cancelled"}


def _handle_invoice_paid(db: Client, invoice: Dict[str, Any]) -> Dict[str, Any]:
    """Successful recurring payment — log it. The subscription.updated event
    that follows handles the period_end update."""
    logger.info("Invoice paid: %s amount_paid=%s", invoice.get("id"), invoice.get("amount_paid"))
    return {"status": "logged"}


def _handle_invoice_failed(db: Client, invoice: Dict[str, Any]) -> Dict[str, Any]:
    """Failed payment — flag the org's subscription as past_due. Stripe
    handles dunning automatically; this just mirrors the state."""
    customer_id = invoice.get("customer")
    if customer_id:
        db.table("organisations").update({
            "subscription_status": "past_due",
        }).eq("stripe_customer_id", customer_id).execute()
    logger.warning("Invoice payment failed for customer %s", customer_id)
    return {"status": "marked_past_due"}
