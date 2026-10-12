import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from supabase import Client

from app.services.activity import log_activity
from app.services.threat_intel.analyser import (
    analyse_threat,
    match_threat_to_orgs,
    match_threat_to_single_org,
)
from app.services.threat_intel.fetcher import fetch_all_recent, fetch_tavily_news

logger = logging.getLogger(__name__)


def run_threat_monitoring(db: Client, since_hours: int = 48) -> Dict[str, Any]:
    """Run threat monitoring for all organisations.

    1. Fetch all recent threats from all sources
    2. Deduplicate against existing threat_intel table
    3. Pre-filter threats against all orgs' ICP keywords (skip irrelevant ones)
    4. For each matched threat: store, analyse with Claude, match orgs, generate alerts
    5. Return summary

    Args:
        db: Supabase client
        since_hours: How far back to look for threats

    Returns:
        Summary dict with counts
    """
    logger.info("Starting threat monitoring run (since_hours=%d)", since_hours)

    # 1. Fetch all recent threats
    raw_items = fetch_all_recent(since_hours)

    if not raw_items:
        logger.info("Fetched 0 raw threats, 0 matched ICP keywords, analysing 0 with Claude")
        return {
            "status": "complete",
            "threats_fetched": 0,
            "new_threats": 0,
            "threats_matched_icp": 0,
            "alerts_generated": 0,
            "alerts_by_org": {},
        }

    # 2. Deduplicate against existing entries
    new_items = _deduplicate(db, raw_items)

    # 3. Pre-filter: build keyword set from ALL orgs' ICPs and pipeline tech stacks
    icp_keywords = _build_all_orgs_keywords(db)
    matched_items = _pre_filter_threats(new_items, icp_keywords)

    logger.info(
        "Fetched %d raw threats, %d matched ICP keywords, analysing %d with Claude",
        len(raw_items),
        len(matched_items),
        len(matched_items),
    )

    total_alerts = 0
    alerts_by_org = {}  # type: Dict[str, int]
    stored_count = 0

    # 4. Process only matched threats
    for item in matched_items:
        try:
            # a. Store in threat_intel table
            threat_row = _store_threat(db, item)
            if not threat_row:
                continue
            stored_count += 1

            # b. Analyse with Claude
            analysed = analyse_threat(item)
            if "error" in analysed:
                continue

            # Update the stored threat with analysis
            _update_threat_analysis(db, threat_row["id"], analysed)

            # c. Match against all orgs
            alerts = match_threat_to_orgs(db, item, analysed)

            # d. Store alerts
            for alert in alerts:
                _store_alert(db, threat_row["id"], alert)
                org_name = alert.get("org_name", "unknown")
                alerts_by_org[org_name] = alerts_by_org.get(org_name, 0) + 1
                total_alerts += 1

                # e. Log activity
                log_activity(
                    db,
                    alert["org_id"],
                    None,
                    "threat_alert",
                    {
                        "threat_title": item.get("title"),
                        "threat_source": item.get("source"),
                        "urgency": alert.get("urgency", "medium"),
                        "matched_industries": alert.get("matched_industries", []),
                    },
                )

        except Exception as e:
            logger.error("Failed to process threat %s: %s", item.get("title", "unknown"), e)

    summary = {
        "status": "complete",
        "threats_fetched": len(raw_items),
        "new_threats": stored_count,
        "threats_matched_icp": len(matched_items),
        "alerts_generated": total_alerts,
        "alerts_by_org": alerts_by_org,
    }

    logger.info(
        "Threat monitoring complete: %d fetched, %d matched ICP, %d stored, %d alerts",
        len(raw_items),
        len(matched_items),
        stored_count,
        total_alerts,
    )

    return summary


