"""Deep company research via Perplexity Sonar (OpenRouter)."""

import hashlib
import json
import logging
import re
from typing import Any, Dict, Optional

import httpx

from supabase import Client

from app.config import settings
from app.services.enrichers.cache import get_cached, set_cache

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30

RESEARCH_PROMPT = """Research the company '{company_name}' ({company_domain}) and answer these questions. Only state facts you can verify from public sources. If you cannot find information, say "Not found".

1. COMPLIANCE & CERTIFICATIONS:
   - Are they ISO 27001 certified?
   - Do they have SOC 2 Type I or Type II?
   - Cyber Essentials or Cyber Essentials Plus?
   - CREST accredited?
   - CHECK approved?
   - PCI DSS compliant?
   - Any other security certifications?

2. SECURITY POSTURE:
   - Do they have a security page or trust centre on their website?
   - Do they have a responsible disclosure or bug bounty program?
   - Do they publish a privacy policy and modern slavery statement?

3. BUSINESS INTELLIGENCE:
   - What is their estimated company size or revenue range?
   - When were they founded?
   - Who are their key customers (if publicly referenced)?
   - Who are their main competitors?
   - Have they won any recent awards?

4. GROWTH SIGNALS:
   - Any recent partnerships announced in the last 6 months?
   - Any recent product launches in the last 6 months?
   - Any recent office expansions?
   - Are they exhibiting at any upcoming events or conferences?

Respond ONLY with valid JSON matching this structure:
{{
    "compliance": {{
        "iso_27001": true,
        "soc_2": "Type II",
        "cyber_essentials": true,
        "cyber_essentials_plus": false,
        "crest": true,
        "check": false,
        "pci_dss": false,
        "other_certifications": []
    }},
    "security_posture": {{
        "has_security_page": true,
        "has_trust_centre": false,
        "has_responsible_disclosure": false,
        "has_bug_bounty": false,
        "has_privacy_policy": true
    }},
    "business_intel": {{
        "estimated_revenue": "Not found",
        "founded_year": 2015,
        "key_customers": [],
        "competitors": [],
        "recent_awards": []
    }},
    "growth_signals": {{
        "recent_partnerships": [],
        "product_launches": [],
        "upcoming_events": [],
        "office_expansions": []
    }}
}}"""


def _parse_json_response(raw: str) -> Optional[Dict[str, Any]]:
    """Parse JSON from Perplexity response, stripping markdown fencing."""
    # Strip markdown code blocks
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", raw.strip())
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find JSON object in the response
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return None


def _build_client_aware_prompt(
    company_name: str,
    company_domain: str,
    client_context: Optional[Dict[str, Any]],
) -> str:
    """Build a Perplexity research prompt, injecting client context when available."""
    base_prompt = RESEARCH_PROMPT.format(
        company_name=company_name,
        company_domain=company_domain or "unknown",
    )

    if not client_context or not client_context.get("what_they_sell"):
        return base_prompt

    # Inject client-aware research focus
    from app.services.client_context import format_product_names
    product_names = format_product_names(client_context)
    what_they_sell = client_context.get("what_they_sell", "")

    client_section = f"""

Additionally, research {company_name} focusing on:
- Compliance and certifications relevant to {what_they_sell}
- Security posture gaps that {product_names} could address
- Recent incidents that would create urgency for {what_they_sell}"""

    # Insert before the JSON response instruction
    return base_prompt.replace(
        "Respond ONLY with valid JSON",
        client_section + "\n\nRespond ONLY with valid JSON",
    )


def _client_context_fingerprint(client_context: Optional[Dict[str, Any]]) -> str:
    """Stable short hash of the parts of client_context that change the prompt.

    Cached deep_research results depend on `what_they_sell` (because that
    drives the client-aware research focus). Two clients selling different
    things see different research, so they need separate cache entries.
    `none` is the sentinel for "no client context — base prompt only".
    """
    if not client_context or not client_context.get("what_they_sell"):
        return "none"
    fingerprint_input = json.dumps(
        {
            "what_they_sell": client_context.get("what_they_sell", ""),
            "client_name": client_context.get("client_name", ""),
            "competitors": client_context.get("competitors", ""),
            "product_names": client_context.get("product_names", ""),
        },
        sort_keys=True,
    )
    return hashlib.sha256(fingerprint_input.encode()).hexdigest()[:12]


