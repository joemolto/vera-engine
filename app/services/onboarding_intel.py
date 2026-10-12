"""Onboarding intelligence — the premium bits of the Solo wizard.

Pure helpers, no DB writes (callers persist):

  - scan_website(url): Tavily pulls the company's own site, Claude reads
    it and returns what they sell + a short summary + a sub-sector guess
    + a draft value proposition, so the wizard can PREFILL rather than
    make the user type it. The value proposition is shown to the user as
    an EDITABLE step ("how Vera sees you") — their confirmed version is
    the anchor of the whole lens.
  - generate_company_summary(website_scan, answers): Claude writes the
    end-of-wizard summary — what the company does + the kinds of business
    they want to win.
  - map_answers_to_icp(answers): turns the wizard's multiple-choice
    answers into the ICP fields the suggestions engine reads, expanding
    each option to Apollo-valid variants (titles, keyword tags, locations).
  - build_wizard_personas(answers): the personas[0] row account matching
    prefers, in the same shape hand-configured personas use.
  - signal_priorities_from_answers(answers): per-org signal weights preset
    by the ambulance axis (defend/expose) + the signals the user picked.
  - build_client_context(org_name, answers, website_scan): the one-time
    AI-built seller understanding (value prop, differentiators, pains,
    ICP guidance...) written to organisations.client_context — the same
    document the operator used to hand-write on setup calls. Everything
    that reads client_context (enrichment prompts, suggestions classifier,
    revenue potential) then sees accounts through this customer's lens.

Model choice: Sonnet (settings.CLAUDE_MODEL) — these are website-synthesis
+ summary tasks where quality matters, per the "Sonnet on website/news"
rule. The cheap classifier stays on the suggestions path.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings
from app.services.claude import call_claude

logger = logging.getLogger(__name__)

# Website value-prop scans are stable, so cache them by domain and reuse across
# signups (a second seller from the same company gets an instant prefill).
_SCAN_CACHE_TTL_DAYS = 90


def _get_cached_scan(domain: str) -> Optional[Dict[str, Any]]:
    """Return a recent cached scan for this domain, or None. Best-effort — any
    error just falls through to a live scan."""
    if not domain:
        return None
    try:
        from app.database import get_supabase
        cutoff = (datetime.now(timezone.utc) - timedelta(days=_SCAN_CACHE_TTL_DAYS)).isoformat()
        res = (
            get_supabase().table("website_scan_cache")
            .select("scan")
            .eq("domain", domain)
            .gte("scanned_at", cutoff)
            .maybe_single()
            .execute()
        )
        row = res.data if res else None
        return row.get("scan") if row else None
    except Exception as e:  # noqa: BLE001
        logger.info("scan cache read skipped for %s: %s", domain, e)
        return None


def _cache_scan(domain: str, scan: Dict[str, Any]) -> None:
    """Store a successful scan for reuse by future signups. Best-effort."""
    if not domain:
        return
    try:
        from app.database import get_supabase
        get_supabase().table("website_scan_cache").upsert(
            {
                "domain": domain,
                "scan": scan,
                "scanned_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="domain",
        ).execute()
    except Exception as e:  # noqa: BLE001
        logger.info("scan cache write skipped for %s: %s", domain, e)

_TAVILY_URL = "https://api.tavily.com/search"

# Canonical cyber sub-sectors — mirrors the wizard's SUB_SECTORS chips so
# Claude's guess maps straight onto a selectable option.
SUB_SECTORS = [
    "Pentesting", "Threat intelligence", "MDR / SOC", "XDR / EDR",
    "vCISO / consulting", "GRC / compliance", "DFIR", "Vulnerability management",
    "Identity / IAM", "Cloud security", "Application security", "OT / ICS security",
    "Email / phishing security", "Data security / DLP", "Network / SASE",
    "Attack surface management", "SIEM / SOAR", "Security awareness training",
    "MSSP (managed)", "Other",
]


def _domain_of(url: str) -> str:
    host = (url or "").strip().replace("https://", "").replace("http://", "")
    host = host.split("/")[0].replace("www.", "").lower()
    return host


def _tavily_site_content(domain: str) -> str:
    """Pull the company's own site content via Tavily, scoped to their
    domain. Returns a concatenated text blob (answer + page snippets) or
    "" if Tavily is unconfigured / fails."""
    if not settings.TAVILY_API_KEY or not domain:
        return ""
    payload = {
        "query": (
            f"What does the company at {domain} sell? What products and "
            f"services do they offer, and who are their customers?"
        ),
        "include_domains": [domain],
        "search_depth": "advanced",
        "max_results": 5,
        "include_answer": True,
        "include_raw_content": True,
    }
    headers = {
        "Authorization": f"Bearer {settings.TAVILY_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=25) as client:
            resp = client.post(_TAVILY_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("Tavily site scan failed for %s: %s", domain, e)
        return ""

    parts: List[str] = []
    answer = data.get("answer")
    if isinstance(answer, str) and answer.strip():
        parts.append(answer.strip())
    for r in (data.get("results") or [])[:5]:
        if not isinstance(r, dict):
            continue
        snippet = r.get("raw_content") or r.get("content") or ""
        if isinstance(snippet, str) and snippet.strip():
            parts.append(snippet.strip()[:2000])  # cap each page's contribution
    return "\n\n".join(parts)[:12000]  # overall cap to keep token cost sane


def scan_website(url: str) -> Dict[str, Any]:
    """Scan a company's website and return a prefill-ready read.

    Returns {what_they_sell, sub_sectors, summary, value_proposition}.
    Empty strings/lists on any failure (caller treats that as 'nothing to
    prefill')."""
    empty = {"what_they_sell": "", "sub_sectors": [], "summary": "", "value_proposition": ""}
    domain = _domain_of(url)

    # Cache-first: a value prop derived from a company's public site is stable,
    # so a domain another signup already scanned returns instantly and free.
    cached = _get_cached_scan(domain)
    if cached is not None:
        return cached

    content = _tavily_site_content(domain)
    if not content:
        return empty

    system = (
        "You analyse a cybersecurity company's own website to understand "
        "what they sell. Reply with ONLY a JSON object, no prose."
    )
    prompt = (
        "From the website content below, return a JSON object with exactly "
        "these keys:\n"
        '  "what_they_sell": one or two plain-English sentences on what this '
        "company sells (write it as if for their own sales team),\n"
        '  "sub_sectors": an array of 1-3 values chosen ONLY from this list: '
        f"{SUB_SECTORS},\n"
        '  "summary": a 2-3 sentence overview of the company,\n'
        '  "value_proposition": 2-3 sentences in the company\'s own voice '
        '("We help...") capturing the value they create: who they serve, the '
        "pain they remove, and what makes them different. Ground every claim "
        "in the website content — never invent capabilities.\n\n"
        "If the content is too thin to tell, use empty values.\n\n"
        f"WEBSITE CONTENT:\n{content}"
    )
    result = call_claude(prompt, system=system, model=settings.CLAUDE_MODEL)
    if not isinstance(result, dict) or result.get("error"):
        logger.warning("scan_website Claude call failed for %s: %s", domain, result)
        return empty

    subs = result.get("sub_sectors") or []
    if not isinstance(subs, list):
        subs = []
    # Keep only recognised sub-sectors so the frontend can map to chips.
    subs = [s for s in subs if s in SUB_SECTORS][:3]
    scan = {
        "what_they_sell": str(result.get("what_they_sell") or "").strip(),
        "sub_sectors": subs,
        "summary": str(result.get("summary") or "").strip(),
        "value_proposition": str(result.get("value_proposition") or "").strip(),
    }
    # Only cache a scan that actually found something — never poison the cache
    # with an empty result from a transient Tavily/Claude miss.
    if any(scan.values()):
        _cache_scan(domain, scan)
    return scan


def generate_company_summary(
    *, website_scan: Dict[str, Any], answers: Dict[str, Any]
) -> Dict[str, Any]:
    """The end-of-wizard AI summary: what the company does + the kinds of
    business they want to win. Returns {company_summary, target_business_types}
    or {} on failure."""
    scan_blurb = (website_scan or {}).get("summary") or (website_scan or {}).get("what_they_sell") or ""
    facts = {
        "what_they_sell": answers.get("sells_what") or scan_blurb,
        "value_proposition": answers.get("value_prop") or "",
        "sub_sectors": answers.get("sub_sectors") or [],
        "angle": answers.get("ambulance"),  # defend | expose
        "target_titles": answers.get("titles") or [],
        "target_industries": answers.get("industries") or [],
        "target_geographies": answers.get("geos") or [],
        "target_company_size": answers.get("target_company_size") or [],
        "deal_band": answers.get("deal_band"),
    }
    system = (
        "You write a short, confident onboarding summary for a cybersecurity "
        "company using Vera. Reply with ONLY a JSON object, no prose. Never "
        "invent facts not supported by the inputs."
    )
    prompt = (
        "Using the structured inputs below, return a JSON object with exactly "
        "these keys:\n"
        '  "company_summary": 2-3 sentences describing what this company does '
        "and sells, in plain English,\n"
        '  "target_business_types": an array of 3-6 short phrases describing '
        "the kinds of businesses they want to win (combine industry, size and "
        'geography, e.g. "UK financial-services firms, 1,000-5,000 employees").\n\n'
        f"INPUTS:\n{facts}"
    )
    result = call_claude(prompt, system=system, model=settings.CLAUDE_MODEL)
    if not isinstance(result, dict) or result.get("error"):
        logger.warning("generate_company_summary Claude call failed: %s", result)
        return {}
    types = result.get("target_business_types") or []
    if not isinstance(types, list):
        types = []
    return {
        "company_summary": str(result.get("company_summary") or "").strip(),
        "target_business_types": [str(t).strip() for t in types if str(t).strip()][:6],
    }


def deal_context_from_band(deal_band: str | None) -> Dict[str, Any]:
    """Map the wizard's deal-size band to numeric client_context fields so
    revenue_potential can estimate (it returns 'unknown' without an
    average_deal_size). 'It varies' / unknown → {} (stay unknown, no guess).
    Dash-insensitive so en-dash vs hyphen doesn't matter.
    """
    b = (deal_band or "").strip().lower().replace("–", "-").replace(" ", "")
    table = {
        "under$10k":  {"average_deal_size": 5000,   "min_deal_size": 1000,   "max_deal_size": 10000},
        "$10k-50k":   {"average_deal_size": 30000,  "min_deal_size": 10000,  "max_deal_size": 50000},
        "$50k-250k":  {"average_deal_size": 150000, "min_deal_size": 50000,  "max_deal_size": 250000},
        "$250k+":     {"average_deal_size": 350000, "min_deal_size": 250000, "max_deal_size": 1000000},
    }
    return dict(table.get(b, {}))


def build_solo_seller_profile(
    answers: Dict[str, Any], website_scan: Dict[str, Any] | None = None
) -> str:
    """Compose a seller profile for the suggestions classifier — the SAME
    role a hand-configured icp_config.client_context.product_pitch plays on the live
    dashboard tray.

    This is what stops the classifier surfacing financial-services
    *recruiters* for a cyber vendor: it hard-anchors "cybersecurity vendor"
    + sub-sector so the model knows the candidate must be a BUYER of
    security, not just a company tagged with the same industry keywords.

    Deterministic (no LLM) so it can't hallucinate — it leans on the
    customer's explicit selections, seeding "what they sell" from what they
    typed, then the website scan as fallback.
    """
    website_scan = website_scan or {}

    def _list(key: str) -> List[str]:
        v = answers.get(key)
        return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []

    sells = (answers.get("sells_what") or "").strip() or str(website_scan.get("what_they_sell") or "").strip()
    value_prop = (answers.get("value_prop") or "").strip()
    subs = [s for s in _list("sub_sectors") if s.lower() != "other"]
    titles = _list("titles")
    industries = _list("industries")
    geos = _list("geos")
    competitors = _competitor_list(answers)

    parts: List[str] = []
    if sells:
        parts.append(sells if sells.endswith(".") else sells + ".")
    # The user-confirmed value proposition is the strongest lens signal we
    # have — they edited it until it sounded like them.
    if value_prop and value_prop.lower() != sells.lower():
        parts.append(value_prop if value_prop.endswith(".") else value_prop + ".")
    sub_str = ", ".join(subs) if subs else "cybersecurity"
    parts.append(f"A {sub_str} cybersecurity vendor.")
    if industries:
        who = ", ".join(titles) if titles else "security and IT leaders"
        where = f" in {', '.join(geos)}" if geos else ""
        parts.append(f"Sells to {who} at {', '.join(industries)} organisations{where}.")
    if competitors:
        parts.append(f"Competes with {', '.join(competitors)}.")
    return " ".join(parts).strip() or "a cybersecurity vendor"


def _competitor_list(answers: Dict[str, Any]) -> List[str]:
    """Normalise the wizard's competitors answer — accepts a list or a
    comma/newline-separated string."""
    raw = (answers or {}).get("competitors")
    if isinstance(raw, list):
        items = [str(x).strip() for x in raw]
    elif isinstance(raw, str):
        items = [p.strip() for chunk in raw.split("\n") for p in chunk.split(",")]
    else:
        items = []
    return [i for i in items if i][:10]


# Angle → the critical rule the operator used to write by hand. Two shapes:
# defend (anti-ambulance) and expose (the exposure IS the product).
_CRITICAL_RULES = {
    "defend": (
        "Never lead with the prospect's own breach, CVE, credential leak or "
        "compliance gap — this client sells protection, and opening on the "
        "prospect's own exposure reads as ambulance-chasing. Lead with growth, "
        "hiring, leadership and change signals instead."
    ),
    "expose": (
        "This client SELLS detection/exposure intelligence, so leading with "
        "what was found about a prospect is their pitch, not ambulance-chasing. "
        "Exposure, vulnerability and compliance signals are fair openers."
    ),
}


def build_client_context(
    org_name: str,
    answers: Dict[str, Any],
    website_scan: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """The one-time AI-built seller understanding for a self-serve org —
    the same document the operator used to hand-write into
    organisations.client_context on setup calls.

    Deterministic skeleton from the wizard's confirmed answers, enriched
    by ONE Claude pass over the website scan for the judgment fields
    (differentiators, pains, ICP guidance, product names). The AI pass is
    best-effort: on any failure the deterministic fields still make every
    client_context reader work, just with less colour.
    """
    answers = answers or {}
    website_scan = website_scan or {}

    def _list(key: str) -> List[str]:
        v = answers.get(key)
        return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []

    value_prop = (answers.get("value_prop") or "").strip()
    sells = (answers.get("sells_what") or "").strip() or str(website_scan.get("what_they_sell") or "").strip()
    angle = answers.get("ambulance") if answers.get("ambulance") in _CRITICAL_RULES else "defend"

    ctx: Dict[str, Any] = {
        "company_name": org_name,
        "what_they_sell": sells or value_prop,
        "value_proposition": value_prop,
        "competitors": _competitor_list(answers),
        "sub_sectors": [s for s in _list("sub_sectors") if s.lower() != "other"],
        "critical_rules": _CRITICAL_RULES[angle],
        "context_source": "wizard_auto",
    }
    ctx.update(deal_context_from_band(answers.get("deal_band")))

    # One Claude pass for the judgment fields. Facts-only: everything must
    # be grounded in the scan/answers, matching the no-invented-facts rule.
    scan_text = "\n".join(
        str(website_scan.get(k) or "")
        for k in ("summary", "what_they_sell", "value_proposition")
    ).strip()
    inputs = {
        "company_name": org_name,
        "what_they_sell": ctx["what_they_sell"],
        "value_proposition": value_prop,
        "sub_sectors": ctx["sub_sectors"],
        "angle": angle,
        "target_titles": _list("titles"),
        "target_industries": _list("industries"),
        "target_geographies": _list("geos"),
        "website_read": scan_text,
    }
    system = (
        "You build a seller-context profile for a cybersecurity vendor from "
        "their own answers and website. Reply with ONLY a JSON object, no "
        "prose. Never invent facts, product names or capabilities that are "
        "not supported by the inputs."
    )
    prompt = (
        "Using the structured inputs below, return a JSON object with exactly "
        "these keys:\n"
        '  "key_differentiators": array of 2-5 short phrases — what makes this '
        "vendor different, grounded in their value proposition and website,\n"
        '  "pain_points_they_solve": array of 2-5 short phrases — the buyer '
        "pains this vendor removes,\n"
        '  "icp_guidance": 2-4 sentences telling an account-qualification '
        "model who is and is NOT a prospect for this vendor (remember: other "
        "cybersecurity vendors are competitors, not prospects),\n"
        '  "product_names": array of named products/services from the inputs '
        "(empty if none are named).\n\n"
        f"INPUTS:\n{inputs}"
    )
    result = call_claude(prompt, system=system, model=settings.CLAUDE_MODEL)
    if isinstance(result, dict) and not result.get("error"):
        for key in ("key_differentiators", "pain_points_they_solve", "product_names"):
            v = result.get(key)
            if isinstance(v, list):
                cleaned = [str(x).strip() for x in v if str(x).strip()]
                if cleaned:
                    ctx[key] = cleaned[:6]
        guidance = str(result.get("icp_guidance") or "").strip()
        if guidance:
            ctx["icp_guidance"] = guidance
    else:
        logger.warning("build_client_context Claude call failed for %s: %s", org_name, result)

    return ctx


# ---------------------------------------------------------------------------
# Wizard-option → Apollo expansion tables.
#
# Apollo SILENTLY ignores filter values it doesn't recognise (proven
# 2026-06-07 with employee ranges, re-proven 2026-07-08: organization_locations
# "Middle East" returns 0 results with no error). Every value below was
# dry-run against mixed_companies/search on 2026-07-08 and returned hits,
# except the title variants, which follow the exact format of the
# hand-tuned personas (proven in production on the suggestions tray).
# Unknown labels (future wizard options) pass through unexpanded rather
# than vanishing.
# ---------------------------------------------------------------------------

TITLE_EXPANSIONS: Dict[str, List[str]] = {
    "CISO": ["CISO", "Chief Information Security Officer", "Chief Security Officer"],
    "Head of Security": [
        "Head of Security", "Head of Information Security", "Head of Cyber Security",
        "Head of Cyber", "Director of Security", "Director of Information Security",
        "Director of Cybersecurity", "VP Security", "VP of Security", "VP Information Security",
    ],
    "SOC Manager": [
        "SOC Manager", "Head of SOC", "Security Operations Manager",
        "Head of Security Operations", "SOC Lead",
    ],
    "CIO": ["CIO", "Chief Information Officer"],
    "CTO": ["CTO", "Chief Technology Officer"],
    "IT Director": ["IT Director", "Head of IT", "Director of IT", "VP of IT"],
    "DPO / Data Protection Officer": [
        "Data Protection Officer", "DPO", "Head of Data Protection",
        "Chief Privacy Officer", "Head of Privacy",
    ],
    "Head of Risk / Compliance": [
        "Head of Risk", "Head of Compliance", "Chief Risk Officer",
        "Chief Compliance Officer", "Director of Risk", "Director of Compliance",
        "Head of Risk and Compliance",
    ],
    "VP Engineering": [
        "VP Engineering", "VP of Engineering", "Head of Engineering", "Director of Engineering",
    ],
    "Founder / CEO": [
        "Founder", "Co-Founder", "CEO", "Chief Executive Officer", "Managing Director",
    ],
}

INDUSTRY_EXPANSIONS: Dict[str, List[str]] = {
    "Financial services": ["financial services", "banking", "fintech", "investment management"],
    "Healthcare": ["healthcare", "hospital", "pharmaceuticals"],
    "Retail / e-commerce": ["retail", "e-commerce"],
    "Energy / utilities": ["energy", "utilities", "oil & gas"],
    "Public sector / government": ["government"],
    "Software / SaaS": ["software", "saas"],
    "Manufacturing": ["manufacturing"],
    "Insurance": ["insurance"],
    "Legal": ["legal services", "law practice"],
    "Education": ["education", "higher education"],
}

GEO_EXPANSIONS: Dict[str, List[str]] = {
    "UK": ["United Kingdom"],
    "United States": ["United States"],
    "Europe (EU)": ["Europe"],
    "Canada": ["Canada"],
    "APAC": ["APAC"],
    # Apollo has no "Middle East" region — the label silently matches nothing,
    # so expand to the region's main markets by country name.
    "Middle East": [
        "United Arab Emirates", "Saudi Arabia", "Qatar", "Israel",
        "Bahrain", "Kuwait", "Oman", "Jordan",
    ],
    # "Global" is handled in map_answers_to_icp: no location filter at all.
}

_GLOBAL_GEO = "Global"


def _expand(values: List[str], table: Dict[str, List[str]]) -> List[str]:
    """Expand wizard options through a table, de-duplicated, order kept.
    Unknown options pass through as-is so a new wizard chip can never
    silently drop out of the ICP."""
    out: List[str] = []
    for v in values:
        for expanded in table.get(v, [v]):
            if expanded not in out:
                out.append(expanded)
    return out


def map_answers_to_icp(answers: Dict[str, Any]) -> Dict[str, Any]:
    """Map wizard answers to the ICP fields the suggestions engine reads,
    expanded to Apollo-valid variants. Only includes non-empty fields so
    we never blank out something already configured on the org."""
    out: Dict[str, Any] = {}

    def _list(key: str) -> List[str]:
        v = answers.get(key)
        return [str(x) for x in v if x] if isinstance(v, list) else []

    industries = _expand(_list("industries"), INDUSTRY_EXPANSIONS)
    sizes = _list("target_company_size")
    titles = _expand(_list("titles"), TITLE_EXPANSIONS)

    geos = _list("geos")
    # "Global" means no location filter — expanding the OTHER selections
    # would silently narrow a global ICP to a few named regions.
    locations = [] if _GLOBAL_GEO in geos else _expand(geos, GEO_EXPANSIONS)

    if industries:
        out["target_industries"] = industries
    if locations:
        out["target_locations"] = locations
    if sizes:
        out["target_company_size"] = sizes
    if titles:
        out["target_titles"] = titles
    return out


def build_wizard_personas(answers: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build the personas list account matching prefers (personas[0]),
    in the exact shape hand-configured personas use. Empty
    list when the wizard gave us nothing to target on."""
    icp = map_answers_to_icp(answers)
    if not icp:
        return []
    return [{
        "id": str(uuid.uuid4()),
        "name": "Default profile",
        "active": True,
        "target_titles": icp.get("target_titles") or [],
        "target_locations": icp.get("target_locations") or [],
        "target_tech_uses": [],
        "target_industries": icp.get("target_industries") or [],
        "target_seniorities": [],
        "target_company_size": icp.get("target_company_size") or [],
        "target_revenue_range": None,
        "target_tech_excludes": [],
        "target_funding_stages": [],
    }]


# ---------------------------------------------------------------------------
# Signal priorities preset — the quiet part of the wizard.
#
# Presence of icp_config.signal_priorities is what turns per-org relevance
# ranking on (the implicit signal_ranking flag), and weights >= 0.8 extend
# the webhook-firing whitelist. The two bases mirror the hand-tuned
# defend (never lead with the prospect's own exposure — no cve_in_stack /
# compliance_gap / credential_leak at all) and expose (the exposure IS
# the product) configs.
# ---------------------------------------------------------------------------

_PRIORITIES_COMMON: Dict[str, float] = {
    "breach_proximity": 1.0,
    "security_leadership_hire": 1.0,
    "social_leadership_change": 0.9,
    "hiring_security": 0.9,
    "security_team_expansion": 0.9,
    "hiring_grc": 0.5,
    "social_hiring_buzz": 0.7,
    "social_company_mention": 0.4,
    "enterprise_customer_detected": 0.5,
    "person_engaged_with_competitor": 0.9,
    "person_posted_relevant_topic": 0.8,
    "person_engaged_with_topic": 0.7,
    "person_new_in_role": 0.8,
}

_PRIORITIES_DEFEND: Dict[str, float] = {
    **_PRIORITIES_COMMON,
    "social_breach_mention": 0.9,
    "new_leadership": 0.8,
    "recently_funded": 0.5,
    "social_funding_news": 0.6,
    "recent_acquisition": 0.5,
    "social_vendor_relationship": 1.0,
    "social_complaint": 0.6,
    "social_employee_chatter": 0.6,
    "regulated_customer_detected": 0.6,
}

_PRIORITIES_EXPOSE: Dict[str, float] = {
    **_PRIORITIES_COMMON,
    "cve_in_stack": 1.0,
    "compliance_gap": 0.9,
    "credential_leak": 1.0,
    "credential_leak_supply_chain": 0.85,
    "public_proof_not_visible": 0.6,
    "social_breach_mention": 1.0,
    "new_leadership": 0.9,
    "recently_funded": 0.9,
    "social_funding_news": 0.9,
    "recent_acquisition": 0.7,
    "social_vendor_relationship": 0.6,
    "social_complaint": 0.5,
    "social_employee_chatter": 0.5,
    "regulated_customer_detected": 0.8,
}


# ---------------------------------------------------------------------------
# Signal proposal — the custom layer of the wizard (onboarding phase 2,
# early-user feedback 2026-08-14). Vera PROPOSES which signal
# types fit what this seller sells, each with a one-line why in their
# language; the user corrects. A type toggled off persists as weight 0 =
# never shown anywhere (signal_visibility Phase 1 rule).
# ---------------------------------------------------------------------------

# The proposable catalogue: what the step shows, with sentence-case labels.
PROPOSABLE_SIGNALS: List[Dict[str, str]] = [
    {"type": "hiring_security", "label": "Hiring security roles"},
    {"type": "security_team_expansion", "label": "Security team expanding"},
    {"type": "security_leadership_hire", "label": "New security leader"},
    {"type": "new_leadership", "label": "New exec / leadership change"},
    {"type": "recently_funded", "label": "Recently funded"},
    {"type": "recent_acquisition", "label": "Acquisition / merger"},
    {"type": "breach_proximity", "label": "Breach near their supply chain"},
    {"type": "social_breach_mention", "label": "Breach in the news"},
    {"type": "cve_in_stack", "label": "Vulnerabilities in their stack"},
    {"type": "compliance_gap", "label": "Compliance gaps"},
    {"type": "credential_leak", "label": "Leaked employee credentials"},
    {"type": "credential_leak_supply_chain", "label": "Supply-chain credential leaks"},
    {"type": "hiring_grc", "label": "Hiring risk & compliance roles"},
    {"type": "person_engaged_with_competitor", "label": "Contact engaged with a competitor"},
    {"type": "person_new_in_role", "label": "Contact new in role"},
]


def propose_signal_config(
    answers: Dict[str, Any], website_scan: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    """One Claude pass mapping what the seller sells onto the signal
    catalogue: per-signal on/off + a one-line reason in THEIR language,
    the seller archetype for the step header, plus qualifier suggestions
    (which security ROLES matter, which funding EVENT TYPES count).

    Deterministic fallback on any AI failure: everything on with the
    defend-angle base — exactly today's behaviour, never a blocked wizard.
    """
    answers = answers or {}
    website_scan = website_scan or {}
    sells = (answers.get("sells_what") or "").strip() or str(website_scan.get("what_they_sell") or "")
    value_prop = (answers.get("value_prop") or "").strip()
    subs = answers.get("sub_sectors") or []

    fallback_angle = answers.get("ambulance") if answers.get("ambulance") in _CRITICAL_RULES else "defend"
    fallback = {
        "archetype": ", ".join(str(s) for s in subs[:2]) + " vendor" if subs else "Cybersecurity vendor",
        "angle": fallback_angle,
        "signals": [
            {**s, "on": True, "reason": "Included by default — tune any time in Settings."}
            for s in PROPOSABLE_SIGNALS
        ],
        "hiring_roles": [],
        "funding_event_types": [],
        "source": "fallback",
    }
    if not sells and not value_prop:
        return fallback

    catalogue = [{"type": s["type"], "label": s["label"]} for s in PROPOSABLE_SIGNALS]
    system = (
        "You configure a sales-signals product for a cybersecurity vendor "
        "from what THEY sell. Reply with ONLY a JSON object, no prose. "
        "Ground every reason in their own offering; never invent "
        "capabilities. A signal should be OFF when surfacing it would "
        "clash with what they sell (e.g. leaked-credential intel is a "
        "dark-web product — wrong for a vulnerability-management vendor) "
        "or add noise they can't act on."
    )
    prompt = (
        "SELLER:\n"
        f"  sells: {sells}\n"
        f"  value proposition: {value_prop}\n"
        f"  sub-sectors: {subs}\n\n"
        f"SIGNAL CATALOGUE:\n{catalogue}\n\n"
        "Return a JSON object with exactly these keys:\n"
        '  "archetype": 2-5 word seller archetype (e.g. "Vulnerability '
        'management vendor"),\n'
        '  "angle": "defend" (never lead with a prospect\'s own exposure) '
        'or "expose" (their product IS finding exposure — leading with it '
        "is the pitch),\n"
        '  "signals": array covering EVERY catalogue type: {"type": ..., '
        '"on": true|false, "reason": one short sentence addressed to the '
        'seller explaining why this signal is on or off FOR THEM},\n'
        '  "hiring_roles": array of 3-6 security job-role phrases whose '
        "hiring most signals a buyer for this seller (e.g. a pentest firm "
        "cares about AppSec/offensive roles more than GRC analysts),\n"
        '  "funding_event_types": array from ["venture round", "debt '
        'financing", "acquisition", "private equity"] — which events '
        "genuinely unlock budget for this seller."
    )
    result = call_claude(prompt, system=system, model=settings.CLAUDE_MODEL)
    if not isinstance(result, dict) or result.get("error"):
        logger.warning("propose_signal_config Claude call failed: %s", result)
        return fallback

    by_type = {s.get("type"): s for s in (result.get("signals") or []) if isinstance(s, dict)}
    signals = []
    for s in PROPOSABLE_SIGNALS:
        got = by_type.get(s["type"]) or {}
        signals.append({
            **s,
            "on": bool(got.get("on", True)),
            "reason": str(got.get("reason") or "").strip()
            or "Included by default — tune any time in Settings.",
        })
    angle = result.get("angle") if result.get("angle") in _CRITICAL_RULES else fallback_angle
    return {
        "archetype": str(result.get("archetype") or "").strip() or fallback["archetype"],
        "angle": angle,
        "signals": signals,
        "hiring_roles": [str(r).strip() for r in (result.get("hiring_roles") or []) if str(r).strip()][:6],
        "funding_event_types": [str(t).strip().lower() for t in (result.get("funding_event_types") or []) if str(t).strip()][:4],
        "source": "ai",
    }


def apply_signal_config(
    answers: Dict[str, Any], signal_config: Dict[str, Any]
) -> Dict[str, float]:
    """User-confirmed proposal → the org's signal_priorities. Off = 0.0
    (never shown, Phase 1 rule); on = the angle preset's weight (0.8 when
    the preset doesn't carry the type). Types outside the proposable
    catalogue keep their preset weights untouched."""
    angle = signal_config.get("angle")
    base = dict(_PRIORITIES_EXPOSE if angle == "expose" else _PRIORITIES_DEFEND)
    enabled = signal_config.get("enabled") or {}
    for s in PROPOSABLE_SIGNALS:
        t = s["type"]
        if t in enabled:
            base[t] = (base.get(t) or 0.8) if enabled[t] else 0.0
    return base


def signal_priorities_from_answers(answers: Dict[str, Any]) -> Dict[str, float]:
    """Preset per-org signal weights from the ambulance axis, boosting the
    signals the user explicitly picked to 1.0. Defaults to the defend base
    when the angle is missing — never-lead-with-fear is the safe default."""
    base = dict(
        _PRIORITIES_EXPOSE if (answers or {}).get("ambulance") == "expose"
        else _PRIORITIES_DEFEND
    )
    chosen = (answers or {}).get("signals")
    if isinstance(chosen, list):
        for sig in chosen:
            if isinstance(sig, str) and sig in base:
                base[sig] = 1.0
    return base
