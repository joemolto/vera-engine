"""Tech stack enrichment — PredictLeads first, then raw HTML + Claude, Apollo as final fallback.

BuiltWith via Apify (the previous Tier 1) was removed May 2026: PredictLeads
already provides comprehensive technology data and BuiltWith was duplicating
the work for ~$0.01/call. The cascade is now:
  Tier 0: PredictLeads
  Tier 1: Raw HTML + Jina + Claude
  Tier 2: Apollo Python rules
"""

import ipaddress
import logging
import socket
import threading
from contextlib import contextmanager

import httpx
from supabase import Client

from app.config import settings
from app.prompts.enrichment import _format_services_list
from app.services.claude import call_claude
from app.services.enrichers.cache import get_cached, set_cache
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10
HTML_FETCH_TIMEOUT = 15
MAX_HTML_LENGTH = 50_000

TECH_DETECTION_PROMPT = """Analyse this company's website to detect their technology stack. You have two sources:

RAW HTML (contains script tags, CDN references, tracking codes, meta tags):
{raw_html}

PAGE CONTENT (readable text, images, links):
{website_content}

Detect technologies from:
- <script> tags and src attributes (Google Analytics, HubSpot tracking, Facebook Pixel, etc.)
- CSS/JS CDN references (cdnjs.cloudflare.com, cdn.jsdelivr.net, unpkg.com)
- Meta tags (generator, cms indicators)
- Image/asset paths (/hubfs/ = HubSpot, /wp-content/ = WordPress)
- URL parameters (hsLang = HubSpot, utm_ = marketing tools)
- Tracking pixels and conversion scripts
- Chat widget scripts (Intercom, Drift, HubSpot)
- Form handlers and action URLs
- Partner/vendor logos and integration mentions from the page content
- Job board links (Workable, Greenhouse, Lever = HR tools)
- Cookie consent tools (OneTrust, CookieBot, HubSpot cookie banner)

For each technology found, provide:
- name: The tool/platform name
- category: crm, marketing, analytics, sales, security, email, cms, hr, development, cdn, other
- confidence: "detected" or "mentioned" (see rules below)
- evidence: Brief note on where/how it was found

IMPORTANT: Clearly distinguish between technologies the company USES versus technologies they PARTNER WITH or MENTION:

- "detected" = Found in the HTML source code as a script, tracking code, CDN reference, or technical dependency. This company actively uses this technology. Examples: HubSpot tracking code in HTML, Google Analytics script tag, Cloudflare CDN reference.

- "mentioned" = Referenced in page text, displayed as a partner logo, listed as an integration, or mentioned in marketing copy. This does NOT mean the company uses it internally. Examples: Security vendor logos in a "partners" section, technology names in blog posts, integration marketplace listings.

Do NOT mark partner logos, vendor partnerships, or integration listings as "detected". A cybersecurity company displaying CrowdStrike and SentinelOne logos means they PARTNER with those vendors, not that they USE those products internally.

{apollo_tech_section}

Respond ONLY with valid JSON:
{{
    "technologies_detected": [
        {{"name": "HubSpot", "category": "crm", "confidence": "detected", "evidence": "URLs contain /hubfs/ and hsLang parameters"}}
    ],
    "crm_tools": [],
    "marketing_tools": [],
    "sales_tools": [],
    "security_tools": [],
    "email_tools": [],
    "cms_tools": [],
    "hr_tools": [],
    "development_tools": [],
    "partner_technologies": [],
    "tech_stack_summary": "Brief summary of the company's technology maturity and stack"
}}"""

