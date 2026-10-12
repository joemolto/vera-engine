"""Vera Brain — autonomous agent using Anthropic tool_use loop.

Claude gets a set of tools and reasons through multi-step decisions
autonomously. It can search Apollo, enrich leads, manage the watchlist,
check data quality, and replenish the suggestion pool — all without
human intervention.

Two modes:
  1. Reactive: fast deterministic checks after each enrichment (no Claude)
  2. Proactive: full autonomous agent loop with tool_use (daily schedule)
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import anthropic
import httpx

from app.config import settings
from app.services.activity import log_activity

logger = logging.getLogger(__name__)

MAX_AGENT_TURNS = 15  # safety limit on tool_use iterations

# Sonnet for the executor loop (complex reasoning across tool calls).
# Haiku for the pre-flight planner (cheap pass to bound what the
# executor should focus on this run). Pulled from settings so config
# stays single-source-of-truth.
from app.config import settings as _settings_for_models
AGENT_MODEL = _settings_for_models.CLAUDE_MODEL
PLANNER_MODEL = _settings_for_models.CLAUDE_FAST_MODEL

AGENT_SYSTEM = """You are Vera, an autonomous AI sales intelligence agent. You manage a B2B sales pipeline for {client_name}.

WHO {client_name} IS AND WHAT THEY SELL:
{what_they_sell}

KEY DIFFERENTIATORS: {key_differentiators}
PAIN POINTS THEY SOLVE: {pain_points}

IDEAL CUSTOMER PROFILE (ICP) — THIS DEFINES WHO {client_name} WANTS TO SELL TO:
- Target titles: {target_titles}
- Target industries: {target_industries}
- Target company size: {target_company_size}
- Target locations: {target_locations}
- Who they're looking for: {target_description}
- Buying signals that indicate readiness: {ideal_triggers}

ICP JUDGMENT GUIDANCE — READ THIS CAREFULLY, IT IS THE MOST IMPORTANT CONTEXT:
{icp_guidance}

EXAMPLES OF GOOD LEADS (YES, take action):
{icp_examples_yes}

EXAMPLES OF BAD LEADS (NO, do not take action):
{icp_examples_no}

{personas_section}

ICP RULES:
1. Use the examples above to JUDGE leads, not just the target_titles list. The titles list is a starting point — if someone is clearly a senior decision-maker at a matching company, they're a target even if their exact title isn't listed. Ask: "Would {client_name} want to talk to this person?"

2. Understand the direction of sale: {client_name} sells TO {target_industries_short} companies.

3. Leads with source "linkedin_url" were manually researched by the user. DO NOT auto-enrich or auto-watchlist these. The user chose to research them for their own reasons — leave them alone. Only auto-act on leads from "apollo_discovery" or "vera_brain_apollo" sources.

{critical_rules_section}

YOUR JOB:
Review the pipeline state and take actions to keep it healthy:
- Search Apollo to replenish the suggestion pool when it drops below 5 (use ICP titles, industries, locations)
- Enrich high-potential ICP-matching suggestions that haven't been researched yet
- Auto-watchlist companies of hot leads THAT MATCH THE ICP
- **IMPORTANT: For hot leads, use find_contacts_at_company to discover other decision-makers at the same company.** If we have the CISO, also look for the CTO, CIO, Head of IT etc. Multiple contacts at a hot account dramatically increases the chance of a meeting. Store the new contacts as suggestions, then enrich the best one.
- **IMPORTANT: When watchlist alerts show new signals for a company (funding, product launch, hiring, events), re-enrich the lead at that company so their briefing reflects the fresh intel.** This is high priority — stale briefings with new watchlist signals are a missed opportunity.
- Flag data quality issues (mismatched emails, sparse enrichment, missing LinkedIn verification)
- Flag leads that don't match the ICP so the user knows
- Re-enrich stale leads (>14 days old) that are hot/warm and match the ICP

OPERATIONAL RULES:
- Be conservative with credits. Each enrichment costs 1 credit. Credits remaining: {credits_remaining}.
- If credits are low (<10), only take zero-cost actions (watchlist adds, data flags).
- Always check the pipeline state first before deciding what to do.
- Don't re-enrich leads that were enriched in the last 7 days.
- After taking actions, summarise what you did and why, explaining the ICP reasoning.

