"""News enrichment via Tavily search.

Replaces the old Google News RSS source (dropped May 2026 — RSS was noisy,
had no native recency filter, and Tavily covers everything it did with
better signal). PredictLeads news_events stays primary in the pipeline;
this module is the supplementary layer that fills in when PredictLeads
returns nothing for a domain.

Output shape matches what the rest of the engine expects:
  {
    "source": "tavily",
    "news_found": bool,
    "article_count": int,
    "key_stories": [{title, url, source, date, summary}, ...],
    "growth_indicators": [...],   # from Claude analysis
    "personalisation_hooks": [...],
    "sentiment": "...",
    "notes": "...",
  }

Cached in `enrichment_cache` keyed by (cache_key, "news") with the standard
24h TTL configured in cache.TTL_SECONDS — daily-volatile, same as PredictLeads
news_events.
"""

import logging
from typing import Dict, List, Optional, Set

import httpx
from supabase import Client

from app.config import settings
from app.prompts.enrichment import (
    NEWS_ANALYSIS_PROMPT,
    CONTEXTUAL_NEWS_PROMPT,
    _format_services_list,
)
from app.services.claude import call_claude
from app.services.enrichers.cache import get_cached, set_cache
from app.services.enrichers.recency import filter_recent, parse_date

logger = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"
REQUEST_TIMEOUT = 20
FRESHNESS_DAYS = 180
MAX_RESULTS = 8


def enrich_news(
    db: Client,
    search_queries: List[str],
    company_name: str = "",
    first_name: str = "",
    last_name: str = "",
    title: str = "",
    client_context: Optional[Dict[str, object]] = None,
) -> dict:
    """Search Tavily for recent news matching the queries, summarise via Claude.

    Tavily handles the recency filter natively (`days=180`), and we double-check
    in Python via filter_recent so any results that slip past the API filter
    don't make it to the dashboard.
    """
    cache_key = "|".join(sorted(q.lower() for q in search_queries if q))
    cached = get_cached(db, cache_key, "news")
    if cached:
        return cached

    if not settings.TAVILY_API_KEY:
        logger.warning("TAVILY_API_KEY not set — returning empty news result")
        return _empty_result("Tavily API key not configured")

    # Run each query through Tavily and merge — first query is usually the
    # company name, second is "First Last Company" so we get both angles
    all_articles: List[dict] = []
    for query in search_queries:
        if not query:
            continue
        articles = _fetch_tavily_news(query)
        all_articles.extend(articles)

    # Dedupe by URL (more reliable than title across sources)
    seen: Set[str] = set()
    unique: List[dict] = []
    for a in all_articles:
        key = (a.get("url") or a.get("title") or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(a)

    # Defence-in-depth: even though Tavily is days-filtered, drop anything
    # that lacks a parseable date or falls outside our window.
    unique = filter_recent(unique, days=FRESHNESS_DAYS, date_key="date")

    if not unique:
        result = _empty_result("No recent news articles found")
        set_cache(db, cache_key, "news", None, result)
        return result

    # Format for Claude. We keep up to MAX_RESULTS by score, then sort by
    # date for the prompt so Claude sees recency.
    unique.sort(key=lambda a: -float(a.get("score") or 0))
    top = unique[:MAX_RESULTS]
    top.sort(
        key=lambda a: parse_date(a.get("date")) or _epoch(),
        reverse=True,
    )

    news_text = "\n\n".join(
        f"- Title: {a.get('title', 'N/A')}\n"
        f"  Source: {a.get('source', 'N/A')}\n"
        f"  Date: {a.get('date', 'N/A')}\n"
        f"  URL: {a.get('url', 'N/A')}\n"
        f"  Snippet: {(a.get('content') or '')[:400]}"
        for a in top
    )

    if client_context and client_context.get("services_offered"):
        prompt = CONTEXTUAL_NEWS_PROMPT.format(
            company_name=company_name,
            services_offered_list=_format_services_list(client_context.get("services_offered", [])),
            client_positioning=client_context.get("client_positioning", ""),
            client_name=client_context.get("client_name", "the client"),
            first_name=first_name,
            last_name=last_name,
            title=title,
            news_items=news_text,
        )
    else:
        prompt = NEWS_ANALYSIS_PROMPT.format(
            company_name=company_name,
            first_name=first_name,
            last_name=last_name,
            title=title,
            news_items=news_text,
        )

    result = call_claude(prompt)

    if isinstance(result, dict) and "error" not in result:
        # Force the source tag and ensure key_stories is also recency-filtered
        # in case Claude pulled in a story whose date we can't trust.
        result["source"] = "tavily"
        if isinstance(result.get("key_stories"), list):
            result["key_stories"] = filter_recent(
                result["key_stories"], days=FRESHNESS_DAYS, date_key="date",
            )
        result["article_count"] = len(top)
        set_cache(db, cache_key, "news", None, result)

    return result


# ---------------------------------------------------------------------------
# Tavily client
# ---------------------------------------------------------------------------

def _fetch_tavily_news(query: str) -> List[dict]:
    """Fetch news results from Tavily for a single query.

    Returns articles in our internal flat shape, with `date` as the original
    Tavily `published_date` string (RFC-2822). Claude understands either
    format; the recency filter handles both.
    """
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.post(
                TAVILY_URL,
                headers={"Content-Type": "application/json"},
                json={
                    "api_key": settings.TAVILY_API_KEY,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": MAX_RESULTS,
                    "days": FRESHNESS_DAYS,
                    "topic": "news",
                    "include_answer": False,
                    "include_raw_content": False,
                    "include_images": False,
                },
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as e:
        body = e.response.text if e.response is not None else ""
        logger.warning("Tavily HTTP %s for '%s': %s", e.response.status_code, query, body[:200])
        return []
    except httpx.TimeoutException:
        logger.warning("Tavily timed out for '%s'", query)
        return []
    except Exception as e:
        logger.warning("Tavily error for '%s': %s", query, e)
        return []

    raw_results = data.get("results") or []
    articles: List[dict] = []
    for r in raw_results:
        if not isinstance(r, dict):
            continue
        articles.append({
            "title": r.get("title") or "",
            "url": r.get("url") or "",
            "date": r.get("published_date") or "",
            "source": _domain_from_url(r.get("url") or ""),
            "content": r.get("content") or "",
            "score": float(r.get("score") or 0),
        })
    return articles


def _domain_from_url(url: str) -> str:
    """Extract a friendly source label from a URL (host without scheme/www)."""
    if not url:
        return ""
    s = url.split("://", 1)[-1]
    s = s.split("/", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    return s


def _empty_result(notes: str) -> dict:
    return {
        "source": "tavily",
        "news_found": False,
        "article_count": 0,
        "key_stories": [],
        "overall_media_presence": "none",
        "growth_indicators": [],
        "personalisation_hooks": [],
        "sentiment": "neutral",
        "notes": notes,
    }


def _epoch():
    """Earliest possible datetime — used as the sort sentinel for items
    whose date we couldn't parse, so they sink to the bottom."""
    from datetime import datetime, timezone
    return datetime.min.replace(tzinfo=timezone.utc)
