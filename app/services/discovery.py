"""Per-persona Apollo fan-out for lead discovery.

The problem this fixes
----------------------
Until now discovery ran ONE Apollo search against the *union* of every
active buyer profile (icp_config dual-writes the flat target_* fields as
the OR of all personas). Apollo OR-matches titles and OR-matches
industries independently, so a two-profile org —

    Profile A = CISO @ fintech
    Profile B = Head of IT @ manufacturing

— would surface "Head of IT @ fintech" and "CISO @ manufacturer":
cross-product combinations no single profile asked for. On tight tiers
(Scout = 5 suggestions) those contaminated rows are pure waste.

What this does instead
----------------------
One Apollo search PER active persona, using the existing per-persona
`build_search_payload()` (the single source of truth for the
persona→Apollo mapping). Every returned person is a genuine member of
the profile that found them. Results are deduped across personas and
each lead is stamped with its sourcing persona so attribution is
ground-truth rather than a post-hoc guess.

Cost note: Apollo people-search consumes NO credits (only personal-data
reveals do — see _run_apollo_search_for_icp). N searches instead of 1 is
N cheap list calls, not N× spend. The downstream tier cap + Claude
classifier in /discover is still the real gate on how many leads get
enriched, so fan-out improves *which* leads land, not *how many*.

Backward compatibility: orgs whose icp_config has no `personas` array
(legacy flat config, pre-migration) fall back to a single synthesised
union persona — identical to the old behaviour, one search.
"""

from __future__ import annotations

import logging
import math
import random
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings
from app.models.schemas import LeadInput
from app.services.apollo_payload import build_search_payload
from app.services.personas import PERSONA_FIELDS, Persona

logger = logging.getLogger(__name__)

APOLLO_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"

# Each persona search asks Apollo for at least this many rows even when the
# split would allocate fewer — a narrow profile still needs a real sample to
# survive dedupe + the Claude classifier. Apollo caps per_page at 100.
MIN_PER_PERSONA = 10
MAX_PER_PAGE = 100


def synthesise_union_persona(icp_config: Dict[str, Any]) -> Persona:
    """Build a single pseudo-persona from the legacy flat target_* fields.

    Used only when an org has no `personas` array yet (pre-migration
    configs). Running one search against this is exactly the old union
    behaviour, so the fallback path is a no-op change for those orgs.
    """
    persona: Persona = {
        "id": "__union__",
        "name": "All targets (union)",
        "active": True,
    }
    for field in PERSONA_FIELDS:
        value = icp_config.get(field)
        if isinstance(value, list):
            persona[field] = value  # type: ignore[literal-required]
    revenue = icp_config.get("target_revenue_range")
    if revenue:
        persona["target_revenue_range"] = revenue
    return persona


def _active_personas_from_icp(icp_config: Dict[str, Any]) -> List[Persona]:
    """Return active personas if the org has a personas array, else a
    single synthesised union persona (legacy/flat config fallback)."""
    personas = icp_config.get("personas")
    if personas and isinstance(personas, list):
        active = [p for p in personas if p.get("active", True)]
        if active:
            return active
        logger.info("All personas inactive — falling back to union search")
    return [synthesise_union_persona(icp_config)]


def _dedupe_key(lead: LeadInput) -> str:
    """Stable identity for cross-persona dedupe. Apollo id is best; fall
    back to linkedin URL then email so a person found by two profiles is
    counted (and attributed) once."""
    if lead.apollo_id:
        return f"apollo:{lead.apollo_id}"
    if lead.linkedin_url:
        return f"li:{lead.linkedin_url.strip().lower()}"
    return f"email:{(lead.email or '').strip().lower()}"