CONTEXTUAL_TECH_DETECTION_PROMPT = """You are analysing this company's tech stack for a sales rep at {client_name} who sells {what_they_sell}.
Their competitors are {competitors}.
Flag if the prospect already uses a competitor product.
Flag if there are gaps that {product_names} could fill.

The client offers these services: {services_offered_list}

Target company: {domain}

RAW HTML (contains script tags, CDN references, tracking codes, meta tags):
{raw_html}

PAGE CONTENT (readable text, images, links):
{website_content}

Detect technologies from:
- <script> tags and src attributes (Google Analytics, HubSpot tracking, Facebook Pixel, etc.)
- CSS/JS CDN references (cdnjs.cloudflare.com, cdn.jsdelivr.net, unpkg.com)
- Meta tags (generator, cms indicators)
- Image/asset paths (/hubfs/ = HubSpot, /wp-content/ = WordPress)
- URL parameters (hsLang = HubSpot, utm_ = marketing tools)
- Tracking pixels and conversion scripts
- Chat widget scripts (Intercom, Drift, HubSpot)
- Form handlers and action URLs
- Partner/vendor logos and integration mentions from the page content
- Job board links (Workable, Greenhouse, Lever = HR tools)
- Cookie consent tools (OneTrust, CookieBot, HubSpot cookie banner)

For each technology found, provide:
- name: The tool/platform name
- category: crm, marketing, analytics, sales, security, email, cms, hr, development, cdn, other
- confidence: "detected" or "mentioned" (see rules below)
- evidence: Brief note on where/how it was found

IMPORTANT: Clearly distinguish between technologies the company USES versus technologies they PARTNER WITH or MENTION:

- "detected" = Found in the HTML source code as a script, tracking code, CDN reference, or technical dependency. This company actively uses this technology. Examples: HubSpot tracking code in HTML, Google Analytics script tag, Cloudflare CDN reference.

- "mentioned" = Referenced in page text, displayed as a partner logo, listed as an integration, or mentioned in marketing copy. This does NOT mean the company uses it internally. Examples: Security vendor logos in a "partners" section, technology names in blog posts, integration marketplace listings.

Do NOT mark partner logos, vendor partnerships, or integration listings as "detected". A cybersecurity company displaying CrowdStrike and SentinelOne logos means they PARTNER with those vendors, not that they USE those products internally.

{apollo_tech_section}

Analyse the target company's technology stack from the perspective of what's relevant to the client's services ({services_offered_list}). Identify gaps the client could address.

Respond ONLY with valid JSON:
{{
    "technologies_detected": [
        {{"name": "HubSpot", "category": "crm", "confidence": "detected", "evidence": "URLs contain /hubfs/ and hsLang parameters"}}
    ],
    "crm_tools": [],
    "marketing_tools": [],
    "sales_tools": [],
    "security_tools": [],
    "email_tools": [],
    "cms_tools": [],
    "hr_tools": [],
    "development_tools": [],
    "partner_technologies": [],
    "security_maturity_from_stack": "<none|basic|intermediate|advanced>",
    "gtm_maturity_from_stack": "<none|basic|intermediate|advanced>",
    "relevant_tools_for_client": "<tools relevant to {services_offered_list}>",
    "missing_tools_for_client": "<gaps relevant to {services_offered_list}>",
    "opportunity_notes": "<observations about how the client's services address gaps>",
    "personalisation_hooks": [],
    "tech_stack_summary": "Brief summary of the company's technology maturity and stack"
}}"""


# SSRF guard — this fetcher receives the user-supplied domain from the public
# free-report tool, so it must never be coaxed into hitting internal/metadata
# hosts (directly or via a redirect).
_BLOCKED_HOST_SUFFIXES = (".internal", ".local", ".consul", ".cluster.local", ".lan", ".home.arpa")
_REBIND_HELPERS = ("nip.io", "sslip.io", "xip.io")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _norm_host(host: Optional[str]) -> str:
    return (host or "").strip().lower().rstrip(".")


def _ip_is_public(ip) -> bool:
    """Reject any address that could reach internal infra. Unwraps IPv4-mapped
    IPv6 (::ffff:a.b.c.d) and blocks the NAT64 prefix so an IPv6-capable host
    can't be steered at a private v4 target."""
    if ip.version == 6:
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            return _ip_is_public(mapped)
        if ip in _NAT64:
            return False
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified or ip in _CGNAT
    )


