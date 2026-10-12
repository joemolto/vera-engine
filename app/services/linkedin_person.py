"""Apify wrappers for person-level LinkedIn scraping.

Three actors wrapped here, each with a focused purpose:

  1. PROFILE       (supreme_coder/linkedin-profile-scraper, $3/1k)
     → current role, current_company, work history with dates
     → used to detect job changes by comparing baseline → next scrape

  2. POSTS         (harvestapi/linkedin-profile-posts, $1.50/1k)
     → posts written BY the person on their own profile
     → existing apify_client also calls this actor for company posts;
       we call it here with the person profile URL

  3. ACTIVITY      (unseenuser/linkedin-user-comments-reactions, $5/1k)
     → comments and reactions the person left on OTHERS' posts
     → THIS is the new capability — what they're listening to,
       not just what they're saying

All three are pay-per-result, cookie-free. Each function returns
normalised dicts ready for the person_intent.py classifier, or [] on
failure (we never raise — monitor pass must not block on LinkedIn
fragility).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from supabase import Client

from app.config import settings
from app.services.enrichers.cache import get_cached, set_cache

logger = logging.getLogger(__name__)

_APIFY_BASE = "https://api.apify.com/v2"
_TIMEOUT = 120

# Drop activity older than this when normalising. A post older than a
# month is a dead opener — "the moment's gone" (2026-07-07), so we
# never collect or classify it. Tightened from 90d.
_FRESHNESS_DAYS = 30


def _scrub(e: Exception) -> str:
    """Exception text with the Apify token redacted — httpx errors embed
    the full request URL, token included (found leaking 2026-07-07)."""
    s = str(e)
    if settings.APIFY_API_KEY:
        s = s.replace(settings.APIFY_API_KEY, "***")
    return s


# ---------------------------------------------------------------------------
# 1. PROFILE — for job-change detection
# ---------------------------------------------------------------------------
def scrape_profile(
    linkedin_url: str, db: Optional[Client] = None
) -> Optional[Dict[str, Any]]:
    """Scrape one LinkedIn profile. Returns a dict with current role
    info + work history, or None on failure.

    The actor returns rich data; we only normalise the fields needed
    for downstream job-change detection. Full payload is persisted to
    raw_artifacts by the caller if they want it.

    With `db`, the raw actor payload is cached in enrichment_cache
    keyed by profile URL (6-day TTL) — pay-per-result actors shouldn't
    be re-run when another org scraped the same person this week. The
    raw item is cached (not the normalised dict) so reads always go
    through the current normaliser. Failures are never cached.
    """
    if not settings.APIFY_API_KEY:
        return None
    url = _ensure_in_profile_url(linkedin_url)
    if not url:
        return None
    if db is not None:
        cached = get_cached(db, url, "linkedin_profile")
        if cached is not None:
            return _normalise_profile(cached.get("item") or {})
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            res = c.post(
                f"{_APIFY_BASE}/acts/supreme_coder~linkedin-profile-scraper/run-sync-get-dataset-items",
                params={"token": settings.APIFY_API_KEY},
                # Actor wants `urls` as an array of {url} objects, not
                # `profileUrls` of strings (verified 2026-07-07 — the old
                # form 400'd every call). NOTE: this actor also needs an
                # Apify residential proxy; without one it returns
                # "Proxy authentication required" per profile. Job-change
                # detection (person_new_in_role) depends on it; the posts
                # and activity scrapes below do not.
                json={"urls": [{"url": url}]},
            )
            res.raise_for_status()
            items = res.json()
    except Exception as e:
        logger.warning("Profile scrape failed for %s: %s", url, _scrub(e))
        return None

    if not isinstance(items, list) or not items:
        return None
    if db is not None:
        set_cache(db, url, "linkedin_profile", None, {"item": items[0]})
    return _normalise_profile(items[0])


def _normalise_profile(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Extract just the fields we need for change-detection.

    The actor's output schema varies slightly between runs; we pull
    work history defensively. Current role = first item in the
    positions list (LinkedIn shows current at top)."""
    positions = (
        raw.get("positions")
        or raw.get("experience")
        or raw.get("workExperience")
        or []
    )
    current: Dict[str, Any] = {}
    if isinstance(positions, list) and positions:
        first = positions[0]
        if isinstance(first, dict):
            current = first

    started_at = (
        current.get("startDate")
        or current.get("started_at")
        or current.get("start_date")
    )
    days_in_role = _days_since(started_at)

    return {
        "name": (raw.get("name") or raw.get("fullName") or "").strip(),
        "current_title": (
            current.get("title")
            or raw.get("headline")
            or raw.get("title")
            or ""
        ).strip(),
        "current_company": (
            current.get("companyName")
            or current.get("company")
            or ""
        ).strip(),
        "current_role_started_at": started_at,
        "days_in_current_role": days_in_role,
        "headline": (raw.get("headline") or "").strip(),
        "location": (raw.get("location") or "").strip(),
        "positions": positions if isinstance(positions, list) else [],
    }