def _person_to_lead_input(
    p: Dict[str, Any], persona: Persona, source: str
) -> LeadInput:
    """Map one Apollo person row to a LeadInput, tagged with the persona
    whose search surfaced them. Mirrors the mapping that previously lived
    inline in _run_apollo_search_for_icp."""
    org = p.get("organization") or {}
    # Apollo people-search returns email_status='unverified' for most rows
    # without burning credits. We still need a string for LeadInput — use a
    # placeholder; the enricher resolves the real address via Findymail when
    # the user actually enriches.
    email = (p.get("email") or "").strip() or f"placeholder_{p.get('id', 'noid')}@unresolved.local"
    return LeadInput(
        email=email,
        first_name=p.get("first_name") or "",
        last_name=p.get("last_name") or "",
        title=p.get("title") or "",
        company_name=org.get("name") or "",
        company_domain=org.get("primary_domain") or "",
        company_size=str(org.get("estimated_num_employees")) if org.get("estimated_num_employees") else "",
        company_industry=(org.get("industry") or (org.get("industries") or [None])[0] or "") or "",
        company_location=", ".join(filter(None, [org.get("city"), org.get("state"), org.get("country")])) or "",
        company_country=org.get("country") or "",
        company_city=org.get("city") or "",
        company_state=org.get("state") or "",
        linkedin_url=p.get("linkedin_url") or "",
        apollo_id=p.get("id") or "",
        source=source,
        sourced_persona_id=str(persona.get("id")) if persona.get("id") else None,
        sourced_persona_name=persona.get("name"),
    )


def _apollo_search(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Single Apollo mixed_people/api_search call. Returns the people
    array (possibly empty). Isolated so callers/tests can patch one seam.

    api_search + X-Api-Key header — body `api_key` is silently ignored on
    this endpoint (same root cause as the multithread + brain fixes).
    """
    if not settings.APOLLO_API_KEY:
        logger.warning("APOLLO_API_KEY not set — skipping Apollo search")
        return []
    headers = {"X-Api-Key": settings.APOLLO_API_KEY}
    try:
        with httpx.Client(timeout=20) as client:
            resp = client.post(APOLLO_SEARCH_URL, json=payload, headers=headers)
            if not resp.is_success:
                logger.warning(
                    "Apollo fan-out search failed: HTTP %d %s",
                    resp.status_code, resp.text[:200],
                )
                return []
            return resp.json().get("people") or []
    except Exception as e:
        logger.warning("Apollo fan-out search exception: %s", e)
        return []


def run_persona_fanout(
    icp_config: Dict[str, Any],
    total_budget: int = 25,
    source: str = "apollo_fanout",
) -> Dict[str, Any]:
    """Run one Apollo search per active persona, dedupe across them, and
    return LeadInput-shaped rows each tagged with its sourcing persona.

    `total_budget` is split across the active personas (floor MIN_PER_PERSONA
    per persona, Apollo's 100 ceiling). Over-fetching candidates is free —
    Apollo people-search costs no credits — and the /discover Claude
    classifier + tier cap pick the final suggestions, so we err toward a
    fuller candidate set per profile.

    Returns: {count, leads, per_persona: {persona_name: raw_hits}}.
    """
    personas = _active_personas_from_icp(icp_config)
    n = len(personas)
    per_persona_size = min(
        MAX_PER_PAGE,
        max(MIN_PER_PERSONA, math.ceil(total_budget / n)),
    )

    seen: set[str] = set()
    leads: List[LeadInput] = []
    per_persona_counts: Dict[str, int] = {}

    for persona in personas:
        payload = build_search_payload(
            persona,
            # Vary the page across runs so daily runs don't return an identical first page.
            page=random.randint(1, 5),
            per_page=per_persona_size,
        )
        # build_search_payload omits person_titles when a persona has none;
        # a totally empty profile would pull Apollo's unfiltered firehose.
        # Skip it — an empty profile is not a search instruction.
        searchable = any(
            k not in ("page", "per_page") for k in payload
        )
        if not searchable:
            logger.info(
                "Persona %r has no filters set — skipped (would match everyone)",
                persona.get("name"),
            )
            per_persona_counts[str(persona.get("name"))] = 0
            continue

        people = _apollo_search(payload)
        kept = 0
        for p in people:
            lead = _person_to_lead_input(p, persona, source)
            key = _dedupe_key(lead)
            if key in seen:
                continue
            seen.add(key)
            leads.append(lead)
            kept += 1
        per_persona_counts[str(persona.get("name"))] = kept
        logger.info(
            "Fan-out: persona %r → %d new leads (asked %d)",
            persona.get("name"), kept, per_persona_size,
        )

    return {
        "count": len(leads),
        "leads": leads,
        "per_persona": per_persona_counts,
    }
