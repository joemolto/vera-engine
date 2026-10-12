"""Slack push — Block Kit cards to an org's connected channel.

The connection (provider='slack' in org_integrations) stores a
channel-scoped incoming-webhook URL in ``secret`` — write-only
credentials Slack minted for the channel the org's admin picked during
OAuth. Treated like a password: read server-side only, never logged.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

import httpx
from supabase import Client

logger = logging.getLogger(__name__)

TIMEOUT = 10


class SlackNotConnected(Exception):
    """Org has no live Slack connection."""


class SlackApiError(Exception):
    """Slack rejected the webhook post."""


def parse_secret(secret: Optional[str]) -> dict:
    """Slack secrets come in two shapes: legacy plain webhook-URL strings
    (pre-bot connects) and JSON {webhook_url, bot_token} (bot-era).
    Returns a dict either way; missing keys are simply absent."""
    if not secret:
        return {}
    if secret.startswith("{"):
        try:
            parsed = json.loads(secret)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {"webhook_url": secret}


def load_webhook(db: Client, org_id: str) -> str:
    rows = (
        db.table("org_integrations")
        .select("status, secret")
        .eq("org_id", org_id)
        .eq("provider", "slack")
        .limit(1)
        .execute()
        .data
    )
    if not rows or rows[0].get("status") != "connected":
        raise SlackNotConnected()
    url = parse_secret(rows[0].get("secret")).get("webhook_url")
    if not url:
        raise SlackNotConnected()
    return url


def _card_blocks(
    heading: str,
    subtitle: Optional[str],
    signal_lines: List[str],
    vera_url: str,
    hubspot_url: Optional[str] = None,
    intro: Optional[str] = None,
    narrative: Optional[str] = None,
    contact_line: Optional[str] = None,
) -> list:
    """Agent-voice briefing card (2026-08-03): open with what just
    happened, explain why the account matters, list the recent picture,
    name who to start with. All content is assembled from cached
    facts-only sources — nothing is generated at send time."""
    blocks: list = []
    if intro:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": intro}}
        )
    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{heading}*" + (f"\n{subtitle}" if subtitle else "")},
        }
    )
    if narrative:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": narrative}}
        )
    if signal_lines:
        bullet_text = "*The recent picture:*\n" + "\n".join(
            f"• {line}" for line in signal_lines[:6]
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": bullet_text}})
    if contact_line:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": contact_line}}
        )
    actions = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Open in Vera"},
            "url": vera_url,
        }
    ]
    if hubspot_url:
        actions.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "View in HubSpot"},
                "url": hubspot_url,
            }
        )
    blocks.append({"type": "actions", "elements": actions})
    return blocks


def push_card(
    db: Client,
    org_id: str,
    *,
    heading: str,
    subtitle: Optional[str],
    signal_lines: List[str],
    vera_url: str,
    hubspot_url: Optional[str] = None,
    intro: Optional[str] = None,
    narrative: Optional[str] = None,
    contact_line: Optional[str] = None,
) -> None:
    webhook_url = load_webhook(db, org_id)
    payload = {
        # Fallback text for notifications / clients that don't render blocks.
        "text": intro or (heading + (f" — {subtitle}" if subtitle else "")),
        "blocks": _card_blocks(
            heading,
            subtitle,
            signal_lines,
            vera_url,
            hubspot_url,
            intro=intro,
            narrative=narrative,
            contact_line=contact_line,
        ),
    }
    post_payload(db, org_id, webhook_url, payload)


def post_payload(db: Client, org_id: str, webhook_url: str, payload: dict) -> None:
    """POST a payload to an org's webhook; revocations flip the
    connection to reconnect_required. Shared by cards + the daily brief."""
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(webhook_url, json=payload)
    if resp.status_code >= 400:
        # 404/410 = webhook revoked (app removed from channel).
        if resp.status_code in (403, 404, 410):
            db.table("org_integrations").update({"status": "reconnect_required"}).eq(
                "org_id", org_id
            ).eq("provider", "slack").execute()
            raise SlackNotConnected()
        raise SlackApiError(f"slack webhook returned {resp.status_code}")