def run_threat_monitoring_for_org(
    db: Client,
    org_id: str,
    since_hours: int = 48,
) -> Dict[str, Any]:
    """Run threat monitoring for a single organisation.

    Same as run_threat_monitoring but pre-filters and matches against one org only.
    Also fetches an ICP-tailored Tavily news search for this org so the alert
    feed includes breach/incident reporting beyond the structured CVE feeds.
    """
    logger.info("Starting threat monitoring for org %s (since_hours=%d)", org_id, since_hours)

    raw_items = fetch_all_recent(since_hours)

    # ICP-tailored Tavily news fetch — only runs in the per-org path so each
    # client gets news scoped to their geography + verticals.
    try:
        tavily_query = _build_tavily_query_for_org(db, org_id)
        if tavily_query:
            tavily_items = fetch_tavily_news(tavily_query, since_hours=since_hours)
            raw_items.extend(tavily_items)
    except Exception as e:
        logger.error("Tavily news fetch failed for org %s: %s", org_id, e)

    if not raw_items:
        logger.info("Fetched 0 raw threats, 0 matched ICP keywords, analysing 0 with Claude")
        return {
            "status": "complete",
            "org_id": org_id,
            "threats_fetched": 0,
            "new_threats": 0,
            "threats_matched_icp": 0,
            "alerts_generated": 0,
        }

    new_items = _deduplicate(db, raw_items)

    # Pre-filter against this org's ICP keywords and pipeline tech stacks
    icp_keywords = _build_org_keywords(db, org_id)
    matched_items = _pre_filter_threats(new_items, icp_keywords)

    logger.info(
        "Fetched %d raw threats, %d matched ICP keywords, analysing %d with Claude",
        len(raw_items),
        len(matched_items),
        len(matched_items),
    )

    total_alerts = 0
    stored_count = 0

    for item in matched_items:
        try:
            threat_row = _store_threat(db, item)
            if not threat_row:
                continue
            stored_count += 1

            analysed = analyse_threat(item)
            if "error" in analysed:
                continue

            _update_threat_analysis(db, threat_row["id"], analysed)

            alerts = match_threat_to_single_org(db, item, analysed, org_id)

            for alert in alerts:
                _store_alert(db, threat_row["id"], alert)
                total_alerts += 1

                log_activity(
                    db,
                    org_id,
                    None,
                    "threat_alert",
                    {
                        "threat_title": item.get("title"),
                        "threat_source": item.get("source"),
                        "urgency": alert.get("urgency", "medium"),
                        "matched_industries": alert.get("matched_industries", []),
                    },
                )

        except Exception as e:
            logger.error("Failed to process threat %s: %s", item.get("title", "unknown"), e)

    summary = {
        "status": "complete",
        "org_id": org_id,
        "threats_fetched": len(raw_items),
        "new_threats": stored_count,
        "threats_matched_icp": len(matched_items),
        "alerts_generated": total_alerts,
    }

    logger.info(
        "Threat monitoring for org %s complete: %d fetched, %d matched ICP, %d stored, %d alerts",
        org_id,
        len(raw_items),
        len(matched_items),
        stored_count,
        total_alerts,
    )

    return summary


def _build_tavily_query_for_org(db: Client, org_id: str) -> str:
    """Return the Tavily search query for an org.

    First preference: ``icp_config.tavily_news_query`` — a free-text query
    string set per-client so each client can target the news they actually
    care about (e.g. one client cares about cyber funding + M&A, another about
    SaaS startups raising money or hiring security, etc.).

    Fallback: derive a generic query from the org's target_industries +
    target_locations. Used when ``tavily_news_query`` is not set.
    """
    try:
        org_resp = db.table("organisations").select(
            "icp_config"
        ).eq("id", org_id).limit(1).execute()
    except Exception as e:
        logger.error("Failed to fetch org %s for Tavily query: %s", org_id, e)
        return ""

    if not org_resp.data:
        return ""

    icp = org_resp.data[0].get("icp_config") or {}

    # Per-client custom query takes precedence
    custom_query = icp.get("tavily_news_query")
    if isinstance(custom_query, str) and custom_query.strip():
        query = custom_query.strip()
        logger.info("Using custom Tavily query for org %s: %s", org_id, query[:120])
        return query

    # Fallback: derive a generic query from target_industries + geography.
    # Note: this fallback intentionally focuses on breach/incident reporting
    # because that's a sensible default for an unconfigured org. Clients
    # should set tavily_news_query themselves to get something tailored.
    industries = icp.get("target_industries") or icp.get("target_verticals") or []
    industries = [
        i.strip() for i in industries
        if isinstance(i, str) and len(i.strip()) > 2
    ][:3]

    locations = icp.get("target_locations") or []
    geo = ""
    if isinstance(locations, list) and locations:
        first = locations[0]
        if isinstance(first, str) and first.strip():
            geo = first.strip()

    breach_terms = "data breach OR cyberattack OR ransomware OR security incident"
    parts = [f"({breach_terms})"]
    if industries:
        industry_phrase = " OR ".join(f'"{i}"' for i in industries)
        parts.append(f"({industry_phrase})")
    if geo:
        parts.append(f'"{geo}"')

    query = " ".join(parts)
    logger.info(
        "Built fallback Tavily query for org %s (no tavily_news_query set): %s",
        org_id, query[:120],
    )
    return query


