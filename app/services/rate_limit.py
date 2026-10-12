"""Table-backed per-org rate limiter.

Replaces the in-memory defaultdict[deque] previously in
``app/routers/scoring.py``. The in-memory version had two silent failure
modes: it didn't survive a process restart, and it didn't share state
across multiple instances — anyone who horizontally scaled the engine
would have bypassed the limit by hitting a different worker.

Usage::

    from app.services.rate_limit import check_and_record

    # Raises HTTPException(429) if exceeded
    check_and_record(db, org_id, bucket_key="scoring",
                     max_per_minute=100, count=len(lead_ids))

Cost: one count query per call + one bulk insert. The index covers
both. Cleanup of rows older than ~1 hour is a separate scheduled
sweep, not run inline.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import HTTPException
from supabase import Client

logger = logging.getLogger(__name__)


def _window_start_iso(seconds: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).isoformat()


def current_count(
    db: Client,
    org_id: str,
    *,
    bucket_key: str,
    window_seconds: int = 60,
) -> int:
    """Return the number of events recorded for (org_id, bucket_key) in
    the last `window_seconds`. Does not write."""
    resp = (
        db.table("rate_limit_events")
        .select("id", count="exact")
        .eq("org_id", org_id)
        .eq("bucket_key", bucket_key)
        .gte("event_at", _window_start_iso(window_seconds))
        .execute()
    )
    return resp.count or 0


def record_events(
    db: Client,
    org_id: str,
    *,
    bucket_key: str,
    count: int = 1,
) -> None:
    """Insert `count` rate-limit event rows. Best-effort — failures are
    logged at WARN but never propagate, because losing one count is
    less bad than failing a request the user paid for."""
    if count <= 0:
        return
    rows = [
        {"org_id": org_id, "bucket_key": bucket_key}
        for _ in range(count)
    ]
    try:
        db.table("rate_limit_events").insert(rows).execute()
    except Exception as e:
        logger.warning(
            "Rate-limit event insert failed (org=%s, bucket=%s, n=%d): %s",
            org_id, bucket_key, count, e,
        )


def check_and_record(
    db: Client,
    org_id: str,
    *,
    bucket_key: str,
    max_per_minute: int,
    count: int = 1,
) -> None:
    """Atomic-feeling check+record: count events in the last 60 s,
    raise 429 if (current + count) would exceed the limit, otherwise
    record the new events.

    The check is not strictly atomic — two concurrent requests can both
    pass the check before either records — but the worst case is an
    over-allowance of a few events per process race, not unbounded
    drift. Acceptable for a usage cap.
    """
    current = current_count(db, org_id, bucket_key=bucket_key)
    if current + count > max_per_minute:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Rate limit exceeded: max {max_per_minute} per minute "
                f"for {bucket_key} (current={current}, attempted={count})."
            ),
        )
    record_events(db, org_id, bucket_key=bucket_key, count=count)


def sweep_old_events(
    db: Client,
    older_than_minutes: int = 60,
) -> int:
    """Delete events older than `older_than_minutes`. Returns the row
    count deleted (best-effort; 0 if the underlying provider doesn't
    return one)."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
    ).isoformat()
    try:
        resp = (
            db.table("rate_limit_events")
            .delete()
            .lt("event_at", cutoff)
            .execute()
        )
        return len(resp.data or [])
    except Exception as e:
        logger.warning("Rate-limit sweep failed: %s", e)
        return 0
