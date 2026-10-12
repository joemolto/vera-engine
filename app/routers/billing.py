"""Billing endpoints — Stripe Checkout creation, customer portal, webhook."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Header
from pydantic import BaseModel, Field
from typing import Literal, Optional
from uuid import UUID

# `stripe` is imported here ONLY for its exception classes so handlers can
# `except StripeError`. If the package isn't installed (which crashes the
# engine at startup before this resilience fix landed), we substitute stub
# classes so the module still imports and the engine still boots. The
# stubs are unreachable in practice — when stripe isn't installed, the
# billing service's lazy-import raises 503 long before any except clause
# fires — but they keep this module's import-time semantics valid.
try:
    import stripe as _stripe_pkg  # noqa: F401 — imported for its exception classes
    StripeError = _stripe_pkg.StripeError
    SignatureVerificationError = _stripe_pkg.SignatureVerificationError
except ImportError:
    class StripeError(Exception):  # type: ignore[no-redef]
        """Stub used when the stripe package isn't installed."""

    class SignatureVerificationError(Exception):  # type: ignore[no-redef]
        """Stub used when the stripe package isn't installed."""


from app.database import get_supabase
from app.services.api_auth import hosted_only
from app.services.billing import (
    TOPUP_PACKS,
    _provision_customer_login,
    complete_signup_set_password,
    create_signup_checkout_session,
    create_topup_checkout_session,
    create_customer_portal_session,
    cancel_subscription_at_period_end,
    apply_retention_discount,
    delete_account,
    set_solo_capacity,
    handle_webhook_event,
    start_signup_for_new_customer,
    verify_webhook_signature,
)
from app.services.activity import log_activity

logger = logging.getLogger(__name__)

# Two routers split by auth requirement:
#   - `router` (auth-required): customer-facing endpoints called from the
#     dashboard. Requires X-API-Key applied at registration time.
#   - `webhook_router` (no auth): Stripe webhook receiver. Stripe doesn't
#     send X-API-Key — it uses signature verification (STRIPE_WEBHOOK_SECRET)
#     which is enforced at the endpoint itself.
router = APIRouter(prefix="/billing", tags=["billing"])
webhook_router = APIRouter(prefix="/billing", tags=["billing-webhook"])


# ---------------------------------------------------------------------------
# Signup checkout — integration fee + chosen subscription with 90-day trial
# ---------------------------------------------------------------------------

class SignupCheckoutRequest(BaseModel):
    org_id: UUID
    email: str = Field(..., min_length=3)
    org_name: Optional[str] = None
    tier: Literal["starter", "solo", "scout", "growth", "scale"]
    cycle: Literal["monthly", "annual"]
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Self-serve signup from the marketing pricing page
# ---------------------------------------------------------------------------
# Brand-new customers don't have an org yet when they hit /pricing. This
# endpoint wraps "create the org row + initiate Stripe checkout" so the
# marketing page only needs one call to start signup. The org is created
# in a pending_payment state; the webhook flips it to active once Stripe
# confirms.

class StartSignupRequest(BaseModel):
    email: str = Field(..., min_length=3)
    company_name: str = Field(..., min_length=1)
    tier: Literal["starter", "solo", "scout", "growth", "scale"]
    cycle: Literal["monthly", "annual"]
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None
    full_name: str = ""


@router.post("/start-signup", dependencies=[Depends(hosted_only)])
def start_signup(request: StartSignupRequest):
    """Self-serve signup from the marketing pricing page.

    Creates a new organisation row in `pending_payment` state and returns
    a Stripe Checkout URL. The customer:
      1. Lands on /pricing, picks a plan
      2. Slides to confirm — modal collects email + company name
      3. Submits → we hit this endpoint → returns Stripe URL
      4. Redirects to Stripe → pays
      5. Webhook flips the org to `active` and invites the customer's
         email via Supabase (sends them a magic-link login email)
    """
    db = get_supabase()
    try:
        result = start_signup_for_new_customer(
            db=db,
            email=request.email,
            company_name=request.company_name,
            tier=request.tier,
            cycle=request.cycle,
            success_url=request.success_url,
            cancel_url=request.cancel_url,
            full_name=request.full_name,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except StripeError as e:
        logger.error("Stripe error starting signup: %s", e)
        raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message or str(e)}")

    return result


class CompleteSignupRequest(BaseModel):
    session_id: str = Field(..., min_length=3)
    password: str = Field(..., min_length=8)


