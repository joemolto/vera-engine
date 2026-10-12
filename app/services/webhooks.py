"""Outbound webhook delivery for the public Vera API.

Customers register webhook endpoints in the dashboard. We POST signed
JSON to those URLs whenever an interesting event happens — new lead
scored, brief created, watchlist alert fired, etc.

Architecture:
- enqueue_event(): fan-out an event to every matching subscriber.
  Inserts a row in webhook_deliveries with status='pending'. Cheap.
- deliver_pending(): the worker. Finds rows due for delivery, POSTs
  them, updates status. Run every 30s by the built-in scheduler
  (services/scheduler.py). Idempotent
  (uses next_attempt_at to claim rows).
- Retry schedule: exponential backoff at attempts 1-6, then dead.
  0s → 30s → 5min → 1hr → 6hr → 24hr → DEAD

Signing:
- Header: X-Vera-Signature: sha256=<hex>
- Computed as HMAC-SHA256(endpoint.secret, raw_request_body).
- Plus X-Vera-Event: <event_type>, X-Vera-Delivery: <delivery_id>
  for idempotency on the receiver side.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from supabase import Client

logger = logging.getLogger(__name__)


def validate_webhook_url(url: str) -> None:
    """SSRF guard for customer-supplied webhook URLs.

    Customers control the destination URL, so without this they could point
    Vera at internal services (the engine on :8000, other services on localhost), the
    cloud metadata endpoint (169.254.169.254), or other RFC1918 hosts and use
    Vera as an SSRF proxy — with response bodies reflected back into their
    dashboard. We require HTTPS and reject any URL whose host resolves to a
    non-public address. Enforced at registration AND again at delivery time
    (so a DNS record can't be flipped to an internal IP after the fact).
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Webhook URL must start with https://")
    host = parsed.hostname
    if not host:
        raise ValueError("Webhook URL has no host.")

    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError("Webhook URL host does not resolve.")

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local      # blocks 169.254.0.0/16 incl. cloud metadata
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(
                "Webhook URL must be a public address — internal, loopback, "
                "and cloud-metadata addresses are not allowed."
            )

# Event types Vera emits. Customers can subscribe to any subset via the
# `events` column on webhook_endpoints (empty array = subscribe to all).
EVENT_TYPES = (
    # Lead-level (legacy / contact-led flows)
    "lead.created",
    "lead.enriched",
    "lead.scored",
    "lead.rated_hot",
    "brief.created",
    # Watchlist + signals
    "watchlist.alert_created",
    "signal.detected",                # fires for buying-trigger signals only
    "threat.matched_to_lead",         # CVE/breach matched a watched account's stack
    # Account-led events (added 2026-06-09)
    "account.went_hot",               # heat-tier transition (fresh material changes)
    "account.suggestion_created",     # new candidate landed in the suggestions pool
)

# Retry schedule by attempt number (1-indexed). After exceeding, deliveries
# are marked 'dead' and only manual replay can re-trigger.
RETRY_BACKOFF_SECONDS = [
    0,        # attempt 1: immediate
    30,       # attempt 2: 30s
    300,      # attempt 3: 5 min
    3_600,    # attempt 4: 1 hr
    21_600,   # attempt 5: 6 hr
    86_400,   # attempt 6: 24 hr
]
MAX_ATTEMPTS = len(RETRY_BACKOFF_SECONDS)
WEBHOOK_TIMEOUT_SECONDS = 10
PAYLOAD_MAX_BYTES = 64_000      # safety cap on stored payload


# ---------------------------------------------------------------
# Endpoint management
# ---------------------------------------------------------------

def generate_secret() -> str:
    """32-char URL-safe secret for HMAC signing. Customers see it once
    at endpoint creation."""
    return f"vera_whsec_{secrets.token_urlsafe(32)}"


def create_endpoint(
    db: Client,
    org_id: str,
    url: str,
    name: str = "",
    events: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Create a webhook endpoint. Returns the inserted row including
    the freshly-generated secret (only time it's returned in full)."""
    validate_webhook_url(url)

    secret = generate_secret()
    insert = {
        "org_id": org_id,
        "url": url,
        "secret": secret,
        "events": events or [],
        "name": name.strip()[:120],
    }
    resp = db.table("webhook_endpoints").insert(insert).execute()
    if not resp.data:
        raise RuntimeError("Failed to create webhook endpoint")
    return resp.data[0]


def list_endpoints(db: Client, org_id: str) -> List[Dict[str, Any]]:
    """List all webhook endpoints for an org. Includes the secret —
    callers should mask it before sending to the dashboard."""
    resp = (
        db.table("webhook_endpoints")
        .select("*")
        .eq("org_id", org_id)
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data or []


def delete_endpoint(db: Client, org_id: str, endpoint_id: str) -> bool:
    """Hard-delete an endpoint. Cascades to webhook_deliveries."""
    resp = (
        db.table("webhook_endpoints")
        .delete()
        .eq("id", endpoint_id)
        .eq("org_id", org_id)
        .execute()
    )
    return bool(resp.data)


# ---------------------------------------------------------------
# Event enqueue (called from across the app whenever something fires)
# ---------------------------------------------------------------

def enqueue_event(
    db: Client,
    org_id: str,
    event_type: str,
    payload: Dict[str, Any],
) -> int:
    """Enqueue an event for delivery to all subscribed endpoints in
    the org. Returns the number of deliveries created.

    Cheap to call — this is a single SELECT + N inserts. Safe to call
    inline from any handler. Actual HTTP delivery happens in the
    worker (deliver_pending), so the calling request isn't blocked.
    """
    if event_type not in EVENT_TYPES:
        logger.warning("Unknown webhook event_type emitted: %s", event_type)

    # Lapsed orgs get no webhook traffic (2026-08-23): events simply
    # aren't enqueued, so nothing retries and reactivation resumes the
    # stream with no cleanup.
    from app.services.plan_limits import org_lapsed
    if org_lapsed(org_id, db):
        return 0

    endpoints = (
        db.table("webhook_endpoints")
        .select("id, events")
        .eq("org_id", org_id)
        .eq("active", True)
        .execute()
    ).data or []

    matched = [
        ep for ep in endpoints
        if not ep["events"] or event_type in ep["events"]
    ]
    if not matched:
        return 0

    # Cap the stored payload size to avoid runaway DB rows
    payload_str = json.dumps(payload)
    if len(payload_str) > PAYLOAD_MAX_BYTES:
        # Truncate by stripping the largest top-level field. Crude but
        # protects the DB.
        payload = {**payload, "_truncated": True}
        payload_str = json.dumps(payload)[:PAYLOAD_MAX_BYTES]
        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError:
            payload = {"_truncated": True, "event_type": event_type}

    rows = [
        {
            "endpoint_id": ep["id"],
            "org_id": org_id,
            "event_type": event_type,
            "payload": payload,
        }
        for ep in matched
    ]
    db.table("webhook_deliveries").insert(rows).execute()
    return len(rows)


# ---------------------------------------------------------------
# Delivery worker
# ---------------------------------------------------------------

def _sign_payload(secret: str, body: bytes) -> str:
    """Compute the body-only HMAC-SHA256 signature.

    Legacy signature scheme — kept for backward compatibility with any
    existing receiver verification code. New integrations should
    verify the timestamped V2 signature instead (see _sign_payload_v2)
    because body-only signatures are replayable: an attacker who
    captures one payload can re-POST it indefinitely with no way for
    the receiver to detect the replay.
    """
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def _sign_payload_v2(secret: str, timestamp: int, body: bytes) -> str:
    """Replay-resistant signature: HMAC of "{timestamp}.{body}".

    Receivers verify by:
      1. Reading the X-Vera-Timestamp header
      2. Rejecting if abs(now - timestamp) > 5 minutes (replay window)
      3. Computing HMAC-SHA256(secret, f"{timestamp}.{raw_body}")
      4. Comparing with the X-Vera-Signature-V2 header value

    Stripe / GitHub / Slack all use this pattern — industry standard
    for webhook integrity.
    """
    signed_payload = f"{timestamp}.".encode("utf-8") + body
    sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def _next_attempt_time(attempt: int) -> datetime:
    """When the next attempt should run, based on the retry schedule."""
    if attempt < 1 or attempt > MAX_ATTEMPTS:
        return datetime.now(timezone.utc)
    delay = RETRY_BACKOFF_SECONDS[attempt - 1]
    return datetime.now(timezone.utc) + timedelta(seconds=delay)


def _deliver_one(
    db: Client,
    delivery: Dict[str, Any],
    endpoint: Dict[str, Any],
) -> None:
    """POST a single delivery + record the result. Updates the row in
    webhook_deliveries with status / response / next_attempt_at."""
    delivery_id = delivery["id"]
    body_obj = {
        "id": delivery_id,
        "event": delivery["event_type"],
        "created_at": delivery["created_at"],
        "data": delivery["payload"],
    }
    body = json.dumps(body_obj, default=str).encode("utf-8")
    timestamp = int(datetime.now(timezone.utc).timestamp())
    signature = _sign_payload(endpoint["secret"], body)
    signature_v2 = _sign_payload_v2(endpoint["secret"], timestamp, body)

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Vera-Webhooks/1.0",
        "X-Vera-Event": delivery["event_type"],
        "X-Vera-Delivery": delivery_id,
        "X-Vera-Timestamp": str(timestamp),
        # Legacy body-only signature — kept for backward compatibility
        "X-Vera-Signature": signature,
        # Replay-resistant signature — receivers should prefer this and
        # reject deliveries where timestamp is > 5min from now
        "X-Vera-Signature-V2": signature_v2,
    }

    started = datetime.now(timezone.utc)
    response_status = None
    response_body = ""
    error = None
    try:
        # Re-validate the destination at delivery time — a host that passed
        # at registration could have had its DNS repointed at an internal IP
        # since (DNS rebinding), and this also guards any endpoint created
        # before the SSRF check existed.
        validate_webhook_url(endpoint["url"])
        with httpx.Client(
            timeout=WEBHOOK_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            resp = client.post(endpoint["url"], content=body, headers=headers)
            response_status = resp.status_code
            response_body = (resp.text or "")[:1000]
    except Exception as e:
        error = str(e)
        logger.warning("Webhook delivery %s to %s failed: %s",
                         delivery_id, endpoint["url"], e)
    duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)

    new_attempt_count = delivery["attempt_count"] + 1
    succeeded = response_status is not None and 200 <= response_status < 300

    update: Dict[str, Any] = {
        "attempt_count": new_attempt_count,
        "last_attempt_at": started.isoformat(),
        "response_status": response_status,
        "response_body": response_body or (error or "")[:1000],
        "duration_ms": duration_ms,
    }
    if succeeded:
        update["status"] = "delivered"
        update["delivered_at"] = datetime.now(timezone.utc).isoformat()
    elif new_attempt_count >= MAX_ATTEMPTS:
        update["status"] = "dead"
    else:
        update["status"] = "failed"
        update["next_attempt_at"] = _next_attempt_time(new_attempt_count + 1).isoformat()

    db.table("webhook_deliveries").update(update).eq("id", delivery_id).execute()

    # Update the endpoint's last_*_at counters so the dashboard can show health
    endpoint_update: Dict[str, Any] = {}
    if succeeded:
        endpoint_update["last_delivered_at"] = datetime.now(timezone.utc).isoformat()
        endpoint_update["consecutive_failures"] = 0
    else:
        endpoint_update["last_failed_at"] = datetime.now(timezone.utc).isoformat()
        endpoint_update["consecutive_failures"] = (endpoint.get("consecutive_failures") or 0) + 1
    if endpoint_update:
        try:
            db.table("webhook_endpoints").update(endpoint_update).eq("id", endpoint["id"]).execute()
        except Exception as e:
            logger.warning("Failed to update endpoint health for %s: %s", endpoint["id"], e)


def deliver_pending(db: Client, batch_size: int = 25) -> Dict[str, int]:
    """Worker: claim and deliver up to N pending webhook deliveries that
    are due. Returns counts. Called every 30s by the built-in scheduler.

    Race-safe: each delivery is independently updated. Multiple workers
    delivering the same row would just double-deliver, which is why
    receivers MUST be idempotent (we send X-Vera-Delivery for that).
    """
    now = datetime.now(timezone.utc).isoformat()
    pending = (
        db.table("webhook_deliveries")
        .select("*")
        .in_("status", ["pending", "failed"])
        .lte("next_attempt_at", now)
        .order("next_attempt_at")
        .limit(batch_size)
        .execute()
    ).data or []

    if not pending:
        return {"attempted": 0, "delivered": 0, "still_pending": 0, "dead": 0}

    # Bulk-load the relevant endpoints so we don't query per row
    endpoint_ids = list({d["endpoint_id"] for d in pending})
    endpoints_resp = (
        db.table("webhook_endpoints")
        .select("*")
        .in_("id", endpoint_ids)
        .execute()
    )
    endpoints_by_id = {ep["id"]: ep for ep in (endpoints_resp.data or [])}

    delivered = 0
    still_pending = 0
    dead = 0
    for delivery in pending:
        endpoint = endpoints_by_id.get(delivery["endpoint_id"])
        if not endpoint or not endpoint.get("active"):
            # Endpoint was deleted or deactivated mid-flight — mark dead
            db.table("webhook_deliveries").update({
                "status": "dead",
                "response_body": "endpoint deleted or deactivated",
                "last_attempt_at": now,
            }).eq("id", delivery["id"]).execute()
            dead += 1
            continue
        _deliver_one(db, delivery, endpoint)
        # Re-read the row to get its latest status — cheap one-row lookup
        check = (
            db.table("webhook_deliveries")
            .select("status")
            .eq("id", delivery["id"])
            .limit(1)
            .execute()
        )
        status = (check.data or [{}])[0].get("status")
        if status == "delivered":
            delivered += 1
        elif status == "dead":
            dead += 1
        else:
            still_pending += 1

    return {
        "attempted": len(pending),
        "delivered": delivered,
        "still_pending": still_pending,
        "dead": dead,
    }


def send_test_delivery(
    db: Client, org_id: str, endpoint_id: str
) -> Dict[str, Any]:
    """Fire a synthetic `webhook.test` event to a single endpoint and
    deliver immediately (in-process). Used by the 'Send test' button
    on settings. Returns {success, status, duration_ms, response_body}.

    Org scoping: explicitly checks endpoint.org_id matches the caller's
    org. A customer cannot test another customer's endpoint.
    """
    ep_res = (
        db.table("webhook_endpoints")
        .select("*")
        .eq("id", endpoint_id)
        .eq("org_id", org_id)
        .maybe_single()
        .execute()
    )
    endpoint = ep_res.data
    if not endpoint:
        return {"success": False, "error": "endpoint not found"}

    # Synthetic delivery row — written to webhook_deliveries so the test
    # appears in the deliveries log alongside real events, then
    # immediately delivered (bypasses the cron worker).
    now_iso = datetime.now(timezone.utc).isoformat()
    insert_res = (
        db.table("webhook_deliveries")
        .insert({
            "org_id": org_id,
            "endpoint_id": endpoint_id,
            "event_type": "webhook.test",
            "payload": {
                "message": "This is a test delivery from your Vera dashboard.",
                "endpoint_id": endpoint_id,
                "sent_at": now_iso,
            },
            "status": "pending",
            "attempt_count": 0,
            "next_attempt_at": now_iso,
        })
        .execute()
    )
    if not insert_res.data:
        return {"success": False, "error": "could not enqueue test delivery"}
    delivery = insert_res.data[0]

    _deliver_one(db, delivery, endpoint)

    # Re-read the delivery to see how it landed
    after_res = (
        db.table("webhook_deliveries")
        .select("status, response_status, response_body, duration_ms")
        .eq("id", delivery["id"])
        .maybe_single()
        .execute()
    )
    after = after_res.data or {}
    return {
        "success": after.get("status") == "delivered",
        "delivery_id": delivery["id"],
        "status": after.get("response_status"),
        "duration_ms": after.get("duration_ms"),
        "response_body": (after.get("response_body") or "")[:500],
    }


def replay_delivery(db: Client, org_id: str, delivery_id: str) -> bool:
    """Reset a delivery so the worker picks it up again. Used for the
    'retry' button on dead/failed deliveries in the dashboard."""
    resp = (
        db.table("webhook_deliveries")
        .update({
            "status": "pending",
            "next_attempt_at": datetime.now(timezone.utc).isoformat(),
        })
        .eq("id", delivery_id)
        .eq("org_id", org_id)
        .execute()
    )
    return bool(resp.data)
