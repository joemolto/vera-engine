"""Jobs enrichment — checks website, Apollo, and Indeed for open positions."""

import logging
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

import httpx
from supabase import Client

from app.prompts.enrichment import JOBS_ANALYSIS_PROMPT, CONTEXTUAL_JOBS_PROMPT, _format_services_list
from app.services.claude import call_claude
from app.services.enrichers.cache import get_cached, set_cache
from app.services.enrichers.tech_stack import fetch_apollo_job_postings
from app.services.apify_client import scrape_linkedin_jobs
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10

# Canonical security-LEADERSHIP title vocabulary. Lives here (rather than in
# enricher.py) because enricher imports this module, not the reverse — one
# definition, two consumers.
#
# Matched case-insensitively as substrings against a role title. Deliberately
# conservative: a false "they're hiring a CISO" is far more damaging than a
# missed one, since it drives the strongest commercial read we have.
SECURITY_LEADERSHIP_TITLES = {
    "ciso", "cso", "chief information security",
    "chief security", "head of security", "head of cyber",
    "head of information security", "head of infosec",
    "vp security", "vp of security", "vice president security",
    "director of security", "director, security",
    "security director", "security officer",
    "deputy ciso", "regional ciso", "divisional ciso",
    # Manager grades (added 2026-08-04): a Head-of / Manager role owns
    # budget and vendor choice, so it belongs with leadership rather than
    # with the practitioner roles. "Senior Information Security Manager"
    # was previously filed alongside "SOC Analyst".
    "security manager", "information security manager",
    "infosec manager", "cyber security manager", "cybersecurity manager",
}


def split_security_roles(roles: List[str]) -> tuple[List[str], List[str]]:
    """Partition security job titles into (leadership, practitioner).

    Commercially these are opposite signals. Hiring a CISO often means no
    senior owner of security exists today — an open door. Hiring a SOC
    analyst means a mature team is expanding, a much harder sell. Filing
    both under `hiring_security` lost that distinction entirely.
    """
    leadership: List[str] = []
    practitioner: List[str] = []
    for role in roles or []:
        if not role:
            continue
        title = str(role).lower()
        if any(kw in title for kw in SECURITY_LEADERSHIP_TITLES):
            leadership.append(role)
        else:
            practitioner.append(role)
    return leadership, practitioner