@router.post("/complete-signup", dependencies=[Depends(hosted_only)])
def complete_signup(request: CompleteSignupRequest):
    """Set the customer's password straight after checkout — no email detour.

    The /welcome page (the Stripe success_url) calls this with the Checkout
    session_id and the chosen password. Verifies payment, provisions the
    account idempotently, sets the password + confirms it, and returns the
    email so the frontend can sign the customer straight in. Public path —
    the caller has no session yet (allowlisted in the clara-api proxy).
    """
    db = get_supabase()
    try:
        result = complete_signup_set_password(
            db=db,
            session_id=request.session_id,
            password=request.password,
        )
    except HTTPException:
        raise
    except StripeError as e:
        logger.error("Stripe error completing signup: %s", e)
        raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message or str(e)}")
    except Exception as e:
        logger.exception("complete-signup failed: %s", e)
        raise HTTPException(status_code=500, detail="Could not complete signup — check your email for a sign-in link.")

    return result


@router.post("/create-signup-checkout")
def create_signup_checkout(request: SignupCheckoutRequest):
    """Create a Stripe Checkout session for a new customer signing up.

    Customer pays the one-time integration fee on completion; their
    subscription is created with a 90-day trial that aligns with our
    calibration period. First recurring charge fires on day 91.
    """
    db = get_supabase()
    try:
        result = create_signup_checkout_session(
            db=db,
            org_id=str(request.org_id),
            email=request.email,
            org_name=request.org_name or "",
            tier=request.tier,
            cycle=request.cycle,
            success_url=request.success_url,
            cancel_url=request.cancel_url,
        )
    except RuntimeError as e:
        # Missing Stripe Price IDs — likely the seed script hasn't been run
        raise HTTPException(status_code=500, detail=str(e))
    except StripeError as e:
        logger.error("Stripe error creating signup checkout: %s", e)
        raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message or str(e)}")

    log_activity(db, str(request.org_id), None, "signup_checkout_created", {
        "tier": request.tier,
        "cycle": request.cycle,
        "session_id": result["session_id"],
    })

    return result


class ActivationCheckoutRequest(BaseModel):
    org_id: UUID
    email: str = Field(..., min_length=3)
    tier: Literal["starter", "solo"] = "solo"
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None


@router.post("/create-activation-checkout")
def create_activation_checkout(request: ActivationCheckoutRequest):
    """Checkout for an EXISTING org starting its paid subscription (the
    payment_required gate, 2026-08-25). No trial — the account is
    established and the first invoice bills immediately. The webhook
    (purpose='activation' + subscription metadata org_id) lifts the gate
    on completion; no login provisioning runs."""
    db = get_supabase()
    try:
        result = create_signup_checkout_session(
            db=db,
            org_id=str(request.org_id),
            email=request.email,
            org_name="",
            tier=request.tier,
            cycle="monthly",
            success_url=request.success_url,
            cancel_url=request.cancel_url,
            trial_period_days=0,
            purpose="activation",
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except StripeError as e:
        logger.error("Stripe error creating activation checkout: %s", e)
        raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message or str(e)}")

    log_activity(db, str(request.org_id), None, "activation_checkout_created", {
        "tier": request.tier,
        "session_id": result["session_id"],
    })

    return result


# ---------------------------------------------------------------------------
# Credit top-up checkout — one-off purchase of N enrichment credits
# ---------------------------------------------------------------------------

class TopupCheckoutRequest(BaseModel):
    org_id: UUID
    email: str = Field(..., min_length=3)
    org_name: Optional[str] = None
    pack: Literal["small", "medium", "large"]
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None


@router.post("/create-topup-checkout")
def create_topup_checkout(request: TopupCheckoutRequest):
    """Create a Stripe Checkout session for a credit top-up.

    Customer is already a paying user; they're buying extra credits this
    month. Three pack options — small / medium / large — encoded in
    billing.TOPUP_PACKS.

    On payment success, the webhook handler bumps their enrichments_limit
    on usage_tracking for the current month and flips the credit_topups
    row to 'completed'.
    """
    db = get_supabase()
    try:
        result = create_topup_checkout_session(
            db=db,
            org_id=str(request.org_id),
            email=request.email,
            org_name=request.org_name or "",
            pack=request.pack,
            success_url=request.success_url,
            cancel_url=request.cancel_url,
        )
    except (ValueError, RuntimeError) as e:
        # Bad pack or a Solo org being refused a top-up — both are client
        # errors, mirroring the /solo/capacity guard's 400.
        raise HTTPException(status_code=400, detail=str(e))
    except StripeError as e:
        logger.error("Stripe error creating top-up checkout: %s", e)
        raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message or str(e)}")

    log_activity(db, str(request.org_id), None, "topup_checkout_created", {
        "pack": request.pack,
        "credits": result["credits"],
        "amount_cents": result["amount_cents"],
        "session_id": result["session_id"],
    })

    return result


