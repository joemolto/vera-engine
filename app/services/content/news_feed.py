"""Fetch industry-level news for content generation using Google News RSS."""

import logging
from typing import Any, Dict, List, Set
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10


def fetch_industry_news(icp_config: Dict[str, Any]) -> List[Dict[str, str]]:
    """Fetch industry news based on the client's ICP target verticals.

    Builds search queries from ICP config, fetches Google News RSS,
    and returns the top 10-15 unique articles.
    """
    queries = _build_queries(icp_config)

    all_articles: List[Dict[str, str]] = []
    for query in queries:
        articles = _fetch_google_news_rss(query)
        all_articles.extend(articles)

    # Deduplicate by title
    seen_titles: Set[str] = set()
    unique_articles: List[Dict[str, str]] = []
    for article in all_articles:
        title_key = article.get("title", "").lower()
        if title_key and title_key not in seen_titles:
            seen_titles.add(title_key)
            unique_articles.append(article)

    # Return top 15
    result = unique_articles[:15]
    logger.info("Fetched %d unique industry news articles from %d queries", len(result), len(queries))
    return result


def _build_queries(icp_config: Dict[str, Any]) -> List[str]:
    """Build 3-5 Google News search queries from ICP config."""
    queries: List[str] = []

    # Extract useful fields from ICP config
    target_industries = icp_config.get("target_industries", [])
    target_verticals = icp_config.get("target_verticals", [])
    keywords = icp_config.get("keywords", [])
    company_type = icp_config.get("company_type", "")
    geo = icp_config.get("geography", icp_config.get("geo", "UK"))

    # Combine industries and verticals
    industries = target_industries or target_verticals

    # Always include a broad cybersecurity query
    queries.append(f"cybersecurity {geo} compliance regulation")

    # Industry-specific queries
    for industry in industries[:2]:
        queries.append(f"{industry} cybersecurity {geo}")

    # Keyword-based queries
    if keywords:
        kw_str = " ".join(keywords[:3])
        queries.append(f"{kw_str} {geo}")

    # Company-type specific
    if company_type:
        queries.append(f"{company_type} trends {geo}")

    # Ensure at least 3 queries, max 5
    if len(queries) < 3:
        queries.append(f"CISO hiring trends {geo}")
    if len(queries) < 3:
        queries.append(f"managed security services {geo}")

    return queries[:5]


def _fetch_google_news_rss(query: str) -> List[Dict[str, str]]:
    """Fetch articles from Google News RSS feed."""
    encoded_query = quote_plus(query)
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-GB&gl=GB&ceid=GB:en"

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.get(url)
            response.raise_for_status()

        soup = BeautifulSoup(response.text, "xml")
        items = soup.find_all("item")

        articles: List[Dict[str, str]] = []
        for item in items:
            articles.append({
                "title": item.find("title").get_text() if item.find("title") else "",
                "link": item.find("link").get_text() if item.find("link") else "",
                "date": item.find("pubDate").get_text() if item.find("pubDate") else "",
                "source": item.find("source").get_text() if item.find("source") else "",
            })

        return articles

    except Exception as e:
        logger.error("Google News RSS error for '%s': %s", query, e)
        return []
