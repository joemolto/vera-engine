"""PredictLeads API enrichment — technology, jobs, news, financing, similar companies."""

import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Dict, List, Optional

import httpx

from supabase import Client

from app.config import settings
from app.services.claude import call_claude
from app.services.enrichers.cache import get_cached, set_cache
from app.services.llm_responses import TechFilterResponse, parse_llm_response

logger = logging.getLogger(__name__)

# Thread-local DB handle so _get() can persist raw payloads without
# threading db through every _fetch_* function. Set by enrich_predictleads.
_current_db: contextvars.ContextVar[Optional[Client]] = contextvars.ContextVar(
    "_predictleads_db", default=None,
)

BASE_URL = "https://predictleads.com/api/v3"
REQUEST_TIMEOUT = 15
PARALLEL_TIMEOUT = 30

# Sales/BD title keywords for hiring signal detection
_SALES_KEYWORDS = {"sales", "bdr", "sdr", "account executive", "ae ", "revenue", "business development"}
_LEADERSHIP_KEYWORDS = {"cmo", "cro", "vp ", "vice president", "head of", "director", "chief"}

# ---------------------------------------------------------------------------
# Technology filtering for sales relevance
# ---------------------------------------------------------------------------

_NOISE_TECHS = {
    "jQuery", "jQuery UI", "jQuery Migrate", "jQuery Modal",
    "Font Awesome", "Google Fonts API", "Open Graph", "HTTP/3",
    "HSTS", "X-Content-Type-Options", "cdnjs", "jsDelivr", "Unpkg",
    "LottieFiles", "Slick", "Preact", "Alpine.js", "Lua", "Perl", "DAV",
    "SPF", "DMARC", "DMARC Quarantine", "Organization Schema",
    "Styled Components", "Viewport Meta", "RSS", "CommonCrawl",
    "CrUX Dataset", "CrUX Top 5000", "Cloudflare Radar", "Apple Whitelist",
    "US Privacy User Signal Mechanism", "Content Delivery Network",
    "GStatic", "CDN JS", "Cloudflare JS", "Google Hosted Libraries",
    "UPS", "Goober", "Auxilia", "Pinterest Domain Verify",
    "Facebook Domain Insights", "core-js", "Intersection Observer",
    "JavaScript Modules", "Webpack", "IPhone / Mobile Compatible",
    "Apple Mobile Web Clips Icon", "SSL by Default", "IPv6",
    "Really Simple Discovery", "Google Webmaster", "English - Inferred",
    "Common Crawl", "CommonCrawl Top 50m", "403 Error",
    "Cloudflare Network Error Logging", "imagesLoaded", "Lightbox",
    "Classnames", "Varnish", "OpenResty", "Nginx",
    "HubSpot Cookie Policy Banner",
}