def _resolve_public_ips(host: Optional[str]) -> Optional[List[str]]:
    """Resolve `host` and return its IPs only if EVERY one is public and the
    name isn't an internal/rebind name; otherwise None. The returned list is
    then pinned for the actual connection so the address we validated is the
    address we connect to (closing the DNS-rebinding TOCTOU)."""
    h = _norm_host(host)
    if not h or len(h) > 253:
        return None
    if h.endswith(_BLOCKED_HOST_SUFFIXES) or any(
        h == r or h.endswith("." + r) for r in _REBIND_HELPERS
    ):
        return None
    try:
        infos = _ORIG_GETADDRINFO(h, 443, proto=socket.IPPROTO_TCP)
    except Exception:
        return None
    ips: List[str] = []
    for info in infos or ():
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return None
        if not _ip_is_public(ip):
            return None
        ips.append(info[4][0])
    return ips or None


def _host_is_public(host: Optional[str]) -> bool:
    """True only if `host` resolves exclusively to public IPs and isn't an
    internal/rebind name. Kept as a cheap boolean pre-check; `guarded_get`
    additionally pins the resolved IP for the connection."""
    return _resolve_public_ips(host) is not None


# --- DNS-rebinding defence -------------------------------------------------
# Validating a host then letting httpx re-resolve it at connect time is a
# TOCTOU: an attacker's nameserver can answer "public" to the check and
# "127.0.0.1" to the connection. We close that by pinning resolution to the
# exact IPs we validated. The pin is thread-local (safe under the FastAPI
# threadpool) and only affects hosts we've explicitly pinned — every other
# lookup delegates to the real resolver unchanged.
_ORIG_GETADDRINFO = socket.getaddrinfo
_PIN = threading.local()


def _pinned_getaddrinfo(host, port, *args, **kwargs):
    pins = getattr(_PIN, "map", None)
    ips = pins.get(_norm_host(host)) if pins else None
    if not ips:
        return _ORIG_GETADDRINFO(host, port, *args, **kwargs)
    results = []
    for ip in ips:
        results.extend(_ORIG_GETADDRINFO(ip, port, *args, **kwargs))
    return results


if not getattr(socket.getaddrinfo, "_vera_ssrf_pinned", False):
    _pinned_getaddrinfo._vera_ssrf_pinned = True
    socket.getaddrinfo = _pinned_getaddrinfo


@contextmanager
def _pin_host(host: str, ips: List[str]):
    if not hasattr(_PIN, "map"):
        _PIN.map = {}
    key = _norm_host(host)
    _PIN.map[key] = ips
    try:
        yield
    finally:
        _PIN.map.pop(key, None)


def guarded_get(
    client: "httpx.Client",
    url: str,
    *,
    method: str = "get",
    max_hops: int = 4,
    **kwargs,
) -> Optional["httpx.Response"]:
    """SSRF-safe GET/HEAD. Validates that every host in the redirect chain
    resolves to a public IP, PINS that resolution for the connection, and
    follows redirects manually rather than letting httpx chase a 302 into an
    internal host. Returns the final response, or None if a hop is non-public
    / the chain is too long / the request errors. The client MUST be created
    with follow_redirects=False (this helper does the following)."""
    for _hop in range(max_hops):
        host = httpx.URL(url).host
        ips = _resolve_public_ips(host)
        if not ips:
            logger.warning("SSRF guard: refusing non-public host in %s", url)
            return None
        try:
            with _pin_host(host, ips):
                resp = getattr(client, method)(url, **kwargs)
        except (httpx.RequestError, httpx.HTTPError):
            return None
        loc = resp.headers.get("location")
        if resp.is_redirect and loc:
            url = str(httpx.URL(url).join(loc))
            continue
        return resp
    return None