Current date: {current_date}"""


# =========================================================================
# Tool definitions for the Anthropic API
# =========================================================================

TOOLS = [
    {
        "name": "get_pipeline_state",
        "description": "Get a summary of the current pipeline: hot leads, warm leads, watchlist, recent signals, stale leads, suggestions, and data quality flags.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_lead_details",
        "description": "Get full details of a specific lead including enrichment data, email confidence, verification status, and signals.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lead_id": {"type": "string", "description": "The UUID of the lead to inspect"},
            },
            "required": ["lead_id"],
        },
    },
    {
        "name": "search_apollo",
        "description": "Search Apollo for new prospects matching criteria. Returns up to 10 people. Use this to replenish the suggestion pool when it's running low.",
        "input_schema": {
            "type": "object",
            "properties": {
                "person_titles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Job titles to search for, e.g. ['CISO', 'CTO', 'VP Security']",
                },
                "person_locations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Locations to search in, e.g. ['United Kingdom']",
                },
                "min_company_size": {"type": "integer", "description": "Minimum company employee count"},
                "max_company_size": {"type": "integer", "description": "Maximum company employee count"},
                "company_industries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Industries to target, e.g. ['financial services', 'insurance']",
                },
            },
            "required": ["person_titles"],
        },
    },
    {
        "name": "find_contacts_at_company",
        "description": "Search Apollo for other decision-makers at a specific company. Use this when a hot lead is found — find other ICP-matching titles at the same company (e.g. if we found the CISO, also look for the CTO, CIO, Head of IT). Returns up to 5 people. Zero credit cost for the search — only costs credits if you then enrich them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company_domain": {"type": "string", "description": "The company domain to search within, e.g. 'acme.com'"},
                "company_name": {"type": "string", "description": "The company name for logging"},
                "exclude_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Names of people we already have — exclude from results to avoid duplicates",
                },
            },
            "required": ["company_domain", "company_name"],
        },
    },
    {
        "name": "enrich_lead",
        "description": "Trigger full enrichment for a lead. Costs 1 credit. The lead must already exist in the database. Returns enrichment summary.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lead_id": {"type": "string", "description": "The UUID of the lead to enrich"},
            },
            "required": ["lead_id"],
        },
    },
    {
        "name": "add_to_watchlist",
        "description": "Add a company to the watchlist for ongoing monitoring. Zero cost. Use this for hot leads or companies with strong signals.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name": {"type": "string", "description": "Company name to watch"},
                "company_domain": {"type": "string", "description": "Company domain"},
                "contact_name": {"type": "string", "description": "Primary contact name"},
                "contact_title": {"type": "string", "description": "Primary contact title"},
                "lead_id": {"type": "string", "description": "Lead ID to link to the watchlist entry"},
                "reason": {"type": "string", "description": "Why this company should be watched"},
            },
            "required": ["company_name", "reason"],
        },
    },
    {
        "name": "flag_data_issue",
        "description": "Flag a data quality issue on a lead for human review. Zero cost.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lead_id": {"type": "string", "description": "The lead with the issue"},
                "issue": {"type": "string", "description": "Description of the data quality issue"},
            },
            "required": ["lead_id", "issue"],
        },
    },
    {
        "name": "store_suggestions",
        "description": "Store new Apollo search results as suggested leads in the database. Call this after search_apollo to save the results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "leads": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "email": {"type": "string"},
                            "first_name": {"type": "string"},
                            "last_name": {"type": "string"},
                            "title": {"type": "string"},
                            "company_name": {"type": "string"},
                            "company_domain": {"type": "string"},
                            "linkedin_url": {"type": "string"},
                        },
                    },
                    "description": "Array of lead objects from Apollo search to store as suggestions",
                },
            },
            "required": ["leads"],
        },
    },
]


# =========================================================================
# Tool implementations
# =========================================================================

def _exec_get_pipeline_state(db, org_id: str) -> str:
    """Gather and return pipeline state as a readable string."""
    now = datetime.now(timezone.utc)

    def _freshness_tag(enriched_at: Optional[str]) -> str:
        """Human-readable freshness label so the brain can see at a glance
        whether re-enrichment is allowed under the 7-day rule."""
        if not enriched_at:
            return "never enriched"
        try:
            ts = datetime.fromisoformat(enriched_at.replace("Z", "+00:00"))
            days = (now - ts).days
        except Exception:
            return "unknown"
        if days <= 0:
            return "enriched today — DO NOT re-enrich"
        if days < 7:
            return f"enriched {days}d ago — DO NOT re-enrich (under 7-day cooldown)"
        if days < 14:
            return f"enriched {days}d ago — OK to re-enrich if signals warrant"
        return f"enriched {days}d ago — STALE, re-enrich"

    # Hot leads
    hot = db.table("leads").select("id, first_name, last_name, title, company_name, company_industry, enriched_at, email_confidence, lead_rating, source").eq("org_id", org_id).eq("lead_rating", "hot").order("enriched_at", desc=True).limit(10).execute()
    hot_lines = [f"- [{l['id']}] {l['first_name']} {l['last_name']}, {l['title']} at {l['company_name']} (industry: {l.get('company_industry', 'N/A')}, source: {l.get('source', 'N/A')}, email conf: {l.get('email_confidence', 'N/A')}%, {_freshness_tag(l.get('enriched_at'))})" for l in (hot.data or [])]

    # Warm leads
    warm = db.table("leads").select("id, first_name, last_name, title, company_name, company_industry, enriched_at, email_confidence, source").eq("org_id", org_id).eq("lead_rating", "warm").order("enriched_at", desc=True).limit(10).execute()
    warm_lines = [f"- [{l['id']}] {l['first_name']} {l['last_name']}, {l['title']} at {l['company_name']} (industry: {l.get('company_industry', 'N/A')}, source: {l.get('source', 'N/A')}, email conf: {l.get('email_confidence', 'N/A')}%, {_freshness_tag(l.get('enriched_at'))})" for l in (warm.data or [])]

    # Watchlist
    wl = db.table("watchlist").select("company_name, company_domain, target_contact_name").eq("org_id", org_id).execute()
    wl_lines = [f"- {w['company_name']} ({w.get('company_domain', '')})" for w in (wl.data or [])]
    wl_companies = {w["company_name"].lower() for w in (wl.data or []) if w.get("company_name")}

    # Hot leads not on watchlist
    hot_not_watched = [l for l in (hot.data or []) if l.get("company_name", "").lower() not in wl_companies]
    hot_not_watched_lines = [f"- {l['company_name']} ({l['first_name']} {l['last_name']})" for l in hot_not_watched]

    # Suggestions
    suggestions = db.table("leads").select("id, first_name, last_name, title, company_name", count="exact").eq("org_id", org_id).eq("enrichment_status", "suggested").execute()
    suggestion_count = suggestions.count if suggestions.count else len(suggestions.data or [])
    from app.services.tiers import get_suggestion_target
    _sugg_target = get_suggestion_target(db, org_id)
    _pool_full = suggestion_count >= _sugg_target

    # Stale leads (enriched >14 days ago)
    fourteen_ago = (now - timedelta(days=14)).isoformat()
    stale = db.table("leads").select("id, first_name, last_name, company_name, lead_rating, enriched_at").eq("org_id", org_id).in_("lead_rating", ["hot", "warm"]).lt("enriched_at", fourteen_ago).limit(10).execute()
    stale_lines = [f"- [{l['id']}] {l['first_name']} {l['last_name']} at {l['company_name']} ({l['lead_rating']}, enriched: {l.get('enriched_at', 'N/A')})" for l in (stale.data or [])]

    # Recent signals
    seven_ago = (now - timedelta(days=7)).isoformat()
    signals = db.table("signals").select("signal_type, lead_id, detected_at").eq("org_id", org_id).gte("detected_at", seven_ago).order("detected_at", desc=True).limit(15).execute()
    signal_lines = [f"- {s['signal_type']} (lead: {s.get('lead_id', 'N/A')}, {s.get('detected_at', '')})" for s in (signals.data or [])]

    # Data quality flags
    flagged = db.table("leads").select("id, first_name, last_name, company_name, enrichment_data").eq("org_id", org_id).not_.is_("enriched_at", "null").order("enriched_at", desc=True).limit(20).execute()
    flag_lines = []
    for l in (flagged.data or []):
        flags = (l.get("enrichment_data") or {}).get("vera_flags") or []
        for f in flags:
            flag_lines.append(f"- [{l['id']}] {l['first_name']} {l['last_name']} at {l['company_name']}: {f}")

    # Recent watchlist alerts (last 7 days) — cross-reference with enriched leads
    wl_alerts = db.table("watchlist_alerts").select("alert_type, alert_title, alert_summary, watchlist_id, detected_at").eq("org_id", org_id).gte("detected_at", seven_ago).order("detected_at", desc=True).limit(20).execute()
    # Map watchlist_id to company name
    wl_id_to_company = {}
    wl_id_to_domain = {}
    for w in (wl.data or []):
        # wl was fetched above — but doesn't have id. Re-fetch with id.
        pass
    wl_full = db.table("watchlist").select("id, company_name, company_domain").eq("org_id", org_id).execute()
    for w in (wl_full.data or []):
        wl_id_to_company[w["id"]] = w.get("company_name", "")
        wl_id_to_domain[w["id"]] = w.get("company_domain", "")

    wl_alert_lines = []
    companies_with_new_alerts = set()
    for a in (wl_alerts.data or []):
        company = wl_id_to_company.get(a.get("watchlist_id"), "Unknown")
        companies_with_new_alerts.add(company.lower())
        wl_alert_lines.append(f"- {company}: [{a.get('alert_type')}] {a.get('alert_title', '')} ({a.get('detected_at', '')})")

    # Leads at companies with new watchlist alerts that should be re-enriched
    leads_needing_refresh = []
    if companies_with_new_alerts:
        enriched_leads = db.table("leads").select("id, first_name, last_name, company_name, enriched_at, lead_rating").eq("org_id", org_id).not_.is_("enriched_at", "null").execute()
        for l in (enriched_leads.data or []):
            if l.get("company_name", "").lower() in companies_with_new_alerts:
                leads_needing_refresh.append(
                    f"- [{l['id']}] {l['first_name']} {l['last_name']} at {l['company_name']} "
                    f"({l.get('lead_rating', 'unrated')}, enriched: {l.get('enriched_at', 'N/A')})"
                )

    return f"""PIPELINE STATE:

HOT LEADS ({len(hot.data or [])}):
{chr(10).join(hot_lines) or 'None'}

WARM LEADS ({len(warm.data or [])}):
{chr(10).join(warm_lines) or 'None'}

WATCHLIST ({len(wl.data or [])} accounts):
{chr(10).join(wl_lines) or 'None'}

HOT LEADS NOT ON WATCHLIST:
{chr(10).join(hot_not_watched_lines) or 'All hot leads are on the watchlist'}

RECENT WATCHLIST ALERTS (last 7 days — these are NEW signals from monitored accounts):
{chr(10).join(wl_alert_lines) or 'No recent alerts'}

LEADS THAT SHOULD BE RE-ENRICHED (they have new watchlist alerts since last enrichment):
{chr(10).join(leads_needing_refresh) or 'None — all leads are up to date'}

SUGGESTION POOL: {suggestion_count}/{_sugg_target} (tier cap). {'POOL FULL — do NOT search_apollo / store_suggestions. Spend this run enriching the suggested leads so they become scored, briefed leads.' if _pool_full else f'Room for {_sugg_target - suggestion_count} more before the cap.'}

STALE LEADS (enriched >14 days ago):
{chr(10).join(stale_lines) or 'None'}

RECENT SIGNALS (7 days):
{chr(10).join(signal_lines) or 'None'}

DATA QUALITY FLAGS:
{chr(10).join(flag_lines) or 'No flags'}"""


def _exec_get_lead_details(db, org_id: str, lead_id: str) -> str:
    """Get full details of a specific lead."""
    result = db.table("leads").select("*").eq("id", lead_id).eq("org_id", org_id).execute()
    if not result.data:
        return f"Lead {lead_id} not found in this org."

    lead = result.data[0]
    ed = lead.get("enrichment_data") or {}

    verification = ed.get("linkedin_verification") or {}
    email_res = ed.get("email_resolution") or {}
    briefing = ed.get("gtm_briefing") or {}

    return f"""LEAD: {lead.get('first_name')} {lead.get('last_name')}