# ---------------------------------------------------------------------------
# 2. POSTS — content the person posted themselves
# ---------------------------------------------------------------------------
def scrape_person_posts(
    linkedin_url: str,
    *,
    max_posts: int = 15,
    since_iso: Optional[str] = None,
    db: Optional[Client] = None,
) -> List[Dict[str, Any]]:
    """Posts BY the person on their own profile. `since_iso` lets the
    monitor pass only fetch incremental items after baseline.

    With `db`, raw actor items are cached for 6 days (shared across
    orgs) and re-normalised on read, so each caller's own since_iso
    and the 30-day freshness window still apply."""
    if not settings.APIFY_API_KEY:
        return []
    url = _ensure_in_profile_url(linkedin_url)
    if not url:
        return []
    if db is not None:
        cached = get_cached(db, url, "linkedin_person_posts")
        if cached is not None:
            return _normalise_posts(cached.get("items") or [], since_iso=since_iso)
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            res = c.post(
                f"{_APIFY_BASE}/acts/harvestapi~linkedin-profile-posts/run-sync-get-dataset-items",
                params={"token": settings.APIFY_API_KEY},
                json={
                    "profileUrls": [url],
                    "maxPosts": max_posts,
                    "includeQuotePosts": True,
                    "includeReposts": False,
                },
            )
            res.raise_for_status()
            items = res.json()
    except Exception as e:
        logger.warning("Person posts scrape failed for %s: %s", url, _scrub(e))
        return []
    if not isinstance(items, list):
        return []
    if db is not None:
        # An empty list is a real actor result (person posts nothing) —
        # cache it too so we don't re-pay for the same answer this week.
        set_cache(db, url, "linkedin_person_posts", None, {"items": items})
    return _normalise_posts(items, since_iso=since_iso)