def fetch_raw_html(domain: str) -> Optional[str]:
    """Fetch raw HTML from a domain's homepage (first 50000 chars), or None.

    SSRF-hardened via `guarded_get`: every host in the redirect chain must
    resolve to a public IP, and that resolution is pinned for the connection
    so httpx can't be rebound onto an internal host between check and connect.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; VeraBot/1.0)"}

    for start in (f"https://www.{domain}", f"https://{domain}"):
        try:
            with httpx.Client(timeout=HTML_FETCH_TIMEOUT, follow_redirects=False) as client:
                response = guarded_get(client, start, headers=headers)
                if response is not None and response.status_code == 200 and response.text:
                    html = response.text[:MAX_HTML_LENGTH]
                    logger.info("Fetched %d chars of raw HTML from %s", len(html), start)
                    return html
        except Exception as e:
            logger.debug("Raw HTML fetch failed for %s: %s", start, e)
            continue

    logger.warning("Could not fetch raw HTML for %s", domain)
    return None


def enrich_tech_stack(
    db: Client,
    domain: str,
    client_context: Optional[Dict[str, object]] = None,
    website_content: Optional[str] = None,
    predictleads_tech: Optional[Dict[str, Any]] = None,
) -> dict:
    """Detect tech stack using a three-tier fallback chain.

    1. PredictLeads technology detections (if provided)
    2. Raw HTML + Jina content → Claude analysis
    3. Apollo technologies → Python analysis

    Args:
        db: Supabase client
        domain: Company domain
        client_context: Optional dict with services_offered, client_positioning, client_name
        website_content: Jina AI markdown from the website enricher
        predictleads_tech: PredictLeads technology data (if already fetched in Phase 1)
    """
    cached = get_cached(db, domain, "tech_stack")
    if cached:
        return cached

    # --- Tier 0: PredictLeads technology detections ---
    if predictleads_tech and predictleads_tech.get("technologies"):
        techs = predictleads_tech["technologies"]
        logger.info("Tech stack for %s via PredictLeads (%d techs, %d behind firewall)",
                    domain, len(techs), predictleads_tech.get("behind_firewall_count", 0))
        result = {
            "tech_stack_source": "predictleads",
            "domain": domain,
            "technologies_detected": techs,
            "total_detected": predictleads_tech.get("total_detected", len(techs)),
            "behind_firewall_count": predictleads_tech.get("behind_firewall_count", 0),
            "behind_firewall_techs": predictleads_tech.get("behind_firewall_techs", []),
            "crm_tools": [t["name"] for t in techs if t.get("category") in ("crm", "marketing")],
            "security_tools": [t["name"] for t in techs if "security" in (t.get("category") or "").lower()],
            "development_tools": [t["name"] for t in techs if t.get("category") in ("development", "devops")],
        }
        set_cache(db, domain, "tech_stack", None, result)
        return result

    # --- Tier 1: Raw HTML + website content → Claude ---
    raw_html = fetch_raw_html(domain)

    # Fetch Apollo technologies (used in tiers 2 and 3)
    apollo_technologies, _job_postings, _apollo_business_intel = _fetch_apollo_org_data(domain)
    apollo_tech_section = ""
    if apollo_technologies:
        apollo_tech_section = (
            "Additionally, Apollo.io reports the following technologies for this company: "
            + ", ".join(apollo_technologies)
            + "\nMerge these with any technologies you detect from the website."
        )

    has_content = raw_html or website_content
    if has_content:
        raw_html_for_prompt = raw_html or "Not available"
        content_for_prompt = website_content or "Not available"

        if client_context and (client_context.get("services_offered") or client_context.get("what_they_sell")):
            from app.services.client_context import format_competitors, format_product_names
            prompt = CONTEXTUAL_TECH_DETECTION_PROMPT.format(
                client_name=client_context.get("client_name", "") or client_context.get("company_name", "the client"),
                what_they_sell=client_context.get("what_they_sell", "") or ", ".join(client_context.get("services_offered", [])),
                competitors=format_competitors(client_context),
                product_names=format_product_names(client_context),
                services_offered_list=_format_services_list(client_context.get("services_offered", [])),
                domain=domain,
                raw_html=raw_html_for_prompt,
                website_content=content_for_prompt,
                apollo_tech_section=apollo_tech_section,
            )
        else:
            prompt = TECH_DETECTION_PROMPT.format(
                raw_html=raw_html_for_prompt,
                website_content=content_for_prompt,
                apollo_tech_section=apollo_tech_section,
            )

        result = call_claude(prompt)
        if "error" not in result:
            result["domain"] = domain
            result["tech_stack_source"] = "html_and_content_claude" if raw_html and website_content else (
                "html_claude" if raw_html else "content_claude"
            )
            set_cache(db, domain, "tech_stack", None, result)
            return result

        logger.warning("Claude tech detection failed for %s, falling back to Apollo", domain)

    # --- Tier 2: Apollo-only with Python analysis ---
    if apollo_technologies is None:
        return {"error": f"Could not detect tech stack for {domain}", "tech_stack_source": "none"}

    result = analyse_tech_stack(apollo_technologies)
    result["domain"] = domain
    result["tech_stack_source"] = "apollo"

    set_cache(db, domain, "tech_stack", None, result)
    return result


# --- GTM tool categories for Python fallback analysis ---

GTM_TOOLS: Dict[str, List[str]] = {
    "outbound": [
        "instantly", "outreach", "salesloft", "apollo", "lemlist",
        "smartlead", "woodpecker",
    ],
    "crm": [
        "salesforce", "hubspot", "pipedrive", "zoho_crm", "close",
    ],
    "marketing_automation": [
        "marketo", "pardot", "mailchimp", "activecampaign", "hubspot_marketing",
    ],
    "sales_intelligence": [
        "zoominfo", "cognism", "lusha", "seamless_ai",
    ],
    "linkedin_tools": [
        "linkedin_sales_navigator", "heyreach", "expandi", "dripify",
    ],
    "analytics": [
        "google_analytics", "mixpanel", "amplitude", "hotjar",
    ],
    "chat": [
        "intercom", "drift", "hubspot_chat", "crisp",
    ],
    "content": [
        "wordpress", "webflow", "ghost", "contentful",
    ],
}


def fetch_apollo_job_postings(domain: str) -> Optional[List[Dict[str, Any]]]:
    """Fetch job postings from Apollo Organization Enrichment.

    Standalone function for use by the jobs enricher.
    """
    _technologies, job_postings, _bi = _fetch_apollo_org_data(domain)
    return job_postings


def fetch_apollo_business_intel(domain: str) -> Optional[Dict[str, Any]]:
    """Fetch the business-intel slice of Apollo's organization enrichment
    (revenue, funding, founded year, headcount growth, etc.). Standalone
    helper for the enricher to merge into enrichment_data.business_intel."""
    _technologies, _job_postings, business_intel = _fetch_apollo_org_data(domain)
    return business_intel


def _fetch_apollo_org_data(
    domain: str,
) -> Tuple[Optional[List[str]], Optional[List[Dict[str, Any]]], Optional[Dict[str, Any]]]:
    """Call Apollo Organization Enrichment to get technology, job posting,
    and business-intel data (revenue, funding, founded year, employee count).

    Returns:
        Tuple of (technologies, job_postings, business_intel). Any may be
        None on error / when Apollo has no record. business_intel is a flat
        dict suitable for merging into enrichment_data.business_intel as
        a fallback when deep_research couldn't find these fields.
    """
    try:
        _db = None
        try:
            from app.database import get_supabase
            _db = get_supabase()
        except Exception:
            pass

        # Shared cache first — the payload is identical for every org
        # asking about a domain, and Apollo rate limits are the constraint.
        data = get_cached(_db, domain, "apollo_org") if _db is not None else None

        if data is None:
            with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                response = client.post(
                    "https://api.apollo.io/api/v1/organizations/enrich",
                    json={"domain": domain},
                    headers={"X-Api-Key": settings.APOLLO_API_KEY},
                )
                response.raise_for_status()
                data = response.json()

            if _db is not None:
                try:
                    from app.services.raw_artifacts import persist_raw
                    persist_raw(
                        _db, domain, "apollo", data,
                        source_endpoint="organizations/enrich",
                    )
                except Exception:
                    pass
                set_cache(_db, domain, "apollo_org", None, data)

        org_data = data.get("organization", {})

        # Extract technologies
        raw_technologies = org_data.get("technologies", [])
        technologies = [t.lower().replace(" ", "_") for t in raw_technologies]

        # Extract job postings
        raw_postings = org_data.get("job_postings", [])
        job_postings: Optional[List[Dict[str, Any]]] = None
        if raw_postings:
            job_postings = [
                {
                    "title": p.get("title", ""),
                    "url": p.get("url", ""),
                    "location": p.get("location", ""),
                    "posted_date": p.get("posted_date", p.get("posted_at", "")),
                }
                for p in raw_postings
            ]

        # Extract business-intel fields. These power the Company panel on
        # the dashboard and give the rep concrete context (size, revenue,
        # funding trajectory) without needing Perplexity Sonar to find them.
        funding_events_raw = org_data.get("funding_events") or []
        funding_events = [
            {
                "amount": e.get("amount"),
                "amount_printed": e.get("amount_printed"),
                "round": e.get("type") or e.get("round"),
                "date": e.get("date"),
                "currency": e.get("currency"),
            }
            for e in funding_events_raw
            if isinstance(e, dict)
        ]

        business_intel = {
            "name": org_data.get("name"),
            "founded_year": org_data.get("founded_year"),
            "estimated_num_employees": org_data.get("estimated_num_employees"),
            "annual_revenue": org_data.get("annual_revenue") or org_data.get("organization_revenue"),
            "annual_revenue_printed": (
                org_data.get("annual_revenue_printed")
                or org_data.get("organization_revenue_printed")
            ),
            "total_funding": org_data.get("total_funding"),
            "total_funding_printed": org_data.get("total_funding_printed"),
            "latest_funding_round_date": org_data.get("latest_funding_round_date"),
            "latest_funding_stage": org_data.get("latest_funding_stage"),
            "funding_events": funding_events,
            "publicly_traded_symbol": org_data.get("publicly_traded_symbol"),
            "publicly_traded_exchange": org_data.get("publicly_traded_exchange"),
            "industry": org_data.get("industry"),
            "headcount_six_month_growth": org_data.get("organization_headcount_six_month_growth"),
            "headcount_twelve_month_growth": org_data.get("organization_headcount_twelve_month_growth"),
            "short_description": org_data.get("short_description"),
            # Location — Apollo exposes country / state / city at the org
            # level. We surface country in the dashboard's accounts table
            # so company rows show their HQ properly (otherwise the cell
            # is "—" for every account, since PL company_info.location is
            # usually blank).
            "country": org_data.get("country"),
            "state": org_data.get("state"),
            "city": org_data.get("city"),
            # LinkedIn company page — feeds the small LinkedIn icon next
            # to the company name on the accounts dashboard. Apollo
            # almost always has this; PL only sometimes. Without
            # extraction it just sits in apollo_data unread.
            "linkedin_url": org_data.get("linkedin_url"),
            # The domain Apollo considers PRIMARY for the matched org. The
            # enricher uses this to sanity-check that enrich-by-domain didn't
            # match a SUBSIDIARY that merely lists the requested domain as a
            # secondary (e.g. tesco.com -> "Tesco Bengaluru"), whose name
            # would mislabel the whole account.
            "primary_domain": org_data.get("primary_domain"),
            "website_url": org_data.get("website_url"),
            "source": "apollo",
        }
        # Drop None fields so the merged business_intel object stays compact
        business_intel = {k: v for k, v in business_intel.items() if v not in (None, "", [])}

        return technologies, job_postings, business_intel

    except Exception as e:
        logger.error("Apollo org data error for %s: %s", domain, e)
        return None, None, None


def analyse_tech_stack(technologies: List[str]) -> dict:
    """Analyse a tech stack against GTM tool categories (Python fallback)."""
    tech_lower = [t.lower() for t in technologies]

    has_outbound = any(t in GTM_TOOLS["outbound"] for t in tech_lower)
    has_crm = any(t in GTM_TOOLS["crm"] for t in tech_lower)
    has_marketing = any(t in GTM_TOOLS["marketing_automation"] for t in tech_lower)
    has_sales_intel = any(t in GTM_TOOLS["sales_intelligence"] for t in tech_lower)
    has_linkedin = any(t in GTM_TOOLS["linkedin_tools"] for t in tech_lower)

    # Determine GTM maturity level
    if has_crm and has_outbound and has_sales_intel:
        gtm_maturity = "advanced"
    elif has_crm and (has_outbound or has_marketing):
        gtm_maturity = "intermediate"
    elif has_crm or has_marketing:
        gtm_maturity = "basic"
    else:
        gtm_maturity = "none"

    # Identify gaps
    gaps = _identify_gaps(tech_lower)

    # Generate opportunity notes
    opportunity_notes = _generate_opportunity_notes(tech_lower, gtm_maturity)

    return {
        "technologies_detected": technologies,
        "has_outbound_tools": has_outbound,
        "outbound_tools": [t for t in tech_lower if t in GTM_TOOLS["outbound"]],
        "has_crm": has_crm,
        "crm_tools": [t for t in tech_lower if t in GTM_TOOLS["crm"]],
        "has_marketing_automation": has_marketing,
        "has_sales_intelligence": has_sales_intel,
        "has_linkedin_tools": has_linkedin,
        "gtm_maturity_from_stack": gtm_maturity,
        "gtm_gaps": gaps,
        "opportunity_notes": opportunity_notes,
    }


def _identify_gaps(technologies: List[str]) -> List[str]:
    """Identify what GTM tools are missing."""
    gaps = []
    if not any(t in GTM_TOOLS["outbound"] for t in technologies):
        gaps.append("No outbound email tools detected")
    if not any(t in GTM_TOOLS["crm"] for t in technologies):
        gaps.append("No CRM detected")
    if not any(t in GTM_TOOLS["marketing_automation"] for t in technologies):
        gaps.append("No marketing automation detected")
    if not any(t in GTM_TOOLS["sales_intelligence"] for t in technologies):
        gaps.append("No sales intelligence tools detected")
    if not any(t in GTM_TOOLS["linkedin_tools"] for t in technologies):
        gaps.append("No LinkedIn outreach tools detected")
    return gaps


def _generate_opportunity_notes(technologies: List[str], gtm_maturity: str) -> str:
    """Generate opportunity notes based on tech stack analysis."""
    if gtm_maturity == "none":
        return (
            "No GTM tools detected — this company likely has no outbound infrastructure. "
            "Prime candidate for full pipeline setup."
        )
    elif gtm_maturity == "basic":
        return (
            "Basic GTM setup (CRM or marketing automation only). "
            "Missing outbound and sales intelligence — strong opportunity for pipeline services."
        )
    elif gtm_maturity == "intermediate":
        return (
            "Intermediate GTM stack — has some tools but may not be using them effectively. "
            "Opportunity to optimise and scale existing setup."
        )
    else:
        return (
            "Advanced GTM stack — already has outbound, CRM, and sales intelligence. "
            "May still benefit from managed execution but less likely to need full setup."
        )
