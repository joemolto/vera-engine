"""Date filtering — keeps enrichment data within a freshness window.

The dashboard surfaces news, signals, and "recent" events. Without enforcement,
old PredictLeads events (>2 years ago) and stale Tavily results leak through
into the GTM briefing and confuse outreach. We filter at the boundary between
raw payload and the rendered `key_stories` list — raw stays full (so we can
re-derive if the cutoff changes), the rendered list only contains items inside
the window.

Default window: 180 days (≈ 6 months).
"""

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, List, Optional

DEFAULT_FRESHNESS_DAYS = 180


def parse_date(value: Any) -> Optional[datetime]:
    """Parse a date from any of the formats our enrichers emit.

    PredictLeads and Anthropic emit ISO-8601 (`2025-04-28T18:19:00Z`).
    Tavily and Google RSS emit RFC-2822 (`Tue, 28 Apr 2026 18:19:00 GMT`).
    Returns timezone-aware datetime in UTC, or None if unparseable.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()

    # ISO-8601 — fromisoformat handles "2025-04-28T18:19:00+00:00" but not "Z"
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    # RFC-2822
    try:
        d = parsedate_to_datetime(s)
        if d is None:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def is_recent(value: Any, days: int = DEFAULT_FRESHNESS_DAYS) -> bool:
    """True if `value` parses to a date inside the freshness window.

    Items with no parseable date are treated as NOT recent — we'd rather
    drop a story whose date we can't verify than surface a year-old event
    in a "recent activity" feed.
    """
    d = parse_date(value)
    if d is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return d >= cutoff


def filter_recent(
    items: List[dict],
    days: int = DEFAULT_FRESHNESS_DAYS,
    date_key: str = "date",
) -> List[dict]:
    """Return items whose `date_key` is within the last `days` days.

    Drops items with missing or unparseable dates — see is_recent.
    Preserves order.
    """
    return [
        item for item in items
        if isinstance(item, dict) and is_recent(item.get(date_key), days=days)
    ]
