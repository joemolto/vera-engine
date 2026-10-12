import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from supabase import Client

from app.prompts.threat_intel import THREAT_ANALYSIS_PROMPT, THREAT_SALES_PROMPT
from app.services.claude import call_claude
from app.services.geo_policy import geo_excluded, is_global_threat
from app.services.relevance_policy import sector_excluded
from app.services.signal_payload import is_fear_led, wants_threats_deprioritised

_NO_FEAR_DIRECTIVE = (
    "\n## HARD RULE — NO FEAR FRAMING:\n"
    "This client never leads with fear. Frame sales_angle, "
    "suggested_outreach and content_hook as SECTOR / PEER context and "
    "consultative insight — what's happening across their market and "
    "what good decision-making looks like. NEVER reference the "
    "prospect's own breach/exposure/CVE, and never use alarmist or "
    "urgency-baiting language. Helpful expert, not ambulance-chaser.\n"
)

logger = logging.getLogger(__name__)


def _threat_geo_text(threat: Dict[str, Any], analysed: Dict[str, Any]) -> str:
    """The text the geo gate scans for country markers."""
    return " ".join([
        (threat or {}).get("title") or "",
        (threat or {}).get("description") or "",
        (analysed or {}).get("ai_summary") or "",
    ])


def analyse_threat(threat_item: Dict[str, Any]) -> Dict[str, Any]:
    """Send a single threat item to Claude for analysis.

    Returns the parsed analysis dict with affected industries, technologies, etc.
    """
    prompt = THREAT_ANALYSIS_PROMPT.format(
        source=threat_item.get("source", "unknown"),
        title=threat_item.get("title", ""),
        description=threat_item.get("description", ""),
        severity=threat_item.get("severity", "unknown"),
        url=threat_item.get("url", ""),
    )

    result = call_claude(prompt)

    if "error" in result:
        logger.error("Threat analysis failed for %s: %s", threat_item.get("title"), result.get("error"))
        return result

    logger.info(
        "Analysed threat: %s — industries=%s, urgency=%s",
        threat_item.get("title", "")[:60],
        result.get("affected_industries", []),
        result.get("urgency_for_sales", "unknown"),
    )
    return result