def _normalise_posts(
    raw_items: List[Dict[str, Any]], *, since_iso: Optional[str]
) -> List[Dict[str, Any]]:
    cutoff = _resolve_cutoff(since_iso)
    out: List[Dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        text = (
            item.get("content")
            or item.get("text")
            or item.get("post_content")
            or ""
        ).strip()
        if not text:
            continue
        posted = _resolve_post_date(item)
        if posted and cutoff and posted < cutoff:
            continue
        out.append(
            {
                "kind": "post",
                "text": text,
                "posted_at": posted.isoformat() if posted else None,
                "url": (
                    item.get("linkedinUrl")
                    or item.get("url")
                    or ""
                ),
                "engagement": item.get("engagement") or {},
            }
        )
    return out


# ---------------------------------------------------------------------------
# 3. ACTIVITY — comments + reactions on OTHERS' posts
# ---------------------------------------------------------------------------
def scrape_person_activity(
    linkedin_url: str,
    *,
    max_items: int = 15,
    since_iso: Optional[str] = None,
    db: Optional[Client] = None,
) -> List[Dict[str, Any]]:
    """Comments + reactions the person left on OTHERS' posts.

    Returns each item with the ORIGINAL post's text + author plus the
    person's contribution (comment text, or reaction type). The
    classifier uses both the original post content (what was the topic?)
    and the engagement type (comment vs reaction) to score.

    With `db`, raw actor items are cached for 6 days (shared across
    orgs) — this is our most expensive actor ($5/1k), so one org's
    scrape of a person should serve everyone watching them that week.
    Re-normalised on read so since_iso + freshness still apply.
    """
    if not settings.APIFY_API_KEY:
        return []
    url = _ensure_in_profile_url(linkedin_url)
    if not url:
        return []
    if db is not None:
        cached = get_cached(db, url, "linkedin_person_activity")
        if cached is not None:
            return _normalise_activity(
                cached.get("items") or [], since_iso=since_iso
            )[:max_items]
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            res = c.post(
                f"{_APIFY_BASE}/acts/unseenuser~linkedin-user-comments-reactions/run-sync-get-dataset-items",
                params={"token": settings.APIFY_API_KEY},
                json={
                    # The actor's required field is `profiles` — the code
                    # shipped 2026-06-08 sent `profileUrls`, which the actor
                    # 400-rejected on EVERY call, so this scrape had never
                    # succeeded in production (found via dry run 2026-07-07).
                    "profiles": [url],
                    "scrapeType": "both",
                    "maxItems": max_items,
                    "dateFilter": "past_3_months",
                },
            )
            res.raise_for_status()
            items = res.json()
    except Exception as e:
        logger.warning("Person activity scrape failed for %s: %s", url, _scrub(e))
        return []
    if not isinstance(items, list):
        return []
    if db is not None:
        set_cache(db, url, "linkedin_person_activity", None, {"items": items})
    # The actor ignores maxItems (a 10-item request returned 159), so cap
    # after normalising — freshest first because the actor returns newest
    # first.
    return _normalise_activity(items, since_iso=since_iso)[:max_items]


def _normalise_activity(
    raw_items: List[Dict[str, Any]], *, since_iso: Optional[str]
) -> List[Dict[str, Any]]:
    cutoff = _resolve_cutoff(since_iso)
    out: List[Dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        # The unseenuser actor returns one record per comment OR reaction.
        # Real payloads (verified 2026-07-07) carry no `type` field — the
        # `action` string says which it is ("James commented" / "James
        # liked/celebrated/reacted...").
        item_type = (item.get("type") or "").lower()
        if item_type not in ("comment", "reaction", "like"):
            action = (item.get("action") or "").lower()
            if "comment" in action or item.get("commentText") or item.get("comment_text") or item.get("comment"):
                item_type = "comment"
            elif item.get("reactionType") or item.get("reaction_type") or any(
                # Stems, not past-tense words: the actor emits "loves this",
                # "likes this", "celebrates this" etc. (verified 2026-07-07).
                w in action for w in ("like", "love", "celebrat", "support", "insight", "react", "found this", "curious", "funny")
            ):
                item_type = "reaction"
            else:
                continue

        posted = _resolve_activity_date(item)
        if posted and cutoff and posted < cutoff:
            continue

        # Original post lives nested under `post` in the real payload
        # (post_text / post_author / post_url); older key guesses kept as
        # fallbacks.
        post = item.get("post") if isinstance(item.get("post"), dict) else {}
        original_text = (
            post.get("post_text")
            or post.get("content")
            or item.get("postContent")
            or item.get("post_content")
            or item.get("originalPost")
            or ""
        )
        if isinstance(original_text, dict):
            original_text = original_text.get("content") or ""
        original_text = (original_text or "").strip()

        original_author = (
            post.get("post_author")
            or item.get("postAuthor")
            or item.get("post_author")
            or item.get("author")
            or ""
        )
        if isinstance(original_author, dict):
            original_author = (
                original_author.get("name")
                or original_author.get("fullName")
                or ""
            )
        original_author = (original_author or "").strip()

        comment_text = (
            item.get("commentText")
            or item.get("comment_text")
            or item.get("comment")
            or ""
        ).strip()
        reaction_type = (
            item.get("reactionType")
            or item.get("reaction_type")
            or ""
        ).strip()

        # Skip empty entries — no original-post text means we can't
        # judge topic, no comment + no reaction means nothing happened.
        if not original_text:
            continue
        if item_type == "comment" and not comment_text:
            continue

        out.append(
            {
                "kind": item_type,            # "comment" | "reaction"
                "comment_text": comment_text,
                "reaction_type": reaction_type,
                "original_post_text": original_text,
                "original_post_author": original_author,
                "posted_at": posted.isoformat() if posted else None,
                "url": (
                    item.get("commentLink")
                    or item.get("comment_link")
                    or (item.get("post") or {}).get("post_url")
                    or item.get("postUrl")
                    or item.get("post_url")
                    or ""
                ),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ensure_in_profile_url(url: str) -> Optional[str]:
    """Normalise to https://www.linkedin.com/in/<handle>/ form.
    Returns None for anything that doesn't look like a person URL."""
    if not url:
        return None
    u = url.strip()
    if u.startswith("http://"):
        u = u.replace("http://", "https://")
    if u.startswith("www."):
        u = "https://" + u
    if not u.startswith("https://"):
        # Assume it's a handle
        u = f"https://www.linkedin.com/in/{u.strip('/')}/"
    if "/in/" not in u:
        return None
    if not u.endswith("/"):
        u += "/"
    return u


def _resolve_cutoff(since_iso: Optional[str]) -> Optional[datetime]:
    """Cutoff for filtering: max(since_iso, freshness window)."""
    fresh = datetime.now(timezone.utc) - timedelta(days=_FRESHNESS_DAYS)
    if not since_iso:
        return fresh
    try:
        s = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        return max(fresh, s)
    except Exception:
        return fresh


def _resolve_post_date(item: Dict[str, Any]) -> Optional[datetime]:
    raw = item.get("postedAt")
    if isinstance(raw, dict):
        s = raw.get("date") or raw.get("iso")
        if s:
            return _parse_iso(s)
        ts = raw.get("timestamp")
        if isinstance(ts, (int, float)) and ts > 0:
            # Apify timestamps are ms since epoch
            return datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
    for key in ("posted_at", "date", "publishedAt", "published_at"):
        v = item.get(key)
        if isinstance(v, str):
            return _parse_iso(v)
    return None


def _parse_date_value(v: Any) -> Optional[datetime]:
    if isinstance(v, dict):
        # Real actor payload: {"formatted": "2026-05-04T11:49:39.833Z",
        # "relative": "...", "timestamp": ...} (verified 2026-07-07).
        s = v.get("formatted") or v.get("date") or v.get("iso")
        if isinstance(s, str):
            parsed = _parse_iso(s)
            if parsed:
                return parsed
        ts = v.get("timestamp")
        if isinstance(ts, (int, float)) and ts > 0:
            return datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
        return None
    if isinstance(v, str):
        return _parse_iso(v)
    if isinstance(v, (int, float)) and v > 0:
        return datetime.fromtimestamp(v / 1000.0, tz=timezone.utc)
    return None


def _resolve_activity_date(item: Dict[str, Any]) -> Optional[datetime]:
    for key in ("commentDate", "comment_date", "createdAt", "created_at", "date"):
        parsed = _parse_date_value(item.get(key))
        if parsed:
            return parsed
    # Reactions carry no date of their own — fall back to the reacted-to
    # post's date, which is the OLDEST the reaction can be (a reaction
    # can't precede its post), i.e. a conservative freshness bound.
    post = item.get("post")
    if isinstance(post, dict):
        return _parse_date_value(post.get("created_at"))
    return None


def _parse_iso(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _days_since(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None
    return (datetime.now(timezone.utc) - dt).days
