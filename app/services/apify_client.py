"""Apify API client for LinkedIn scraping."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
from urllib.parse import quote

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

APIFY_BASE_URL = "https://api.apify.com/v2"
REQUEST_TIMEOUT = 180  # actors can take a few minutes

# How far back to keep LinkedIn posts when normalising. Anything older than
# this is dropped before being passed to Claude — old posts produce stale
# "buying signals" like a year-old funding round being treated as recent.
LINKEDIN_POSTS_MAX_AGE_DAYS = 180  # ~6 months


def scrape_linkedin_jobs(company_name: str, max_results: int = 25) -> Optional[List[Dict[str, Any]]]:
    """Scrape LinkedIn job postings for a company using Apify.

    Args:
        company_name: Company name to search for
        max_results: Maximum number of job results

    Returns:
        List of job dicts with title, location, url, description, etc.
        None if scraping failed.
    """
    if not settings.APIFY_API_KEY:
        logger.warning("APIFY_API_KEY not set, skipping LinkedIn jobs scrape")
        return None

    encoded_name = quote(company_name)
    search_url = f"https://www.linkedin.com/jobs/search?keywords={encoded_name}&location=United%20Kingdom"

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.post(
                f"{APIFY_BASE_URL}/acts/curious_coder~linkedin-jobs-scraper/run-sync-get-dataset-items",
                params={"token": settings.APIFY_API_KEY},
                json={
                    "urls": [search_url],
                    "scrapeCompany": True,
                    "count": max_results,
                },
            )
            if not response.is_success:
                logger.error("Apify LinkedIn jobs scrape HTTP %d for '%s': %s", response.status_code, company_name, response.text)
                return None
            results = response.json()

        logger.info("Apify LinkedIn jobs scrape for '%s': %d results", company_name, len(results))

        # Normalise the results into a consistent format
        jobs = []
        for item in results:
            jobs.append({
                "title": item.get("title") or item.get("jobTitle") or "",
                "company": item.get("companyName") or item.get("company") or company_name,
                "location": item.get("location") or item.get("jobLocation") or "",
                "url": item.get("url") or item.get("jobUrl") or item.get("link") or "",
                "description": (item.get("description") or item.get("jobDescription") or "")[:500],
                "posted_date": item.get("postedAt") or item.get("publishedAt") or item.get("date") or "",
                "source": "linkedin_apify",
            })

        return jobs

    except Exception as e:
        logger.error("Apify LinkedIn jobs scrape failed for '%s': %s", company_name, e)
        return None


def scrape_linkedin_posts(
    profile_url: str,
    max_posts: int = 20,
    timeout: int = REQUEST_TIMEOUT,
    db: Optional[Any] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Scrape LinkedIn posts from a profile using Apify.

    Args:
        profile_url: LinkedIn profile URL (e.g. https://linkedin.com/in/username)
        max_posts: Maximum number of posts to scrape
        timeout: httpx request timeout in seconds. Defaults to the module-level
            REQUEST_TIMEOUT (180s). Callers that need a faster fail (e.g. the
            weekly watchlist cron) can pass a smaller value.
        db: optional Supabase client. When set, raw actor items are cached
            in enrichment_cache for 24h keyed by profile URL (max_posts in
            the mode so a 10-post scrape never shortchanges a 20-post
            caller) and re-normalised on read, keeping the freshness
            filter relative to now. Failures are never cached.

    Returns:
        List of post dicts with text, date, likes, comments, etc.
        None if scraping failed.
    """
    if not settings.APIFY_API_KEY:
        logger.warning("APIFY_API_KEY not set, skipping LinkedIn posts scrape")
        return None

    # Ensure URL has trailing slash and correct format
    if not profile_url.endswith("/"):
        profile_url += "/"
    if not profile_url.startswith("https://"):
        if profile_url.startswith("http://"):
            profile_url = profile_url.replace("http://", "https://")
        elif profile_url.startswith("www."):
            profile_url = "https://" + profile_url
        else:
            profile_url = "https://www.linkedin.com/in/" + profile_url

    if db is not None:
        from app.services.enrichers.cache import get_cached
        cached = get_cached(db, profile_url, "linkedin_posts", f"max{max_posts}")
        if cached is not None:
            return _normalise_linkedin_posts(cached.get("items") or [])

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{APIFY_BASE_URL}/acts/harvestapi~linkedin-profile-posts/run-sync-get-dataset-items",
                params={"token": settings.APIFY_API_KEY},
                json={
                    "profileUrls": [profile_url],
                    "maxPosts": max_posts,
                    "includeQuotePosts": True,
                    "includeReposts": False,
                },
            )
            response.raise_for_status()
            results = response.json()

        logger.info("Apify LinkedIn posts scrape for '%s': %d posts", profile_url, len(results))

        if db is not None and isinstance(results, list):
            from app.services.enrichers.cache import set_cache
            set_cache(db, profile_url, "linkedin_posts", f"max{max_posts}", {"items": results})

        # Normalise the results into a consistent format and drop stale posts.
        posts = _normalise_linkedin_posts(results)
        logger.info(
            "After normalisation + freshness filter: %d posts (max age %d days)",
            len(posts),
            LINKEDIN_POSTS_MAX_AGE_DAYS,
        )

        return posts

    except Exception as e:
        logger.error("Apify LinkedIn posts scrape failed for '%s': %s", profile_url, e)
        return None


