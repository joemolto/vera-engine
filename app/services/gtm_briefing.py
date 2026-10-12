"""GTM Intelligence Briefing — confidence-weighted buying readiness with recommended plays.

This is Vera's proprietary intelligence layer. It combines signals from multiple
enrichment sources, assigns confidence levels deterministically (not AI-guessed),
matches against a play library, and uses Claude only for the narrative.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.services.claude import call_claude
from app.services import signal_payload as sp
from app.services.llm_responses import (
    CustomSignalEvaluation,
    NarrativeResponse,
    parse_llm_response,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Confidence rules — deterministic, not AI-guessed
# ---------------------------------------------------------------------------

_SOURCE_CONFIDENCE = {
    "linkedin_verified": "high",
    "predictleads_tech": "high",
    "predictleads_jobs": "high",
    "predictleads_news": "moderate",

    "deep_research": "moderate",
    "google_news": "low",
    "linkedin_jobs_scraped": "low",
    "tech_stack_html": "moderate",
    "missing_tool_inference": "low",
    "website_analysis": "moderate",
}


def _confidence_rank(level: str) -> int:
    return {"high": 3, "moderate": 2, "low": 1}.get(level, 0)


# ---------------------------------------------------------------------------
# Play library — pure signal definitions
# ---------------------------------------------------------------------------
# Each play describes a SIGNAL pattern, not a pitch. The pitch ("how should
# THIS client approach this signal") is generated per-client by Claude using
# client_context.what_they_sell + ideal_triggers. This is what makes the
# briefing client-aware: same signal → different angle for each client
# (a staffing firm vs a threat-intel vendor vs a sales-tools vendor).

_PLAYS = [
    # ────────────────────────────────────────────────────────────────────
    # Cyber-specific plays (Phase A) — tier=cyber so they out-rank the
    # generic B2B plays when both match. Vera's edge over Apollo is here:
    # urgency from CISA-KEV CVEs in the prospect's stack, breaches in their
    # adjacent ecosystem, compliance gaps in regulated industries, and new
    # security leadership in their first 90 days. Apollo has none of this.
    # ────────────────────────────────────────────────────────────────────
    {
        "name": "CVE Urgency",
        "tier": "cyber",
        "description": "Actively-exploited CVE affects a technology in the prospect's stack",
        "required_signals": ["cve_in_stack"],
        "boost_signals": ["regulated_industry", "growing_tech_stack", "hiring_security"],
        "min_score": 1,
    },
    {
        "name": "Breach Adjacent",
        "tier": "cyber",
        "description": "Recent breach or ransomware affects the prospect's tech or industry",
        "required_signals": ["breach_proximity"],
        "boost_signals": ["regulated_industry", "growing_tech_stack"],
        "min_score": 1,
    },
    {
        "name": "Compliance Window",
        "tier": "cyber",
        "description": "Regulated industry with missing core compliance certifications",
        "required_signals": ["compliance_gap"],
        "boost_signals": ["regulated_industry", "recent_funding", "hiring_security"],
        "min_score": 1,
    },
    {
        "name": "Security Leadership",
        "tier": "cyber",
        "description": "Prospect or company recently hired a security leader (CISO/CSO)",
        "required_signals": ["security_leadership_hire"],
        "boost_signals": ["recent_funding", "hiring", "security_team_expansion"],
        "min_score": 1,
    },
    {
        # M&A is a top-priority generic signal — kept at the top of the
        # generic plays so when multiple match, the acquisition story wins.
        # Acquisitions are massive timing triggers for almost any sales motion.
        "name": "Recent Acquisition",
        "description": "Company involved in recent M&A activity",
        "required_signals": ["recent_acquisition"],
        "boost_signals": ["new_leadership", "growing_tech_stack", "hiring"],
        "min_score": 1,
    },
    {
        "name": "Funding Surge",
        "description": "Recently raised funding — has budget, scaling fast",
        "required_signals": ["recent_funding"],
        "boost_signals": ["hiring", "hiring_sales_bd", "growing_tech_stack"],
        "min_score": 2,
    },
    {
        "name": "Leadership Change",
        "description": "New leader in seat — reviews all vendors in first 90 days",
        "required_signals": ["new_leadership"],
        "boost_signals": ["hiring", "recent_funding"],
        "min_score": 1,
    },
    {
        "name": "Security Hiring",
        "description": "Actively hiring security roles — investing in security capacity",
        "required_signals": ["hiring_security"],
        "boost_signals": ["recent_funding", "hiring", "growing_tech_stack"],
        "min_score": 1,
    },
    {
        "name": "Sales Hiring",
        "description": "Actively hiring sales/BD roles — building outbound capacity",
        "required_signals": ["hiring_sales_bd"],
        "boost_signals": ["recent_funding", "growing_tech_stack"],
        "min_score": 1,
    },
    {
        "name": "Expansion Activity",
        "description": "Company expanding via product launch or partnership",
        "required_signals": ["product_launch_or_partnership"],
        "boost_signals": ["hiring", "growing_tech_stack"],
        "min_score": 2,
    },
    {
        "name": "Regulated Industry",
        "description": "Operates in a regulated sector — compliance pressure",
        "required_signals": ["regulated_industry"],
        "boost_signals": ["missing_compliance_tools", "recent_funding"],
        "min_score": 2,
    },
    {
        "name": "Competitive Displacement",
        "description": "Competitor tool detected in their stack",
        "required_signals": ["competitor_tool_detected"],
        "boost_signals": ["growing_tech_stack"],
        "min_score": 1,
    },
    {
        "name": "Security Gap",
        "description": "No security tooling detected despite scale",
        "required_signals": ["no_security_tooling"],
        "boost_signals": ["growing_tech_stack", "hiring", "regulated_industry"],
        "min_score": 2,
    },
]

# Regulated industries for compliance play
_REGULATED_INDUSTRIES = {
    "financial services", "banking", "insurance", "healthcare",
    "pharmaceuticals", "legal services", "law practice", "government",
    "gambling & casinos", "fintech", "investment management",
    "venture capital & private equity", "energy", "oil & gas",
    "utilities", "telecommunications",
}


# ---------------------------------------------------------------------------
# Signal extraction — pull structured signals from enrichment data
# ---------------------------------------------------------------------------

def _extract_signals(
    enrichment_data: Dict,
    lead: Dict,
    icp_config: Optional[Dict] = None,
) -> Dict[str, bool]:
    """Extract boolean signal flags from enrichment data. Deterministic.

    All event-based signals are filtered to the last 6 months. Do NOT trust
    PredictLeads' top-level has_recent_* booleans — those flag whether the
    event has EVER happened, not whether it's recent. Iterate the events
    array and check dates explicitly via _is_recent_event().

    When `icp_config.signals_enabled` is a non-empty list, signals NOT in that
    list are forced to False so they can't trigger plays. The full set is
    still returned (preserves dict shape for downstream code) — disabled
    signals just always read False.
    """
    signals: Dict[str, bool] = {}

    # News signals — derived from filtered events, NOT from the unfiltered
    # PredictLeads top-level booleans which would fire on year-old events.
    news = enrichment_data.get("news") or {}
    pl_news = enrichment_data.get("predictleads_news") or {}
    pl_events = pl_news.get("events") or []

    # Helper: does any event in the last 6 months match one of these categories?
    def _has_recent_event_of_category(categories: tuple) -> bool:
        return any(
            (e.get("category") or "") in categories
            for e in pl_events
            if _is_recent_event(e, months=6)
        )

    # Google News still uses its own boolean (it's already date-filtered upstream)
    # but PredictLeads contributes via the explicit event-date check.
    signals["recent_funding"] = bool(news.get("has_recent_funding")) or _has_recent_event_of_category(
        ("receives_financing", "raises_funding")
    )
    signals["recent_acquisition"] = bool(news.get("has_recent_acquisition")) or _has_recent_event_of_category(
        ("acquires", "acquired_by")
    )
    signals["product_launch_or_partnership"] = bool(
        news.get("has_recent_product_launch") or news.get("has_recent_partnership")
    ) or _has_recent_event_of_category(("launches", "partners_with"))

    # Leadership signals — already filtered, kept as-is
    signals["new_leadership"] = _has_recent_event_of_category(("hires", "promotes"))

    # Jobs signals
    jobs = enrichment_data.get("jobs") or {}
    pl_jobs = enrichment_data.get("predictleads_jobs") or {}
    total_jobs = jobs.get("total_roles_found", 0) or pl_jobs.get("total_jobs", 0)
    signals["hiring"] = total_jobs >= 3
    signals["hiring_sales_bd"] = bool(jobs.get("is_hiring_sales_bd") or pl_jobs.get("is_hiring_sales"))

    # Detect security role hiring from job titles
    _SECURITY_KEYWORDS = {
        "security", "ciso", "infosec", "information security", "cybersecurity",
        "cyber security", "security engineer", "security analyst", "security architect",
        "security operations", "soc ", "devsecops", "appsec", "application security",
        "penetration", "vulnerability", "threat", "compliance officer", "grc ",
        "governance risk", "chief information security",
    }
    security_hiring = False
    for job in (pl_jobs.get("jobs") or []):
        title_lower = (job.get("title") or "").lower()
        if any(kw in title_lower for kw in _SECURITY_KEYWORDS):
            security_hiring = True
            break
    signals["hiring_security"] = security_hiring

    # Tech stack signals
    tech = enrichment_data.get("tech_stack") or {}
    tech_filtered = tech.get("technologies_filtered") or {}
    security_tools = tech_filtered.get("security_tools") or []
    total_detected = tech.get("total_detected", 0)
    signals["no_security_tooling"] = len(security_tools) <= 1 and total_detected > 10
    signals["growing_tech_stack"] = total_detected >= 30
    signals["competitor_tool_detected"] = False  # Will be set by client context matching

    # Compliance signals
    industry = (lead.get("company_industry") or "").lower()
    signals["regulated_industry"] = industry in _REGULATED_INDUSTRIES
    compliance = tech_filtered.get("compliance") or []
    deep = enrichment_data.get("deep_research") or {}
    deep_compliance = deep.get("compliance") or {}
    has_iso = bool(deep_compliance.get("iso_27001")) and deep_compliance.get("iso_27001") != "Not found"
    has_soc2 = bool(deep_compliance.get("soc_2")) and deep_compliance.get("soc_2") != "Not found"
    signals["missing_compliance_tools"] = signals["regulated_industry"] and not has_iso and not has_soc2 and len(compliance) == 0

    # ────────────────────────────────────────────────────────────────────
    # Cyber-specific signals (Phase A). Sourced from enrichment_data.threat_alerts
    # (populated by correlate_threats_for_lead() in the enricher) plus a few
    # secondary checks against deep_research compliance and predictleads_jobs.
    # The corresponding rows in the `signals` table are written by
    # enricher._create_cyber_signals — these dict entries make the signals
    # available to the play matcher below.
    # ────────────────────────────────────────────────────────────────────
    threat_alerts = enrichment_data.get("threat_alerts") or []

    cve_alerts = [
        a for a in threat_alerts
        if isinstance(a, dict)
        and (
            a.get("threat_type") == "vulnerability"
            or a.get("is_actively_exploited")
        )
    ]
    breach_alerts = [
        a for a in threat_alerts
        if isinstance(a, dict)
        and a.get("threat_type") in ("breach", "ransomware")
    ]
    signals["cve_in_stack"] = bool(cve_alerts)
    signals["breach_proximity"] = bool(breach_alerts)

    # Security leadership hire — true if the prospect's verified primary role
    # is a security leadership title, OR a recent PredictLeads hires-event
    # mentions one. The enricher writes the row to `signals`; here we reuse
    # the same logic so the play matcher can fire even on cached enrichment
    # where the row was written but later wiped.
    linkedin = enrichment_data.get("linkedin_verification") or {}
    primary_title = (
        (linkedin.get("primary_role") or {}).get("title")
        or lead.get("title")
        or ""
    ).lower()
    _LEADERSHIP_TITLES = {
        "ciso", "cso", "chief information security",
        "chief security", "head of security", "vp security",
        "vp of security", "vice president security",
        "director of security", "security director",
    }
    is_security_titled = any(kw in primary_title for kw in _LEADERSHIP_TITLES)
    recent_security_hire_in_news = any(
        e.get("category") == "hires"
        and any(kw in (e.get("summary") or "").lower() for kw in _LEADERSHIP_TITLES)
        and _is_recent_event(e, months=3)
        for e in pl_events
    )
    signals["security_leadership_hire"] = bool(is_security_titled or recent_security_hire_in_news)

    # SOC expansion — ≥3 security-titled roles on the careers page
    _SOC_ROLE_KEYWORDS = {
        "security engineer", "security analyst", "security architect",
        "security operations", "soc analyst", "soc engineer",
        "devsecops", "appsec", "application security",
        "incident response", "threat hunter", "vulnerability management",
        "penetration tester", "pentester",
    }
    sec_role_count = sum(
        1 for j in (pl_jobs.get("jobs") or [])
        if any(kw in (j.get("title") or "").lower() for kw in _SOC_ROLE_KEYWORDS)
    )
    signals["security_team_expansion"] = sec_role_count >= 3

    # Compliance gap — regulated industry + missing both ISO 27001 and SOC 2.
    # Stricter than missing_compliance_tools (which requires `compliance` to
    # also be empty); compliance_gap fires the moment a regulated industry
    # lacks the two universal certs that drive most procurement.
    signals["compliance_gap"] = signals["regulated_industry"] and not has_iso and not has_soc2

    # Per-client signal gating: if the org has explicitly listed which signals
    # they care about (icp_config.signals_enabled), force everything else to
    # False so it can't drive play matching. This is how a SaaS-startup-services
    # client avoids inheriting cyber-vendor-to-cyber signals like
    # security_leadership_hire that are a buying trigger for one motion and a
    # disqualifier for another.
    enabled = (icp_config or {}).get("signals_enabled")
    if isinstance(enabled, list) and enabled:
        enabled_set = {s for s in enabled if isinstance(s, str)}
        signals = {k: (v if k in enabled_set else False) for k, v in signals.items()}

    return signals


def _is_recent_event(event: Dict, months: int = 12) -> bool:
    """Check if a PredictLeads event is within the last N months."""
    date_str = event.get("date", "")
    if not date_str:
        return False
    try:
        event_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        cutoff = datetime.now(timezone.utc) - timedelta(days=months * 30)
        return event_date >= cutoff
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Confidence-weighted intelligence extraction
# ---------------------------------------------------------------------------

def _build_intelligence_items(enrichment_data: Dict, lead: Dict) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """Build the what_we_know, what_we_detected, and gaps lists."""
    know: List[Dict] = []      # High confidence
    detected: List[Dict] = []  # Moderate confidence
    gaps: List[Dict] = []      # Unknown

    # LinkedIn verification
    linkedin = enrichment_data.get("linkedin_verification") or {}
    status = linkedin.get("verification_status", "")
    if status == "verified":
        know.append({
            "signal": f"{lead.get('first_name', '')} {lead.get('last_name', '')} verified as {linkedin.get('primary_role', {}).get('title', 'unknown role')} at {linkedin.get('primary_role', {}).get('company', 'unknown')}",
            "source": "LinkedIn Verification",
            "confidence": "high",
        })
    elif status == "stale":
        detected.append({
            "signal": f"LinkedIn shows role may have changed — data could be stale",
            "source": "LinkedIn Verification",
            "confidence": "low",
            "caveat": "Contact may have moved on",
        })

    # PredictLeads tech (behind firewall = high confidence)
    pl_tech = enrichment_data.get("predictleads_tech") or {}
    behind_firewall = pl_tech.get("behind_firewall_techs") or []
    if behind_firewall:
        know.append({
            "signal": f"{len(behind_firewall)} behind-firewall technologies detected",
            "source": "PredictLeads Tech",
            "confidence": "high",
        })

    # Tech stack filtered (security tools)
    tech_filtered = (enrichment_data.get("tech_stack") or {}).get("technologies_filtered") or {}
    security_tools = tech_filtered.get("security_tools") or []
    if security_tools:
        tool_names = [t.get("name", "") for t in security_tools[:5]]
        know.append({
            "signal": f"Security tools in use: {', '.join(tool_names)}",
            "source": "PredictLeads Tech",
            "confidence": "high",
        })
    elif (enrichment_data.get("tech_stack") or {}).get("total_detected", 0) > 10:
        detected.append({
            "signal": "No dedicated security tooling detected",
            "source": "Tech Stack Analysis",
            "confidence": "moderate",
            "caveat": "May use tools not detectable externally",
        })

    # Compliance from deep research
    deep = enrichment_data.get("deep_research") or {}
    deep_compliance = deep.get("compliance") or {}
    compliance_items = []
    if deep_compliance.get("iso_27001") and deep_compliance["iso_27001"] != "Not found":
        compliance_items.append("ISO 27001")
    if deep_compliance.get("soc_2") and deep_compliance["soc_2"] != "Not found":
        compliance_items.append("SOC 2")
    if deep_compliance.get("cyber_essentials") and deep_compliance["cyber_essentials"] != "Not found":
        compliance_items.append("Cyber Essentials")
    if compliance_items:
        know.append({
            "signal": f"Compliance certifications: {', '.join(compliance_items)}",
            "source": "Deep Research",
            "confidence": "moderate",
        })

    # PredictLeads news events — filter by age FIRST, then take the top 5.
    # The previous version sliced [:5] before filtering, which meant if the
    # first 5 events in the array were old, recent events further down the
    # list got dropped.
    pl_news = enrichment_data.get("predictleads_news") or {}
    recent_events = [
        e for e in (pl_news.get("events") or [])
        if _is_recent_event(e, months=6)
    ][:5]
    for event in recent_events:
        summary = event.get("summary", "")
        category = event.get("category", "")
        if category in ("receives_financing", "raises_funding", "acquires", "acquired_by"):
            know.append({
                "signal": summary,
                "source": "PredictLeads News",
                "confidence": "high",
                "date": event.get("date", ""),
            })
        elif category in ("launches", "partners_with", "hires", "promotes"):
            detected.append({
                "signal": summary,
                "source": "PredictLeads News",
                "confidence": "moderate",
                "date": event.get("date", ""),
            })

    # PredictLeads jobs
    pl_jobs = enrichment_data.get("predictleads_jobs") or {}
    total_jobs = pl_jobs.get("total_jobs", 0)
    if total_jobs > 0:
        know.append({
            "signal": f"{total_jobs} open roles detected on careers page",
            "source": "PredictLeads Jobs",
            "confidence": "high",
        })
        # Check for sales hiring specifically
        if pl_jobs.get("is_hiring_sales"):
            know.append({
                "signal": "Actively hiring sales/BD roles",
                "source": "PredictLeads Jobs",
                "confidence": "high",
            })

        # Check for security role hiring
        _SEC_KW = {"security", "ciso", "infosec", "cybersecurity", "devsecops", "appsec", "soc ", "penetration", "compliance officer"}
        sec_roles = [j.get("title", "") for j in (pl_jobs.get("jobs") or []) if any(k in (j.get("title") or "").lower() for k in _SEC_KW)]
        if sec_roles:
            know.append({
                "signal": f"Hiring security roles: {', '.join(sec_roles[:3])}",
                "source": "PredictLeads Jobs",
                "confidence": "high",
            })

    # Standard gaps (always present)
    gaps.append({"question": "Current security vendor contracts", "why_it_matters": "Could be locked into multi-year deal"})
    gaps.append({"question": "Security budget allocation", "why_it_matters": "Affects ability to purchase"})
    gaps.append({"question": "Internal security team size", "why_it_matters": "Affects whether they build vs buy"})
    gaps.append({"question": "Decision-making process and timeline", "why_it_matters": "Determines sales cycle length"})

    return know, detected, gaps


# ---------------------------------------------------------------------------
# Play matching — deterministic signal pattern matching
# ---------------------------------------------------------------------------

_TIER_RANK = {"cyber": 2, "generic": 1}


def _match_plays(
    signals: Dict[str, bool],
    client_context: Optional[Dict] = None,
    icp_config: Optional[Dict] = None,
) -> Tuple[Optional[Dict], List[Dict]]:
    """Match signals against the play library. Returns (best_play, rejected_plays).

    Per-client play library:
      - When `icp_config.plays` is a non-empty list, use it as the source of
        plays. This is how each client gets its OWN library of plays, framed
        for THEIR motion. Every Vera client should have one configured during
        onboarding — different products, different framings, different signals
        as required.
      - When absent or empty, fall back to the global `_PLAYS` list (logged at
        WARN so we know which orgs aren't fully configured).

    Sort order:
      1. Tier — `cyber` plays out-rank `generic` when both match. Per-client
         plays specify their own tier or default to `generic`.
      2. Within a tier, score descending (1 per required + 1 per boost match).
      3. List order (Python's stable sort) is the final tiebreaker, so the
         array order in `icp_config.plays` is meaningful for ties.
    """
    scored_plays: List[Tuple[int, int, Dict]] = []
    rejected: List[Dict] = []

    plays_source = _PLAYS
    custom_plays = (icp_config or {}).get("plays")
    if isinstance(custom_plays, list) and custom_plays:
        plays_source = custom_plays
    else:
        client_name = (client_context or {}).get("company_name") or (client_context or {}).get("client_name") or "unknown"
        logger.warning(
            "_match_plays: org has no icp_config.plays — falling back to global _PLAYS for client=%s. "
            "Configure per-client plays during onboarding.",
            client_name,
        )

    for play in plays_source:
        # Check required signals
        has_required = all(signals.get(s, False) for s in play["required_signals"])
        if not has_required:
            missing = [s for s in play["required_signals"] if not signals.get(s, False)]
            rejected.append({
                "play": play["name"],
                "rejected_because": f"Missing required signal(s): {', '.join(missing).replace('_', ' ')}",
            })
            continue

        # Score: 1 per required + 1 per boost signal present
        score = len(play["required_signals"])
        for s in play["boost_signals"]:
            if signals.get(s, False):
                score += 1

        if score >= play["min_score"]:
            tier = _TIER_RANK.get(play.get("tier", "generic"), 1)
            scored_plays.append((tier, score, play))
        else:
            rejected.append({
                "play": play["name"],
                "rejected_because": f"Score {score} below minimum {play['min_score']}",
            })

    # Sort by (tier desc, score desc); list order is the natural tiebreaker.
    scored_plays.sort(key=lambda x: (x[0], x[1]), reverse=True)

    best = scored_plays[0][2] if scored_plays else None

    # Add non-selected scored plays to rejected
    for _tier, _score, play in scored_plays[1:]:
        rejected.append({
            "play": play["name"],
            "rejected_because": f"Lower priority than {scored_plays[0][2]['name']}",
        })

    return best, rejected


# ---------------------------------------------------------------------------
# Buying readiness assessment — deterministic
# ---------------------------------------------------------------------------

def _assess_buying_readiness(signals: Dict[str, bool], lead_rating: str) -> Tuple[str, str]:
    """Assess buying readiness and confidence level. Returns (readiness, confidence)."""
    high_intent_signals = sum([
        signals.get("recent_funding", False),
        signals.get("new_leadership", False),
        signals.get("hiring_sales_bd", False),
        signals.get("recent_acquisition", False),
    ])

    moderate_signals = sum([
        signals.get("hiring", False),
        signals.get("product_launch_or_partnership", False),
        signals.get("no_security_tooling", False),
        signals.get("regulated_industry", False),
    ])

    # Buying readiness
    if lead_rating == "hot" and high_intent_signals >= 2:
        readiness = "high"
    elif lead_rating == "hot" or (lead_rating == "warm" and high_intent_signals >= 1):
        readiness = "moderate"
    elif lead_rating == "warm" and moderate_signals >= 2:
        readiness = "moderate"
    elif lead_rating == "cold":
        readiness = "low"
    else:
        readiness = "unknown"

    # Confidence level (based on data quality, not buying readiness)
    total_signals = high_intent_signals + moderate_signals
    if total_signals >= 4:
        confidence = "high"
    elif total_signals >= 2:
        confidence = "moderate"
    else:
        confidence = "low"

    return readiness, confidence


def _commercial_section(
    rp: Dict[str, Any], findings: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Render the briefing's commercial block from the revenue estimate +
    findings. Deterministic prose; carries the no-overclaim caveats so the
    no-ambulance-chasing rule survives into the commercial framing.
    """
    cur = rp.get("currency", "GBP")
    est = rp.get("estimated_revenue_potential")
    if est is None:
        upside = (
            "Commercial upside: not estimated — insufficient deal-economics "
            "baseline for this client."
        )
    else:
        upside = (
            f"Commercial upside: estimated {cur} {rp.get('range_low'):,}–"
            f"{rp.get('range_high'):,} ({rp.get('confidence')} confidence)."
        )
    why = rp.get("why_this_value") or []
    why_line = ("Why: " + "; ".join(why) + ".") if why else ""

    # Caveats: the deterministic no-overclaim risk_notes from findings, then
    # the estimate's own assumptions. De-duped, capped.
    caveats: List[str] = []
    for f in findings or []:
        rn = (f.get("risk_note") or "").strip()
        if rn and rn not in caveats:
            caveats.append(rn)
    for a in rp.get("assumptions", []) or []:
        if a and a not in caveats:
            caveats.append(a)

    strongest = [
        f.get("headline") for f in (findings or [])[:3] if f.get("headline")
    ]
    return {
        "upside": upside,
        "why": why_line,
        "positioning": rp.get("recommended_commercial_angle", ""),
        "caveats": caveats[:5],
        "strongest_findings": strongest,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _intelligence_only(icp_config: Optional[Dict]) -> bool:
    """True when the org runs the intelligence-only experience (no
    AI-drafted openers, emails, or "what to say" copy anywhere —
    product rule 10). Per-org flag, 2026-06-11."""
    features = (icp_config or {}).get("features") or {}
    return bool(features.get("intelligence_only"))


def generate_gtm_briefing(
    lead: Dict,
    enrichment_data: Dict,
    scoring_data: Dict,
    client_context: Optional[Dict] = None,
    icp_config: Optional[Dict] = None,
    db: Optional[Any] = None,
) -> Dict[str, Any]:
    """Generate a confidence-weighted GTM intelligence briefing.

    The structure and confidence levels are deterministic (our code).
    Claude is used only for the narrative (intelligence_summary, reasoning, opening_line).

    `icp_config` carries the per-client signal gating (`signals_enabled`)
    and per-client play library (`plays`). Every Vera client should have
    these configured during onboarding so signals + plays reflect THEIR
    motion, not a one-size-fits-all assumption.
    """
    lead_rating = scoring_data.get("lead_rating", "unrated")

    # Step 0: Load the lead's contract-compliant findings (the deterministic
    # human sentences the dashboard/digest already show). Display-only and
    # decoupled — [] when no live rows exist. Used as the narrative's factual
    # spine and surfaced verbatim so score / briefing / dashboard agree.
    findings = sp.load_findings(
        db,
        org_id=lead.get("org_id"),
        lead_id=lead.get("id"),
        enrichment_data=enrichment_data,
        deprioritise_threats=sp.wants_threats_deprioritised(client_context),
    )

    # Step 1: Extract signals (deterministic, gated by icp_config.signals_enabled)
    signals = _extract_signals(enrichment_data, lead, icp_config=icp_config)

    # Step 1b: Evaluate per-client custom signals (LLM-driven). Each Vera
    # client can define their own signals in icp_config.custom_signals — this
    # is what makes the engine bespoke per cyber sub-sector. Brand-protection
    # clients fire on different signals than threat-intel vendors, even when
    # given the same enrichment data. Custom signals merge into the signals
    # dict so the existing play matcher and briefing flow recognise them by
    # name automatically — no schema or matcher changes required.
    custom_signals = _evaluate_custom_signals(enrichment_data, lead, icp_config)
    signals.update(custom_signals)

    # Step 2: Build intelligence items (deterministic)
    what_we_know, what_we_detected, gaps = _build_intelligence_items(enrichment_data, lead)

    # Step 3: Match plays (deterministic, uses icp_config.plays when configured)
    best_play, rejected_plays = _match_plays(signals, client_context, icp_config=icp_config)

    # Step 4: Assess buying readiness (deterministic)
    buying_readiness, confidence_level = _assess_buying_readiness(signals, lead_rating)

    # Step 5: Generate narrative (Claude — only part that's AI).
    # Pass db so the narrative can pull recent outcome exemplars for the org.
    narrative = _generate_narrative(
        lead, enrichment_data, scoring_data, client_context,
        signals, best_play, buying_readiness, confidence_level,
        what_we_know, what_we_detected,
        findings=findings,
        db=db,
    )

    # Build the final briefing
    recommended_play = None
    if best_play:
        # Evidence: the findings whose type backs this play's signals — the
        # concrete, deterministic proof of why this play fired. Additive
        # sub-key; existing UI fields untouched.
        _play_keys = set(best_play.get("required_signals", [])) | set(
            best_play.get("boost_signals", [])
        )
        evidence = [
            {"signal_type": f["signal_type"], "headline": f["headline"]}
            for f in findings
            if sp.finding_play_keys(f["signal_type"]) & _play_keys
        ][:6]
        recommended_play = {
            "play_name": best_play["name"],
            # description = the SIGNAL we detected (client-neutral)
            "description": best_play["description"],
            # pitch = the client-specific angle Claude generated
            "pitch": narrative.get("play_pitch", ""),
            # reasoning kept as legacy alias so existing Lovable UI fields don't break
            "reasoning": narrative.get("play_pitch", ""),
            "opening_line": (
                "" if _intelligence_only(icp_config)
                else narrative.get("opening_line", "")
            ),
            "timing": narrative.get("timing_assessment", ""),
            "evidence": evidence,
        }

    # Step 6: Pre-generate 5-tone email drafts (Claude — single call, all tones)
    # The rep can copy-paste these directly from the dashboard. Generated once
    # at briefing time so re-opening the lead doesn't re-spend Claude tokens.
    #
    # Orgs with features.intelligence_only (2026-06-11) get NO
    # drafted copy — Vera is intelligence, not outreach (product rule 10).
    # Skipping also saves the Claude call per briefing.
    if _intelligence_only(icp_config):
        email_drafts = []
    else:
        email_drafts = _generate_email_drafts(
            lead, client_context, narrative, recommended_play, signals,
        )

    # Step 7: Revenue potential — account commercial upside. Deterministic
    # (no LLM here). Full context is present at this point (post-scoring,
    # signals + findings computed) and this is where it's rendered. Additive
    # and failure-safe: a failure leaves revenue_potential/commercial None
    # and the rest of the briefing is unaffected.
    revenue_potential = None
    commercial = None
    try:
        from app.services.revenue_potential import estimate_revenue_potential
        revenue_potential = estimate_revenue_potential(
            lead=lead,
            client_ctx=client_context,
            signals={k: v for k, v in signals.items() if v},
            findings=findings,
            scoring_data=scoring_data,
        )
        commercial = _commercial_section(revenue_potential, findings)
    except Exception as e:
        logger.warning("Revenue potential in briefing failed: %s", e)

    return {
        "buying_readiness": buying_readiness,
        "confidence_level": confidence_level,
        "intelligence_summary": narrative.get("intelligence_summary", ""),
        "what_we_know": what_we_know,
        "what_we_detected": what_we_detected,
        "gaps": gaps,
        "recommended_play": recommended_play,
        "plays_considered": rejected_plays[:5],
        "signals_detected": {k: v for k, v in signals.items() if v},
        # Normalized contract findings — the same human sentences the
        # dashboard/digest render, surfaced here so consumers don't re-derive.
        "findings": findings,
        # Account commercial upside (deterministic) + a rendered, no-overclaim
        # commercial section. Additive — existing keys untouched.
        "revenue_potential": revenue_potential,
        "commercial": commercial,
        "email_drafts": email_drafts,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _generate_narrative(
    lead: Dict,
    enrichment_data: Dict,
    scoring_data: Dict,
    client_context: Optional[Dict],
    signals: Dict[str, bool],
    best_play: Optional[Dict],
    buying_readiness: str,
    confidence_level: str,
    what_we_know: List[Dict],
    what_we_detected: List[Dict],
    findings: Optional[List[Dict]] = None,
    db: Optional[Any] = None,
) -> Dict[str, str]:
    """Use Claude to generate the client-specific narrative parts of the briefing.

    The play library detects the SIGNAL (e.g. "hiring security roles") but
    the PITCH is generated here, framed around the specific client's
    `what_they_sell` and `ideal_triggers`. So the same signal produces a
    different angle for each client:
      - a sales-tools vendor: "their reps are ramping → sell them pre-researched pipeline"
      - a staffing firm: "hiring is hard → sell them embedded teams"
      - a threat-intel vendor: "they're maturing security → sell them threat intel"
    """
    ctx = client_context or {}
    client_name = ctx.get("company_name") or ctx.get("client_name") or "the client"
    what_they_sell = ctx.get("what_they_sell") or "B2B services"
    tone = ctx.get("tone_of_voice") or "professional and direct"
    approach_style = ctx.get("approach_style") or ""
    pain_points_solved = ctx.get("pain_points_they_solve") or ""

    ideal_triggers_raw = ctx.get("ideal_triggers")
    if isinstance(ideal_triggers_raw, list):
        ideal_triggers = "\n".join(f"- {t}" for t in ideal_triggers_raw if t)
    elif isinstance(ideal_triggers_raw, str) and ideal_triggers_raw.strip():
        ideal_triggers = ideal_triggers_raw
    else:
        ideal_triggers = "Any signal that the prospect is actively buying or growing."

    if isinstance(pain_points_solved, list):
        pain_points_str = "\n".join(f"- {p}" for p in pain_points_solved if p)
    else:
        pain_points_str = str(pain_points_solved) if pain_points_solved else "(not specified)"

    lead_name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip()
    company = lead.get("company_name", "Unknown")
    title = lead.get("title", "Unknown")
    industry = lead.get("company_industry", "Unknown")

    know_summary = "\n".join(f"- {item['signal']} ({item['confidence']} confidence)" for item in what_we_know[:8])
    detected_summary = "\n".join(f"- {item['signal']} ({item.get('caveat', '')})" for item in what_we_detected[:5])
    active_signals = ", ".join(k.replace("_", " ") for k, v in signals.items() if v)
    findings_block = "\n".join(
        f"- {f['headline']} — {f['detail']}" for f in (findings or [])[:8]
    ) or "(no contract findings on this lead yet)"

    # Build play context. If the matched play has a custom `framing` field
    # (set in the org's icp_config.plays during onboarding), use it directly —
    # that framing is the bespoke angle this client wants for this signal.
    # Otherwise fall back to the generic "describe signal, ask Claude to
    # pitch it" pattern.
    play_context = ""
    if best_play:
        framing = (best_play.get("framing") or "").strip()
        if framing:
            play_context = (
                f"\nMatched play: {best_play['name']}.\n"
                f"PRESCRIBED FRAMING for {client_name} (from their playbook):\n"
                f"{framing}\n\n"
                f"Adapt this framing to THIS specific lead. Keep the angle and the "
                f"product references intact — make it human, specific, and concise."
            )
        else:
            play_context = (
                f"\nWe detected this signal pattern: {best_play['name']} — "
                f"{best_play.get('description', '')}.\n"
                f"You must write a pitch that turns this signal into an angle "
                f"FROM {client_name}'s perspective (not a generic security pitch)."
            )

    # If the prospect was recently acquired, surface that as the headline
    # because it's the most actionable single fact in the briefing.
    # CRITICAL: only inject if the matching event is within 6 months —
    # otherwise we surface year-old M&A as "headline insight" and Claude
    # leads the briefing with stale news. (The signals dict is already
    # date-filtered, but we re-verify here as a defense-in-depth check.)
    acquisition_note = ""
    if signals.get("recent_acquisition"):
        pl_news = enrichment_data.get("predictleads_news") or {}
        for ev in (pl_news.get("events") or []):
            if ev.get("category") in ("acquires", "acquired_by") and _is_recent_event(ev, months=6):
                acquisition_note = (
                    f"\n\n*** HEADLINE INSIGHT: {ev.get('summary', 'Recent acquisition activity detected')} ***\n"
                    f"Lead the intelligence_summary with this fact. The acquisition is "
                    f"the most important context for any outreach to this person right now."
                )
                break

    # Pull recent outcome exemplars for the org. Below the cold-start
    # threshold this returns a "(none yet)" marker.
    past_outcomes_block = "(none yet)"
    if db is not None and lead.get("org_id"):
        try:
            from app.services.outcomes import get_outcome_block
            past_outcomes_block = get_outcome_block(db, lead["org_id"])
        except Exception as e:
            logger.warning("Outcome exemplars unavailable for briefing: %s", e)

    prompt = f"""You are writing a GTM intelligence briefing for {client_name}'s sales team.

## What {client_name} sells:
{what_they_sell}

## What pain points {client_name} solves for customers:
{pain_points_str}

## {client_name}'s ideal buying triggers (signals that mean a prospect is ready to buy):
{ideal_triggers}

## {client_name}'s tone of voice:
{tone}

## {client_name}'s approach style:
{approach_style}

---

## Target prospect:
{lead_name}, {title} at {company} ({industry})
Lead rating: {scoring_data.get('lead_rating', 'unrated')}
Buying readiness: {buying_readiness} (confidence: {confidence_level})
Active signals detected: {active_signals}
{play_context}
{acquisition_note}

## Verified intelligence findings (deterministic — the factual spine; ground the briefing in these, do not contradict or invent beyond them):
{findings_block}

## What we know about them (high confidence):
{know_summary}

## What we detected (moderate confidence):
{detected_summary}

## Recent outcomes from this team (use as exemplars, not filters):
{past_outcomes_block}

---

## Your task

Write a briefing that maps the detected signals to {client_name}'s SPECIFIC product and pitch — not a generic sales angle. The whole point is that the same signal means different things to different sellers:
- A company hiring security people is a "hire vs buy" pitch for one client, a "they're investing in security tooling, sell them threat intel" pitch for another, and a "they're growing fast, sell them GTM automation" pitch for a third.
- Reference {client_name}'s actual products and pain points listed above.
- DO NOT default to a generic cybersecurity pitch.
- DO NOT recommend embedded security teams unless {client_name} actually sells them.
- DO NOT recommend threat intel unless {client_name} actually sells it.
- DO NOT recommend GTM automation unless {client_name} actually sells it.

**NO AMBULANCE-CHASING — hard rule.** The cybersecurity industry mocks
vendors who lead with "we noticed you have a CVE / unpatched system /
breach / missing compliance cert". It reads as opportunistic.
- DO NOT lead with the prospect's own CVE, KEV entry, breach, or
  compliance gap in the intelligence_summary, play_pitch, or
  opening_line.
- Threat intel about the prospect is **background context** — it
  informs the briefing but is NOT the headline. If you need to surface
  it, frame it as their *sector / customers / peer companies* facing it
  ("recent ransomware activity in UK financial services", not "your
  Fortinet is exposed").
- Anchor the lead on a positive or neutral signal where possible:
  hiring, funding, leadership change, acquisition, expansion, product
  launch, partnership, market position.
- The right tone for genuine threat-intel matches is "we noticed this is
  happening in your sector and have something useful to share" — not
  "you have a problem we'd like to sell into".

Write four things:

1. "intelligence_summary" — 2-3 sentences. The executive summary. Lead with the most important POSITIVE or NEUTRAL fact about this account. If there's a recent acquisition, that's the lead. Otherwise lead with the strongest growth/hiring/funding signal. Be specific.

2. "play_pitch" — 2-3 sentences explaining how {client_name} should approach this account given the signals. Reference {client_name}'s actual products/services. Make the connection between the signal and what {client_name} sells explicit. If the relevant signal is threat-intel, frame as offering help/expertise on something happening in their sector — never as exploiting their exposure.

3. "opening_line" — A single sentence the sales rep could use to open a conversation. Must reference a specific signal from above. Must sound human, not AI-generated. Match {client_name}'s tone. Must position {client_name}'s actual offering, not a generic one. **Never** open with the prospect's own CVE/breach/compliance gap.

4. "timing_assessment" — One sentence on whether NOW is the right time and why.

Respond ONLY with valid JSON:
{{
    "intelligence_summary": "...",
    "play_pitch": "...",
    "opening_line": "...",
    "timing_assessment": "..."
}}"""

    try:
        result = call_claude(prompt)
    except Exception as e:
        logger.warning("GTM briefing narrative generation failed: %s", e)
        result = None

    # Validate against the schema. parse_llm_response logs the failed
    # fields, so a downstream operator can see *why* a briefing
    # degraded instead of just noticing thin output.
    parsed = parse_llm_response(
        NarrativeResponse, result, context="generate_narrative"
    )
    if parsed is not None and isinstance(result, dict):
        return result

    # Fallback: minimal narrative (no client-specific framing — we don't have Claude)
    return {
        "intelligence_summary": f"{company} in {industry} — {buying_readiness} buying readiness based on {active_signals or 'limited signals'}.",
        "play_pitch": (best_play or {}).get("description", "Insufficient signals to recommend a specific approach."),
        "opening_line": "",
        "timing_assessment": f"Buying readiness assessed as {buying_readiness} with {confidence_level} confidence.",
    }


# ---------------------------------------------------------------------------
# Email drafts — five tones, single Claude call
# ---------------------------------------------------------------------------

EMAIL_TONES = ["direct", "formal", "informal", "humour", "curiosity"]

_EMAIL_TONE_GUIDE = {
    "direct": "Plain, no-frills, get-to-the-point. State the angle and ask. No throat-clearing, no compliments. Greeting: 'Hi [first_name],'.",
    "formal": "Polished business English. Full sentences, no contractions. Suits regulated industries (finance, legal, government). Greeting: 'Dear [first_name],'.",
    "informal": "Conversational, like one peer messaging another. Contractions OK. Slightly warm but still professional. Reads like a Slack DM written carefully. Greeting: 'Hi [first_name],' or 'Hey [first_name],'.",
    "humour": "Light wit — one well-placed joke or sharp observation, not stand-up. The humour should land off the prospect's situation, not random. Stay tasteful. Greeting: 'Hey [first_name],' or 'Hi [first_name],'.",
    "curiosity": "Lead with a question or an unexpected observation that makes the prospect want to read on. Withhold the ask until the second half. Hooks via intrigue, not novelty. Greeting: 'Hi [first_name],'.",
}


def _generate_email_drafts(
    lead: Dict,
    client_context: Optional[Dict],
    narrative: Dict[str, str],
    recommended_play: Optional[Dict],
    signals: Dict[str, bool],
) -> Dict[str, Dict[str, str]]:
    """Generate five email drafts (one per tone) in a single Claude call.

    Returns dict keyed by tone, each value `{"subject": "...", "body": "..."}`.
    No signature block — the rep adds their own when sending.

    Falls back to an empty dict on Claude error so the briefing still saves.
    """
    ctx = client_context or {}
    client_name = ctx.get("company_name") or ctx.get("client_name") or "the client"
    what_they_sell = ctx.get("what_they_sell") or "B2B services"
    pain_points = ctx.get("pain_points_they_solve") or ""
    if isinstance(pain_points, list):
        pain_points = "; ".join(p for p in pain_points if p)

    lead_first = lead.get("first_name") or "there"
    lead_full = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip()
    company = lead.get("company_name") or "your company"
    title = lead.get("title") or ""
    industry = lead.get("company_industry") or ""

    play_name = (recommended_play or {}).get("play_name") or "general outreach"
    play_pitch = (recommended_play or {}).get("pitch") or narrative.get("play_pitch", "")
    opening_line = (recommended_play or {}).get("opening_line") or narrative.get("opening_line", "")
    intel = narrative.get("intelligence_summary", "")
    active_signals = ", ".join(k.replace("_", " ") for k, v in signals.items() if v) or "(none specific)"

    tone_block = "\n".join(f"- **{t}**: {_EMAIL_TONE_GUIDE[t]}" for t in EMAIL_TONES)

    prompt = f"""You are writing five cold-outreach email drafts for {client_name}'s sales team — same prospect, same angle, five different TONES. The rep will pick the tone that suits the moment and copy-paste.

## Sender (who is emailing)
{client_name} — they sell: {what_they_sell}
Pain points they solve: {pain_points or "(not specified)"}

## Prospect (recipient)
- Name: {lead_full or lead_first}
- First name (for greeting): {lead_first}
- Title: {title or "(unknown)"}
- Company: {company}
- Industry: {industry or "(unknown)"}

## Why now (use these as the email's hook)
- Recommended play: {play_name}
- Pitch angle: {play_pitch}
- Best opening signal: {opening_line}
- Intelligence summary: {intel}
- Active signals: {active_signals}

## Tones — write one email per tone
{tone_block}

## Rules (apply to all five)
1. **Body shape — IMPORTANT, this is how a real email reads:**
   - Line 1: greeting on its own line (per the tone guide above), ending in a comma.
   - Line 2: blank line.
   - Then 2 to 3 short paragraphs, EACH SEPARATED BY A BLANK LINE (use `\\n\\n` between paragraphs in the JSON string).
   - Final paragraph ends with the ask (a question or specific next step like "15-min call next week").
   - NO sign-off line ("Best," / "Thanks," / etc.). NO signature. NO sender name. The body ends after the ask. The rep adds their own sign-off when sending.
2. Subject lines must reference the specific signal or angle for this prospect — never generic like "Quick question" or "Following up".
3. Body length: 60-130 words total across the paragraphs. Tight. No filler.
4. Reference one concrete fact about {company} from the intelligence above. Don't write generic copy that could be sent to any company.
5. **Do not invent facts.** If a fact isn't in the intelligence above, don't include it. This rule has explicit no-go patterns:
   - **No invented quantities.** "We identified 847 prospects", "we found 142 companies like yours", "our 3,400 customers" — these are forbidden unless those exact numbers appear in the intelligence input. If you don't have a real number, don't reach for one.
   - **No invented statistics or percentages.** "Reps using Vera see 3x reply rates", "30% faster ramp time", "twice the pipeline" — forbidden unless the figure is in the briefing data. No ROI claims, no time-to-value claims, no benchmarks.
   - **No invented customer names or logos** beyond what's listed in the client's `key_differentiators` or `pain_points_they_solve`.
   - **No invented prospect lists.** Don't reference "companies like yours that we work with" or "your peer set" as if you have a list — you don't.
   - The integrity rule is simple: every concrete claim in the email must trace to a fact in the intelligence above. If you can't trace it, leave it out and write something less specific instead.
6. Plain text only. No markdown, no bullet points inside the body.
7. **NO AMBULANCE-CHASING — this is a hard rule.** The cybersecurity industry mocks vendors who lead with "we noticed you have a CVE / a breach / a missing compliance cert / unpatched legacy systems". It reads as opportunistic and damages the sender's reputation. Therefore:
   - DO NOT lead with the prospect's own CVE, KEV entry, vulnerability, breach, or compliance gap.
   - DO NOT use the prospect's own threat exposure as the hook, even softened with humour or "noticed". Especially not with humour — joking about a peer's vulnerability is the most-mocked tactic of all.
   - DO NOT reference the matched-tech list or "your stack contains X" as the email's opener.
   - Threat intel about the prospect is **background context for you, the writer**, so the email sounds informed about their world. It is NOT the email's hook.
   - Anchor every email on a positive or neutral signal: hiring, funding, leadership change, acquisition, expansion, product launch, partnership, market position. Use those as the lead.
   - If you genuinely need to mention the threat landscape, frame it as "what their *customers* / *sector* / *peer companies* face" — never as "what *you* have".

## Output

Reply with valid JSON only — exactly five entries keyed by tone:
{{
  "direct":    {{"subject": "...", "body": "..."}},
  "formal":    {{"subject": "...", "body": "..."}},
  "informal":  {{"subject": "...", "body": "..."}},
  "humour":    {{"subject": "...", "body": "..."}},
  "curiosity": {{"subject": "...", "body": "..."}}
}}"""

    try:
        result = call_claude(prompt)
        if not isinstance(result, dict):
            return {}
        # Validate shape — only keep entries with both subject and body strings
        cleaned: Dict[str, Dict[str, str]] = {}
        for tone in EMAIL_TONES:
            entry = result.get(tone)
            if isinstance(entry, dict):
                subj = entry.get("subject")
                body = entry.get("body")
                if isinstance(subj, str) and isinstance(body, str) and subj.strip() and body.strip():
                    cleaned[tone] = {"subject": subj.strip(), "body": body.strip()}
        return cleaned
    except Exception as e:
        logger.warning("Email draft generation failed: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Custom signals — per-client signal extraction via Claude
# ---------------------------------------------------------------------------
# Different cyber clients care about different signals. A brand-protection
# vendor fires on typosquat counts; a threat-intel vendor fires on
# sector-specific breach activity; a vCISO firm fires on "fast-growth + no
# CISO yet". Rather than maintain one fixed signal library that fits every
# sub-sector, each client's icp_config.custom_signals defines their own.
#
# Schema (per signal):
#   {
#     "name": "brand_abuse_high",
#     "description": "Prospect has detected typosquats / phishing infrastructure",
#     "evaluation_prompt": "Inspect enrichment_data.X. Fire when condition Y holds.",
#     "framing": "(used by plays — what to pitch when this signal fires)"
#   }
#
# The framing field is read by plays in icp_config.plays — plays can list
# custom signal names in required_signals / boost_signals like any other.

_CUSTOM_SIGNALS_PROMPT = """You are evaluating per-client custom signals for a B2B sales intelligence agent. The signal definitions are listed below. For each one, decide whether it FIRES for this prospect based on the available data.

## The prospect

- Name: {full_name}
- Title: {title}
- Company: {company_name}
- Industry: {industry}
- Company size: {company_size}
- Location: {location}

## Available enrichment data (compressed)

{enrichment_summary}

## Custom signals to evaluate

{signal_definitions}

## Decision rules

1. A signal fires only when the data supports it. Be conservative — when in doubt, do NOT fire.
2. Do not invent or infer data not present above. If the relevant enrichment field is empty, the signal does not fire.
3. Each signal's `evaluation_prompt` describes the precise trigger condition. Apply it literally.
4. Output a single JSON object mapping each signal name to a boolean.

## Output

Respond with valid JSON only — exactly one entry per signal name above:
{{
  "signal_name_1": true,
  "signal_name_2": false,
  ...
}}"""


def _summarise_enrichment_for_signals(enrichment_data: Dict, lead: Dict) -> str:
    """Compact, structured summary of enrichment fields most signal-relevant.

    Custom signals reason over enrichment_data so the LLM needs the data in a
    digestible form. We strip noise (full DOM, raw HTML, base64 screenshots)
    and surface the structured fields signals are most likely to look at.
    """
    parts: List[str] = []

    tech = enrichment_data.get("tech_stack") or {}
    techs = tech.get("technologies_detected") or []
    if techs:
        names = ", ".join(t.get("name") for t in techs if isinstance(t, dict) and t.get("name"))[:1500]
        parts.append(f"Tech stack: {names}")

    jobs = enrichment_data.get("jobs") or {}
    if jobs:
        parts.append(
            f"Jobs: total={jobs.get('total_roles_found', 0)}, "
            f"hiring_sales_bd={jobs.get('is_hiring_sales_bd')}, "
            f"hiring_security={jobs.get('is_hiring_security')}, "
            f"hiring_leadership={jobs.get('is_hiring_leadership')}, "
            f"hiring_technical={jobs.get('is_hiring_technical')}, "
            f"velocity={jobs.get('hiring_velocity')}"
        )

    news = enrichment_data.get("news") or {}
    if news:
        stories = news.get("key_stories") or []
        flags = (
            f"funding={news.get('has_recent_funding')}, "
            f"acquisition={news.get('has_recent_acquisition')}, "
            f"product_launch={news.get('has_recent_product_launch')}, "
            f"partnership={news.get('has_recent_partnership')}"
        )
        parts.append(f"News flags: {flags}")
        if stories:
            top = "; ".join(
                f"[{s.get('category', '?')}] {s.get('headline', '')[:120]}" for s in stories[:6]
            )
            parts.append(f"News stories: {top}")

    deep = enrichment_data.get("deep_research") or {}
    compliance = deep.get("compliance") or {}
    if compliance:
        parts.append(f"Compliance: {json.dumps(compliance)[:600]}")
    funding = deep.get("funding") or {}
    if funding:
        parts.append(f"Funding (deep research): {json.dumps(funding)[:400]}")

    pl_news = enrichment_data.get("predictleads_news") or {}
    pl_events = pl_news.get("events") or []
    if pl_events:
        evt_summary = "; ".join(
            f"[{e.get('category', '?')}] {(e.get('summary') or '')[:90]}" for e in pl_events[:6]
        )
        parts.append(f"PredictLeads events: {evt_summary}")

    pl_jobs = enrichment_data.get("predictleads_jobs") or {}
    pl_titles = pl_jobs.get("titles") or pl_jobs.get("roles") or []
    if pl_titles:
        parts.append(f"PredictLeads recent job titles: {', '.join(pl_titles[:20])}")

    threat_alerts = enrichment_data.get("threat_alerts") or []
    if threat_alerts:
        ta_summary = "; ".join(
            f"{a.get('title', '')} (sev={a.get('severity', '?')}, exploited={a.get('is_actively_exploited', False)})"
            for a in threat_alerts[:6]
        )
        parts.append(f"Threat alerts: {ta_summary}")

    # Allow each new enrichment provider to drop a structured blob under a
    # well-known key — custom signals can then reference it. Without listing
    # every blob explicitly we still surface the keys present so the LLM
    # knows what's available.
    other_keys = sorted(
        k for k in enrichment_data.keys()
        if k not in {
            "tech_stack", "jobs", "news", "deep_research",
            "predictleads_news", "predictleads_jobs", "threat_alerts",
            "gtm_briefing", "enriched_at", "enrichment_sources_run",
            "linkedin_content", "email_resolution", "predictleads_financing",
            "changes_since_last",
        }
    )
    if other_keys:
        parts.append(f"Additional enrichment keys present: {', '.join(other_keys)}")

    return "\n".join(parts) if parts else "(no enrichment data extracted)"


def _evaluate_custom_signals(
    enrichment_data: Dict,
    lead: Dict,
    icp_config: Optional[Dict],
) -> Dict[str, bool]:
    """Evaluate per-client custom signals against this lead's enrichment data.

    Returns a dict of {signal_name: True/False}. Returns {} when the org has
    no custom signals defined — no LLM call made in that case.

    Cost: one Claude call per briefing IF custom signals are defined for the
    client. Skipped entirely otherwise — no overhead for clients that don't
    use this feature.
    """
    if not icp_config:
        return {}
    custom_signals = icp_config.get("custom_signals") or []
    if not isinstance(custom_signals, list) or not custom_signals:
        return {}

    # Validate + format the signal definitions for the prompt
    valid_signals: List[Dict[str, str]] = []
    for sig in custom_signals:
        if not isinstance(sig, dict):
            continue
        name = (sig.get("name") or "").strip()
        eval_prompt = (sig.get("evaluation_prompt") or "").strip()
        if not name or not eval_prompt:
            continue
        valid_signals.append({
            "name": name,
            "description": (sig.get("description") or "").strip(),
            "evaluation_prompt": eval_prompt,
        })

    if not valid_signals:
        return {}

    signal_definitions = "\n\n".join(
        f"### `{s['name']}`\n"
        f"Description: {s['description'] or '(none)'}\n"
        f"Trigger condition: {s['evaluation_prompt']}"
        for s in valid_signals
    )

    full_name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip() or lead.get("email", "(unknown)")
    prompt = _CUSTOM_SIGNALS_PROMPT.format(
        full_name=full_name,
        title=lead.get("title", "(unknown)"),
        company_name=lead.get("company_name", "(unknown)"),
        industry=lead.get("company_industry", "(unknown)"),
        company_size=lead.get("company_size", "(unknown)"),
        location=lead.get("company_location", "(unknown)"),
        enrichment_summary=_summarise_enrichment_for_signals(enrichment_data, lead),
        signal_definitions=signal_definitions,
    )

    try:
        result = call_claude(prompt)
    except Exception as e:
        logger.warning("Custom signal evaluation failed for %s: %s", lead.get("email", "?"), e)
        return {}

    parsed = parse_llm_response(
        CustomSignalEvaluation, result, context="custom_signals"
    )
    if parsed is None:
        return {}
    # CustomSignalEvaluation is keys-passthrough — caller works off the dict
    result = parsed.model_dump()

    # Coerce only the signals we asked about — ignore stray keys the model added
    out: Dict[str, bool] = {}
    for s in valid_signals:
        v = result.get(s["name"])
        if isinstance(v, bool):
            out[s["name"]] = v
        elif isinstance(v, str):
            out[s["name"]] = v.lower() in {"true", "yes", "fired", "1"}
    return out