# ---------------------------------------------------------------------------
# Customer portal — Stripe-hosted page for managing subscription
# ---------------------------------------------------------------------------

class PortalRequest(BaseModel):
    org_id: UUID
    return_url: Optional[str] = None


@router.post("/customer-portal")
def customer_portal(request: PortalRequest):
    """Generate a Stripe Customer Portal URL where the customer can manage
    their subscription, view invoices, update card on file, cancel, etc.
    Stripe-hosted; we don't render any of this UI ourselves.
    """
    db = get_supabase()
    try:
        url = create_customer_portal_session(
            db=db,
            org_id=str(request.org_id),
            return_url=request.return_url,
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except StripeError as e:
        logger.error("Stripe error creating portal session: %s", e)
        raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message or str(e)}")

    return {"url": url}


# ---------------------------------------------------------------------------
# Cancel — in-app cancellation that captures churn feedback first
# ---------------------------------------------------------------------------

class CancelRequest(BaseModel):
    org_id: UUID
    reasons: list[str] = []
    details: Optional[str] = None


class CapacityRequest(BaseModel):
    org_id: UUID
    packs: int  # target capacity tier: 0 (40 acc) .. 3 (100 acc)


@router.post("/solo/capacity")
def solo_capacity(request: CapacityRequest):
    """Switch a Vera Solo subscription to a capacity tier (0-3).
    Stripe prorates onto the next invoice; the webhook keeps the cap in sync.
    """
    db = get_supabase()
    try:
        result = set_solo_capacity(db, str(request.org_id), request.packs)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except StripeError as e:
        logger.error("Stripe error modifying capacity: %s", e)
        raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message or str(e)}")
    return result


@router.post("/cancel")
def cancel_subscription(request: CancelRequest):
    """Cancel the org's subscription at period end and store why they left.

    Period-end (not immediate) so they keep what they paid for. Feedback is
    recorded even if the Stripe call fails.
    """
    db = get_supabase()
    try:
        result = cancel_subscription_at_period_end(
            db=db,
            org_id=str(request.org_id),
            reasons=request.reasons,
            details=request.details,
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except StripeError as e:
        logger.error("Stripe error cancelling subscription: %s", e)
        raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message or str(e)}")
    return result


@router.post("/retention-discount")
def retention_discount(request: CancelRequest):
    """Churn save: apply a one-time 50%-off coupon to the org's next invoice
    instead of cancelling. Records the same feedback the cancel flow does and
    refuses if the offer was already used. Reuses CancelRequest's shape.
    """
    db = get_supabase()
    try:
        result = apply_retention_discount(
            db=db,
            org_id=str(request.org_id),
            reasons=request.reasons,
            details=request.details,
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except StripeError as e:
        logger.error("Stripe error applying retention discount: %s", e)
        raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message or str(e)}")
    return result


class DeleteAccountRequest(BaseModel):
    org_id: UUID


@router.post("/delete-account")
def delete_account_endpoint(request: DeleteAccountRequest):
    """Permanently erase the org + all personal data (GDPR right to erasure).

    Distinct from /cancel: this deletes everything (org, data, logins, name,
    email) and the Stripe customer. Irreversible. The clara-api proxy has
    already verified the caller belongs to this org.
    """
    db = get_supabase()
    try:
        result = delete_account(db, str(request.org_id))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("delete-account failed for %s: %s", request.org_id, e)
        raise HTTPException(status_code=500, detail="Could not delete the account — please contact support.")
    return result


# ---------------------------------------------------------------------------
# Pack listing — for the dashboard top-up UI
# ---------------------------------------------------------------------------