def match_threat_to_orgs(
    db: Client,
    threat: Dict[str, Any],
    analysed: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Match a threat against all organisations' ICPs and generate sales alerts.

    Args:
        db: Supabase client
        threat: The raw threat dict (stored in threat_intel table)
        analysed: The Claude analysis result

    Returns:
        List of alert dicts that were generated
    """
    # Fetch all orgs with their ICP config + client_context (no-fear flag).
    # Lapsed orgs are excluded — no fresh threat alerts accumulate for a
    # cancelled org (2026-08-23 lapsed-org audit).
    from app.services.plan_limits import REVOKED_STATUSES
    orgs_resp = db.table("organisations").select(
        "id, name, icp_config, client_context, subscription_status"
    ).execute()
    org_rows = [
        o for o in (orgs_resp.data or [])
        if (o.get("subscription_status") or "").lower() not in REVOKED_STATUSES
    ]

    if not org_rows:
        logger.warning("No organisations found for threat matching")
        return []

    affected_industries = set(
        ind.lower() for ind in analysed.get("affected_industries", [])
    )
    affected_technologies = set(
        tech.lower() for tech in analysed.get("affected_technologies", [])
    )
    affected_vendors = set(
        v.lower() for v in analysed.get("affected_vendors", [])
    )

    alerts = []

    for org in org_rows:
        org_id = org["id"]
        org_name = org.get("name", "Unknown")
        icp_config = org.get("icp_config") or {}
        client_context = org.get("client_context") or {}
        no_fear = wants_threats_deprioritised(client_context)

        # Compare affected industries against org's target industries
        target_industries = icp_config.get("target_industries", [])
        target_verticals = icp_config.get("target_verticals", [])
        all_target = set(
            ind.lower() for ind in (target_industries + target_verticals)
        )

        matched_industries = affected_industries & all_target

        # Check tech stacks of leads in their pipeline
        matched_technologies = _check_pipeline_tech_stacks(
            db, org_id, affected_technologies
        )

        # Check watchlist companies in affected industries
        matched_watchlist = _check_watchlist_companies(
            db, org_id, affected_industries, affected_technologies
        )

        # If there's any match, generate a sales alert
        if not matched_industries and not matched_technologies and not matched_watchlist:
            continue

        # Policy gates — a by-name watchlist hit bypasses both (we want
        # that company regardless). Otherwise drop clearly-foreign
        # localized incidents (geo) and clearly off-sector stories the
        # client's industries aren't in (relevance). Conservative:
        # global CVEs/KEV and unmarked/ambiguous threats pass.
        if not matched_watchlist:
            _gtext = _threat_geo_text(threat, analysed)
            _glob = is_global_threat(threat, analysed)
            geo_x, geo_reason = geo_excluded(
                text=_gtext, icp_config=icp_config, is_global_threat=_glob,
            )
            sec_x, sec_reason = sector_excluded(
                text=_gtext, icp_config=icp_config, is_global_threat=_glob,
            )
            if geo_x or sec_x:
                logger.info(
                    "Threat %s excluded for org %s: %s",
                    threat.get("title", "")[:40], org_name,
                    geo_reason if geo_x else sec_reason,
                )
                continue

        logger.info(
            "Threat %s matches org %s — industries=%s, tech=%s, watchlist=%s",
            threat.get("title", "")[:40],
            org_name,
            list(matched_industries),
            list(matched_technologies),
            [w.get("company_name") for w in matched_watchlist],
        )

        # Generate sales context via Claude
        alert_data = _generate_sales_alert(
            threat=threat,
            analysed=analysed,
            org=org,
            icp_config=icp_config,
            matched_industries=matched_industries,
            matched_technologies=matched_technologies,
            matched_watchlist=matched_watchlist,
            no_fear=no_fear,
        )

        if alert_data:
            alert_data["org_id"] = org_id
            alert_data["org_name"] = org_name
            alert_data["matched_industries"] = list(matched_industries)
            alert_data["matched_technologies"] = list(matched_technologies)
            alert_data["matched_watchlist_companies"] = [
                w.get("company_name") for w in matched_watchlist
            ]
            alerts.append(alert_data)

            # Also create watchlist alerts for affected watchlist companies
            _create_watchlist_threat_alerts(
                db, org_id, org, threat, analysed, matched_watchlist
            )

    return alerts


def match_threat_to_single_org(
    db: Client,
    threat: Dict[str, Any],
    analysed: Dict[str, Any],
    org_id: str,
) -> List[Dict[str, Any]]:
    """Match a threat against a single organisation's ICP."""
    org_resp = db.table("organisations").select(
        "id, name, icp_config, client_context"
    ).eq("id", org_id).limit(1).execute()

    if not org_resp.data:
        logger.warning("Organisation %s not found", org_id)
        return []

    org = org_resp.data[0]
    icp_config = org.get("icp_config") or {}
    client_context = org.get("client_context") or {}
    no_fear = wants_threats_deprioritised(client_context)

    affected_industries = set(
        ind.lower() for ind in analysed.get("affected_industries", [])
    )
    affected_technologies = set(
        tech.lower() for tech in analysed.get("affected_technologies", [])
    )

    target_industries = icp_config.get("target_industries", [])
    target_verticals = icp_config.get("target_verticals", [])
    all_target = set(
        ind.lower() for ind in (target_industries + target_verticals)
    )

    matched_industries = affected_industries & all_target
    matched_technologies = _check_pipeline_tech_stacks(
        db, org_id, affected_technologies
    )
    matched_watchlist = _check_watchlist_companies(
        db, org_id, affected_industries, affected_technologies
    )

    if not matched_industries and not matched_technologies and not matched_watchlist:
        return []

    # Geo + sector gates (see match_threat_to_orgs). A by-name watchlist
    # hit bypasses both; otherwise drop clearly-foreign localized
    # incidents and clearly off-sector stories. Global CVEs/KEV pass.
    if not matched_watchlist:
        _gtext = _threat_geo_text(threat, analysed)
        _glob = is_global_threat(threat, analysed)
        geo_x, geo_reason = geo_excluded(
            text=_gtext, icp_config=icp_config, is_global_threat=_glob,
        )
        sec_x, sec_reason = sector_excluded(
            text=_gtext, icp_config=icp_config, is_global_threat=_glob,
        )
        if geo_x or sec_x:
            logger.info(
                "Threat %s excluded for org %s: %s",
                threat.get("title", "")[:40], org_id,
                geo_reason if geo_x else sec_reason,
            )
            return []

    alert_data = _generate_sales_alert(
        threat=threat,
        analysed=analysed,
        org=org,
        icp_config=icp_config,
        matched_industries=matched_industries,
        matched_technologies=matched_technologies,
        matched_watchlist=matched_watchlist,
        no_fear=no_fear,
    )

    if alert_data:
        alert_data["org_id"] = org_id
        alert_data["org_name"] = org.get("name", "Unknown")
        alert_data["matched_industries"] = list(matched_industries)
        alert_data["matched_technologies"] = list(matched_technologies)
        alert_data["matched_watchlist_companies"] = [
            w.get("company_name") for w in matched_watchlist
        ]

        _create_watchlist_threat_alerts(
            db, org_id, org, threat, analysed, matched_watchlist
        )

        return [alert_data]

    return []


def _extract_tech_names(techs: Any) -> set:
    """Extract a lowercase set of technology names from a tech list.

    Handles both shapes the codebase produces:
    - PredictLeads dicts: ``[{"name": "HubSpot", "category": "crm", ...}, ...]``
    - Legacy strings: ``["HubSpot", "Apache Tomcat", ...]``
    """
    out: set = set()
    if not isinstance(techs, list):
        return out
    for t in techs:
        if isinstance(t, dict):
            name = t.get("name") or ""
        elif isinstance(t, str):
            name = t
        else:
            name = ""
        if name:
            out.add(name.lower())
    return out


def _check_pipeline_tech_stacks(
    db: Client,
    org_id: str,
    affected_technologies: set,
) -> List[str]:
    """Check if any leads in the org's pipeline use affected technologies."""
    if not affected_technologies:
        return []

    try:
        leads_resp = db.table("leads").select(
            "enrichment_data"
        ).eq("org_id", org_id).not_.is_("enrichment_data", "null").limit(100).execute()

        if not leads_resp.data:
            return []

        matched: set = set()
        for lead in leads_resp.data:
            enrichment = lead.get("enrichment_data") or {}
            tech_stack = enrichment.get("tech_stack") or {}
            technologies = tech_stack.get("technologies_detected", [])
            lead_techs = _extract_tech_names(technologies)
            matched |= lead_techs & affected_technologies

        return list(matched)
    except Exception as e:
        logger.error("Tech stack check failed for org %s: %s", org_id, e)
        return []


def _check_watchlist_companies(
    db: Client,
    org_id: str,
    affected_industries: set,
    affected_technologies: set,
) -> List[Dict[str, Any]]:
    """Check if any watchlist companies use affected technologies.

    Reads from ``latest_snapshot.predictleads_tech`` (the v2 watchlist
    structure) with a fallback to the legacy ``tech_stack`` key.
    """
    try:
        wl_resp = db.table("watchlist").select(
            "id, company_name, company_domain, latest_snapshot"
        ).eq("org_id", org_id).eq("monitoring_active", True).execute()

        if not wl_resp.data:
            return []

        matched = []
        for entry in wl_resp.data:
            snapshot = entry.get("latest_snapshot") or {}
            # v2 watchlist stores under predictleads_tech; older rows may
            # still have tech_stack from the previous monitor implementation.
            tech_data = (
                snapshot.get("predictleads_tech")
                or snapshot.get("tech_stack")
                or {}
            )
            techs = tech_data.get("technologies") or []

            company_techs = _extract_tech_names(techs)
            tech_overlap = company_techs & affected_technologies

            if tech_overlap:
                matched.append({
                    "id": entry["id"],
                    "company_name": entry["company_name"],
                    "company_domain": entry.get("company_domain", ""),
                    "match_reason": f"Uses affected technology: {', '.join(sorted(tech_overlap))}",
                })

        return matched
    except Exception as e:
        logger.error("Watchlist check failed for org %s: %s", org_id, e)
        return []


def _generate_sales_alert(
    threat: Dict[str, Any],
    analysed: Dict[str, Any],
    org: Dict[str, Any],
    icp_config: Dict[str, Any],
    matched_industries: set,
    matched_technologies: list,
    matched_watchlist: list,
    no_fear: bool = False,
) -> Dict[str, Any]:
    """Generate a sales-focused alert using Claude."""
    threat_summary = (
        f"Title: {threat.get('title', '')}\n"
        f"Summary: {analysed.get('ai_summary', threat.get('description', ''))}\n"
        f"Severity: {threat.get('severity', 'unknown')}\n"
        f"Type: {analysed.get('threat_type', 'unknown')}\n"
        f"Actively exploited: {analysed.get('is_actively_exploited', False)}\n"
        f"Business impact: {analysed.get('business_impact', 'Unknown')}"
    )

    icp_parts = []
    for key, value in icp_config.items():
        if isinstance(value, (list, dict)):
            icp_parts.append(f"{key}: {json.dumps(value)}")
        else:
            icp_parts.append(f"{key}: {value}")
    icp_description = "\n".join(icp_parts) if icp_parts else "Not configured"

    watchlist_names = [w.get("company_name", "") for w in matched_watchlist]

    prompt = THREAT_SALES_PROMPT.format(
        threat_summary=threat_summary,
        org_name=org.get("name", "Unknown"),
        org_services=icp_config.get("services_offered", icp_config.get("what_we_do", "cybersecurity services")),
        icp_description=icp_description,
        matched_industries=", ".join(matched_industries) if matched_industries else "None specific",
        matched_technologies=", ".join(matched_technologies) if matched_technologies else "None specific",
        matched_watchlist=", ".join(watchlist_names) if watchlist_names else "None",
        no_fear_directive=_NO_FEAR_DIRECTIVE if no_fear else "",
    )

    result = call_claude(prompt)

    if "error" in result:
        logger.error("Sales alert generation failed: %s", result.get("error"))
        return {}

    # Deterministic backstop: even with the prompt directive, the LLM can
    # still open on fear. For a no-fear client, a fear-led content_hook /
    # sales_angle is replaced with a safe sector-context line — better a
    # plain hook than one that breaks the client's hard rule.
    if no_fear and isinstance(result, dict):
        safe = (
            f"Sector context for {org.get('name', 'the client')}'s market: "
            f"{threat.get('title', 'a relevant industry development')}. "
            f"Use as a peer/market-resilience talking point — never the "
            f"prospect's own exposure."
        )
        for field in ("content_hook", "sales_angle", "suggested_outreach"):
            if is_fear_led(str(result.get(field) or "")):
                result[field] = safe

    return result


def _create_watchlist_threat_alerts(
    db: Client,
    org_id: str,
    org: Dict[str, Any],
    threat: Dict[str, Any],
    analysed: Dict[str, Any],
    matched_watchlist: List[Dict[str, Any]],
) -> None:
    """Create watchlist_alerts for watchlist companies affected by a threat."""
    category = analysed.get("category") or "threat_relevant"
    is_commercial = category in {
        "news_funding",
        "news_acquisition",
        "news_hiring",
        "news_product_launch",
    }
    headline_verb = "is in the news" if is_commercial else "may be affected by"
    title_prefix = "Signal" if is_commercial else "Threat relevant"

    for wl_company in matched_watchlist:
        try:
            alert_row = {
                "org_id": org_id,
                "watchlist_id": wl_company["id"],
                "alert_type": category,
                "alert_title": f"{title_prefix}: {wl_company['company_name']}",
                "alert_summary": (
                    f"Your target account {wl_company['company_name']} {headline_verb} "
                    f"{threat.get('source_id', threat.get('title', 'an article'))} — "
                    f"{wl_company.get('match_reason', 'industry overlap')}. "
                    f"This is a timely reason to reach out."
                ),
                "alert_data": {
                    "category": category,
                    "threat_source": threat.get("source"),
                    "threat_id": threat.get("source_id"),
                    "threat_title": threat.get("title"),
                    "threat_severity": threat.get("severity"),
                    "match_reason": wl_company.get("match_reason"),
                    "ai_summary": analysed.get("ai_summary"),
                },
                "suggested_action": (
                    f"Reach out to {wl_company['company_name']} referencing "
                    f"{threat.get('source_id', 'this article')} and offer assistance."
                ),
                "approach_angle": analysed.get("business_impact", ""),
                "urgency": analysed.get("urgency_for_sales", "medium"),
                "status": "new",
            }
            db.table("watchlist_alerts").insert(alert_row).execute()
            logger.info(
                "Created watchlist threat alert for %s (org %s)",
                wl_company["company_name"],
                org_id,
            )
            # Fire threat.matched_to_lead webhook — distinct from
            # watchlist.alert_created in that it carries the threat
            # details (source, severity, CVE id) for customers building
            # security-ops automations off Vera's feed. Best-effort.
            try:
                from app.services.webhooks import enqueue_event
                enqueue_event(db, org_id, "threat.matched_to_lead", {
                    "watchlist_id": wl_company.get("id"),
                    "company_name": wl_company.get("company_name"),
                    "company_domain": wl_company.get("company_domain"),
                    "threat_source": threat.get("source"),
                    "threat_id": threat.get("source_id"),
                    "threat_title": threat.get("title"),
                    "threat_severity": threat.get("severity"),
                    "threat_url": threat.get("url"),
                    "match_reason": wl_company.get("match_reason"),
                    "category": category,
                })
            except Exception as e:
                logger.warning(
                    "threat.matched_to_lead webhook enqueue failed: %s", e,
                )
        except Exception as e:
            logger.error(
                "Failed to create watchlist threat alert for %s: %s",
                wl_company["company_name"],
                e,
            )


# ---------------------------------------------------------------------------
# Lead-level correlation — Phase A
# ---------------------------------------------------------------------------
# The functions above operate per-threat (one threat → all matching orgs) for
# the scheduled threat-intel poller. The function below is the inverse: given
# a single lead's tech stack and industry, find the threats that affect them.
# Used during quick-enrich to turn the orphaned threat_intel data into
# lead-attached threat_alerts that the scorer + GTM briefing can read.

# Severity weight for ranking. CISA KEV (actively exploited) always wins.
_SEVERITY_WEIGHT = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def correlate_threats_for_lead(
    db: Client,
    tech_stack: List[Dict[str, Any]],
    industry: Optional[str] = None,
    company_domain: Optional[str] = None,
    days: int = 365,
    limit: int = 5,
) -> Dict[str, Any]:
    """Correlate one lead against the threat_intel table.

    Returns a dict with:
      - threat_alerts: top `limit` matched threats, each as a dict with
        title, severity, source, why_relevant, sales_angle, urgency, url,
        published_at, threat_type, is_actively_exploited.
      - cve_in_stack_matches: list of dicts {cve_title, tech, is_kev,
        severity} for signal extraction and the confidence ladder.
      - breach_proximity_matches: list of breach/ransomware threats whose
        affected_technologies overlap the prospect's stack.

    Reads-only against threat_intel — does not write watchlist_alerts or
    threat_alerts rows. The caller (enricher) embeds the returned alerts
    inside enrichment_data.threat_alerts so the scorer prompt can read them.
    """
    lead_techs = _extract_tech_names(tech_stack)
    industry_lc = (industry or "").lower().strip()

    if not lead_techs and not industry_lc:
        return {
            "threat_alerts": [],
            "cve_in_stack_matches": [],
            "breach_proximity_matches": [],
        }

    # Pull recent high-signal threats. We only ever consider critical/high or
    # any CISA KEV (KEV doesn't always have a CVSS-derived severity but is
    # always actively exploited).
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        resp = (
            db.table("threat_intel")
            .select("id, source, source_id, title, severity, url, "
                    "published_at, ai_summary, affected_industries, "
                    "affected_technologies, affected_vendors, sales_relevance")
            .gte("published_at", cutoff)
            .order("published_at", desc=True)
            .limit(200)
            .execute()
        )
    except Exception as e:
        logger.error("threat_intel query failed during lead correlation: %s", e)
        return {
            "threat_alerts": [],
            "cve_in_stack_matches": [],
            "breach_proximity_matches": [],
        }

    rows = resp.data or []
    matches: List[Dict[str, Any]] = []
    cve_matches: List[Dict[str, str]] = []
    breach_matches: List[Dict[str, Any]] = []

    for row in rows:
        sev = (row.get("severity") or "").lower()
        is_kev = (row.get("source") or "") == "cisa_kev"

        # Skip low-impact unless it's KEV (KEV is always relevant)
        if not is_kev and sev not in ("critical", "high"):
            continue

        affected_techs = {
            t.lower() for t in (row.get("affected_technologies") or [])
            if isinstance(t, str)
        }
        affected_inds = {
            i.lower() for i in (row.get("affected_industries") or [])
            if isinstance(i, str)
        }
        affected_vendors = {
            v.lower() for v in (row.get("affected_vendors") or [])
            if isinstance(v, str)
        }

        # Match by tech overlap (highest signal — vulnerability in stack)
        # OR vendor overlap (still relevant — they may use the vendor)
        # OR industry overlap (broader context)
        tech_overlap = (lead_techs & affected_techs) | (lead_techs & affected_vendors)
        industry_overlap = bool(industry_lc) and industry_lc in affected_inds

        if not tech_overlap and not industry_overlap:
            continue

        # Classify the threat. The threat_intel.sales_relevance jsonb may
        # already carry a threat_type from the analyser; fall back to keyword
        # heuristics on title/summary.
        sales_relevance = row.get("sales_relevance") or {}
        threat_type = (sales_relevance.get("threat_type") or "").lower()
        if not threat_type:
            t_lower = (row.get("title") or "").lower()
            if "breach" in t_lower or "ransomware" in t_lower or "attack" in t_lower:
                threat_type = "breach"
            elif "cve" in t_lower or "vulnerability" in t_lower or sev in ("critical", "high"):
                threat_type = "vulnerability"
            else:
                threat_type = "advisory"

        # Build why_relevant + urgency
        if tech_overlap:
            why = f"Affects {', '.join(sorted(tech_overlap)[:3])} which is in this prospect's stack"
        else:
            why = f"Affects {industry_lc} sector"

        urgency = "high" if is_kev or sev == "critical" else "medium"

        alert = {
            "title": row.get("title", ""),
            "severity": row.get("severity", "unknown"),
            "source": row.get("source", ""),
            "url": row.get("url", ""),
            "published_at": row.get("published_at", ""),
            "ai_summary": (row.get("ai_summary") or "")[:600],
            "why_relevant": why,
            "sales_angle": sales_relevance.get("sales_angle") or "",
            "urgency": urgency,
            "threat_type": threat_type,
            "is_actively_exploited": is_kev,
            "matched_technologies": sorted(tech_overlap)[:5],
        }
        matches.append(alert)

        if threat_type == "vulnerability" and tech_overlap:
            for t in sorted(tech_overlap)[:5]:
                cve_matches.append({
                    "cve_title": row.get("title", ""),
                    "tech": t,
                    # Carries the raw material for the confidence ladder:
                    # is_kev = CISA KEV (actively exploited); severity lets
                    # the humaniser distinguish a critical-but-not-exploited
                    # CVE from one that's actually being weaponised.
                    "is_kev": is_kev,
                    "severity": (row.get("severity") or "unknown").lower(),
                })
        if threat_type in ("breach", "ransomware") and (tech_overlap or industry_overlap):
            breach_matches.append(alert)

    # Rank: KEV first, then severity, then recency (newest first).
    # All three components negated so a single ascending sort orders correctly.
    def _rank_key(a: Dict[str, Any]) -> tuple:
        kev = 1 if a.get("is_actively_exploited") else 0
        sev_w = _SEVERITY_WEIGHT.get((a.get("severity") or "").lower(), 0)
        pub = a.get("published_at") or ""
        try:
            ts = datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            ts = 0.0
        return (-kev, -sev_w, -ts)

    matches.sort(key=_rank_key)
    matches = matches[:limit]

    logger.info(
        "Lead correlation: %d threats matched (cve=%d, breach=%d) for industry=%s, %d techs",
        len(matches), len(cve_matches), len(breach_matches),
        industry_lc or "—", len(lead_techs),
    )

    return {
        "threat_alerts": matches,
        "cve_in_stack_matches": cve_matches,
        "breach_proximity_matches": breach_matches,
    }
