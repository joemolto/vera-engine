"""Slack Events API endpoint — @Vera mentions and DMs.

Auth model: NO X-API-Key (Slack calls this directly). Every request is
verified against the app's signing secret (v0 HMAC of the raw body,
5-minute timestamp tolerance) before anything is parsed. Unverifiable
requests are 401s that do no reads.

Flow: verify → ack within Slack's 3s window → process in a background
task (resolve team_id → org, run the agent, post the threaded reply).
Slack retries deliveries, so events are deduped by event_id in-memory
(single-instance engine; restarts at worst reprocess one message).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from app.config import settings
from app.database import get_supabase

logger = logging.getLogger(__name__)

router = APIRouter()

# event_id → monotonic seconds; pruned on insert. Slack retries at most
# a handful of times within minutes, so a small TTL window is plenty.
_SEEN: dict = {}
_SEEN_TTL = 600


def _verify(raw: bytes, timestamp: Optional[str], signature: Optional[str]) -> bool:
    if not settings.SLACK_SIGNING_SECRET or not timestamp or not signature:
        return False
    try:
        if abs(time.time() - float(timestamp)) > 300:
            return False
    except ValueError:
        return False
    base = f"v0:{timestamp}:".encode() + raw
    expected = "v0=" + hmac.new(
        settings.SLACK_SIGNING_SECRET.encode(), base, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _dedupe(event_id: Optional[str]) -> bool:
    """True if this event was already seen."""
    if not event_id:
        return False
    now = time.monotonic()
    for k in [k for k, v in _SEEN.items() if now - v > _SEEN_TTL]:
        _SEEN.pop(k, None)
    if event_id in _SEEN:
        return True
    _SEEN[event_id] = now
    return False


def _process(team_id: str, event: dict) -> None:
    """Background: map workspace → org, run the agent, reply in thread."""
    from app.services.integrations.slack_agent import (
        normalize_slack_text,
        post_reply,
        run_agent,
    )
    from app.services.integrations.slack_push import parse_secret

    db = get_supabase()
    rows = (
        db.table("org_integrations")
        .select("org_id, status, secret, meta")
        .eq("provider", "slack")
        .eq("meta->>team_id", team_id)
        .limit(1)
        .execute()
        .data
    )
    if not rows or rows[0].get("status") != "connected":
        logger.info("slack event for unmapped team %s — ignored", team_id)
        return
    org_id = rows[0]["org_id"]
    bot_token = parse_secret(rows[0].get("secret")).get("bot_token")
    if not bot_token:
        # Pre-bot connection: we can hear (events are app-level) but not
        # speak. Stay silent rather than half-working.
        logger.info("slack event for org %s without bot token — reconnect needed", org_id)
        return

    # A lapsed org gets a clear answer, not a working agent
    # (2026-08-23) — the bot can speak, so say why instead of going quiet.
    from app.services.plan_limits import org_lapsed
    if org_lapsed(org_id, db):
        ch = event.get("channel")
        if ch:
            post_reply(
                bot_token, ch,
                "Your Vera subscription has ended, so I can't run research right now — reactivate from your dashboard settings and I'll pick straight back up.",
                event.get("thread_ts") or event.get("ts"),
            )
        return

    text = normalize_slack_text(event.get("text") or "")
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or (
        event.get("ts") if event.get("channel_type") != "im" else None
    )
    if not text or not channel:
        return

    # Per-org guard so a runaway thread can't burn tokens: reuse the
    # shared table-backed limiter.
    try:
        from app.services.rate_limit import check_and_record
        check_and_record(db, org_id, bucket_key="slack_agent", max_per_minute=10)
    except Exception:
        post_reply(bot_token, channel, "Give me a minute — getting a lot of messages at once.", thread_ts)
        return

    org = (
        db.table("organisations").select("name").eq("id", org_id).limit(1).execute().data
    )
    client_name = org[0].get("name") if org else None

    try:
        reply = run_agent(db, org_id, text, client_name=client_name)
    except Exception as e:  # noqa: BLE001
        logger.error("slack agent failed for org %s: %s", org_id, e)
        reply = "Something went wrong on my side — try that again in a minute."
    post_reply(bot_token, channel, reply, thread_ts)


@router.post("/events")
async def slack_events(
    request: Request,
    background_tasks: BackgroundTasks,
    x_slack_request_timestamp: Optional[str] = Header(default=None),
    x_slack_signature: Optional[str] = Header(default=None),
):
    raw = await request.body()
    if not _verify(raw, x_slack_request_timestamp, x_slack_signature):
        raise HTTPException(status_code=401, detail="bad_signature")

    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad_payload")

    # Slack's one-time URL verification handshake.
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}

    if payload.get("type") != "event_callback":
        return {"ok": True}
    if _dedupe(payload.get("event_id")):
        return {"ok": True}

    event = payload.get("event") or {}
    etype = event.get("type")
    # Mentions anywhere; plain messages only in DMs. Never react to
    # bots (including ourselves) or message edits/joins.
    if event.get("bot_id") or event.get("subtype"):
        return {"ok": True}
    is_mention = etype == "app_mention"
    is_dm = etype == "message" and event.get("channel_type") == "im"
    if not (is_mention or is_dm):
        return {"ok": True}

    team_id = payload.get("team_id")
    if not team_id:
        return {"ok": True}

    background_tasks.add_task(_process, team_id, event)
    return {"ok": True}