# Universal, long-lived deep-research cache. One key for all orgs so the
# Perplexity result is cached once per domain; 30-day TTL because the company
# profile changes slowly. Bump the key suffix to bust the cache if the
# research prompt changes materially.
_DEEP_RESEARCH_KEY = "universal-v1"
_DEEP_RESEARCH_TTL = 30 * 24 * 3600  # 30 days


def enrich_deep_research(
    company_name: str,
    company_domain: str,
    client_context: Optional[Dict[str, Any]] = None,
    db: Optional[Client] = None,
    max_age_seconds: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Run deep company research via Perplexity Sonar.

    Cached in `enrichment_cache` keyed by (domain, "deep_research",
    "universal-v1") with a 30-day TTL — one shared entry per domain,
    reused across every org. `db` is optional for backwards compat;
    without it, no caching happens.

    max_age_seconds tightens the read: reuse the cache only if it was
    fetched within that window, else re-fetch and write back. Used by
    watchlist monitoring (7-day window) so cost cutting never means
    serving research older than the window.

    Returns structured research data or None on failure.
    Never blocks the pipeline — all errors are caught and logged.
    """
    api_key = settings.OPENROUTER_API_KEY
    if not api_key:
        return None

    if not company_name:
        return None

    # Universal deep research (2026-06-11): one client-agnostic company
    # profile per domain. Aligns with the universal cyber lens (rule 12 —
    # no per-vendor AI tuning) and avoids repeating the expensive
    # Perplexity call for the same domain within the 30-day TTL. The
    # per-client angle lives in scoring/signal_priorities, not the research
    # itself. The fast-moving signals (news/breach/jobs) keep their own short
    # TTLs and refresh on every add + monitor pass, so nothing stale surfaces.
    fingerprint = _DEEP_RESEARCH_KEY

    # Cache check — any org's fresh research for this domain is reused.
    if db is not None and company_domain:
        cached = get_cached(
            db, company_domain, "deep_research", fingerprint,
            max_age_seconds=max_age_seconds,
        )
        if cached is not None:
            logger.info("Deep research: cache hit for %s", company_domain)
            return None if cached.get("_none") else cached

    prompt = _build_client_aware_prompt(company_name, company_domain, None)

    try:
        response = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "perplexity/sonar",
                "messages": [
                    {"role": "user", "content": prompt}
                ],
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        # Persist raw payload for future re-extraction.
        if db is not None and company_domain:
            try:
                from app.services.raw_artifacts import persist_raw
                persist_raw(
                    db, company_domain, "perplexity_sonar", data,
                    source_endpoint="deep_research",
                )
            except Exception:
                pass

        raw_content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not raw_content:
            logger.warning("Perplexity Sonar returned empty response for %s", company_name)
            return None

        parsed = _parse_json_response(raw_content)
        if not parsed:
            logger.warning("Could not parse Perplexity response for %s: %s", company_name, raw_content[:200])
            return None

        parsed["source"] = "perplexity_sonar"
        parsed["company_name"] = company_name
        parsed["company_domain"] = company_domain

        # Persist to the shared cache — reused by every org for 30 days.
        if db is not None and company_domain:
            set_cache(
                db, company_domain, "deep_research", fingerprint, parsed,
                ttl_seconds=_DEEP_RESEARCH_TTL,
            )

        logger.info("Deep research completed for %s", company_name)
        return parsed

    except httpx.TimeoutException:
        logger.warning("Deep research timed out for %s", company_name)
        return None
    except Exception as e:
        logger.warning("Deep research failed for %s: %s", company_name, e)
        return None
