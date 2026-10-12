"""Dead-man heartbeat — a monitoring product must notice when its own
monitoring stops.

`stamp()` is called at the end of every Solo monitor pass. `check()` is
called once a day (by the engine's built-in scheduler, or a POST to
/health/heartbeat-check): if the last stamp is older than the threshold,
the monitor silently stopped, so it emails the operator (OPERATOR_EMAIL). Runs on the data we already have + the Resend sender that's
already wired; no external uptime service needed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from supabase import Client

from app.config import settings

logger = logging.getLogger(__name__)

_KEY = "solo_monitor"
# The monitor runs daily and is checked 3h later (scheduler.py). Alert if it
# hasn't stamped in 27h: one missed run, while a pass that's still running at
# check time (previous stamp up to 27h old) doesn't count as missed.
_STALE_HOURS = 27


def stamp(db: Client, key: str = _KEY, detail: Optional[Dict[str, Any]] = None) -> None:
    """Record that a background job just completed. Best-effort — a failed
    stamp must never break the job it's stamping."""
    try:
        db.table("system_heartbeat").upsert(
            {
                "key": key,
                "beat_at": datetime.now(timezone.utc).isoformat(),
                "detail": detail or {},
            },
            on_conflict="key",
        ).execute()
    except Exception as e:
        logger.warning("heartbeat stamp failed for %s: %s", key, e)


def check(db: Client, key: str = _KEY) -> Dict[str, Any]:
    """Return liveness for one job; email the operator if it's stale. Idempotent to
    call daily — at most one alert per day while broken (persistent nudge)."""
    row = None
    try:
        res = (
            db.table("system_heartbeat")
            .select("beat_at")
            .eq("key", key)
            .maybe_single()
            .execute()
        )
        row = res.data if res else None
    except Exception as e:
        logger.warning("heartbeat read failed for %s: %s", key, e)

    now = datetime.now(timezone.utc)
    if not row or not row.get("beat_at"):
        # Never stamped — could be a fresh deploy. Report, don't alert yet.
        return {"key": key, "ok": False, "stale": None, "reason": "no heartbeat recorded yet"}

    beat = datetime.fromisoformat(str(row["beat_at"]).replace("Z", "+00:00"))
    hours = (now - beat).total_seconds() / 3600.0
    stale = hours > _STALE_HOURS
    result = {
        "key": key,
        "ok": not stale,
        "stale": stale,
        "last_beat_at": beat.isoformat(),
        "hours_since": round(hours, 1),
    }
    if stale:
        _alert(key, hours)
    return result


def _alert(key: str, hours: float) -> None:
    try:
        from app.services.email_sender import _send_via_resend
    except Exception:
        logger.error("heartbeat: could not import email sender for alert")
        return
    if not settings.OPERATOR_EMAIL:
        logger.error("heartbeat: %s STALE %.1fh but OPERATOR_EMAIL unset", key, hours)
        return
    if not settings.RESEND_API_KEY:
        logger.error("heartbeat: %s STALE %.1fh but RESEND_API_KEY unset", key, hours)
        return
    subject = f"Vera alert: the {key} monitor hasn't run in {hours:.0f}h"
    html = (
        f"<p>The <b>{key}</b> background job last completed "
        f"<b>{hours:.1f} hours ago</b> (threshold {_STALE_HOURS}h).</p>"
        f"<p>The daily monitor pass has likely stopped. Check that the "
        f"engine is running and look at its logs.</p>"
    )
    text = (
        f"The {key} job last ran {hours:.1f}h ago (threshold {_STALE_HOURS}h). "
        f"The daily monitor has likely stopped — check the engine and its logs."
    )
    try:
        _send_via_resend(settings.OPERATOR_EMAIL, subject, html, text=text)
        logger.warning("heartbeat: emailed stale alert for %s (%.1fh)", key, hours)
    except Exception as e:
        logger.error("heartbeat: alert email failed for %s: %s", key, e)