def enrich_jobs(
    db: Client,
    domain: str,
    company_name: str,
    website_careers_content: Optional[str] = None,
    client_context: Optional[Dict[str, object]] = None,
) -> dict:
    """Check multiple sources for job postings and analyse with Claude.

    Sources:
        1. Website careers page (passed in or fetched via Jina AI)
        2. Apollo organization job postings
        3. Indeed UK RSS feed

    Returns:
        Claude analysis dict with hiring signals and optional _create_signal flag.
    """
    # Check cache first
    cached = get_cached(db, domain, "jobs")
    if cached:
        return cached

    # Source 1 (Primary): Apify LinkedIn Jobs Scraper
    linkedin_postings = scrape_linkedin_jobs(company_name)
    if linkedin_postings:
        linkedin_jobs = "\n".join(
            f"- {p.get('title', 'Unknown role')}"
            f" | Location: {p.get('location', 'N/A')}"
            f" | Posted: {p.get('posted_date', 'N/A')}"
            for p in linkedin_postings
        )
    else:
        linkedin_jobs = "No job postings found on LinkedIn"

    # Source 2: Website careers content
    careers_text = website_careers_content
    if not careers_text:
        careers_text = _fetch_careers_page(domain)
    website_jobs = careers_text or "No careers page content available"

    # Source 3: Apollo job postings
    apollo_postings = fetch_apollo_job_postings(domain)
    if apollo_postings:
        apollo_jobs = "\n".join(
            f"- {p.get('title', 'Unknown role')}"
            f" | Location: {p.get('location', 'N/A')}"
            f" | Posted: {p.get('posted_date', 'N/A')}"
            for p in apollo_postings
        )
    else:
        apollo_jobs = "No job postings found via Apollo"

    # Source 4: Indeed RSS
    indeed_postings = _fetch_indeed_jobs(company_name)
    if indeed_postings:
        indeed_jobs = "\n".join(
            f"- {p.get('title', 'Unknown role')}"
            f" | Location: {p.get('location', 'N/A')}"
            f" | Posted: {p.get('posted_date', 'N/A')}"
            for p in indeed_postings
        )
    else:
        indeed_jobs = "No job postings found on Indeed"

    # Send to Claude for analysis
    if client_context and client_context.get("services_offered"):
        prompt = CONTEXTUAL_JOBS_PROMPT.format(
            company_name=company_name,
            domain=domain,
            services_offered_list=_format_services_list(client_context.get("services_offered", [])),
            linkedin_jobs=linkedin_jobs,
            website_jobs=website_jobs,
            apollo_jobs=apollo_jobs,
            indeed_jobs=indeed_jobs,
        )
    else:
        prompt = JOBS_ANALYSIS_PROMPT.format(
            company_name=company_name,
            domain=domain,
            linkedin_jobs=linkedin_jobs,
            website_jobs=website_jobs,
            apollo_jobs=apollo_jobs,
            indeed_jobs=indeed_jobs,
        )

    result = call_claude(prompt)

    if "error" in result:
        logger.error("Jobs analysis failed for %s: %s", domain, result.get("error"))
        return result

    # Auto-create signals for EVERY hiring category Claude flagged
    # (spec 2026-06-10). The old code only emitted hiring_sales_bd
    # which meant Monzo's "Senior Offensive Security Engineer" role
    # landed in the LLM analysis but silently died on the way to the
    # signals table - making the Maturity axis look thin even though
    # the company is actively building security capacity.
    #
    # Returns `_create_signals` (plural list) instead of `_create_signal`
    # (singular). The enricher consumer iterates the list; each entry
    # is independently wiped + written so the same enrichment can
    # produce hiring_security AND hiring_grc AND hiring_sales_bd from
    # one pass.
    signals_to_create: List[Dict[str, Any]] = []
    common_total = result.get("total_roles_found", 0)
    strength = result.get("signal_strength", 0.8)

    def _push(signal_type: str, roles_key: str) -> None:
        roles = result.get(roles_key) or []
        signals_to_create.append({
            "signal_type": signal_type,
            "signal_source": "jobs_enrichment",
            "signal_data": {
                "roles": roles,
                "source_count": common_total,
            },
            "signal_strength": strength,
        })

    def _push_roles(signal_type: str, roles: List[str], signal_strength: float) -> None:
        signals_to_create.append({
            "signal_type": signal_type,
            "signal_source": "jobs_enrichment",
            "signal_data": {
                "roles": roles,
                "source_count": common_total,
            },
            "signal_strength": signal_strength,
        })

    # Security hiring splits by seniority (2026-08-04). Claude already returns
    # the role titles, so the split is deterministic keyword work rather than
    # another LLM judgement call — and it re-classifies cached payloads too.
    # A leadership opening scores higher than a practitioner one because it
    # usually means nobody senior owns security yet.
    if result.get("is_hiring_security"):
        sec_roles = result.get("security_roles_found") or []
        leadership, practitioner = split_security_roles(sec_roles)
        if leadership:
            _push_roles(
                "hiring_security_leadership",
                leadership,
                min(0.95, strength + 0.1),
            )
        if practitioner:
            _push_roles("hiring_security", practitioner, strength)
        # Claude said security hiring but no title survived the split (empty
        # or unparseable titles) — keep the original behaviour rather than
        # silently dropping the signal.
        if not leadership and not practitioner:
            _push("hiring_security", "security_roles_found")
    if result.get("is_hiring_grc"):
        _push("hiring_grc", "grc_roles_found")
    if result.get("is_hiring_sales_bd"):
        _push("hiring_sales_bd", "sales_bd_roles_found")

    if signals_to_create:
        result["_create_signals"] = signals_to_create

    # Preserve raw postings so downstream consumers (stack_detection's
    # job_posts collector) can grep them for named security tools.
    # Claude's aggregated summary doesn't expose the underlying text.
    # Truncate descriptions and cap total volume to keep enrichment_data
    # payload size sane.
    raw_postings: List[Dict[str, Any]] = []
    for p in (linkedin_postings or []):
        raw_postings.append({
            "title": p.get("title") or "",
            "description": (p.get("description") or "")[:500],
            "source": "linkedin",
        })
    for p in (apollo_postings or []):
        raw_postings.append({
            "title": p.get("title") or "",
            "description": (p.get("description") or "")[:500],
            "source": "apollo",
        })
    for p in (indeed_postings or []):
        raw_postings.append({
            "title": p.get("title") or "",
            "description": "",  # Indeed RSS gives title-only
            "source": "indeed",
        })
    result["raw_postings"] = raw_postings[:75]

    # Cache the result
    set_cache(db, domain, "jobs", None, result)

    return result


def _fetch_careers_page(domain: str) -> Optional[str]:
    """Fetch the careers/jobs page via Jina AI Reader."""
    for path in ["/careers", "/jobs"]:
        url = f"https://r.jina.ai/https://{domain}{path}"
        try:
            with httpx.Client(timeout=15, follow_redirects=True) as client:
                response = client.get(
                    url,
                    headers={
                        "Accept": "text/plain",
                        "X-Timeout": "10",
                    },
                )
                response.raise_for_status()
                text = response.text.strip()
                if text and len(text) > 50:
                    return text
        except Exception as e:
            logger.debug("Jina AI could not fetch %s%s: %s", domain, path, e)
    return None


def _fetch_indeed_jobs(company_name: str) -> Optional[List[Dict[str, Any]]]:
    """Search Indeed UK RSS feed for company job postings."""
    encoded_name = quote_plus(company_name)
    url = f"https://uk.indeed.com/rss?q={encoded_name}&l=United+Kingdom"

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.get(url)
            response.raise_for_status()

        root = ET.fromstring(response.text)

        jobs: List[Dict[str, Any]] = []
        for item in root.iter("item"):
            title_el = item.find("title")
            location_el = item.find("location")
            # Indeed uses georss:point or custom location tag
            if location_el is None:
                # Try with namespace
                for child in item:
                    if "location" in child.tag.lower():
                        location_el = child
                        break
            pub_date_el = item.find("pubDate")

            jobs.append({
                "title": title_el.text if title_el is not None and title_el.text else "Unknown",
                "location": location_el.text if location_el is not None and location_el.text else "N/A",
                "posted_date": pub_date_el.text if pub_date_el is not None and pub_date_el.text else "N/A",
                "source": "indeed",
            })

        return jobs if jobs else None

    except Exception as e:
        logger.error("Indeed RSS error for '%s': %s", company_name, e)
        return None