def _build_org_keywords(db: Client, org_id: str) -> set:
    """Build a set of lowercase keywords from a single org's ICP and pipeline tech stacks."""
    keywords = set()  # type: set

    # 1. Get org's icp_config
    try:
        org_resp = db.table("organisations").select(
            "icp_config"
        ).eq("id", org_id).limit(1).execute()

        if org_resp.data:
            icp = org_resp.data[0].get("icp_config") or {}
            _extract_icp_keywords(icp, keywords)
    except Exception as e:
        logger.error("Failed to fetch org %s ICP for pre-filter: %s", org_id, e)

    # 2. Get tech stacks from enriched leads
    try:
        leads_resp = db.table("leads").select(
            "enrichment_data"
        ).eq("org_id", org_id).not_.is_("enrichment_data", "null").limit(100).execute()

        for lead in (leads_resp.data or []):
            _extract_tech_keywords(lead.get("enrichment_data") or {}, keywords)
    except Exception as e:
        logger.error("Failed to fetch lead tech stacks for org %s: %s", org_id, e)

    logger.info("Built %d ICP keywords for org %s", len(keywords), org_id)
    return keywords


def _build_all_orgs_keywords(db: Client) -> set:
    """Build a combined set of lowercase keywords from ALL orgs' ICPs and pipeline tech stacks."""
    keywords = set()  # type: set

    # 1. Get all orgs' icp_configs — lapsed orgs excluded so their ICP
    #    keywords stop widening the pre-filter into extra Claude passes
    #    (2026-08-23 lapsed-org audit).
    try:
        from app.services.plan_limits import REVOKED_STATUSES
        orgs_resp = db.table("organisations").select(
            "id, icp_config, subscription_status"
        ).execute()

        org_ids = []
        for org in (orgs_resp.data or []):
            if (org.get("subscription_status") or "").lower() in REVOKED_STATUSES:
                continue
            icp = org.get("icp_config") or {}
            _extract_icp_keywords(icp, keywords)
            org_ids.append(org["id"])
    except Exception as e:
        logger.error("Failed to fetch orgs for pre-filter: %s", e)
        return keywords

    # 2. Get tech stacks from enriched leads across all orgs
    try:
        leads_resp = db.table("leads").select(
            "enrichment_data"
        ).not_.is_("enrichment_data", "null").limit(500).execute()

        for lead in (leads_resp.data or []):
            _extract_tech_keywords(lead.get("enrichment_data") or {}, keywords)
    except Exception as e:
        logger.error("Failed to fetch lead tech stacks for pre-filter: %s", e)

    logger.info("Built %d ICP keywords across all orgs", len(keywords))
    return keywords


def _extract_icp_keywords(icp: Dict[str, Any], keywords: set) -> None:
    """Extract searchable keywords from an ICP config into the keywords set."""
    # Target industries and verticals
    for field in ("target_industries", "target_verticals"):
        for item in icp.get(field, []):
            if isinstance(item, str) and len(item) > 2:
                keywords.add(item.lower())

    # Keywords field
    for kw in icp.get("keywords", []):
        if isinstance(kw, str) and len(kw) > 2:
            keywords.add(kw.lower())

    # Services offered (often contains tech/product names)
    services = icp.get("services_offered", icp.get("what_we_do", ""))
    if isinstance(services, str):
        for word in services.split(","):
            word = word.strip().lower()
            if len(word) > 2:
                keywords.add(word)
    elif isinstance(services, list):
        for svc in services:
            if isinstance(svc, str) and len(svc) > 2:
                keywords.add(svc.strip().lower())

    # Geography
    geo = icp.get("geography", icp.get("geo", ""))
    if isinstance(geo, str) and len(geo) > 2:
        keywords.add(geo.lower())


def _extract_tech_keywords(enrichment_data: Dict[str, Any], keywords: set) -> None:
    """Extract technology names from lead enrichment data into the keywords set."""
    tech_stack = enrichment_data.get("tech_stack", {})
    technologies = tech_stack.get("technologies_detected", [])
    if isinstance(technologies, list):
        for tech in technologies:
            if isinstance(tech, str) and len(tech) > 1:
                keywords.add(tech.lower())