Title: {lead.get('title')}
Company: {lead.get('company_name')} ({lead.get('company_domain', '')})
Email: {lead.get('email', 'N/A')} (confidence: {lead.get('email_confidence', 'N/A')}%, source: {email_res.get('source', 'N/A')})
Rating: {lead.get('lead_rating', 'unrated')}
Data status: {lead.get('data_status', 'unknown')}
Enriched at: {lead.get('enriched_at', 'not enriched')}
LinkedIn: {lead.get('linkedin_url', 'N/A')}
Industry: {lead.get('company_industry', 'N/A')}
Company size: {lead.get('company_size', 'N/A')}
Verification: {verification.get('verification_status', 'N/A')} — {verification.get('explanation', '')}
Briefing summary: {briefing.get('intelligence_summary', 'No briefing')}
Vera flags: {ed.get('vera_flags', 'None')}"""


_APOLLO_SEARCH_CACHE_TTL_SECONDS = 600
_apollo_search_cache: Dict[str, Dict[str, Any]] = {}


def _cache_apollo_people(org_id: str, people: List[Dict[str, Any]]) -> None:
    """Stash the raw Apollo people list against an org so a follow-up
    store_suggestions call can hydrate Claude's reconstructed leads with
    the fields the LLM tends to drop (last_name, linkedin_url, email)."""
    _apollo_search_cache[org_id] = {
        "people": people,
        "stored_at": datetime.now(timezone.utc),
    }


def _hydrate_lead_from_apollo_cache(org_id: str, lead: Dict[str, Any]) -> Dict[str, Any]:
    """Look up a freshly-cached Apollo person matching this lead and
    fill in any missing last_name / linkedin_url / email.

    Why this exists: ``_exec_search_apollo`` returns a text summary for
    the LLM. The LLM then has to re-serialise the rows back as dicts
    when calling ``store_suggestions`` — and consistently drops
    last_name / linkedin_url / email even though Apollo had them.

    Match key: first_name + company_domain (or company_name).
    Returns a NEW dict; the input is not mutated.
    """
    cache = _apollo_search_cache.get(org_id)
    if not cache:
        return lead
    age = (datetime.now(timezone.utc) - cache["stored_at"]).total_seconds()
    if age > _APOLLO_SEARCH_CACHE_TTL_SECONDS:
        _apollo_search_cache.pop(org_id, None)
        return lead

    lead_first = (lead.get("first_name") or "").strip().lower()
    lead_domain = (lead.get("company_domain") or "").strip().lower()
    lead_company = (lead.get("company_name") or "").strip().lower()
    if not lead_first:
        return lead

    for p in cache["people"]:
        p_first = (p.get("first_name") or "").strip().lower()
        if not p_first or p_first != lead_first:
            continue
        p_org = p.get("organization") or {}
        p_domain = (p_org.get("primary_domain") or "").strip().lower()
        p_company = (p_org.get("name") or "").strip().lower()
        if lead_domain and p_domain and lead_domain == p_domain:
            pass
        elif lead_company and p_company and lead_company == p_company:
            pass
        else:
            continue
        return {
            **lead,
            "last_name": lead.get("last_name") or p.get("last_name") or "",
            "linkedin_url": lead.get("linkedin_url") or p.get("linkedin_url") or "",
            "email": lead.get("email") or p.get("email") or "",
        }
    return lead


def _exec_search_apollo(org_id: str, person_titles: List[str], person_locations: List[str] = None,
                         min_company_size: int = None, max_company_size: int = None,
                         company_industries: List[str] = None) -> str:
    """Search Apollo for people matching criteria.

    Side effect: caches the raw Apollo people list against ``org_id``
    so a follow-up store_suggestions call can hydrate Claude's
    reconstructed lead dicts with the fields the LLM drops
    (last_name, linkedin_url, email).
    """
    if not settings.APOLLO_API_KEY:
        return "Apollo API key not configured."

    # mixed_people/api_search requires the API key in an X-Api-Key header.
    # Body `api_key` is silently ignored on this endpoint — the call returns
    # HTTP 200 with people:[]. Same root cause that made multithread look
    # broken until we fixed it. Using api_search (newer, the one Apollo's
    # MCP also uses) instead of the legacy mixed_people/search.
    headers = {"X-Api-Key": settings.APOLLO_API_KEY}
    payload: Dict[str, Any] = {
        "person_titles": person_titles,
        "page": 1,
        "per_page": 10,
    }
    if person_locations:
        payload["person_locations"] = person_locations
    if min_company_size is not None:
        # Apollo expects predefined range strings like "1,10", "11,50", "51,100"
        # Map the requested range to the closest Apollo ranges
        ranges = []
        size = min_company_size or 1
        max_size = max_company_size or 10000
        for lo, hi in [(1, 10), (11, 50), (51, 100), (101, 200), (201, 500), (501, 1000), (1001, 5000), (5001, 10000)]:
            if lo <= max_size and hi >= size:
                ranges.append(f"{lo},{hi}")
        if ranges:
            payload["organization_num_employees_ranges"] = ranges
    if company_industries:
        payload["q_organization_keyword_tags"] = company_industries

    try:
        data = None
        with httpx.Client(timeout=20) as client:
            resp = client.post(
                "https://api.apollo.io/api/v1/mixed_people/api_search",
                json=payload,
                headers=headers,
            )
            if resp.is_success:
                data = resp.json()
            else:
                logger.warning(
                    "Apollo api_search returned %d: %s",
                    resp.status_code, resp.text[:200],
                )
                return f"Apollo search failed (HTTP {resp.status_code}). May be a temporary API issue."

        if not data:
            return "Apollo search returned no data."

        people = data.get("people") or []
        if not people:
            return "No results found in Apollo for these criteria."

        # Stash the raw rows so store_suggestions can hydrate Claude's
        # reconstructed dicts (which routinely drop last_name +
        # linkedin_url + email even though Apollo returned them).
        _cache_apollo_people(org_id, people[:10])

        lines = []
        for p in people[:10]:
            org = p.get("organization") or {}
            lines.append(
                f"- {p.get('first_name', '')} {p.get('last_name', '')}, "
                f"{p.get('title', 'N/A')} at {org.get('name', 'N/A')} "
                f"({org.get('primary_domain', 'N/A')}) "
                f"[email: {p.get('email', 'N/A')}, linkedin: {p.get('linkedin_url', 'N/A')}]"
            )
        return f"Found {len(people)} people:\n" + "\n".join(lines)

    except Exception as e:
        return f"Apollo search failed: {e}"


def _exec_find_contacts_at_company(db, org_id: str, company_domain: str,
                                     company_name: str, exclude_names: List[str] = None) -> str:
    """Search Apollo for other decision-makers at a specific company.

    Uses the org's ICP target_titles to find relevant contacts at the
    same domain. Excludes people we already have. Returns matches that
    can be stored via store_suggestions.
    """
    if not settings.APOLLO_API_KEY:
        return "Apollo API key not configured."

    # Get ICP target titles for this org
    org_result = db.table("organisations").select("icp_config").eq("id", org_id).execute()
    target_titles = []
    if org_result.data:
        icp = org_result.data[0].get("icp_config") or {}
        target_titles = icp.get("target_titles") or []

    if not target_titles:
        target_titles = ["CISO", "CTO", "CIO", "Head of Security", "VP Security"]

    # Get existing leads at this company to avoid duplicates
    existing = db.table("leads").select("first_name, last_name").eq("org_id", org_id).eq("company_domain", company_domain).execute()
    existing_names = {f"{l['first_name']} {l['last_name']}".lower() for l in (existing.data or [])}
    if exclude_names:
        existing_names.update(n.lower() for n in exclude_names)

    # mixed_people/api_search with X-Api-Key header is the working combo —
    # `q_organization_domains_list` (list form) is the parameter name that
    # actually filters by domain on this endpoint. Verified live in the
    # multithread fix; using the same shape here so the brain's contact
    # discovery tool works the same way.
    headers = {"X-Api-Key": settings.APOLLO_API_KEY}
    payload = {
        "person_titles": target_titles,
        "q_organization_domains_list": [company_domain],
        "page": 1,
        "per_page": 10,
    }

    try:
        data = None
        with httpx.Client(timeout=20) as client:
            resp = client.post(
                "https://api.apollo.io/api/v1/mixed_people/api_search",
                json=payload,
                headers=headers,
            )
            if resp.is_success:
                data = resp.json()
            else:
                logger.warning(
                    "Apollo find_contacts api_search returned %d: %s",
                    resp.status_code, resp.text[:200],
                )

            # Fallback by org name if domain returned no people. Triggers
            # when the primary call didn't match anything (e.g. the org's
            # domain is a different vanity URL than what Apollo indexed).
            if (not data or not data.get("people")) and company_name:
                fallback_payload = {
                    "person_titles": target_titles,
                    "q_organization_name": company_name,
                    "page": 1,
                    "per_page": 10,
                }
                resp = client.post(
                    "https://api.apollo.io/api/v1/mixed_people/api_search",
                    json=fallback_payload,
                    headers=headers,
                )
                if resp.is_success:
                    data = resp.json()

        if not data:
            return f"Apollo search failed for {company_name} ({company_domain})."

        people = data.get("people") or []
        if not people:
            return f"No other ICP-matching contacts found at {company_name} ({company_domain})."

        # Filter out people we already have
        new_contacts = []
        for p in people:
            full_name = f"{p.get('first_name', '')} {p.get('last_name', '')}".lower()
            if full_name in existing_names:
                continue
            org = p.get("organization") or {}
            new_contacts.append({
                "first_name": p.get("first_name", ""),
                "last_name": p.get("last_name", ""),
                "title": p.get("title", ""),
                "email": p.get("email", ""),
                "linkedin_url": p.get("linkedin_url", ""),
                "company_name": org.get("name", company_name),
                "company_domain": org.get("primary_domain", company_domain),
            })

        if not new_contacts:
            return f"Found people at {company_name} but they're all already in the pipeline."

        lines = []
        for c in new_contacts[:5]:
            lines.append(
                f"- {c['first_name']} {c['last_name']}, {c['title']} "
                f"[email: {c.get('email', 'N/A')}, linkedin: {c.get('linkedin_url', 'N/A')}]"
            )

        return (
            f"Found {len(new_contacts)} new ICP-matching contacts at {company_name}:\n"
            + "\n".join(lines)
            + "\n\nUse store_suggestions to save these to the pipeline, then enrich the most promising ones."
        )

    except Exception as e:
        return f"Apollo company contact search failed for {company_domain}: {e}"


def _exec_enrich_lead(db, org_id: str, lead_id: str) -> str:
    """Trigger enrichment for a lead. Returns summary with critical source warnings.

    Hard 7-day cooldown: refuses if the lead was already enriched within the
    last week, UNLESS there's a recent watchlist alert for the same company
    (in which case fresh intel is warranted). This stops the brain from
    racking up enrichment_version numbers by re-enriching the same hot
    lead every daily run.
    """
    try:
        from app.services.enricher import run_full_enrichment
        from app.services.usage import record_enrichment, check_enrichment_allowed

        # Cooldown check
        existing = (
            db.table("leads")
            .select("enriched_at, company_name, enrichment_version")
            .eq("id", lead_id)
            .eq("org_id", org_id)
            .execute()
        )
        if existing.data and existing.data[0].get("enriched_at"):
            try:
                last = datetime.fromisoformat(
                    existing.data[0]["enriched_at"].replace("Z", "+00:00")
                )
                days_since = (datetime.now(timezone.utc) - last).days
            except Exception:
                days_since = 999

            if days_since < 7:
                # Allow override only if there's a watchlist alert for this
                # company in the last 7 days — that's the documented exception.
                company = (existing.data[0].get("company_name") or "").lower()
                has_fresh_alert = False
                if company:
                    seven_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                    wl = (
                        db.table("watchlist")
                        .select("id")
                        .eq("org_id", org_id)
                        .ilike("company_name", company)
                        .execute()
                    )
                    if wl.data:
                        wl_ids = [w["id"] for w in wl.data]
                        alerts = (
                            db.table("watchlist_alerts")
                            .select("id")
                            .in_("watchlist_id", wl_ids)
                            .gte("detected_at", seven_ago)
                            .limit(1)
                            .execute()
                        )
                        has_fresh_alert = bool(alerts.data)

                if not has_fresh_alert:
                    version = existing.data[0].get("enrichment_version", "?")
                    return (
                        f"Skipped: lead was enriched {days_since}d ago (version {version}) "
                        f"and there's no fresh watchlist alert for the company. "
                        f"The 7-day cooldown is in effect to avoid spending credits on "
                        f"unchanged data. Try a different lead, or wait for a signal."
                    )

        allowed, usage_info = check_enrichment_allowed(db, org_id, 1)
        if not allowed:
            return f"Cannot enrich: credit limit reached ({usage_info['used']}/{usage_info['limit']})."

        result = run_full_enrichment(
            db=db, org_id=org_id, lead_ids=[lead_id],
            enrichment_sources=["website", "tech_stack", "news", "jobs"],
        )
        record_enrichment(db, org_id, 1)

        if result and not result[0].get("error"):
            sources = result[0].get("sources_completed", [])
            response = f"Enrichment complete. Sources: {', '.join(sources)}."

            # Check for missing critical sources
            critical_missing = []
            if "linkedin_verification" not in sources:
                critical_missing.append("LinkedIn verification (employment check failed — company/title may be stale)")
            if "email_finder" not in sources and "predictleads" not in sources:
                critical_missing.append("PredictLeads (company intelligence missing)")

            if critical_missing:
                response += f"\n\nWARNING — Critical sources missing:\n"
                for m in critical_missing:
                    response += f"- {m}\n"
                response += "Consider flagging this lead for re-enrichment later when these sources are available."
            else:
                response += " All critical sources completed. Lead is ready for scoring."

            return response
        else:
            error = result[0].get("error", "Unknown error") if result else "No result"
            return f"Enrichment failed: {error}"

    except Exception as e:
        return f"Enrichment failed: {e}"


def _exec_add_to_watchlist(db, org_id: str, company_name: str, company_domain: str = "",
                            contact_name: str = "", contact_title: str = "",
                            lead_id: str = "", reason: str = "") -> str:
    """Add a company to the watchlist."""
    # Check if already on watchlist
    existing = db.table("watchlist").select("id").eq("org_id", org_id).eq("company_name", company_name).execute()
    if existing.data:
        return f"{company_name} is already on the watchlist."

    # Check tier limit
    from app.services.billing import TIER_LIMITS
    org_resp = db.table("organisations").select("tier").eq("id", org_id).execute()
    tier = (org_resp.data[0].get("tier") or "scout") if org_resp.data else "scout"
    limit = TIER_LIMITS.get(tier, 5)
    count_resp = db.table("watchlist").select("id", count="exact").eq("org_id", org_id).execute()
    current_count = count_resp.count if count_resp.count is not None else 0
    if current_count >= limit:
        return f"Cannot add {company_name}: watchlist limit reached ({current_count}/{limit} on {tier} tier). Upgrade needed."

    row = {
        "org_id": org_id,
        "company_name": company_name,
        "company_domain": company_domain,
        "target_contact_name": contact_name,
        "target_contact_title": contact_title,
        "notes": f"Added by Vera Brain: {reason}",
    }
    if lead_id:
        row["lead_id"] = lead_id

    try:
        db.table("watchlist").insert(row).execute()
        if lead_id:
            log_activity(db, org_id, lead_id, "vera_auto_watchlist", {
                "company_name": company_name, "reason": reason,
            })
        return f"Added {company_name} to watchlist. Reason: {reason}"
    except Exception as e:
        return f"Failed to add {company_name} to watchlist: {e}"


def _exec_flag_data_issue(db, org_id: str, lead_id: str, issue: str) -> str:
    """Flag a data quality issue on a lead."""
    try:
        lead_result = db.table("leads").select("enrichment_data").eq("id", lead_id).eq("org_id", org_id).execute()
        if not lead_result.data:
            return f"Lead {lead_id} not found."

        ed = lead_result.data[0].get("enrichment_data") or {}
        flags = ed.get("vera_flags") or []
        if issue not in flags:
            flags.append(issue)
        ed["vera_flags"] = flags

        db.table("leads").update({"enrichment_data": ed}).eq("id", lead_id).execute()
        log_activity(db, org_id, lead_id, "vera_data_flag", {"issue": issue})
        return f"Flagged: {issue}"
    except Exception as e:
        return f"Failed to flag issue: {e}"


def _apollo_unlock_by_id(apollo_id: str) -> Optional[Dict[str, Any]]:
    """Spend one Apollo credit to unlock a person's verified contact
    data (last_name, work email, linkedin_url, phone).

    Why this exists: Apollo's free search endpoint
    (``mixed_people/api_search``) deliberately obfuscates contact
    fields — last_name comes back as ``last_name_obfuscated: "Wi***s"``,
    linkedin_url and email are stripped to ``None``. To get the real
    values, we have to hit the enrichment endpoint with the
    ``reveal_personal_emails`` flag, which Apollo bills as one credit
    per matched person.

    Critical: the ``apollo_id`` MUST come from a *fresh* search response
    in the same call chain. IDs stored on lead rows from previous runs
    go stale once Apollo's internal team-revealed shadow record diverges
    from the master, and bulk_match silently returns ``match=None`` with
    ``credits_consumed=0``.

    Returns the unlocked ``person`` payload (with verified last_name +
    email + linkedin_url), or ``None`` on any failure / non-match. Never
    raises — one bad unlock cannot abort the calling pipeline.
    """
    if not settings.APOLLO_API_KEY or not apollo_id:
        return None
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                "https://api.apollo.io/api/v1/people/bulk_match",
                json={
                    "details": [{"id": apollo_id}],
                    "reveal_personal_emails": True,
                    "reveal_phone_number": False,
                },
                headers={"X-Api-Key": settings.APOLLO_API_KEY},
            )
    except Exception as e:
        logger.warning("Apollo bulk_match exception for id=%s: %s", apollo_id, e)
        return None
    if not resp.is_success:
        logger.warning(
            "Apollo bulk_match returned %d for id=%s: %s",
            resp.status_code, apollo_id, resp.text[:200],
        )
        return None
    data = resp.json() or {}
    matches = data.get("matches") or []
    credits = data.get("credits_consumed", 0)
    person = matches[0] if matches else None
    if person:
        logger.info(
            "Apollo unlock OK (id=%s, credits=%s): last=%r email=%r li=%r",
            apollo_id, credits,
            person.get("last_name"),
            person.get("email"),
            person.get("linkedin_url"),
        )
    else:
        logger.warning(
            "Apollo unlock returned no match for id=%s (credits=%s) — "
            "likely a stale id from a prior search; need a fresh search",
            apollo_id, credits,
        )
    return person


def _apollo_match_person(
    *,
    first_name: str,
    last_name: str,
    company_domain: str,
    company_name: str,
    title: str = "",
) -> Optional[Dict[str, Any]]:
    """Look up + unlock the full Apollo person record for a partial identity.

    Three-stage strategy:

      1. ``mixed_people/api_search`` filtered by company domain (or
         name) + title (when known). Free; returns obfuscated last_name
         + apollo_id. We pick the candidate whose first_name matches.

      2. ``people/bulk_match`` by the FRESH apollo_id from stage 1 with
         ``reveal_personal_emails=True``. Spends one Apollo credit per
         match. Returns the full record (verified last_name, work
         email, linkedin_url).

      3. ``people/match`` (singular) as a fallback when the caller
         already has full first+last+domain so the endpoint can
         disambiguate. Skipped when we only have first_name.

    Returns the unlocked ``person`` payload on success, or the
    obfuscated search result if unlock fails (better than nothing —
    surfaces last_name_obfuscated to the dashboard).

    Engine operators: each successful unlock at stage 2 consumes
    one Apollo enrichment credit. Check the bulk_match WARN logs for
    ``credits=N`` to track spend.
    """
    if not settings.APOLLO_API_KEY:
        return None
    if not first_name and not last_name:
        return None
    if not company_domain and not company_name:
        return None

    headers = {"X-Api-Key": settings.APOLLO_API_KEY}
    first_lc = (first_name or "").strip().lower()
    last_lc = (last_name or "").strip().lower()

    candidate: Optional[Dict[str, Any]] = None

    # ---- Stage 1: search by domain (+ title), pick first-name match ----
    if company_domain or company_name:
        search_payload: Dict[str, Any] = {"page": 1, "per_page": 25}
        if company_domain:
            search_payload["q_organization_domains"] = [company_domain]
        # Always include org-name when we have one — Apollo's domain
        # filter is brittle (returns 0 for some valid domains) but
        # org-name + title is reliable.
        if company_name:
            search_payload["q_organization_name"] = company_name
        if title:
            # Apollo's title match is fuzzy enough that "Chief Information
            # Security Officer (CISO)" hits "CISO", "Chief Info Sec", etc.
            search_payload["person_titles"] = [title]

        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(
                    "https://api.apollo.io/api/v1/mixed_people/api_search",
                    json=search_payload,
                    headers=headers,
                )
            if resp.is_success:
                people = (resp.json() or {}).get("people") or []
                # Prefer exact first-name match.
                if first_lc:
                    for p in people:
                        if (p.get("first_name") or "").strip().lower() == first_lc:
                            candidate = p
                            break
                # Fallback: exact last-name match (when caller had one).
                if candidate is None and last_lc:
                    for p in people:
                        if (p.get("last_name") or "").strip().lower() == last_lc:
                            candidate = p
                            break
                # Fallback: title+domain narrowed it to a single result.
                if candidate is None and title and len(people) == 1:
                    candidate = people[0]
            else:
                logger.warning(
                    "Apollo search returned %d for %s @ %s / title=%r: %s",
                    resp.status_code, first_name,
                    company_domain or company_name, title, resp.text[:200],
                )
        except Exception as e:
            logger.warning(
                "Apollo search failed for %s @ %s: %s",
                first_name, company_domain or company_name, e,
            )

    # ---- Stage 2: bulk_match unlock by fresh apollo_id (spends 1 credit) ----
    if candidate and candidate.get("id"):
        unlocked = _apollo_unlock_by_id(candidate["id"])
        if unlocked:
            return unlocked
        # If unlock fails (e.g. credits exhausted), fall back to the
        # obfuscated search hit. That at least gives us
        # last_name_obfuscated for the dashboard and an apollo_id for
        # later retry.
        return candidate

    # ---- Stage 3: people/match (singular) fallback for full-name leads ----
    if not (first_name and last_name):
        return None
    payload: Dict[str, Any] = {
        "first_name": first_name,
        "last_name": last_name,
        "reveal_personal_emails": True,
    }
    if company_domain:
        payload["domain"] = company_domain
    if company_name:
        payload["organization_name"] = company_name
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                "https://api.apollo.io/api/v1/people/match",
                json=payload,
                headers=headers,
            )
        if not resp.is_success:
            logger.warning(
                "Apollo people/match returned %d for %s %s @ %s: %s",
                resp.status_code, first_name, last_name,
                company_domain or company_name, resp.text[:200],
            )
            return None
        return (resp.json() or {}).get("person")
    except Exception as e:
        logger.warning(
            "Apollo people/match failed for %s %s @ %s: %s",
            first_name, last_name, company_domain or company_name, e,
        )
        return None


def _hydrate_from_apollo_match(lead: Dict[str, Any]) -> Dict[str, Any]:
    """If a brain-sourced lead is missing the three contact fields the
    downstream enricher needs (last_name, linkedin_url, email), look it
    up in Apollo and merge the verified fields in. Idempotent — leads
    that already have all three pass through untouched.
    """
    has_last = bool((lead.get("last_name") or "").strip())
    has_linkedin = bool((lead.get("linkedin_url") or "").strip())
    has_email = bool((lead.get("email") or "").strip())
    if has_last and has_linkedin and has_email:
        return lead

    person = _apollo_match_person(
        first_name=lead.get("first_name", "") or "",
        last_name=lead.get("last_name", "") or "",
        company_domain=lead.get("company_domain", "") or "",
        company_name=lead.get("company_name", "") or "",
        title=lead.get("title", "") or "",
    )
    if not person:
        return lead

    merged = dict(lead)
    if not has_last and person.get("last_name"):
        merged["last_name"] = person["last_name"]
    if not has_linkedin and person.get("linkedin_url"):
        merged["linkedin_url"] = person["linkedin_url"]
    if not has_email and person.get("email"):
        merged["email"] = person["email"]
    # Track Apollo's stable id so the enricher can re-lookup later if
    # we ever need to refresh contact data without a name guess.
    if person.get("id"):
        merged["apollo_id"] = person["id"]
    # Title sometimes comes back richer from people/match than from
    # search results — backfill only if ours is blank.
    if not (merged.get("title") or "").strip() and person.get("title"):
        merged["title"] = person["title"]
    return merged


def _exec_store_suggestions(db, org_id: str, leads: List[Dict]) -> str:
    """Store Apollo search results as suggested leads.

    Hard tier cap (single source of truth: app/services/tiers.py). The
    Brain must not push a Scout org past 5 suggestions just because it
    thinks a bigger pool is "healthy". This is the authoritative
    guardrail — enforced at the data layer so it holds even when the
    LLM ignores the prompt guidance.

    Before insert, each incoming row is hydrated via Apollo people/match
    if it's missing last_name / linkedin_url / email — the
    ``search_apollo`` tool returns sparse summaries (Apollo redacts
    verified contact data on its search endpoint) and Claude can only
    pass through what it saw. Without hydration the resulting leads
    score cold for "insufficient data quality" and never produce a
    real briefing.
    """
    from app.services.tiers import get_suggestion_target

    target = get_suggestion_target(db, org_id)
    existing_resp = (
        db.table("leads")
        .select("id", count="exact")
        .eq("org_id", org_id)
        .eq("enrichment_status", "suggested")
        .execute()
    )
    existing_count = existing_resp.count or len(existing_resp.data or [])
    remaining = max(0, target - existing_count)
    if remaining <= 0:
        return (
            f"Suggestion pool is already at the tier cap "
            f"({existing_count}/{target}). Do NOT source or store more — the "
            f"pool is full. Spend this run ENRICHING existing suggested "
            f"leads (enrich_lead) so they become scored, briefed leads."
        )

    stored = 0
    dupes = 0
    capped = 0
    hydrated = 0
    for lead in leads:
        if stored >= remaining:
            capped += 1
            continue

        # Hydrate missing last_name / linkedin_url / email before
        # dedup + insert. This is the fix for the long-running
        # "vera_brain_apollo leads land skeletal" bug.
        #
        # Two-tier waterfall, cheapest first:
        #   1. In-memory cache of the raw Apollo search response (free)
        #   2. Apollo people/match API call (one credit per row)
        #
        # The cache is populated by _exec_search_apollo when the same
        # tool_use loop just searched — so in the common case where
        # search_apollo → store_suggestions runs back-to-back, every
        # lead gets hydrated without paying for people/match at all.
        before_hydrate_missing = not (
            (lead.get("last_name") or "").strip()
            and (lead.get("linkedin_url") or "").strip()
            and (lead.get("email") or "").strip()
        )
        lead = _hydrate_lead_from_apollo_cache(org_id, lead)
        # If the cache didn't have it (or filled some but not all),
        # pay for people/match for the remainder.
        lead = _hydrate_from_apollo_match(lead)
        if before_hydrate_missing and (
            (lead.get("last_name") or "").strip()
            or (lead.get("linkedin_url") or "").strip()
            or (lead.get("email") or "").strip()
        ):
            hydrated += 1

        email = lead.get("email") or ""
        linkedin = lead.get("linkedin_url") or ""

        # Dedup check
        if email:
            dupe = db.table("leads").select("id").eq("org_id", org_id).eq("email", email).execute()
            if dupe.data:
                dupes += 1
                continue
        if linkedin:
            dupe = db.table("leads").select("id").eq("org_id", org_id).eq("linkedin_url", linkedin).execute()
            if dupe.data:
                dupes += 1
                continue

        row = {
            "org_id": org_id,
            "email": email,
            "first_name": lead.get("first_name", ""),
            "last_name": lead.get("last_name", ""),
            "title": lead.get("title", ""),
            "company_name": lead.get("company_name", ""),
            "company_domain": lead.get("company_domain", ""),
            "linkedin_url": linkedin,
            "source": "vera_brain_apollo",
            "enrichment_status": "suggested",
            "status": "new",
        }
        if lead.get("apollo_id"):
            row["apollo_id"] = lead["apollo_id"]
        try:
            db.table("leads").insert(row).execute()
            stored += 1
        except Exception as e:
            logger.warning("Failed to store suggestion: %s", e)

    msg = (
        f"Stored {stored} new suggestions ({dupes} duplicates skipped, "
        f"{hydrated} hydrated via Apollo people/match). "
        f"Pool now {existing_count + stored}/{target} for this tier."
    )
    if capped:
        msg += (
            f" {capped} further lead(s) were NOT stored — tier cap reached. "
            f"Stop sourcing; ENRICH the suggested leads instead (enrich_lead)."
        )
    return msg


# =========================================================================
# Tool dispatcher
# =========================================================================

def _execute_tool(db, org_id: str, tool_name: str, tool_input: dict) -> str:
    """Route a tool call to the right implementation."""
    if tool_name == "get_pipeline_state":
        return _exec_get_pipeline_state(db, org_id)
    elif tool_name == "get_lead_details":
        return _exec_get_lead_details(db, org_id, tool_input["lead_id"])
    elif tool_name == "search_apollo":
        return _exec_search_apollo(
            org_id=org_id,
            person_titles=tool_input.get("person_titles", []),
            person_locations=tool_input.get("person_locations"),
            min_company_size=tool_input.get("min_company_size"),
            max_company_size=tool_input.get("max_company_size"),
            company_industries=tool_input.get("company_industries"),
        )
    elif tool_name == "find_contacts_at_company":
        return _exec_find_contacts_at_company(
            db, org_id,
            company_domain=tool_input["company_domain"],
            company_name=tool_input["company_name"],
            exclude_names=tool_input.get("exclude_names", []),
        )
    elif tool_name == "enrich_lead":
        return _exec_enrich_lead(db, org_id, tool_input["lead_id"])
    elif tool_name == "add_to_watchlist":
        return _exec_add_to_watchlist(
            db, org_id,
            company_name=tool_input["company_name"],
            company_domain=tool_input.get("company_domain", ""),
            contact_name=tool_input.get("contact_name", ""),
            contact_title=tool_input.get("contact_title", ""),
            lead_id=tool_input.get("lead_id", ""),
            reason=tool_input.get("reason", ""),
        )
    elif tool_name == "flag_data_issue":
        return _exec_flag_data_issue(db, org_id, tool_input["lead_id"], tool_input["issue"])
    elif tool_name == "store_suggestions":
        return _exec_store_suggestions(db, org_id, tool_input.get("leads", []))
    else:
        return f"Unknown tool: {tool_name}"


# =========================================================================
# Pre-flight planner — Haiku subagent
# =========================================================================

def _run_pre_flight_planner(
    client: "anthropic.Anthropic",
    full_system_prompt: str,
    credits_remaining: int,
) -> str:
    """Run a fast Haiku pass to draft a 1-2 sentence directive for the
    main Sonnet brain. Cheap (cents per call) and shaves Sonnet turns
    off the executor by giving it a clear starting plan.

    The planner doesn't have tools — it reasons over the system prompt
    (which contains the client context + ICP + ideal triggers) and
    returns a focused plan in plain text. The main brain then receives
    this as part of its initial user message.

    Returns an empty string if the planner errors — the main brain
    just runs without a pre-flight plan in that case.
    """
    if credits_remaining <= 0:
        return ""  # No point planning if we can't enrich anyway

    planner_user = (
        "You're a fast pre-flight planner for the Vera brain. The detailed "
        "client context is in your system prompt. Based on that context and "
        "the buyer profile, write a 1-2 sentence directive for today's brain "
        "run — what should the brain prioritise?\n\n"
        "Examples of useful directives:\n"
        "  • \"Re-enrich the top 3 hot leads with fresh signals before "
        "discovering new ones; only search Apollo if the suggestions pool "
        "is below 15.\"\n"
        "  • \"Focus on watchlist — investigate any new alerts that fired "
        "in the last 24 hours and decide whether to enrich the affected "
        "lead. Only top up the prospect pool if there's no watchlist work.\"\n"
        "  • \"Prospect pool is healthy. Spend the credits on enrichment "
        "depth — pick the 3 most promising warm leads and re-enrich.\"\n\n"
        f"You have {credits_remaining} enrichment credits remaining this "
        f"month. Reply with the directive only — no preamble, no quotes, "
        f"plain text."
    )

    try:
        response = client.messages.create(
            model=PLANNER_MODEL,
            max_tokens=256,
            temperature=0.3,
            system=[{
                "type": "text",
                "text": full_system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": planner_user}],
        )
        # The planner's system prompt cache write is shared with the
        # main loop's reads — same cache_control, same content prefix —
        # so the main brain's first turn benefits from this priming.
        text = ""
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text += block.text
        return text.strip()
    except Exception as e:
        logger.warning("Pre-flight planner failed (continuing without): %s", e)
        return ""


def _first_pass_enrich_pool(
    db, org_id: str, limit: int, credits_remaining: int
) -> Dict[str, Any]:
    """Deterministically enrich+score never-enriched 'suggested' leads.

    THE missing wire: discovery only classifies; the Brain's LLM loop only
    enriches hot/alerted leads, so a fresh suggestion pool was never turned
    into scored, briefed leads — the dashboard stayed empty no matter how
    many Brain runs fired. This is enforced in CODE, not via a prompt the
    LLM can rationalise away (same lesson as the tier cap).

    Bounded by BOTH the per-run limit (the tier suggestion target) and the
    org's remaining enrichment credits. Per lead it runs the full intended
    chain — enrich → score — because scoring is what generates the GTM
    briefing, revenue potential, findings and the no-fear ordering.
    Never raises; a per-lead failure is logged and skipped.
    """
    cap = max(0, min(limit, credits_remaining))
    if cap <= 0:
        return {"attempted": 0, "enriched": 0, "scored": 0, "reason": "no_capacity"}

    try:
        rows = (
            db.table("leads")
            .select("id, first_name, last_name, company_name")
            .eq("org_id", org_id)
            .eq("enrichment_status", "suggested")
            .is_("enriched_at", "null")
            .order("created_at")
            .limit(cap)
            .execute()
        ).data or []
    except Exception as e:
        logger.warning("first-pass: failed to list suggestions for %s: %s", org_id, e)
        return {"attempted": 0, "enriched": 0, "scored": 0, "reason": "list_failed"}

    if not rows:
        return {"attempted": 0, "enriched": 0, "scored": 0, "reason": "none_pending"}

    from app.services.enricher import run_full_enrichment
    from app.services.usage import check_enrichment_allowed, record_enrichment
    from app.services.scorer import score_leads

    enriched = 0
    scored = 0
    for r in rows:
        lead_id = r["id"]
        allowed, usage_info = check_enrichment_allowed(db, org_id, 1)
        if not allowed:
            logger.info(
                "first-pass: credit limit hit for %s (%s/%s) — stopping",
                org_id, usage_info.get("used"), usage_info.get("limit"),
            )
            break
        try:
            result = run_full_enrichment(
                db=db, org_id=org_id, lead_ids=[lead_id],
                enrichment_sources=["website", "tech_stack", "news", "jobs"],
            )
            record_enrichment(db, org_id, 1)
            if result and not result[0].get("error"):
                enriched += 1
                try:
                    score_leads(db, org_id, [lead_id])
                    scored += 1
                except Exception as e:
                    logger.warning("first-pass: score failed for %s: %s", lead_id, e)
            else:
                err = (result[0].get("error") if result else "no result")
                logger.warning("first-pass: enrich failed for %s: %s", lead_id, err)
        except Exception as e:
            logger.warning("first-pass: enrich/score crashed for %s: %s", lead_id, e)

    summary = {
        "attempted": len(rows), "enriched": enriched, "scored": scored,
    }
    log_activity(db, org_id, None, "vera_brain_first_pass_enrich", summary)
    logger.info("first-pass enrich for %s: %s", org_id, summary)
    return summary


# =========================================================================
# The agent loop
# =========================================================================

def run_proactive_brain(db, org_id: str) -> Dict[str, Any]:
    """Run the autonomous agent loop. Claude reasons and calls tools until done.

    This is the proactive brain — called on a schedule (daily) or manually.
    Claude reviews the pipeline, decides what to do, and executes actions.
    """
    # Heartbeat: log entry the moment the brain starts. Without this we
    # have no idea whether the daily 8am cron is reaching the engine when
    # the run dies before its first tool call (e.g. on a Claude API
    # error). Pair this with vera_brain_failed in the wrapper so every
    # invocation lands at least one row in activity_log.
    log_activity(db, org_id, None, "vera_brain_started", {
        "started_at": datetime.now(timezone.utc).isoformat(),
    })

    # Get client context
    org_result = (
        db.table("organisations")
        .select("name, client_context, icp_config, monthly_enrichment_limit, brain_mode")
        .eq("id", org_id)
        .execute()
    )
    if not org_result.data:
        return {"error": "Organisation not found"}

    org = org_result.data[0]
    client_ctx = org.get("client_context") or {}
    icp_config = org.get("icp_config") or {}
    client_name = org.get("name") or "Unknown"

    # Brain mode gate — per-org control over autonomy.
    #   off          → bail out immediately, log it, no credits consumed.
    #   surface_only → keep going; we'll strip enrich_lead from Claude's
    #                  tool list below so the model literally cannot call
    #                  it. Lead surfacing / watchlist / data flags still run.
    #   auto         → unchanged from default behaviour.
    brain_mode = (org.get("brain_mode") or "auto").lower()
    if brain_mode == "off":
        log_activity(db, org_id, None, "vera_brain_skipped", {
            "reason": "brain_mode_off",
            "skipped_at": datetime.now(timezone.utc).isoformat(),
        })
        logger.info("Brain skipped for org %s — brain_mode=off", org_id)
        return {"status": "skipped", "reason": "brain_mode=off"}

    # Credits
    from app.services.usage import get_or_create_usage
    usage = get_or_create_usage(db, org_id)
    credits_used = usage.get("enrichments_used") or 0
    credits_limit = org.get("monthly_enrichment_limit") or 100
    credits_remaining = max(0, credits_limit - credits_used)

    # Deterministic first-pass enrichment of the cold suggestion pool.
    # Discovery only classifies; the LLM loop below only enriches
    # hot/alerted leads — so without this a fresh pool never becomes
    # scored/briefed and the dashboard stays empty. Enforced in code
    # (not a prompt) and only in auto mode (surface_only/off must never
    # auto-spend). Bounded by the tier suggestion target + remaining
    # credits. Runs BEFORE the LLM loop so get_pipeline_state then shows
    # real, actionable leads.
    if brain_mode == "auto" and credits_remaining > 0:
        from app.services.tiers import get_suggestion_target
        _fp = _first_pass_enrich_pool(
            db, org_id,
            limit=get_suggestion_target(db, org_id),
            credits_remaining=credits_remaining,
        )
        # Re-read credits so the LLM loop sees what first-pass consumed.
        usage = get_or_create_usage(db, org_id)
        credits_used = usage.get("enrichments_used") or 0
        credits_remaining = max(0, credits_limit - credits_used)

    # ICP fields for the system prompt
    target_titles = icp_config.get("target_titles") or []
    target_locations = icp_config.get("target_locations") or []
    target_industries = icp_config.get("target_industries") or []
    target_company_size = icp_config.get("target_company_size") or []
    target_description = icp_config.get("target_description") or "N/A"
    ideal_triggers = client_ctx.get("ideal_triggers") or icp_config.get("signals_to_detect") or []
    critical_rules = client_ctx.get("critical_rules") or ""

    # Short industry label for the "sells TO X" rule
    target_industries_short = ", ".join(target_industries[:3]) if target_industries else "target"

    # Format company size ranges
    size_display = "N/A"
    if target_company_size:
        ranges = []
        for s in target_company_size:
            if isinstance(s, str) and "," in s:
                lo, hi = s.split(",", 1)
                ranges.append(f"{lo.strip()}-{hi.strip()}")
            else:
                ranges.append(str(s))
        size_display = ", ".join(ranges) + " employees"

    critical_rules_section = ""
    if critical_rules:
        critical_rules_section = f"4. ADDITIONAL CLIENT-SPECIFIC RULES: {critical_rules}"

    # Multi-persona awareness — when an org has >1 active buyer profile,
    # tell the agent about each one explicitly so it can rotate which
    # persona it searches for and reason about cohort balance. Single-
    # persona orgs get no extra prompt content (the standard ICP block
    # already covers them).
    personas_section = ""
    try:
        from app.services.personas import active_personas as _active_personas
        all_personas = _active_personas(db, org_id)
        if len(all_personas) > 1:
            lines = ["BUYER PROFILES YOU MANAGE — rotate searches across all of these, don't keep hitting the same one:"]
            for i, p in enumerate(all_personas, 1):
                titles_p = ", ".join((p.get("target_titles") or [])[:6]) or "—"
                industries_p = ", ".join((p.get("target_industries") or [])[:4]) or "—"
                lines.append(f"  {i}. {p.get('name', 'Unnamed')} — titles: {titles_p}; industries: {industries_p}")
            personas_section = "\n".join(lines)
    except Exception as e:
        logger.debug("Brain persona-section build failed (org %s): %s", org_id, e)

    # ICP judgment guidance — rich examples from client_context
    icp_guidance = client_ctx.get("icp_guidance") or f"Target decision-makers at {target_industries_short} companies."
    icp_examples_yes = client_ctx.get("icp_examples_yes") or []
    icp_examples_no = client_ctx.get("icp_examples_no") or []
    if isinstance(icp_examples_yes, list):
        icp_examples_yes = "\n".join(f"  ✅ {ex}" for ex in icp_examples_yes) if icp_examples_yes else "No examples configured"
    if isinstance(icp_examples_no, list):
        icp_examples_no = "\n".join(f"  ❌ {ex}" for ex in icp_examples_no) if icp_examples_no else "No examples configured"

    # Build pain points and differentiators strings
    pain_points = client_ctx.get("pain_points_they_solve") or client_ctx.get("pain_points") or []
    if isinstance(pain_points, list):
        pain_points = "; ".join(pain_points) if pain_points else "N/A"
    key_differentiators = client_ctx.get("key_differentiators") or []
    if isinstance(key_differentiators, list):
        key_differentiators = "; ".join(key_differentiators) if key_differentiators else "N/A"

    system = AGENT_SYSTEM.format(
        client_name=client_name,
        what_they_sell=client_ctx.get("what_they_sell") or "N/A",
        key_differentiators=key_differentiators,
        pain_points=pain_points,
        target_titles=", ".join(target_titles) if target_titles else "N/A",
        target_industries=", ".join(target_industries) if target_industries else "N/A",
        target_company_size=size_display,
        target_locations=", ".join(target_locations) if target_locations else "N/A",
        target_description=target_description,
        ideal_triggers=", ".join(ideal_triggers) if isinstance(ideal_triggers, list) else str(ideal_triggers),
        target_industries_short=target_industries_short,
        icp_guidance=icp_guidance,
        icp_examples_yes=icp_examples_yes,
        icp_examples_no=icp_examples_no,
        personas_section=personas_section,
        critical_rules_section=critical_rules_section,
        credits_remaining=credits_remaining,
        current_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )

    # Start the agent loop
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    # Pre-flight planner — runs Haiku once before the main Sonnet loop
    # to scope what this brain run should focus on. Output is a 1-2
    # sentence directive ("Today's priorities: re-enrich Acme Ltd,
    # search Apollo for cyber MSPs in UK, watchlist top 3 hot accounts").
    # Costs cents on Haiku; can shave several Sonnet turns off the
    # downstream loop because the executor starts with a clear plan.
    planner_directive = _run_pre_flight_planner(
        client, system, credits_remaining,
    )

    initial_user_message = (
        "Review the pipeline and take any actions needed. Start by checking "
        "the pipeline state."
    )
    if planner_directive:
        initial_user_message += (
            f"\n\nTODAY'S PRE-FLIGHT PLAN (from a fast-pass planner — use this "
            f"to focus, but you can deviate if the pipeline state changes your "
            f"mind):\n{planner_directive}"
        )

    messages = [{"role": "user", "content": initial_user_message}]

    actions_taken = []
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_write_tokens = 0
    total_cache_read_tokens = 0

    # System prompt cached once per run — every turn after the first
    # reads from cache at ~10% the input price. Tools array also cached
    # (cache_control on the last tool covers all preceding tools).
    cached_system: List[Dict[str, Any]] = [{
        "type": "text",
        "text": system,
        "cache_control": {"type": "ephemeral"},
    }]
    cached_tools: List[Dict[str, Any]] = list(TOOLS)

    # Honour brain_mode=surface_only: strip enrich_lead from Claude's
    # available tools entirely. Claude can still search Apollo, store
    # suggestions, watchlist companies, and flag data issues — it just
    # can't spend the customer's enrichment credits without their
    # explicit click in the dashboard.
    if brain_mode == "surface_only":
        cached_tools = [t for t in cached_tools if t.get("name") != "enrich_lead"]
        logger.info("Brain mode surface_only for org %s — enrich_lead tool stripped", org_id)

    if cached_tools:
        # Mutate a copy of the last tool to add cache_control; this caches
        # the tool array (Anthropic caches everything before the marked
        # block, so the marker on the last item is enough).
        cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}

    # Wrap the loop so any mid-flight exception still emits the
    # vera_brain_proactive completion log. Without this, an Anthropic
    # 5xx / a network blip mid-loop leaves the dashboard thinking the
    # brain "never ran" even though tool calls were already logged.
    loop_error: Optional[str] = None
    assistant_content: Any = []  # default if we exit before the first response
    try:
        for turn in range(MAX_AGENT_TURNS):
            response = client.messages.create(
                model=AGENT_MODEL,
                max_tokens=4096,
                temperature=0.0,
                system=cached_system,
                tools=cached_tools,
                messages=messages,
            )

            usage = response.usage
            total_input_tokens += getattr(usage, "input_tokens", 0) or 0
            total_output_tokens += getattr(usage, "output_tokens", 0) or 0
            total_cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
            total_cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0

            assistant_content = response.content
            messages.append({"role": "assistant", "content": assistant_content})

            if response.stop_reason == "end_turn":
                final_text = ""
                for block in assistant_content:
                    if block.type == "text":
                        final_text += block.text
                logger.info("Vera brain completed in %d turns. Final: %s", turn + 1, final_text[:200])
                break

            tool_results = []
            for block in assistant_content:
                if block.type == "tool_use":
                    tool_name = block.name
                    tool_input = block.input
                    tool_id = block.id

                    logger.info("Vera brain calling tool: %s(%s)", tool_name, json.dumps(tool_input)[:200])

                    result_text = _execute_tool(db, org_id, tool_name, tool_input)

                    actions_taken.append({
                        "tool": tool_name,
                        "input": tool_input,
                        "result": result_text[:500],
                        "turn": turn + 1,
                    })

                    log_activity(db, org_id, None, "vera_brain_tool_call", {
                        "tool": tool_name,
                        "input": tool_input,
                        "result": result_text[:1000],
                        "turn": turn + 1,
                    })

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result_text,
                    })

            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            else:
                break
    except Exception as e:
        loop_error = f"{type(e).__name__}: {e}"
        logger.exception("Vera brain loop raised mid-flight: %s", loop_error)

    # Cost estimate — uses the shared pricing table in services/claude.py
    # so cached-token discounts (~10% of fresh input price) are
    # accurately reflected. Without this the brain would over-report
    # cost when caching kicks in.
    from app.services.claude import estimate_call_cost
    estimated_cost = estimate_call_cost(
        AGENT_MODEL,
        total_input_tokens,
        total_output_tokens,
        total_cache_write_tokens,
        total_cache_read_tokens,
    )

    # Extract final assessment from the last assistant message
    assessment = ""
    for block in assistant_content:
        if block.type == "text":
            assessment += block.text

    # Build a brief action summary for the dashboard
    action_summary = []
    for a in actions_taken:
        tool = a.get("tool", "")
        inp = a.get("input", {})
        if tool == "enrich_lead":
            action_summary.append(f"Enriched lead {inp.get('lead_id', '')[:8]}")
        elif tool == "add_to_watchlist":
            action_summary.append(f"Added {inp.get('company_name', '')} to watchlist")
        elif tool == "flag_data_issue":
            action_summary.append(f"Flagged: {inp.get('issue', '')[:80]}")
        elif tool == "find_contacts_at_company":
            action_summary.append(f"Searched for more contacts at {inp.get('company_name', '')}")
        elif tool == "search_apollo":
            action_summary.append("Searched Apollo for new prospects")
        elif tool == "store_suggestions":
            leads = inp.get("leads", [])
            action_summary.append(f"Stored {len(leads)} new suggestions from Apollo")
        elif tool == "get_pipeline_state":
            action_summary.append("Reviewed pipeline state")
        elif tool == "get_lead_details":
            action_summary.append(f"Inspected lead {inp.get('lead_id', '')[:8]}")

    # Log the run with full token telemetry — input + output + cache
    # stats so we can see how much caching is actually saving us. Cache
    # hit rate = cache_read / (cache_read + input + cache_write); a
    # healthy multi-turn brain run should be 60-80%+ on hits.
    cache_total = total_cache_write_tokens + total_cache_read_tokens
    cache_hit_rate = (
        round(total_cache_read_tokens / max(1, cache_total + total_input_tokens), 3)
        if cache_total or total_input_tokens else 0.0
    )

    # `turn` may be undefined if the loop never ran. Default to 0.
    turns_completed = locals().get("turn", -1) + 1

    log_activity(db, org_id, None, "vera_brain_proactive", {
        "turns": turns_completed,
        "actions_taken": len(actions_taken),
        "estimated_cost": round(estimated_cost, 4),
        "model": AGENT_MODEL,
        "planner_model": PLANNER_MODEL,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "cache_write_tokens": total_cache_write_tokens,
        "cache_read_tokens": total_cache_read_tokens,
        "cache_hit_rate": cache_hit_rate,
        "assessment": assessment[:2000],
        "action_summary": action_summary,
        "had_pre_flight_plan": bool(planner_directive),
        "loop_error": loop_error,  # None on clean completion; error string if mid-flight failure
        "completed_cleanly": loop_error is None,
    })

    logger.info(
        "Vera brain for org %s: %d turns, %d actions, cost=$%.4f, cache hit rate=%.0f%%",
        org_id, turn + 1, len(actions_taken), estimated_cost, cache_hit_rate * 100,
    )

    return {
        "assessment": assessment,
        "actions_taken": actions_taken,
        "turns": turn + 1,
        "estimated_cost": round(estimated_cost, 4),
        "credits_remaining": credits_remaining,
    }


# =========================================================================
# Reactive mode — fast deterministic checks (no Claude, runs inline)
# =========================================================================

def post_enrichment_review(db, org_id: str, lead_id: str, lead: dict) -> List[Dict[str, Any]]:
    """Run immediately after enrichment. Fast deterministic checks only — no Claude."""
    actions_taken = []
    enrichment_data = lead.get("enrichment_data") or {}
    lead_rating = lead.get("lead_rating") or ""

    # Auto-watchlist hot leads
    if lead_rating == "hot":
        company_name = lead.get("company_name") or ""
        if company_name:
            result = _exec_add_to_watchlist(
                db, org_id,
                company_name=company_name,
                company_domain=lead.get("company_domain", ""),
                contact_name=f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip(),
                contact_title=lead.get("title", ""),
                lead_id=lead_id,
                reason="Lead rated hot — auto-added by Vera",
            )
            if "already on" not in result:
                actions_taken.append({"action": "auto_watchlist", "target": company_name, "result": result})

    # Data quality flags
    issues = _detect_data_issues(lead)
    if issues:
        for issue in issues:
            _exec_flag_data_issue(db, org_id, lead_id, issue)
            actions_taken.append({"action": "data_flag", "target": lead_id, "issue": issue})

    if actions_taken:
        log_activity(db, org_id, lead_id, "vera_brain_reactive", {"actions": actions_taken})
        logger.info("Vera brain (reactive) took %d actions for lead %s", len(actions_taken), lead_id)

    return actions_taken


def _detect_data_issues(lead: dict) -> List[str]:
    """Detect data quality issues."""
    issues = []
    enrichment_data = lead.get("enrichment_data") or {}

    # Email domain mismatch
    email = lead.get("email") or ""
    domain = lead.get("company_domain") or ""
    if email and domain and "@" in email:
        email_domain = email.split("@")[1].lower()
        common_providers = {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com"}
        if domain.lower() not in email_domain and email_domain not in domain.lower() and email_domain not in common_providers:
            issues.append(f"Email domain '{email_domain}' doesn't match company domain '{domain}'")

    # Sparse enrichment
    sources_completed = lead.get("enrichment_sources_completed") or []
    if len(sources_completed) <= 2:
        issues.append(f"Only {len(sources_completed)} enrichment sources completed — data may be sparse")

    # No signals in briefing
    briefing = enrichment_data.get("gtm_briefing") or {}
    if not briefing.get("signals_detected") and not briefing.get("what_we_detected") and not briefing.get("recommended_play"):
        issues.append("No buying signals detected — briefing has no recommended play")

    return issues
