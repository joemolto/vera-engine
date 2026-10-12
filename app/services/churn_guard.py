"""Churn guard — tell the operator when an org goes quiet BEFORE it cancels.

An org can stop using Vera for weeks without anyone noticing. This closes
that class: every org with active monitoring whose users haven't signed in
for 14 days gets flagged in a single digest email to OPERATOR_EMAIL, at
most once per org per 7 days.

Runs from POST /health/heartbeat-check alongside the heartbeat check
(one POST, both guards; the built-in scheduler runs only the heartbeat). State (who was alerted when) lives in system_heartbeat under
key 'churn_guard' — no new table.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from supabase import Client

from app.config import settings

logger = logging.getLogger(__name__)

_KEY = "churn_guard"
_QUIET_DAYS = 14
_REALERT_DAYS = 7


def check(db: Client) -> Dict[str, Any]:
    """Find quiet orgs; email the operator a digest for newly-alertable ones."""
    now = datetime.now(timezone.utc)

    # Orgs that are actually customers in any sense: active monitoring.
    wl = (
        db.table("watchlist")
        .select("org_id")
        .eq("monitoring_active", True)
        .execute()
    ).data or []
    monitored = {r["org_id"] for r in wl}
    if not monitored:
        return {"quiet": [], "alerted": []}

    orgs = (
        db.table("organisations")
        .select("id, name")
        .in_("id", list(monitored))
        .execute()
    ).data or []
    names = {o["id"]: o.get("name") or o["id"] for o in orgs}

    last_seen: Dict[str, Any] = {}
    try:
        rows = db.rpc("org_last_seen").execute().data or []
        for r in rows:
            last_seen[r["org_id"]] = r.get("last_seen")
    except Exception as e:
        logger.warning("churn-guard: org_last_seen rpc failed: %s", e)
        return {"error": str(e)}

    quiet: List[Dict[str, Any]] = []
    for org_id in monitored:
        seen = last_seen.get(org_id)
        if not seen:
            continue  # no login data — can't judge
        seen_dt = datetime.fromisoformat(str(seen).replace("Z", "+00:00"))
        days = (now - seen_dt).days
        if days >= _QUIET_DAYS:
            quiet.append({"org_id": org_id, "name": names.get(org_id, org_id), "days": days})

    # Re-alert throttle: state in system_heartbeat.detail.alerted {org_id: iso}
    state: Dict[str, Any] = {}
    try:
        res = (
            db.table("system_heartbeat").select("detail").eq("key", _KEY).maybe_single().execute()
        )
        state = (res.data or {}).get("detail") or {} if res else {}
    except Exception:
        state = {}
    alerted_at: Dict[str, str] = state.get("alerted") or {}

    to_alert = []
    for q in quiet:
        prev = alerted_at.get(q["org_id"])
        if prev:
            prev_dt = datetime.fromisoformat(prev.replace("Z", "+00:00"))
            if (now - prev_dt).days < _REALERT_DAYS:
                continue
        to_alert.append(q)

    if to_alert:
        _alert(to_alert)
        for q in to_alert:
            alerted_at[q["org_id"]] = now.isoformat()

    # Beat on EVERY run, not just alert runs (2026-08-25: beat_at read as
    # "churn guard dead since Aug 21" when it had simply had nothing new
    # to say — a dead guard and a quiet one must not look identical).
    try:
        db.table("system_heartbeat").upsert(
            {"key": _KEY, "beat_at": now.isoformat(), "detail": {"alerted": alerted_at}},
            on_conflict="key",
        ).execute()
    except Exception as e:
        logger.warning("churn-guard: state save failed: %s", e)

    # Counts only — this reaches an unauthenticated endpoint's response;
    # names go to the operator's email and the server log, never the wire.
    logger.info("churn-guard: quiet=%s alerted=%s",
                [q["name"] for q in quiet], [q["name"] for q in to_alert])
    return {"quiet_orgs": len(quiet), "alerted": len(to_alert)}


def _alert(quiet: List[Dict[str, Any]]) -> None:
    try:
        from app.services.email_sender import _send_via_resend
    except Exception:
        logger.error("churn-guard: could not import email sender")
        return
    if not settings.OPERATOR_EMAIL:
        logger.warning("churn-guard: quiet orgs %s but OPERATOR_EMAIL unset", quiet)
        return
    if not settings.RESEND_API_KEY:
        logger.error("churn-guard: quiet orgs %s but RESEND_API_KEY unset", quiet)
        return
    rows = "".join(
        f"<li><b>{q['name']}</b> — no sign-in for {q['days']} days</li>" for q in quiet
    )
    subject = (
        f"Vera churn guard: {quiet[0]['name']} has gone quiet"
        if len(quiet) == 1
        else f"Vera churn guard: {len(quiet)} orgs have gone quiet"
    )
    html = (
        "<p>These orgs still have active monitoring but nobody has signed in "
        f"for {_QUIET_DAYS}+ days:</p><ul>{rows}</ul>"
        "<p>Worth a check-in before it becomes a cancellation.</p>"
    )
    text = "Quiet orgs: " + "; ".join(f"{q['name']} ({q['days']}d)" for q in quiet)
    try:
        _send_via_resend(settings.OPERATOR_EMAIL, subject, html, text=text)
        logger.warning("churn-guard: alerted for %s", [q["name"] for q in quiet])
    except Exception as e:
        logger.error("churn-guard: alert email failed: %s", e)