def _pre_filter_threats(
    items: List[Dict[str, Any]],
    icp_keywords: set,
) -> List[Dict[str, Any]]:
    """Filter threats to only those whose title or description match ICP keywords.

    CISA KEV entries always pass (actively exploited = always relevant for cyber vendors).
    """
    if not icp_keywords:
        # No keywords to filter against — pass everything through
        logger.warning("No ICP keywords found, skipping pre-filter")
        return items

    matched = []
    for item in items:
        # CISA KEV items are always high-signal — skip keyword check
        if item.get("source") == "cisa_kev":
            matched.append(item)
            continue

        # Check title + description against keywords
        text = (
            (item.get("title") or "") + " " + (item.get("description") or "")
        ).lower()

        # Also check vendor/tech fields from the raw fetch
        for vendor in item.get("affected_vendors", []):
            text += " " + vendor.lower()
        for tech in item.get("affected_technologies", []):
            text += " " + tech.lower()

        for keyword in icp_keywords:
            if keyword in text:
                matched.append(item)
                break

    return matched


def _deduplicate(db: Client, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter out items that already exist in the threat_intel table."""
    if not items:
        return []

    # Build lookup of source + source_id pairs
    source_ids = []
    for item in items:
        source = item.get("source", "")
        source_id = item.get("source_id", "")
        if source and source_id:
            source_ids.append(source_id)

    if not source_ids:
        return items

    # Fetch existing source_ids
    try:
        existing_resp = db.table("threat_intel").select(
            "source, source_id"
        ).in_("source_id", source_ids).execute()

        existing_keys = set()
        for row in (existing_resp.data or []):
            key = f"{row['source']}:{row['source_id']}"
            existing_keys.add(key)
    except Exception as e:
        logger.error("Dedup query failed: %s", e)
        return items

    new_items = []
    for item in items:
        key = f"{item.get('source', '')}:{item.get('source_id', '')}"
        if key not in existing_keys:
            new_items.append(item)

    return new_items


def _store_threat(db: Client, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Store a threat item in the threat_intel table."""
    try:
        row = {
            "source": item.get("source", "unknown"),
            "source_id": item.get("source_id"),
            "title": item.get("title", ""),
            "description": item.get("description", ""),
            "severity": item.get("severity"),
            "url": item.get("url"),
            "published_at": item.get("published_at"),
            "affected_vendors": item.get("affected_vendors", []),
            "affected_technologies": item.get("affected_technologies", []),
        }

        result = db.table("threat_intel").insert(row).execute()
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.error("Failed to store threat %s: %s", item.get("title", ""), e)
        return None


def _update_threat_analysis(
    db: Client,
    threat_id: str,
    analysed: Dict[str, Any],
) -> None:
    """Update a threat_intel row with Claude's analysis."""
    try:
        db.table("threat_intel").update({
            "ai_summary": analysed.get("ai_summary"),
            "affected_industries": analysed.get("affected_industries", []),
            "affected_technologies": analysed.get("affected_technologies", []),
            "affected_vendors": analysed.get("affected_vendors", []),
            "sales_relevance": {
                "threat_type": analysed.get("threat_type"),
                "business_impact": analysed.get("business_impact"),
                "urgency_for_sales": analysed.get("urgency_for_sales"),
                "is_actively_exploited": analysed.get("is_actively_exploited"),
                "compliance_relevance": analysed.get("compliance_relevance", []),
            },
        }).eq("id", threat_id).execute()
    except Exception as e:
        logger.error("Failed to update threat analysis for %s: %s", threat_id, e)


def _store_alert(
    db: Client,
    threat_intel_id: str,
    alert: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Store a threat alert in the threat_alerts table."""
    try:
        row = {
            "org_id": alert["org_id"],
            "threat_intel_id": threat_intel_id,
            "relevance_reason": alert.get("relevance_reason", "Industry overlap detected"),
            "affected_icp_industries": alert.get("matched_industries", []),
            "affected_watchlist_companies": alert.get("matched_watchlist_companies", []),
            "sales_angle": alert.get("sales_angle"),
            "suggested_outreach": alert.get("suggested_outreach"),
            "content_hook": alert.get("content_hook"),
            "urgency": alert.get("urgency", "medium"),
            "status": "new",
        }

        result = db.table("threat_alerts").insert(row).execute()
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.error("Failed to store alert for org %s: %s", alert.get("org_id"), e)
        return None