@router.get("/topup-packs", dependencies=[Depends(hosted_only)])
def list_topup_packs():
    """Public-ish endpoint returning the credit-pack options. Dashboard
    uses this to render the three-button picker without hardcoding the
    pricing client-side."""
    return {
        "packs": [
            {
                "key": key,
                "credits": pack["credits"],
                "amount_cents": pack["amount_cents"],
                "amount_display": f"${pack['amount_cents'] // 100}",
                "price_per_credit_cents": round(pack["amount_cents"] / pack["credits"], 1),
                "label": pack["label"],
            }
            for key, pack in TOPUP_PACKS.items()
        ],
    }


# ---------------------------------------------------------------------------
# Admin: invite a teammate into an existing org (manual seat fill-out)
# ---------------------------------------------------------------------------
# When a customer pays for N seats at checkout but only the primary
# email gets the auto-invite, this endpoint covers the remaining seats.
# The operator runs it manually (e.g. with curl) after onboarding, until v2 ships a self-serve team-management UI.
#
# Seat enforcement is light right now — we refuse to create the (N+1)th
# user, but anything within seat_count is fine. Customer pays for what
# they're authorised to use.

class TeamInviteRequest(BaseModel):
    org_id: UUID
    email: str = Field(..., min_length=3)


@router.post("/admin/invite-teammate")
def admin_invite_teammate(request: TeamInviteRequest):
    """Add a teammate to an org. Fires the same branded welcome email
    as the primary signup. Refuses if the org is already at seat_count.

    Note: this is gated by the standard X-API-Key, same as every other
    auth-required endpoint. Real admin-only auth lands when the
    dashboard gets a Team page (v2).
    """
    db = get_supabase()

    org_resp = (
        db.table("organisations")
        .select("id, seat_count, name, subscription_tier")
        .eq("id", str(request.org_id))
        .limit(1)
        .execute()
    )
    if not org_resp.data:
        raise HTTPException(status_code=404, detail="Organisation not found")
    org = org_resp.data[0]

    # Count current users on this org
    user_count_resp = (
        db.table("org_users")
        .select("id", count="exact")
        .eq("org_id", str(request.org_id))
        .execute()
    )
    current_users = user_count_resp.count or 0
    seat_count = int(org.get("seat_count") or 1)

    if current_users >= seat_count:
        raise HTTPException(
            status_code=402,
            detail={
                "message": f"Org has {current_users} users and only {seat_count} paid seats. Increase the subscription quantity in Stripe first.",
                "code": "seat_limit_exceeded",
                "current_users": current_users,
                "seat_count": seat_count,
            },
        )

    # Reuse the same provisioning helper the Stripe webhook uses —
    # creates the auth user silently + generates magic link + sends
    # the branded welcome email via Resend.
    try:
        _provision_customer_login(db, str(request.org_id), request.email)
    except Exception as e:
        logger.exception("Failed to invite teammate %s to org %s: %s", request.email, request.org_id, e)
        raise HTTPException(status_code=500, detail=str(e))

    log_activity(db, str(request.org_id), None, "teammate_invited_admin", {
        "email": request.email.lower(),
        "users_after": current_users + 1,
        "seat_count": seat_count,
    })
    return {
        "status": "invited",
        "email": request.email.lower(),
        "seats_used": current_users + 1,
        "seats_total": seat_count,
    }


# ---------------------------------------------------------------------------
# Webhook — Stripe events
# ---------------------------------------------------------------------------

@webhook_router.post("/stripe-webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: Optional[str] = Header(None),
):
    """Stripe webhook receiver. Verifies signature using
    STRIPE_WEBHOOK_SECRET, then dispatches to the appropriate handler in
    app.services.billing.

    Returns 200 on success. Stripe retries with backoff on non-2xx, so
    handlers must be idempotent.
    """
    if not stripe_signature:
        raise HTTPException(status_code=400, detail="Missing Stripe-Signature header")

    payload = await request.body()

    try:
        event = verify_webhook_signature(payload, stripe_signature)
    except SignatureVerificationError as e:
        logger.warning("Stripe webhook signature verification failed: %s", e)
        raise HTTPException(status_code=400, detail="Invalid signature")
    except RuntimeError as e:
        logger.error("Webhook config error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    db = get_supabase()
    try:
        result = handle_webhook_event(db, event)
    except Exception as e:
        # Log but return 200 — we don't want Stripe to retry on bugs in our
        # handler that wouldn't fix themselves on a retry. If a real
        # transient error, we'll catch it via Stripe Dashboard's events page
        # and replay manually.
        logger.exception("Stripe webhook handler raised: %s", e)
        return {"status": "handler_error", "error": str(e)}

    return {"status": "ok", "result": result}
