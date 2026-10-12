"""Build Apollo mixed_people/api_search payloads from a persona.

Single source of truth — every engine consumer (vera_brain, multithread,
enricher discovery) calls build_search_payload() so the persona-to-Apollo
mapping logic lives in one place. Adding a new filter means changing
this file and the persona model, nothing else.

Apollo's API quirks we honour:
  - `person_titles` is an OR match (one match qualifies); empty list = no
    filter applied at all. Setting it to [] is the same as omitting.
  - `q_organization_keyword_tags` is also OR-match. We merge industries
    AND tech_uses into this single field because Apollo doesn't have a
    separate organisation-tech filter that accepts free-text — they only
    accept tech_uids for the dedicated tech filter, which we don't have
    a catalogue for yet.
  - `target_tech_excludes` is NOT honoured at Apollo level today (no
    free-text exclusion in their API). Engine filters post-search,
    after tech_stack enrichment runs. Logged once per build for clarity.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from app.services.apollo_vocabularies import revenue_min_max
from app.services.personas import Persona

logger = logging.getLogger(__name__)


def build_search_payload(persona: Persona, page: int = 1, per_page: int = 10) -> Dict[str, Any]:
    """Translate a persona into an Apollo mixed_people/api_search payload.

    Empty fields on the persona are omitted from the payload — Apollo
    treats omission and empty-array differently for some filters, and
    omitting is the safer default.
    """
    payload: Dict[str, Any] = {"page": page, "per_page": per_page}

    titles = persona.get("target_titles") or []
    if titles:
        payload["person_titles"] = titles

    seniorities = persona.get("target_seniorities") or []
    if seniorities:
        payload["person_seniorities"] = seniorities

    locations = persona.get("target_locations") or []
    if locations:
        payload["person_locations"] = locations

    sizes = persona.get("target_company_size") or []
    if sizes:
        payload["organization_num_employees_ranges"] = sizes

    # Industries and tech_uses both end up in q_organization_keyword_tags
    # because Apollo's only free-text org filter is this one. Sending a
    # merged list = "org matches any industry OR any tech we want".
    industries = persona.get("target_industries") or []
    tech_uses = persona.get("target_tech_uses") or []
    keyword_tags = [*industries, *tech_uses]
    if keyword_tags:
        payload["q_organization_keyword_tags"] = keyword_tags

    revenue_value = persona.get("target_revenue_range")
    if revenue_value:
        try:
            lo, hi = revenue_min_max(revenue_value)
            payload["revenue_range[min]"] = lo
            payload["revenue_range[max]"] = hi
        except (ValueError, AttributeError):
            logger.warning("Persona %s has malformed target_revenue_range=%r — skipped",
                           persona.get("id"), revenue_value)

    funding_stages = persona.get("target_funding_stages") or []
    if funding_stages:
        # Apollo accepts an array even though the param name is singular —
        # treats it as OR-match across listed stages.
        payload["organization_latest_funding_stage_cd"] = funding_stages

    tech_excludes = persona.get("target_tech_excludes") or []
    if tech_excludes:
        # Logged but not sent to Apollo. Post-search filtering happens in
        # the enrichment pipeline once tech_stack data is available.
        logger.debug(
            "Persona %s requests tech excludes %s — enforced post-enrichment, not at Apollo",
            persona.get("id"), tech_excludes,
        )

    return payload


def post_search_filter(persona: Persona, technologies_detected: list) -> bool:
    """Return True if a lead passes the post-search filters that Apollo
    can't apply at search time (tech excludes).

    Used by enricher after tech_stack data is available — caller drops
    leads where this returns False.
    """
    excludes = [t.lower() for t in (persona.get("target_tech_excludes") or [])]
    if not excludes:
        return True
    detected = {t.lower() for t in (technologies_detected or [])}
    return not any(exc in detected or any(exc in d for d in detected) for exc in excludes)