def _normalise_linkedin_posts(raw_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert raw harvestapi~linkedin-profile-posts items to our flat schema.

    The actor returns a richer object than my original code anticipated:
      - Post text lives in `content`
      - Date is a NESTED object: `postedAt.date` (ISO string), `postedAt.timestamp` (ms)
      - Engagement metrics are nested under `engagement.likes`, `engagement.comments`, `engagement.shares`
      - The post URL is `linkedinUrl`

    This function:
      1. Pulls the right fields from the nested structure
      2. Falls back to alternate field names for older actor versions
      3. Drops any post older than LINKEDIN_POSTS_MAX_AGE_DAYS so Claude
         doesn't treat year-old funding rounds as "recent buying signals"
      4. Returns a flat list with consistent field names ready for the
         downstream content analyser
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=LINKEDIN_POSTS_MAX_AGE_DAYS)
    normalised: List[Dict[str, Any]] = []
    dropped_old = 0
    dropped_no_date = 0
    dropped_no_text = 0

    for item in raw_items:
        # Text — `content` is the primary field, fallbacks for older actors
        text = (
            item.get("content")
            or item.get("text")
            or item.get("postText")
            or item.get("commentary")
            or item.get("body")
            or ""
        )
        if not text:
            dropped_no_text += 1
            continue

        # Date — nested under postedAt for the current actor version
        post_date_str, post_date_obj = _extract_post_date(item)

        # Apply freshness filter — drop anything older than the cutoff
        if post_date_obj is None:
            # Unknown date — keep it (don't punish posts for missing metadata),
            # but log so we know if this becomes a problem.
            dropped_no_date += 1
        elif post_date_obj < cutoff:
            dropped_old += 1
            continue

        # Engagement — nested under `engagement` for the current actor version
        engagement = item.get("engagement") or {}
        if isinstance(engagement, dict):
            likes = engagement.get("likes") or 0
            comments = engagement.get("comments") or 0
            shares = engagement.get("shares") or 0
        else:
            likes = comments = shares = 0
        # Fall back to top-level fields for older actor schemas
        likes = likes or item.get("likes") or item.get("numLikes") or item.get("totalReactionCount") or 0
        comments = comments or item.get("comments") or item.get("numComments") or item.get("totalCommentCount") or 0
        shares = shares or item.get("shares") or item.get("numShares") or item.get("repostCount") or 0

        # URL — `linkedinUrl` is the canonical field on the current actor
        url = (
            item.get("linkedinUrl")
            or item.get("postUrl")
            or item.get("url")
            or item.get("link")
            or ""
        )
        # Some actors return a `socialContent.shareUrl` instead
        if not url:
            social = item.get("socialContent") or {}
            if isinstance(social, dict):
                url = social.get("shareUrl") or ""

        normalised.append({
            "text": text,
            "date": post_date_str,  # Always a flat ISO string, never a dict
            "likes": int(likes) if isinstance(likes, (int, float, str)) and str(likes).isdigit() or isinstance(likes, (int, float)) else 0,
            "comments": int(comments) if isinstance(comments, (int, float, str)) and str(comments).isdigit() or isinstance(comments, (int, float)) else 0,
            "shares": int(shares) if isinstance(shares, (int, float, str)) and str(shares).isdigit() or isinstance(shares, (int, float)) else 0,
            "url": url,
            "source": "linkedin_apify",
        })

    if dropped_old or dropped_no_date or dropped_no_text:
        logger.info(
            "LinkedIn post normalisation: dropped %d old, %d undated, %d empty (kept %d)",
            dropped_old, dropped_no_date, dropped_no_text, len(normalised),
        )

    return normalised


def _extract_post_date(item: Dict[str, Any]) -> tuple:
    """Pull a post's date out of the various possible Apify shapes.

    Returns (iso_string, datetime_or_None). The ISO string is what we store;
    the datetime is what the freshness filter compares against.
    """
    # Current actor: postedAt is a nested object
    posted_at = item.get("postedAt")
    if isinstance(posted_at, dict):
        date_str = posted_at.get("date") or ""
        if date_str:
            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return date_str, dt
            except (ValueError, TypeError):
                pass
        # Try the timestamp fallback
        ts = posted_at.get("timestamp")
        if isinstance(ts, (int, float)) and ts > 0:
            try:
                dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                return dt.isoformat(), dt
            except (ValueError, OSError):
                pass

    # Older actors: flat string under various names
    for key in ("postedAt", "date", "publishedAt", "postedDate"):
        val = item.get(key)
        if isinstance(val, str) and val:
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return val, dt
            except (ValueError, TypeError):
                continue

    return "", None
