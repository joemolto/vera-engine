"""Per-tenant API key generation, hashing, and validation.

Key shape:    vera_live_<prefix><secret>
  - prefix:  8 chars  → stored, used for masked display in dashboard
  - secret: 24 chars  → hashed (SHA-256) and stored as key_hash; NEVER stored raw

Why hash-only and not encrypt: stops a DB breach from exposing every
customer's plaintext key. Standard SaaS pattern (Stripe, Anthropic,
GitHub all do this). The cost is the user can't recover a lost key —
they have to rotate, which is the trade we make.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from supabase import Client

logger = logging.getLogger(__name__)

# All keys live under this brand prefix so they're identifiable in logs,
# secret scanners (GitHub, GitLab), and customer codebases.
KEY_BRAND_PREFIX = "vera_live_"
PREFIX_LENGTH = 8     # chars after vera_live_ used for display
SECRET_LENGTH = 24    # chars of hashed entropy
# solo added 2026-07-09, starter added 2026-07-15:
# EVERY paid org gets API, webhooks and MCP. Cost protection is the plan
# caps, which the public API enforces at every cost-incurring endpoint
# (public_api.py) — starter's lower caps bound it the same way packs
# bound solo+N. Starter's remaining fence is smart groups only.
TIERS_WITH_API_ACCESS = ("starter", "solo", "scout", "growth", "scale")


# ---------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------

def generate_key() -> Tuple[str, str, str]:
    """Generate a new API key. Returns (full_key, prefix, key_hash).

    The full_key is shown to the user once at creation. Only the prefix
    and key_hash are stored.
    """
    # Use URL-safe alphabet so keys never need escaping in cli usage
    prefix = secrets.token_urlsafe(8)[:PREFIX_LENGTH]
    secret = secrets.token_urlsafe(24)[:SECRET_LENGTH]
    full_key = f"{KEY_BRAND_PREFIX}{prefix}{secret}"
    key_hash = hashlib.sha256(full_key.encode("utf-8")).hexdigest()
    return full_key, prefix, key_hash


def hash_key(full_key: str) -> str:
    """Hash a presented key for table lookup."""
    return hashlib.sha256(full_key.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------
# Tier gating
# ---------------------------------------------------------------

def org_has_api_access(db: Client, org_id: str) -> bool:
    """API + webhook access is available on every paid tier
    (Scout, Growth, Scale). API/webhooks cost us ~nothing to serve and
    are table-stakes in an AI-driven world, so they're standard on all
    plans — the tier ladder is credits/personas/watchlist, not API.

    Tier is read from `organisations.subscription_tier` (then `tier`).
    Orgs with no tier set get no access — fail closed.

    Per-org override: the `developer_access` feature flag (in
    icp_config.features) grants API + webhook + MCP access even on a tier
    that normally lacks it — e.g. a Solo trial (2026-06-19).
    """
    resp = (
        db.table("organisations")
        .select("tier, subscription_tier, icp_config")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    if not resp.data:
        return False
    row = resp.data[0]
    tier = (row.get("subscription_tier") or row.get("tier") or "").lower()
    if tier in TIERS_WITH_API_ACCESS:
        return True
    features = (row.get("icp_config") or {}).get("features") or {}
    return bool(features.get("developer_access"))


# ---------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------

def create_api_key(
    db: Client,
    org_id: str,
    name: str,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new API key for an org. Returns dict containing the
    plaintext `full_key` (shown once) plus the persisted record.

    Caller is responsible for tier-gating before invoking.
    """
    full_key, prefix, key_hash = generate_key()

    insert = {
        "org_id": org_id,
        "name": name.strip()[:120] or "Untitled key",
        "prefix": prefix,
        "key_hash": key_hash,
    }
    if created_by:
        insert["created_by"] = created_by

    resp = db.table("api_keys").insert(insert).execute()
    if not resp.data:
        raise RuntimeError("Failed to create API key")

    record = resp.data[0]
    record["full_key"] = full_key      # only ever returned at creation
    return record


def list_api_keys(db: Client, org_id: str, include_revoked: bool = False) -> List[Dict[str, Any]]:
    """List all API keys for an org. Plaintext keys are NEVER returned —
    only the prefix + metadata."""
    q = (
        db.table("api_keys")
        .select(
            "id, name, prefix, last_used_at, request_count, created_at, revoked_at",
        )
        .eq("org_id", org_id)
    )
    if not include_revoked:
        q = q.is_("revoked_at", "null")
    resp = q.order("created_at", desc=True).execute()
    return resp.data or []