# Map tech names (lowercased keywords) to sales-relevant categories.
# Order matters: first match wins, so more specific rules go first.
_CATEGORY_RULES: Dict[str, List[str]] = {
    "security": [
        # The word itself catches "Shape Security", "Abnormal Security", etc.
        "security",
        # EDR / XDR / endpoint
        "splunk", "crowdstrike", "sentinelone", "palo alto", "cortex xdr",
        "fortinet", "carbon black", "darktrace", "qualys", "rapid7",
        "tenable", "nessus", "tanium", "cylance", "trend micro", "mcafee",
        "eset", "malwarebytes", "microsoft defender", "windows defender",
        "defender for endpoint", "norton", "bitdefender", "sophos",
        "jamf", "intune",
        # Email / web / fraud
        "proofpoint", "mimecast", "abnormal security", "ironscales", "agari",
        "area 1", "cisco secure email", "cloudflare bot management",
        "recaptcha", "hcaptcha", "threatmetrix", "sift science", "sift ",
        "forter", "very good security", "signifyd", "perimeterx", "kasada",
        "arkose", "human security",
        # Network / firewall / DNS / proxy
        "infoblox", "zscaler", "netskope", "cisco umbrella", "check point",
        "checkpoint", "big-ip", "shape security", "radware", "a10 networks",
        "imperva", "akamai", "sucuri", "wordfence", "barracuda", "snort",
        "suricata",
        # Identity / IAM / PAM
        "okta", "auth0", "onelogin", "duo security", "duo ", "cyberark",
        "beyondtrust", "sailpoint", "ping identity", "ping federate",
        "jumpcloud", "microsoft entra", "azure active directory", "azure ad",
        "active directory", "hashicorp vault", "racf", "ntlm", "kerberos",
        "saml",
        # SIEM / SOC / threat intel
        "threatconnect", "recorded future", "anomali", "microsoft sentinel",
        "azure sentinel", "qradar", "logrhythm", "exabeam", "google chronicle",
        "mandiant", "threatquotient", "elastic security",
        # AppSec / cloud security / supply chain
        "snyk", "veracode", "checkmarx", "semgrep", "sonatype", "black duck",
        "prisma cloud", "lacework", "aqua security", "sysdig", "orca security",
        "burp suite", "varonis", "knowbe4",
        # Posture / compliance signals (regulatory exposure)
        "nis2", "fedramp", "hipaa", "pci dss", "iso 27001", "soc 2",
        "cyber essentials", "gdpr",
        # Generic
        "waf", "siem", "edr", "firewall",
    ],
    "crm_sales": [
        "hubspot", "salesforce", "pipedrive", "zoho crm", "apollo",
        "outreach", "salesloft", "gong", "chorus", "clari", "pardot",
    ],
    "sales_intelligence": [
        "zoominfo", "6sense", "bombora", "g2 ", "trustradius",
        "clearbit", "lusha", "leadiq",
    ],
    "analytics": [
        "google analytics", "fullstory", "hotjar", "mixpanel",
        "amplitude", "datadog", "new relic", "segment", "heap",
        "piwik", "matomo", "plausible", "sentry",
    ],
    "marketing": [
        "google ads", "google tag manager", "linkedin insight",
        "facebook pixel", "meta pixel", "microsoft advertising",
        "marketo", "activecampaign", "mailchimp", "hubspot content hub",
        "brevo", "sendinblue", "convertkit", "klaviyo",
        "tiktok pixel", "twitter pixel", "pinterest tag", "bing ads",
        "optimizely", "unbounce", "leadpages", "google optimize",
    ],
    "collaboration": [
        "slack", "jira", "atlassian jira", "zendesk", "asana",
        "monday", "teams", "confluence", "notion", "intercom",
        "freshdesk", "linear", "trello", "basecamp",
    ],
    "development": [
        "python", "java ", "javascript", "typescript", "react",
        "next.js", "node.js", "angular", "vue", "ruby", "go ",
        "rust", "apollo graphql", "graphql", "django", "flask",
        ".net", "kotlin", "swift", "laravel", "php",
    ],
    "infrastructure": [
        "aws", "amazon web services", "microsoft azure", "azure dns",
        "google cloud", "gcp", "cloudflare hosting", "cloudfront",
        "fastly", "vercel", "netlify", "heroku", "digitalocean",
        "docker", "kubernetes",
    ],
}

# Display order — most important first
_DISPLAY_ORDER = [
    "behind_firewall",
    "security",
    "crm_sales",
    "sales_intelligence",
    "analytics",
    "marketing",
    "collaboration",
    "development",
    "infrastructure",
]

MAX_PER_CATEGORY = 5


def _classify_tech(name: str, pl_category: str) -> str:
    """Classify a technology into a sales-relevant category."""
    name_lower = name.lower()
    for category, keywords in _CATEGORY_RULES.items():
        for kw in keywords:
            if kw in name_lower:
                return category
    # Fall back to PredictLeads category mapping
    pl_lower = pl_category.lower() if pl_category else ""
    if "security" in pl_lower or "fraud" in pl_lower:
        return "security"
    if "analytics" in pl_lower or "tracking" in pl_lower:
        return "analytics"
    if "marketing" in pl_lower or "advertising" in pl_lower:
        return "marketing"
    if "crm" in pl_lower:
        return "crm_sales"
    if "sales" in pl_lower or "intent" in pl_lower:
        return "sales_intelligence"
    if "hosting" in pl_lower or "cloud" in pl_lower or "cdn" in pl_lower:
        return "infrastructure"
    if "programming" in pl_lower or "framework" in pl_lower or "language" in pl_lower:
        return "development"
    if "collaboration" in pl_lower or "project" in pl_lower or "support" in pl_lower:
        return "collaboration"
    return ""


