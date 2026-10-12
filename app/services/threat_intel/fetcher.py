import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def fetch_ncsc_advisories(since_hours: int = 48) -> List[Dict[str, Any]]:
    """Fetch recent NCSC advisories via RSS."""
    url = "https://www.ncsc.gov.uk/api/1/services/v1/report-rss-feed.xml"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    items = []

    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(url)
            response.raise_for_status()

        root = ET.fromstring(response.text)
        # RSS feeds use channel/item structure
        for item in root.iter("item"):
            title = _xml_text(item, "title")
            description = _xml_text(item, "description")
            link = _xml_text(item, "link")
            pub_date = _xml_text(item, "pubDate")

            published_at = _parse_rss_date(pub_date)
            if published_at and published_at < cutoff:
                continue

            items.append({
                "source": "ncsc",
                "source_id": link or title,
                "title": title or "NCSC Advisory",
                "description": description or "",
                "url": link or "",
                "published_at": published_at.isoformat() if published_at else None,
                "severity": "high",
            })

        logger.info("Fetched %d NCSC advisories (since %dh)", len(items), since_hours)
    except Exception as e:
        logger.error("NCSC RSS fetch failed: %s", e)
        raise

    return items


def fetch_ncsc_all_feed(since_hours: int = 48) -> List[Dict[str, Any]]:
    """Fetch all NCSC publications including threat reports."""
    url = "https://www.ncsc.gov.uk/api/1/services/v1/all-rss-feed.xml"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    items = []

    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(url)
            response.raise_for_status()

        root = ET.fromstring(response.text)
        for item in root.iter("item"):
            title = _xml_text(item, "title")
            description = _xml_text(item, "description")
            link = _xml_text(item, "link")
            pub_date = _xml_text(item, "pubDate")

            published_at = _parse_rss_date(pub_date)
            if published_at and published_at < cutoff:
                continue

            # Skip if already captured by advisories feed (dedupe by link)
            items.append({
                "source": "ncsc",
                "source_id": link or title,
                "title": title or "NCSC Publication",
                "description": description or "",
                "url": link or "",
                "published_at": published_at.isoformat() if published_at else None,
                "severity": "medium",
            })

        logger.info("Fetched %d NCSC all-feed items (since %dh)", len(items), since_hours)
    except Exception as e:
        logger.error("NCSC all-feed fetch failed: %s", e)
        raise

    return items


