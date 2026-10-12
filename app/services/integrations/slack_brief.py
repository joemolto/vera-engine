"""Daily Slack brief — the digest's editorial decision, rendered for a channel.

Rides solo_digest.build_and_send: whenever the daily digest email sends
(fresh moves, or people waiting on a quiet day), the same content posts
as a Block Kit brief to the org's connected Slack channel. No separate
cadence, caps or dedupe — the digest already decided today was worth an
interruption; this is a second surface, not a second brain.

Zero configuration: the connection is the subscription (2026-08-03).
Default on; org_integrations.meta.daily_brief = false turns it off (the
toggle on the Connected apps card).
"""

from __future__ import annotations

import logging
from typing import List, Optional

from supabase import Client

from app.config import settings
from app.services.integrations.slack_push import (
    SlackApiError,
    SlackNotConnected,
    parse_secret,
    post_payload,
)

logger = logging.getLogger(__name__)

_DASHBOARD = f"{settings.DASHBOARD_URL}/dashboard"
_ACCOUNTS = f"{settings.DASHBOARD_URL}/dashboard/accounts"


def _account_link(domain: Optional[str], text: str) -> str:
    if not domain:
        return text
    return f"<{_ACCOUNTS}/{domain}|{text}>"


def _brief_blocks(
    first_name: Optional[str],
    moves: List[dict],
    waiting: List[dict],
    cap_line: Optional[str],
) -> list:
    greet = f"Morning {first_name} —" if first_name else "Morning —"
    if moves:
        n = len(moves)
        headline = f"{greet} {n} thing{'s' if n != 1 else ''} moved at your accounts."
    else:
        n = len(waiting)
        headline = (
            f"{greet} quiet on signals today, but {n} "
            f"{'person is' if n == 1 else 'people are'} waiting at your accounts."
        )
    blocks: list = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{headline}*"}}
    ]

    if moves:
        move_lines = "\n".join(
            f"• {_account_link(m.get('domain'), m['line'])}" for m in moves[:6]
        )
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": move_lines}}
        )

    if waiting:
        w_lines = ["*People waiting at your accounts:*"]
        for p in waiting[:4]:
            role = (
                f"{p['title']} at {p['company']}"
                if p.get("title")
                else f"at {p['company']}"
            )
            line = f"• *{p['name']}*, {role}"
            if p.get("reason"):
                line += f" — {p['reason']}"
            if p.get("domain"):
                line += f" (<{_ACCOUNTS}/{p['domain']}|unlock>)"
            w_lines.append(line)
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(w_lines)}}
        )

    if cap_line:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": cap_line}],
            }
        )

    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open Vera"},
                    "url": _DASHBOARD,
                }
            ],
        }
    )
    return blocks


def send_brief(
    db: Client,
    org_id: str,
    *,
    first_name: Optional[str],
    moves: List[dict],
    waiting: List[dict],
    cap_line: Optional[str],
) -> bool:
    """Post the brief if the org has Slack connected and hasn't turned
    the daily brief off. Returns True when posted. Raises nothing the
    caller must handle — Slack problems must never break the email path,
    so callers wrap in try/except anyway, but we keep errors quiet here
    too apart from revocation bookkeeping."""
    rows = (
        db.table("org_integrations")
        .select("status, secret, meta")
        .eq("org_id", org_id)
        .eq("provider", "slack")
        .limit(1)
        .execute()
        .data
    )
    if not rows or rows[0].get("status") != "connected":
        return False
    webhook_url = parse_secret(rows[0].get("secret")).get("webhook_url")
    if not webhook_url:
        return False
    meta = rows[0].get("meta") or {}
    if meta.get("daily_brief") is False:
        return False
    if not moves and not waiting:
        return False

    payload = {
        "text": "Your Vera daily brief",
        "blocks": _brief_blocks(first_name, moves, waiting, cap_line),
    }
    try:
        post_payload(db, org_id, webhook_url, payload)
    except (SlackNotConnected, SlackApiError) as e:
        logger.warning("slack brief post failed for %s: %s", org_id, e)
        return False
    return True