def filter_technologies_for_sales(technologies: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Filter and categorise technologies for sales relevance.

    Returns categorised dict with noise removed and behind_firewall prioritised.
    Display order: behind_firewall, security, crm_sales, sales_intelligence,
    analytics, marketing, collaboration, development, infrastructure.
    """
    total_raw = len(technologies)

    # Remove noise
    filtered = [t for t in technologies if t.get("name") not in _NOISE_TECHS]

    # Categorise
    buckets: Dict[str, List[Dict[str, str]]] = {cat: [] for cat in _DISPLAY_ORDER}

    seen_names: set = set()
    for tech in filtered:
        name = tech.get("name", "")
        if not name or name in seen_names:
            continue
        seen_names.add(name)

        entry = {"name": name, "category": tech.get("category", "other")}

        # Behind-firewall always gets added (no limit)
        if tech.get("behind_firewall"):
            buckets["behind_firewall"].append(entry)

        # Also classify into sales category
        cat = _classify_tech(name, tech.get("category", ""))
        if cat and cat in buckets:
            buckets[cat].append(entry)

    # Limit per category (behind_firewall unlimited)
    for cat in buckets:
        if cat != "behind_firewall":
            buckets[cat] = buckets[cat][:MAX_PER_CATEGORY]

    # Deduplicate count (behind_firewall items may also appear in another category)
    unique_names = set()
    for items in buckets.values():
        for item in items:
            unique_names.add(item["name"])

    return {
        **buckets,
        "display_order": _DISPLAY_ORDER,
        "total_raw": total_raw,
        "total_filtered": len(unique_names),
        "noise_removed": total_raw - len(filtered),
    }


def filter_technologies_with_ai(
    technologies: List[Dict[str, Any]],
    client_context: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Use Claude to filter technologies to only those relevant for the client's sales pitch.

    Falls back to static filter_technologies_for_sales() if Claude fails or no client context.
    """
    if not client_context or not technologies:
        return filter_technologies_for_sales(technologies)

    # Build concise tech list for the prompt
    tech_names = []
    for t in technologies:
        name = t.get("name", "")
        if name:
            label = f"{name} (behind firewall)" if t.get("behind_firewall") else name
            tech_names.append(label)

    if not tech_names:
        return filter_technologies_for_sales(technologies)

    what_they_sell = client_context.get("what_they_sell") or client_context.get("services_offered") or "cybersecurity services"
    differentiators = client_context.get("key_differentiators") or ""
    pain_points = client_context.get("pain_points_they_solve") or ""

    prompt = f"""You are a sales intelligence analyst. A cybersecurity vendor needs to understand a prospect's technology stack to tailor their sales pitch.

## The vendor sells:
{what_they_sell}

## Their key differentiators:
{differentiators}

## Pain points they solve:
{pain_points}

## Prospect's detected technologies ({len(tech_names)} total):
{chr(10).join(f"- {t}" for t in tech_names)}

## Task:
Filter this list to ONLY technologies relevant for the vendor's sales conversation. Keep technologies that:
1. **Security tools** — existing security products (competitors, complements, or gaps)
2. **Compliance frameworks** — ISO 27001, SOC2, PCI DSS, GDPR, OWASP, etc.
3. **Infrastructure** — cloud, hosting, CDN that affects security posture
4. **Development frameworks** — languages/frameworks with known security implications
5. **Data & monitoring** — SIEM, logging, observability, data platforms

Remove everything irrelevant to a cybersecurity sales conversation: office tools, HR tools, marketing platforms, presentation software, recruitment tools, generic productivity software, social media, e-commerce platforms, etc.

Respond ONLY with valid JSON:
{{
    "security_tools": [{{"name": "...", "relevance": "brief reason"}}],
    "compliance": [{{"name": "...", "relevance": "brief reason"}}],
    "infrastructure": [{{"name": "...", "relevance": "brief reason"}}],
    "development": [{{"name": "...", "relevance": "brief reason"}}],
    "data_monitoring": [{{"name": "...", "relevance": "brief reason"}}],
    "total_relevant": <number>,
    "total_removed": <number>
}}"""

    try:
        result = call_claude(prompt)
    except Exception as e:
        logger.warning("AI tech filter failed: %s — falling back to static filter", e)
        return filter_technologies_for_sales(technologies)

    parsed = parse_llm_response(
        TechFilterResponse, result, context="tech_filter"
    )
    if parsed is None:
        return filter_technologies_for_sales(technologies)

    display_order = ["security_tools", "compliance", "infrastructure", "development", "data_monitoring"]
    parsed_dict = parsed.model_dump()
    return {
        **{cat: parsed_dict.get(cat, []) for cat in display_order},
        "display_order": display_order,
        "total_raw": len(technologies),
        "total_relevant": parsed.total_relevant,
        "total_removed": parsed.total_removed,
        "filter_method": "ai",
    }


def _auth_params() -> Dict[str, str]:
    """Return PredictLeads auth query parameters."""
    return {
        "api_token": settings.PREDICTLEADS_API_TOKEN,
        "api_key": settings.PREDICTLEADS_API_KEY,
    }


def _get(endpoint: str, db: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """Make an authenticated GET request to PredictLeads.

    Persists raw JSON to the raw_artifacts corpus when a DB handle is
    available (passed explicitly or set via the contextvar by
    enrich_predictleads).
    """
    if not settings.PREDICTLEADS_API_KEY or not settings.PREDICTLEADS_API_TOKEN:
        return None  # provider not configured: skip, don't call with blank keys
    url = f"{BASE_URL}{endpoint}"
    if db is None:
        db = _current_db.get()
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.get(url, params=_auth_params())
            if response.status_code == 404:
                logger.info("PredictLeads 404 for %s", endpoint)
                return None
            response.raise_for_status()
            data = response.json()

            if db is not None:
                try:
                    # endpoint shape: /companies/<domain>/<sub> or /companies/<domain>
                    parts = endpoint.strip("/").split("/")
                    if len(parts) >= 2 and parts[0] == "companies":
                        dom = parts[1]
                        sub = parts[2] if len(parts) >= 3 else "company_info"
                        from app.services.raw_artifacts import persist_raw
                        persist_raw(
                            db, dom, "predictleads", data, source_endpoint=sub,
                        )
                except Exception:
                    pass

            return data
    except httpx.TimeoutException:
        logger.warning("PredictLeads timeout for %s", endpoint)
        return None
    except Exception as e:
        logger.warning("PredictLeads error for %s: %s", endpoint, e)
        return None


# ---------------------------------------------------------------------------
# Individual endpoint parsers
# ---------------------------------------------------------------------------

def _fetch_technology(domain: str) -> Optional[Dict[str, Any]]:
    """Fetch and parse technology detections."""
    raw = _get(f"/companies/{domain}/technology_detections")
    if not raw:
        return None

    # Build lookup from included array
    included = raw.get("included") or []
    tech_lookup: Dict[str, Dict[str, Any]] = {}
    for item in included:
        if isinstance(item, dict) and item.get("type") == "technology":
            tech_lookup[item.get("id", "")] = item.get("attributes", {})

    techs = []
    data = raw.get("data") or []
    for detection in data:
        if not isinstance(detection, dict):
            continue
        attrs = detection.get("attributes") or {}
        relationships = detection.get("relationships", {}) or {}
        # Get technology ID from relationships
        rel = relationships.get("technology", {}).get("data", {})
        tech_id = rel.get("id", "") if isinstance(rel, dict) else ""
        tech_attrs = tech_lookup.get(tech_id, {})

        name = tech_attrs.get("name") or attrs.get("name") or ""
        if not name:
            continue

        # PL surfaces detection method via populated relationship arrays,
        # NOT via an attributes.source field. Derive observable methods
        # in descending strength order so the primary `source` field is
        # the strongest attestation.
        methods: List[str] = []
        if (relationships.get("seen_on_dns_records") or {}).get("data"):
            methods.append("DNS")
        if (relationships.get("seen_on_subpages") or {}).get("data"):
            methods.append("HtmlSourceCode")
        if (relationships.get("seen_on_job_openings") or {}).get("data"):
            methods.append("JobPost")
        if (relationships.get("seen_on_connection") or {}).get("data"):
            methods.append("Connection")

        primary_source = methods[0] if methods else "DataAnalysis"

        techs.append({
            "name": name,
            # Run through our taxonomy so security/CRM/etc. are populated —
            # PredictLeads' own category is "other" for almost everything.
            "category": _classify_tech(
                name, tech_attrs.get("category") or attrs.get("category") or ""
            ) or "other",
            "behind_firewall": bool(attrs.get("behind_firewall", False)),
            "source": primary_source,
            "detection_methods": methods,
        })

    behind_firewall = [t for t in techs if t["behind_firewall"]]

    return {
        "source": "predictleads",
        "technologies": techs,
        "total_detected": len(techs),
        "behind_firewall_count": len(behind_firewall),
        "behind_firewall_techs": [t["name"] for t in behind_firewall],
    }


def _fetch_jobs(domain: str) -> Optional[Dict[str, Any]]:
    """Fetch and parse job openings."""
    raw = _get(f"/companies/{domain}/job_openings")
    if not raw:
        return None

    jobs = []
    data = raw.get("data") or []
    is_hiring_sales = False
    is_hiring_technical = False
    is_hiring_leadership = False

    for job in data:
        if not isinstance(job, dict):
            continue
        attrs = job.get("attributes") or {}
        title = attrs.get("title") or ""
        title_lower = title.lower()

        if any(kw in title_lower for kw in _SALES_KEYWORDS):
            is_hiring_sales = True
        if attrs.get("category") == "engineering" or "engineer" in title_lower or "developer" in title_lower:
            is_hiring_technical = True
        if any(kw in title_lower for kw in _LEADERSHIP_KEYWORDS):
            is_hiring_leadership = True

        jobs.append({
            "title": title,
            "location": attrs.get("location") or "",
            "seniority": attrs.get("seniority") or "",
            "category": attrs.get("category") or "",
            "first_seen": attrs.get("first_seen_at") or "",
            "url": attrs.get("url") or "",
            "description_preview": (attrs.get("description") or "")[:200],
        })

    return {
        "source": "predictleads",
        "total_jobs": len(jobs),
        "jobs": jobs[:20],  # Cap at 20 for storage
        "is_hiring_sales": is_hiring_sales,
        "is_hiring_technical": is_hiring_technical,
        "is_hiring_leadership": is_hiring_leadership,
    }


def _fetch_news_events(domain: str) -> Optional[Dict[str, Any]]:
    """Fetch and parse news events."""
    raw = _get(f"/companies/{domain}/news_events")
    if not raw:
        return None

    # Build article lookup from included
    included = raw.get("included") or []
    article_lookup: Dict[str, Dict[str, Any]] = {}
    for item in included:
        if isinstance(item, dict) and item.get("type") == "news_article":
            article_lookup[item.get("id", "")] = item.get("attributes", {})

    events = []
    data = raw.get("data") or []
    has_recent_funding = False
    has_recent_acquisition = False
    has_recent_product_launch = False
    has_recent_partnership = False

    for event in data:
        if not isinstance(event, dict):
            continue
        attrs = event.get("attributes") or {}
        category = attrs.get("category") or ""

        if category == "receives_financing":
            has_recent_funding = True
        elif category == "acquires":
            has_recent_acquisition = True
        elif category == "launches":
            has_recent_product_launch = True
        elif category == "partners_with":
            has_recent_partnership = True

        # Get linked article
        article_rel = event.get("relationships", {}).get("news_article", {}).get("data", {})
        article_id = article_rel.get("id", "") if isinstance(article_rel, dict) else ""
        article = article_lookup.get(article_id, {})

        events.append({
            "category": category,
            "summary": attrs.get("summary") or attrs.get("article_sentence") or "",
            "date": attrs.get("found_at") or "",
            "amount": attrs.get("amount") or "",
            "financing_type": attrs.get("financing_type") or "",
            "product": attrs.get("product") or "",
            "article_title": article.get("title") or "",
            "article_url": article.get("url") or "",
        })

    return {
        "source": "predictleads",
        "total_events": len(events),
        "events": events[:15],
        "has_recent_funding": has_recent_funding,
        "has_recent_acquisition": has_recent_acquisition,
        "has_recent_product_launch": has_recent_product_launch,
        "has_recent_partnership": has_recent_partnership,
    }


def _fetch_financing(domain: str) -> Optional[Dict[str, Any]]:
    """Fetch and parse financing events."""
    raw = _get(f"/companies/{domain}/financing_events")
    if not raw:
        return None

    rounds = []
    data = raw.get("data") or []

    # Build investor lookup from included
    included = raw.get("included") or []
    investor_lookup: Dict[str, str] = {}
    for item in included:
        if isinstance(item, dict) and item.get("type") == "company":
            investor_lookup[item.get("id", "")] = (item.get("attributes") or {}).get("name", "")

    for event in data:
        if not isinstance(event, dict):
            continue
        attrs = event.get("attributes") or {}

        # Get investor names from relationships
        investors_rel = event.get("relationships", {}).get("investors", {}).get("data", [])
        investor_names = []
        if isinstance(investors_rel, list):
            for inv in investors_rel:
                if isinstance(inv, dict):
                    name = investor_lookup.get(inv.get("id", ""), "")
                    if name:
                        investor_names.append(name)

        rounds.append({
            "type": attrs.get("financing_type") or attrs.get("type") or "",
            "amount": attrs.get("amount") or "",
            "date": attrs.get("announced_on") or attrs.get("found_at") or "",
            "investors": investor_names,
        })

    return {
        "source": "predictleads",
        "rounds": rounds,
    }


def _fetch_similar_companies(domain: str) -> Optional[Dict[str, Any]]:
    """Fetch and parse similar companies (top 5)."""
    raw = _get(f"/companies/{domain}/similar_companies")
    if not raw:
        return None

    # Build company lookup from included array
    included = raw.get("included") or []
    company_lookup: Dict[str, Dict[str, Any]] = {}
    for item in included:
        if isinstance(item, dict) and item.get("type") == "company":
            company_lookup[item.get("id", "")] = item.get("attributes", {})

    similar = []
    data = raw.get("data") or []
    for entry in data[:5]:
        if not isinstance(entry, dict):
            continue
        attrs = entry.get("attributes") or {}

        # Get similar company ID from relationships
        rel = entry.get("relationships", {}).get("similar_company", {}).get("data", {})
        company_id = rel.get("id", "") if isinstance(rel, dict) else ""
        company_attrs = company_lookup.get(company_id, {})

        name = company_attrs.get("company_name") or company_attrs.get("name") or attrs.get("name") or ""
        comp_domain = company_attrs.get("domain") or attrs.get("domain") or ""

        similar.append({
            "name": name,
            "domain": comp_domain,
            "score": attrs.get("score") or attrs.get("similarity_score") or 0,
            "reason": attrs.get("reason") or "",
        })

    return {
        "source": "predictleads",
        "similar_companies": similar,
    }


def _fetch_company_info(domain: str) -> Optional[Dict[str, Any]]:
    """Fetch and parse company info."""
    raw = _get(f"/companies/{domain}")
    if not raw:
        return None

    data = raw.get("data") or {}
    attrs = data.get("attributes") or {} if isinstance(data, dict) else {}

    # Check for parent/subsidiary relationships
    rels = data.get("relationships", {}) if isinstance(data, dict) else {}
    parent = ""
    parent_data = rels.get("parent_company", {}).get("data")
    if isinstance(parent_data, dict):
        parent = parent_data.get("id", "")

    subsidiaries = []
    subs_data = rels.get("subsidiaries", {}).get("data", [])
    if isinstance(subs_data, list):
        subsidiaries = [s.get("id", "") for s in subs_data if isinstance(s, dict)]

    return {
        "source": "predictleads",
        "company_name": attrs.get("name") or "",
        "description": (attrs.get("description") or "")[:500],
        "location": attrs.get("location") or "",
        "parent_company": parent or None,
        "subsidiaries": subsidiaries,
    }


# ---------------------------------------------------------------------------
# Lightweight pre-enrichment for discovery (3 endpoints only)
# ---------------------------------------------------------------------------

# Security keywords for tech classification in pre-enrichment summaries
_SECURITY_TECH_KEYWORDS = {
    "splunk", "crowdstrike", "sentinelone", "palo alto", "fortinet",
    "carbon black", "darktrace", "qualys", "rapid7", "tenable",
    "proofpoint", "mimecast", "okta", "auth0", "duo", "cyberark",
    "varonis", "imperva", "akamai", "sucuri", "wordfence", "barracuda",
    "onelogin", "knowbe4", "norton", "bitdefender", "sophos",
    "waf", "siem", "edr", "firewall", "endpoint protection",
    "vulnerability", "threat", "antivirus", "ids", "ips",
}


def pre_enrich_domain(domain: str) -> Optional[Dict[str, Any]]:
    """Lightweight pre-enrichment: company info, news events, and tech stack.

    Called during discovery to give Vera intelligence before picking leads.
    Uses a short 8s timeout per endpoint. Returns a summary dict or None.
    """
    if not settings.PREDICTLEADS_API_KEY or not settings.PREDICTLEADS_API_TOKEN:
        return None
    if not domain:
        return None

    endpoints = {
        "company_info": (_fetch_company_info, domain),
        "news_events": (_fetch_news_events, domain),
        "technology": (_fetch_technology, domain),
    }

    results: Dict[str, Any] = {}

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            name: executor.submit(fn, arg)
            for name, (fn, arg) in endpoints.items()
        }
        for name, future in futures.items():
            try:
                results[name] = future.result(timeout=8)
            except (FuturesTimeoutError, Exception) as e:
                logger.debug("Pre-enrich %s failed for %s: %s", name, domain, e)
                results[name] = None

    # Build summary
    company_info = results.get("company_info") or {}
    news = results.get("news_events") or {}
    tech = results.get("technology") or {}

    # Identify security tools
    all_techs = tech.get("technologies") or []
    security_tools = []
    for t in all_techs:
        name_lower = (t.get("name") or "").lower()
        cat_lower = (t.get("category") or "").lower()
        if any(kw in name_lower for kw in _SECURITY_TECH_KEYWORDS) or "security" in cat_lower:
            security_tools.append(t.get("name", ""))

    summary = {
        "domain": domain,
        "description": company_info.get("description", ""),
        "location": company_info.get("location", ""),
        "parent_company": company_info.get("parent_company"),
        "has_recent_funding": bool(news.get("has_recent_funding")),
        "has_recent_acquisition": bool(news.get("has_recent_acquisition")),
        "has_recent_product_launch": bool(news.get("has_recent_product_launch")),
        "has_recent_partnership": bool(news.get("has_recent_partnership")),
        "total_tech_detected": tech.get("total_detected", 0),
        "behind_firewall_count": tech.get("behind_firewall_count", 0),
        "behind_firewall_techs": tech.get("behind_firewall_techs", []),
        "has_security_tools": len(security_tools) > 0,
        "security_tools": security_tools[:10],
        "has_no_security_tools": len(security_tools) == 0 and len(all_techs) > 0,
    }

    # Store the raw PredictLeads data so full enrichment can reuse it
    raw_data = {}
    if results.get("company_info"):
        raw_data["predictleads_company"] = results["company_info"]
    if results.get("news_events"):
        raw_data["predictleads_news"] = results["news_events"]
    if results.get("technology"):
        raw_data["predictleads_tech"] = results["technology"]

    return {"summary": summary, "raw_data": raw_data}


# ---------------------------------------------------------------------------
# Main function — calls all endpoints in parallel
# ---------------------------------------------------------------------------

def enrich_predictleads(
    domain: str,
    client_context: Optional[Dict[str, Any]] = None,
    db: Optional[Client] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch all PredictLeads data for a domain.

    Each endpoint is cached independently in `enrichment_cache` (keyed by
    `predictleads_{type}`) with a per-source TTL — see TTL_SECONDS in
    cache.py. tech / financing / similar_companies / company_info are
    7d, jobs / news_events are 24h.

    Cache hits skip the paid API call entirely. The Claude-driven
    similar_companies relevance filter ALWAYS runs because its result
    depends on the calling client's context — but it operates on the
    cached raw `similar_companies` payload either way.

    Calls remaining endpoints in parallel with individual 15s timeouts.
    Returns combined results or None if the API is not configured.

    `db` is optional for backwards compat — if absent, no caching happens
    and behaviour matches the pre-cache version.
    """
    if not settings.PREDICTLEADS_API_KEY or not settings.PREDICTLEADS_API_TOKEN:
        return None

    endpoint_specs = {
        "technology":        (_fetch_technology,        "predictleads_technology"),
        "jobs":              (_fetch_jobs,              "predictleads_jobs"),
        "news_events":       (_fetch_news_events,       "predictleads_news_events"),
        "financing":         (_fetch_financing,         "predictleads_financing"),
        "similar_companies": (_fetch_similar_companies, "predictleads_similar_companies"),
        "company_info":      (_fetch_company_info,      "predictleads_company_info"),
    }

    results: Dict[str, Any] = {"source": "predictleads", "domain": domain}
    cache_hits: List[str] = []
    to_fetch: Dict[str, Any] = {}

    # Cache lookup pass — populate hits, defer misses to the parallel fetch
    if db is not None:
        for name, (fn, cache_type) in endpoint_specs.items():
            cached = get_cached(db, domain, cache_type)
            if cached is not None:
                # `None` was wrapped as {"_none": True} on write so we can
                # distinguish "we already checked, it returned nothing" from
                # "we never checked". Unwrap on read.
                results[name] = None if cached.get("_none") else cached
                cache_hits.append(name)
            else:
                to_fetch[name] = (fn, cache_type)
    else:
        to_fetch = {n: (fn, ct) for n, (fn, ct) in endpoint_specs.items()}

    if cache_hits:
        logger.info(
            "PredictLeads: %d/%d endpoints from cache for %s (%s)",
            len(cache_hits), len(endpoint_specs), domain, ", ".join(cache_hits),
        )

    # Parallel fetch for cache misses only
    if to_fetch:
        logger.info(
            "PredictLeads: fetching %d/%d endpoints for %s",
            len(to_fetch), len(endpoint_specs), domain,
        )

        # Worker wrapper so each thread sees `db` via the contextvar
        # used by _get() to persist raw payloads to raw_artifacts.
        def _with_db(fn):
            def runner(d):
                token = _current_db.set(db)
                try:
                    return fn(d)
                finally:
                    _current_db.reset(token)
            return runner

        with ThreadPoolExecutor(max_workers=max(1, len(to_fetch))) as executor:
            futures = {
                name: executor.submit(_with_db(fn), domain)
                for name, (fn, _ct) in to_fetch.items()
            }
            for name, future in futures.items():
                cache_type = to_fetch[name][1]
                try:
                    result = future.result(timeout=PARALLEL_TIMEOUT)
                    results[name] = result
                except FuturesTimeoutError:
                    logger.warning("PredictLeads %s timed out for %s", name, domain)
                    results[name] = None
                    continue
                except Exception as e:
                    logger.warning("PredictLeads %s failed for %s: %s", name, domain, e)
                    results[name] = None
                    continue

                # Persist successful response (or sentinel for None) to cache.
                # We cache None responses too — if PredictLeads has nothing
                # for this domain, we shouldn't keep asking for 24h/7d.
                if db is not None:
                    payload = result if isinstance(result, dict) else {"_none": True}
                    set_cache(db, domain, cache_type, None, payload)

    # Count how many endpoints returned data
    data_count = sum(1 for k, v in results.items() if v and k not in ("source", "domain"))
    logger.info("PredictLeads: got data from %d/6 endpoints for %s", data_count, domain)

    if data_count == 0:
        return None

    # Claude-filter similar companies for relevance.
    # NOT cached — depends on the calling client's context. Always re-run.
    similar = results.get("similar_companies")
    if isinstance(similar, dict) and similar.get("similar_companies"):
        company_info = results.get("company_info") or {}
        company_name = company_info.get("company_name") or domain
        results["similar_companies"] = _filter_similar_companies(
            similar, company_name, domain, client_context=client_context,
        )

    return results


def _filter_similar_companies(
    similar_data: Dict[str, Any],
    company_name: str,
    company_domain: str,
    client_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Filter similar companies through Claude for relevance."""
    raw_companies = similar_data.get("similar_companies", [])
    if not raw_companies:
        return similar_data

    company_list = "\n".join(
        f"- {c.get('name', '')} ({c.get('domain', '')}) — score: {c.get('score', 0)}, reason: {c.get('reason', 'N/A')}"
        for c in raw_companies
    )

    # Build client-aware filter context
    client_section = ""
    if client_context and client_context.get("what_they_sell"):
        from app.services.client_context import format_competitors
        competitors = format_competitors(client_context)
        what_they_sell = client_context.get("what_they_sell", "")
        client_section = f"""
You are filtering for companies that would be good prospects for {client_context.get('client_name', 'the client')} who sells {what_they_sell}.
Remove companies that are {competitors} or too large/small for their market.
"""

    prompt = f"""You are filtering a list of similar companies for relevance. The target company is '{company_name}' ({company_domain}).
{client_section}
Here are similar companies suggested by our data provider:
{company_list}

Filter this list to only include companies that are:
1. Similar in SIZE (not massive enterprises like Sophos, CrowdStrike, Palo Alto — unless the target is also enterprise-scale)
2. Similar in TYPE (same business model — if target is an MSSP, show other MSSPs, not pure product companies)
3. Actually RELEVANT for competitive intelligence

Return ONLY the ones that pass the filter as valid JSON:
{{"filtered_similar": [{{"name": "...", "domain": "...", "score": 0.99, "reason": "..."}}]}}

If none pass the filter, return {{"filtered_similar": []}}"""

    try:
        result = call_claude(prompt)
        if "error" not in result:
            filtered = result.get("filtered_similar", [])
            if isinstance(filtered, list):
                return {
                    "source": "predictleads",
                    "similar_companies": filtered,
                    "raw": raw_companies,
                }
    except Exception as e:
        logger.warning("Claude similar companies filter failed: %s", e)

    # Fallback: return unfiltered with raw backup
    return {
        "source": "predictleads",
        "similar_companies": raw_companies,
        "raw": raw_companies,
    }