def fetch_nvd_critical_cves(since_hours: int = 48) -> List[Dict[str, Any]]:
    """Fetch recent critical/high CVEs from NVD."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=since_hours)
    # NVD requires ISO 8601 format with T and specific precision
    start_str = start.strftime("%Y-%m-%dT%H:%M:%S.000")
    end_str = now.strftime("%Y-%m-%dT%H:%M:%S.000")

    base_url = "https://services.nvd.nist.gov/rest/json/cves/2.0"
    items = []

    for severity in ["CRITICAL", "HIGH"]:
        params = {
            "pubStartDate": start_str,
            "pubEndDate": end_str,
            "cvssV3Severity": severity,
        }

        try:
            with httpx.Client(timeout=30) as client:
                response = client.get(base_url, params=params)
                response.raise_for_status()

            data = response.json()
            try:
                from app.database import get_supabase
                from app.services.raw_artifacts import persist_raw
                persist_raw(
                    get_supabase(), "nvd.nist.gov", "nvd", data,
                    source_endpoint=f"cves_{severity.lower()}_{since_hours}h",
                )
            except Exception:
                pass
            vulnerabilities = data.get("vulnerabilities", [])

            for vuln in vulnerabilities:
                cve = vuln.get("cve", {})
                cve_id = cve.get("id", "")
                descriptions = cve.get("descriptions", [])
                desc_en = next(
                    (d.get("value", "") for d in descriptions if d.get("lang") == "en"),
                    descriptions[0].get("value", "") if descriptions else "",
                )

                # Extract CVSS score
                metrics = cve.get("metrics", {})
                cvss_score = None
                cvss_data = metrics.get("cvssMetricV31", metrics.get("cvssMetricV30", []))
                if cvss_data:
                    cvss_score = cvss_data[0].get("cvssData", {}).get("baseScore")

                # Extract affected vendors/products from configurations
                vendors = []
                technologies = []
                configurations = cve.get("configurations", [])
                for config in configurations:
                    for node in config.get("nodes", []):
                        for cpe_match in node.get("cpeMatch", []):
                            criteria = cpe_match.get("criteria", "")
                            parts = criteria.split(":")
                            if len(parts) >= 5:
                                vendor = parts[3]
                                product = parts[4]
                                if vendor and vendor != "*":
                                    vendors.append(vendor)
                                if product and product != "*":
                                    technologies.append(product)

                published = cve.get("published", "")
                url = f"https://nvd.nist.gov/vuln/detail/{cve_id}"

                items.append({
                    "source": "nvd",
                    "source_id": cve_id,
                    "title": f"{cve_id} ({severity})",
                    "description": desc_en,
                    "severity": severity.lower(),
                    "url": url,
                    "published_at": published,
                    "affected_vendors": list(set(vendors)),
                    "affected_technologies": list(set(technologies)),
                    "cvss_score": cvss_score,
                })

            logger.info("Fetched %d %s CVEs from NVD", len(vulnerabilities), severity)
        except Exception as e:
            logger.error("NVD fetch failed for %s: %s", severity, e)
            raise

        # NVD rate limit: 5 requests per 30 seconds
        time.sleep(7)

    return items


def fetch_cisa_kev_recent(since_hours: int = 48) -> List[Dict[str, Any]]:
    """Fetch recently added CISA Known Exploited Vulnerabilities."""
    url = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    items = []

    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(url)
            response.raise_for_status()

        data = response.json()
        try:
            from app.database import get_supabase
            from app.services.raw_artifacts import persist_raw
            persist_raw(
                get_supabase(), "cisa.gov", "cisa_kev", data,
                source_endpoint=f"kev_recent_{since_hours}h",
            )
        except Exception:
            pass
        vulnerabilities = data.get("vulnerabilities", [])

        for vuln in vulnerabilities:
            date_added = vuln.get("dateAdded", "")
            try:
                added_dt = datetime.strptime(date_added, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            if added_dt < cutoff:
                continue

            cve_id = vuln.get("cveID", "")
            vendor = vuln.get("vendorProject", "")
            product = vuln.get("product", "")

            items.append({
                "source": "cisa_kev",
                "source_id": cve_id,
                "title": f"{cve_id} — {vendor} {product} (Actively Exploited)",
                "description": vuln.get("shortDescription", ""),
                "severity": "critical",
                "url": f"https://nvd.nist.gov/vuln/detail/{cve_id}",
                "published_at": added_dt.isoformat(),
                "affected_vendors": [vendor] if vendor else [],
                "affected_technologies": [product] if product else [],
            })

        logger.info("Fetched %d recent CISA KEV entries (since %dh)", len(items), since_hours)
    except Exception as e:
        logger.error("CISA KEV fetch failed: %s", e)
        raise

    return items


def fetch_ico_actions() -> List[Dict[str, Any]]:
    """Fetch recent ICO enforcement actions via Jina AI.

    DISABLED: not called from fetch_all_recent. The Jina markdown output
    has letter-spaced characters that the line-by-line parser can't handle,
    producing garbage titles. Tavily news already catches every meaningful
    ICO fine via the breach query, so we lose nothing by dropping this.
    The function is kept here so we can re-enable later if a clean RSS or
    API feed becomes available.
    """
    url = "https://r.jina.ai/https://ico.org.uk/action-weve-taken/enforcement/"
    items = []

    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            response = client.get(
                url,
                headers={"Accept": "text/plain", "X-Timeout": "15"},
            )
            response.raise_for_status()

        text = response.text
        if not text or len(text) < 100:
            logger.warning("ICO page returned minimal content")
            return items

        # Parse the text for enforcement actions
        # ICO enforcement page lists actions with company names, dates, and fine amounts
        lines = text.split("\n")
        current_item = {}  # type: Dict[str, Any]

        for line in lines:
            line = line.strip()
            if not line:
                if current_item.get("title"):
                    current_item["source"] = "ico"
                    current_item["source_id"] = current_item.get("title", "")[:100]
                    current_item["severity"] = "high"
                    current_item["url"] = "https://ico.org.uk/action-weve-taken/enforcement/"
                    items.append(current_item)
                    current_item = {}
                continue

            # Look for patterns indicating enforcement actions
            lower = line.lower()
            if any(kw in lower for kw in ["fine", "penalty", "monetary", "enforcement", "breach", "reprimand"]):
                if not current_item.get("title"):
                    current_item["title"] = line[:200]
                else:
                    current_item["description"] = current_item.get("description", "") + " " + line

            # Look for monetary amounts
            if "\u00a3" in line or "GBP" in line:
                current_item["description"] = current_item.get("description", "") + " " + line

        # Capture last item
        if current_item.get("title"):
            current_item["source"] = "ico"
            current_item["source_id"] = current_item.get("title", "")[:100]
            current_item["severity"] = "high"
            current_item["url"] = "https://ico.org.uk/action-weve-taken/enforcement/"
            items.append(current_item)

        # Limit to most recent entries
        items = items[:10]
        logger.info("Parsed %d ICO enforcement actions", len(items))
    except Exception as e:
        logger.error("ICO enforcement fetch failed: %s", e)
        raise

    return items


def fetch_tavily_news(
    query: str,
    since_hours: int = 48,
    max_results: int = 15,
) -> List[Dict[str, Any]]:
    """Fetch recent cybersecurity news articles via the Tavily API.

    Tavily is an LLM-friendly news/web search API. We use it as a
    complementary feed to the structured CVE feeds (NVD, CISA, NCSC, ICO),
    capturing breach/incident reports and industry news that don't appear
    in vulnerability databases.

    Auth + payload format follows the official Tavily Search API spec:
    https://docs.tavily.com/documentation/api-reference/endpoint/search

    Args:
        query: Tavily search query (typically built from an org's ICP).
        since_hours: How recent the news must be. Mapped to Tavily's
            ``time_range`` parameter: ``day`` (<=24h), ``week`` (<=168h),
            ``month`` (<=720h), else ``year``.
        max_results: Maximum articles to return (Tavily caps at 20).

    Returns:
        A list of threat-shaped dicts ready for the analyser pipeline.
        Empty list if Tavily is unconfigured or the call fails.
    """
    if not settings.TAVILY_API_KEY:
        logger.info("TAVILY_API_KEY not set, skipping Tavily news fetch")
        return []

    if not query:
        return []

    # Map our since_hours to Tavily's time_range vocabulary
    if since_hours <= 24:
        time_range = "day"
    elif since_hours <= 24 * 7:
        time_range = "week"
    elif since_hours <= 24 * 30:
        time_range = "month"
    else:
        time_range = "year"

    payload = {
        "query": query,
        "topic": "news",
        "search_depth": "basic",
        "max_results": min(max_results, 20),
        "time_range": time_range,
        "include_answer": False,
        "include_raw_content": False,
    }

    headers = {
        "Authorization": f"Bearer {settings.TAVILY_API_KEY}",
        "Content-Type": "application/json",
    }

    items: List[Dict[str, Any]] = []
    try:
        with httpx.Client(timeout=20) as client:
            response = client.post(
                "https://api.tavily.com/search",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
        data = response.json()
        try:
            from app.database import get_supabase
            from app.services.raw_artifacts import persist_raw
            persist_raw(
                get_supabase(), "tavily.com", "tavily", data,
                source_endpoint=f"news_search:{query[:100]}",
            )
        except Exception:
            pass
    except httpx.TimeoutException:
        logger.warning("Tavily news fetch timed out for query: %s", query[:80])
        return []
    except httpx.HTTPStatusError as e:
        # Surface the body so we can see what Tavily complained about
        body = e.response.text[:300] if e.response is not None else ""
        logger.error(
            "Tavily HTTP %s for query '%s': %s",
            e.response.status_code if e.response is not None else "?",
            query[:80],
            body,
        )
        return []
    except Exception as e:
        logger.error("Tavily news fetch failed for query '%s': %s", query[:80], e)
        return []

    results = data.get("results") or []
    for result in results:
        title = (result.get("title") or "").strip()
        url = (result.get("url") or "").strip()
        if not title or not url:
            continue

        published_raw = result.get("published_date") or ""
        published_at = _parse_rss_date(published_raw) or _parse_iso_date(published_raw)

        items.append({
            "source": "tavily_news",
            # Tavily doesn't give us a stable ID — use the URL which is unique
            # per article and stable across runs (the dedupe key in monitor.py
            # is (source, source_id), so different sources won't collide).
            "source_id": url[:500],
            "title": title[:500],
            "description": (result.get("content") or "")[:2000],
            "severity": "medium",
            "url": url,
            "published_at": published_at.isoformat() if published_at else None,
            # Tavily doesn't extract vendors/tech — Claude does it later in
            # analyse_threat() which understands news article descriptions.
            "affected_vendors": [],
            "affected_technologies": [],
        })

    logger.info(
        "Fetched %d Tavily news items (time_range=%s) for query: %s",
        len(items),
        time_range,
        query[:80],
    )
    return items


def _parse_iso_date(date_str: str) -> Optional[datetime]:
    """Parse a Tavily-style ISO date string."""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def fetch_all_recent(since_hours: int = 48) -> List[Dict[str, Any]]:
    """Fetch from all global sources and combine.

    Note: Tavily news is NOT fetched here because it's per-org (each org
    builds its own ICP-tailored query). Tavily fetching happens inside
    ``run_threat_monitoring_for_org`` in monitor.py.
    """
    all_items = []  # type: List[Dict[str, Any]]

    try:
        all_items.extend(fetch_ncsc_advisories(since_hours))
    except Exception as e:
        logger.error("NCSC advisories fetch failed: %s", e)

    try:
        all_items.extend(fetch_nvd_critical_cves(since_hours))
    except Exception as e:
        logger.error("NVD fetch failed: %s", e)

    try:
        all_items.extend(fetch_cisa_kev_recent(since_hours))
    except Exception as e:
        logger.error("CISA KEV fetch failed: %s", e)

    # ICO source intentionally disabled — see fetch_ico_actions() docstring.
    # Tavily news catches ICO enforcement via the breach-themed queries.

    logger.info("Total threat intel items fetched: %d", len(all_items))
    return all_items


# --- Helpers ---

def _xml_text(element: ET.Element, tag: str) -> str:
    """Safely extract text from an XML element."""
    child = element.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return ""


def _parse_rss_date(date_str: str) -> Optional[datetime]:
    """Parse an RSS date string into a timezone-aware datetime."""
    if not date_str:
        return None

    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass

    # Try ISO format
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass

    return None