def revoke_api_key(db: Client, org_id: str, key_id: str) -> bool:
    """Mark a key as revoked. Idempotent. Returns True if a row was
    affected. Scoped to the requesting org so one tenant can't revoke
    another's keys."""
    resp = (
        db.table("api_keys")
        .update({"revoked_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", key_id)
        .eq("org_id", org_id)
        .is_("revoked_at", "null")
        .execute()
    )
    return bool(resp.data)


def rotate_api_key(
    db: Client,
    org_id: str,
    key_id: str,
    created_by: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Revoke an existing key and create a new one with the same name.
    Returns the new key (with `full_key` shown once) or None if the
    original wasn't found / wasn't owned by this org.
    """
    # Fetch the old key first so we can carry its name forward
    old = (
        db.table("api_keys")
        .select("name")
        .eq("id", key_id)
        .eq("org_id", org_id)
        .limit(1)
        .execute()
    )
    if not old.data:
        return None
    name = old.data[0]["name"]

    if not revoke_api_key(db, org_id, key_id):
        return None

    return create_api_key(db, org_id, name, created_by=created_by)


# ---------------------------------------------------------------
# Validation (called by the FastAPI auth dependency)
# ---------------------------------------------------------------

def validate_key(db: Client, presented_key: str) -> Optional[Dict[str, Any]]:
    """Look up an API key by its plaintext value. Returns the api_keys
    row if the key is valid + active, else None.

    Side effect: bumps last_used_at + request_count on success. The
    update is best-effort — auth still succeeds even if it fails.
    """
    if not presented_key or not presented_key.startswith(KEY_BRAND_PREFIX):
        return None
    key_hash = hash_key(presented_key)

    resp = (
        db.table("api_keys")
        .select("id, org_id, name, prefix, request_count, revoked_at")
        .eq("key_hash", key_hash)
        .limit(1)
        .execute()
    )
    if not resp.data:
        return None
    row = resp.data[0]

    if row.get("revoked_at") is not None:
        return None

    # A lapsed org's keys stop working. Cancellation never revokes the key
    # rows themselves (TODO), so enforce it at validation — this
    # closes REST reads for cancelled orgs (2026-08-23 lapsed-org audit).
    # The row is returned FLAGGED rather than None so callers can show a
    # clear "subscription ended" message instead of "invalid key". No usage bump for a refused request. Fail-open on lookup
    # errors: an org-table hiccup must never lock out a paying customer.
    try:
        from app.config import settings
        from app.services.plan_limits import REVOKED_STATUSES
        org = (db.table("organisations").select("subscription_status")
               .eq("id", row["org_id"]).maybe_single().execute().data or {})
        status_lower = (org.get("subscription_status") or "").lower()
        if status_lower in REVOKED_STATUSES:
            logger.info("api key refused: org %s subscription lapsed", row["org_id"])
            row["_subscription_lapsed"] = True
            return row
        if status_lower == "payment_required" and not settings.SELF_HOSTED:
            # Existing account gated until it starts its paid subscription
            # (2026-08-25). Distinct from lapsed:
            # nothing was cancelled, monitoring keeps running, access is
            # held behind checkout. Flagged rather than None so callers
            # show the pay prompt instead of "invalid key".
            logger.info("api key held: org %s payment required", row["org_id"])
            row["_payment_required"] = True
            return row
    except Exception as e:  # noqa: BLE001
        logger.warning("api key org-status check failed (%s): %s", row["org_id"], e)

    # Bump usage. Best-effort — do not raise on failure.
    try:
        db.table("api_keys").update({
            "last_used_at": datetime.now(timezone.utc).isoformat(),
            "request_count": (row.get("request_count") or 0) + 1,
        }).eq("id", row["id"]).execute()
    except Exception as e:
        logger.warning("Failed to bump api_key usage for %s: %s", row["id"], e)

    return row


def log_request(
    db: Client,
    api_key_id: str,
    org_id: str,
    method: str,
    path: str,
    status_code: Optional[int],
    duration_ms: Optional[int],
) -> None:
    """Append a request-log row. Best-effort — never raises."""
    try:
        db.table("api_request_log").insert({
            "api_key_id": api_key_id,
            "org_id": org_id,
            "method": method,
            "path": path[:500],   # safety cap
            "status_code": status_code,
            "duration_ms": duration_ms,
        }).execute()
    except Exception as e:
        logger.warning("Failed to log API request: %s", e)


# ---------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------

# Per-tier requests per minute. Conservative defaults — easy to raise once
# we see real customer usage patterns.
RATE_LIMITS_PER_MINUTE = {
    "starter": 30,
    "solo": 30,
    "scout": 30,
    "growth": 60,
    "scale": 300,
}


def get_rate_limit(db: Client, org_id: str) -> int:
    """Look up the per-minute rate limit for an org's tier. Falls back to
    the most permissive Growth-tier limit if tier isn't recognised."""
    resp = (
        db.table("organisations")
        .select("tier, subscription_tier")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    if not resp.data:
        return RATE_LIMITS_PER_MINUTE["growth"]
    row = resp.data[0]
    tier = (row.get("subscription_tier") or row.get("tier") or "").lower()
    return RATE_LIMITS_PER_MINUTE.get(tier, RATE_LIMITS_PER_MINUTE["growth"])


def count_recent_requests(db: Client, api_key_id: str, seconds: int = 60) -> int:
    """Count requests this key has made in the last N seconds. Used by
    the rate limiter dependency. Cheap query because api_request_log
    is indexed on (api_key_id, created_at DESC)."""
    from datetime import timedelta
    since = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).isoformat()
    resp = (
        db.table("api_request_log")
        .select("id", count="exact")
        .eq("api_key_id", api_key_id)
        .gte("created_at", since)
        .execute()
    )
    # Supabase's count="exact" returns the count in resp.count; data may
    # also be empty since we don't actually need rows.
    return resp.count or 0
