"""Multithreading: find other decision-makers at a company already in your pool.

Same Apollo people-search the Vera Brain uses (via _exec_find_contacts_at_company),
exposed here as structured functions for:

  1. The dashboard's in-context "Find others at this company" button on the
     prospect detail page (one-click multithreading per account).
  2. The Vera Brain's daily multithread scan, where suggestions get tagged
     so the dashboard can surface them as "we found 3 new decision-makers
     at companies you've engaged with."

Apollo people-search consumes no credits — these calls are essentially free
until the user picks a candidate to enrich.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import httpx
from supabase import Client

from app.config import settings
from app.services.activity import log_activity

logger = logging.getLogger(__name__)


DECISION_MAKER_SENIORITIES = [
    "c_suite",   # CEO, CTO, CFO, CISO, CCO, COO, etc.
    "founder",   # Founder, Co-Founder
    "owner",     # Owner / business owner (small companies)
    "partner",   # Partner-level (consultancies, accounting firms)
    "vp",        # VP-level
    "head",      # Head of X
    "director",  # Director-level
]


# Role-family detection — maps title strings to a canonical role family.
# Used for two things during multithreading:
#   1. EXCLUSION: don't surface another person of the same role family as
#      the source lead. If we already have the CISO, we don't need another
#      CISO at the same company — multithread is about adjacent buyers,
#      not duplicates.
#   2. ADJACENCY RANKING: prioritise candidates whose role family has a
#      "dotted line" into the source lead's role. E.g. for a CISO, surface
#      CIO/CTO/CFO/Head of Cyber/Head of SOC first, then everyone else.
#
# Each role family is identified by a list of canonical title fragments.
# Detection is lowercase substring matching — order matters (more specific
# patterns first). Designed for cybersecurity buyer landscape; extensible.

ROLE_FAMILY_PATTERNS: Dict[str, List[str]] = {
    "ciso": [
        "ciso",
        "chief information security officer",
        "chief security officer",
    ],
    "cto": [
        "chief technology officer",
        "chief technical officer",
        "cto",
    ],
    "cio": [
        "chief information officer",
        "cio",
    ],
    "ceo": [
        "chief executive officer",
        "ceo",
    ],
    "founder": [
        "co-founder",
        "cofounder",
        "founder",
    ],
    "cfo": [
        "chief financial officer",
        "cfo",
        "finance director",
    ],
    "coo": [
        "chief operating officer",
        "chief operations officer",
        "coo",
    ],
    "cro": [
        "chief revenue officer",
        "cro",
    ],
    "cco": [
        "chief commercial officer",
        "cco",
    ],
    "head_of_cyber": [
        "head of cyber",
        "head of cyber security",
        "head of cybersecurity",
        "director of cyber",
        "director of cybersecurity",
    ],
    "head_of_security": [
        "head of security",
        "head of information security",
        "vp security",
        "vp of security",
        "vp information security",
        "director of information security",
        "director of security",
    ],
    "head_of_soc": [
        "head of soc",
        "soc director",
        "director of soc",
        "soc manager",  # included; some orgs put SOC at manager level
    ],
    "head_of_risk": [
        "chief risk officer",
        "head of risk",
        "director of risk",
    ],
    "head_of_compliance": [
        "head of compliance",
        "compliance director",
        "director of compliance",
        "head of grc",
    ],
    "head_of_it": [
        "head of it",
        "it director",
        "director of it",
        "vp it",
    ],
    "head_of_engineering": [
        "head of engineering",
        "vp engineering",
        "vp of engineering",
        "director of engineering",
        "engineering director",
    ],
    "head_of_sales": [
        "head of sales",
        "vp sales",
        "vp of sales",
        "sales director",
    ],
}


# For each role family, the title strings we send to Apollo's person_titles
# filter when expanding a multithread search around that family. Inverse of
# ROLE_FAMILY_PATTERNS, phrased the way Apollo indexes titles.
FAMILY_TITLE_QUERIES: Dict[str, List[str]] = {
    "ciso": ["CISO", "Chief Information Security Officer", "Chief Security Officer"],
    "cto": ["CTO", "Chief Technology Officer"],
    "cio": ["CIO", "Chief Information Officer"],
    "ceo": ["CEO", "Chief Executive Officer"],
    "founder": ["Founder", "Co-Founder"],
    "cfo": ["CFO", "Chief Financial Officer"],
    "coo": ["COO", "Chief Operating Officer"],
    "cro": ["CRO", "Chief Revenue Officer"],
    "cco": ["CCO", "Chief Commercial Officer"],
    "head_of_cyber": ["Head of Cyber Security", "Director of Cyber Security"],
    "head_of_security": [
        "Head of Security",
        "Head of Information Security",
        "VP of Security",
        "Director of Security",
        "Director of Information Security",
    ],
    "head_of_soc": ["Head of SOC", "SOC Director", "SOC Manager"],
    "head_of_risk": ["Chief Risk Officer", "Head of Risk", "Director of Risk"],
    "head_of_compliance": ["Head of Compliance", "Compliance Director", "Head of GRC"],
    "head_of_it": [
        "Head of IT",
        "IT Director",
        "VP of IT",
        "Director of Information Technology",
    ],
    "head_of_engineering": [
        "Head of Engineering",
        "VP of Engineering",
        "Director of Engineering",
    ],
    "head_of_sales": ["Head of Sales", "VP of Sales", "Sales Director"],
}


# When the source lead's title doesn't map to a role family AND the org has
# no target_titles configured, search for the generic cyber buying committee
# rather than "anyone senior".
DEFAULT_MULTITHREAD_TITLES: List[str] = [
    "CISO",
    "Chief Information Security Officer",
    "CIO",
    "CTO",
    "Head of Security",
    "Head of Information Security",
    "Head of IT",
    "IT Director",
    "Head of Risk",
    "Head of Compliance",
    "CEO",
]


# Adjacency map: for each role family, the OTHER families that are
# "dotted-line" buyers/influencers in the same purchase decision. Used to
# rank multithread results — adjacent-family candidates surface first.
#
# Cybersecurity-buyer focused. Reads as: "if the source lead is a CISO,
# the people they bring into a buying decision are CIO, CTO, CFO,
# Head of Cyber/Security/SOC/IT/Risk/Compliance."
ADJACENT_ROLE_FAMILIES: Dict[str, List[str]] = {
    "ciso": [
        "cio", "cto", "cfo",
        "head_of_cyber", "head_of_security", "head_of_soc",
        "head_of_risk", "head_of_compliance", "head_of_it",
    ],
    "cto": [
        "ciso", "cio", "cfo",
        "head_of_engineering", "head_of_security", "head_of_it",
    ],
    "cio": [
        "ciso", "cto", "cfo",
        "head_of_security", "head_of_it", "head_of_engineering",
    ],
    "ceo": [
        "founder", "coo", "cfo", "cro", "cco",
        "head_of_sales",
    ],
    "founder": [
        "ceo", "founder", "coo", "cfo", "cro", "cco",
        "head_of_sales",
    ],
    "cfo": [
        "ceo", "coo", "ciso", "cto",
    ],
    "coo": [
        "ceo", "cfo", "head_of_it", "head_of_engineering",
    ],
    "cro": [
        "ceo", "cco", "head_of_sales", "cfo",
    ],
    "cco": [
        "ceo", "cro", "head_of_sales", "cfo",
    ],
    "head_of_cyber": [
        "ciso", "cto", "cio",
        "head_of_security", "head_of_soc", "head_of_risk",
    ],
    "head_of_security": [
        "ciso", "cto", "cio",
        "head_of_cyber", "head_of_soc", "head_of_it",
    ],
    "head_of_soc": [
        "ciso", "head_of_cyber", "head_of_security", "head_of_it",
    ],
    "head_of_risk": [
        "ciso", "head_of_compliance", "head_of_security", "cfo",
    ],
    "head_of_compliance": [
        "ciso", "head_of_risk", "head_of_security", "cfo",
    ],
    "head_of_it": [
        "ciso", "cio", "cto",
        "head_of_security", "head_of_engineering",
    ],
    "head_of_engineering": [
        "cto", "head_of_it",
        "head_of_security",
    ],
    "head_of_sales": [
        "ceo", "cro", "cco", "cfo",
    ],
}


def _pattern_matches(pattern: str, text: str) -> bool:
    """Match a pattern against text with word-boundary semantics.

    Critical for short acronyms — a naive substring match for `cto` against
    `Director of Information Security` matches because "Director" contains
    "ctor" → contains "cto". Word boundaries fix this:
        \bcto\b  matches `CTO` and `CTO (Engineering)` but NOT `Director`.
    """
    return re.search(rf"\b{re.escape(pattern)}\b", text) is not None


def detect_role_family(title: str) -> Optional[str]:
    """Map a free-text title to a canonical role family.

    Word-boundary regex matching against ROLE_FAMILY_PATTERNS (lowercased).
    Returns the first matching family, or None if no pattern matches. Order
    of keys in ROLE_FAMILY_PATTERNS dict and order of patterns within each
    family matters — more specific patterns (longer phrases) appear first.
    """
    if not title or not isinstance(title, str):
        return None
    normalised = title.lower().strip()
    if not normalised:
        return None

    for family, patterns in ROLE_FAMILY_PATTERNS.items():
        for pattern in patterns:
            if _pattern_matches(pattern, normalised):
                return family
    return None


def find_others_at_company(
    db: Client,
    org_id: str,
    company_domain: str,
    company_name: str = "",
    source_lead_title: str = "",
    exclude_names: Optional[List[str]] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Find other decision-makers at a specific company who match the org's
    ICP or sit *adjacent* to the source lead's role (never duplicates of the
    same role, never just "anyone senior").

    Search strategy (Apollo person_titles, same shape as the Vera Brain's
    _exec_find_contacts_at_company — verified live):
      1. Titled search by domain: person_titles = adjacency expansion of the
         source lead's role family (for a CISO: CIO, CTO, CFO, Head of
         Cyber/Security/SOC/Risk/Compliance/IT) + the org's ICP
         target_titles. Falls back to DEFAULT_MULTITHREAD_TITLES when both
         are empty.
      2. Same titled search by company name (Apollo sometimes indexes a
         different vanity domain).
      3. Last resort: broad seniority-only search — but those results are
         kept ONLY if their detected role family is relevant (adjacent to
         the source or matching a searched title's family). This is what
         stops a 3,000-person bank returning market presidents.

    Filters applied:
      - Excludes people already in the org's pool at this domain
      - Excludes candidates whose ROLE FAMILY matches the source lead's
        (e.g. if source is CISO, don't surface other CISOs — they're
        duplicates not multithread targets)

    Ranking applied:
      - Candidates whose role family is "adjacent" to the source lead's
        (per ADJACENT_ROLE_FAMILIES) surface first, in adjacency-list order
        (CIO before Head of IT for a CISO source). Everyone else after, in
        Apollo's natural order.

    Returns nothing rather than junk — an empty list renders the honest
    "no other matches" state in the dashboard.

    Returns a structured candidate list — does NOT write to the leads
    table. Use add_as_multithread_suggestions() to persist a chosen
    subset.
    """
    if not settings.APOLLO_API_KEY:
        logger.warning("APOLLO_API_KEY not set — skipping multithread search")
        return []

    # Existing leads at this domain — exclude their names from results
    existing_resp = (
        db.table("leads")
        .select("first_name, last_name")
        .eq("org_id", org_id)
        .eq("company_domain", company_domain)
        .execute()
    )
    existing_names = {
        f"{l.get('first_name', '')} {l.get('last_name', '')}".lower().strip()
        for l in (existing_resp.data or [])
    }
    if exclude_names:
        existing_names.update(n.lower().strip() for n in exclude_names if n)

    # Org ICP titles — part of the titled search alongside the source
    # lead's adjacent role families.
    target_titles: List[str] = []
    org_resp = (
        db.table("organisations")
        .select("icp_config")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    if org_resp.data:
        icp = org_resp.data[0].get("icp_config") or {}
        target_titles = [
            t for t in (icp.get("target_titles") or [])
            if isinstance(t, str) and t.strip()
        ]

    # Detect the source lead's role family — drives the adjacency expansion,
    # the duplicate exclusion, and the ranking.
    source_family = detect_role_family(source_lead_title)
    adjacent_families: List[str] = (
        ADJACENT_ROLE_FAMILIES.get(source_family, []) if source_family else []
    )

    search_titles: List[str] = []
    for family in adjacent_families:
        search_titles.extend(FAMILY_TITLE_QUERIES.get(family, []))
    search_titles.extend(target_titles)
    if not search_titles:
        search_titles = list(DEFAULT_MULTITHREAD_TITLES)
    seen_titles = set()
    search_titles = [
        t for t in search_titles
        if not (t.lower() in seen_titles or seen_titles.add(t.lower()))
    ]

    # Families we accept from the broad seniority fallback: anything adjacent
    # to the source, or anything we'd have searched for by title.
    relevant_families = set(adjacent_families)
    for t in search_titles:
        family = detect_role_family(t)
        if family:
            relevant_families.add(family)

    # mixed_people/api_search requires the API key in the X-Api-Key
    # header, not the body — body `api_key` is silently ignored on this
    # endpoint and the call returns total_entries:null with people:[]
    # despite HTTP 200. (The older mixed_people/search accepts the body
    # form, which is why this looked like an endpoint bug at first.)
    headers = {"X-Api-Key": settings.APOLLO_API_KEY}

    # Fetch more than we show — dedupe and family filtering below can shrink
    # the list, and Apollo search is free.
    per_page = max(limit, 25)

    people: List[Dict[str, Any]] = []
    used_broad_fallback = False
    try:
        # 9s per request keeps total worst-case budget (titled + name
        # fallback + broad fallback) under the frontend's 30s
        # AbortController timeout even on Apollo slow-days.
        with httpx.Client(timeout=9) as client:

            def _search(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
                resp = client.post(
                    "https://api.apollo.io/api/v1/mixed_people/api_search",
                    json=payload,
                    headers=headers,
                )
                if not resp.is_success:
                    logger.warning(
                        "Apollo multithread search returned HTTP %d: %s",
                        resp.status_code, resp.text[:200],
                    )
                    return []
                return (resp.json() or {}).get("people") or []

            people = _search({
                "person_titles": search_titles,
                "q_organization_domains_list": [company_domain],
                "page": 1,
                "per_page": per_page,
            })

            # Fallback by org name if domain returned no people (e.g. the
            # org's domain is a different vanity URL than Apollo indexed).
            if not people and company_name:
                people = _search({
                    "person_titles": search_titles,
                    "q_organization_name": company_name,
                    "page": 1,
                    "per_page": per_page,
                })

            # Last resort: seniority-only search. Results are filtered to
            # relevant role families below — never surfaced raw.
            if not people:
                used_broad_fallback = True
                people = _search({
                    "person_seniorities": DECISION_MAKER_SENIORITIES,
                    "q_organization_domains_list": [company_domain],
                    "page": 1,
                    "per_page": per_page,
                })
    except Exception as e:
        logger.warning("Apollo multithread search exception: %s", e)
        return []

    candidates: List[Dict[str, Any]] = []
    for p in people:
        full_name = f"{p.get('first_name', '')} {p.get('last_name', '')}".lower().strip()
        if full_name in existing_names:
            continue

        title = p.get("title", "") or ""
        candidate_family = detect_role_family(title)

        # Drop candidates with the same role family as the source lead.
        # Multithread is for buying-committee expansion, not finding
        # duplicate-role people at the same company.
        if source_family and candidate_family == source_family:
            continue

        # Broad-fallback results carry no title guarantee — only keep the
        # ones whose role family is actually relevant to this search.
        if used_broad_fallback and (
            not candidate_family or candidate_family not in relevant_families
        ):
            continue

        org = p.get("organization") or {}
        candidates.append({
            "first_name": p.get("first_name", "") or "",
            "last_name": p.get("last_name", "") or "",
            "title": title,
            "email": p.get("email", "") or "",
            "linkedin_url": p.get("linkedin_url", "") or "",
            "company_name": org.get("name") or company_name or "",
            "company_domain": org.get("primary_domain") or company_domain or "",
            "apollo_id": p.get("id", "") or "",
            # Internal field used for ranking. Frontend can ignore.
            "_role_family": candidate_family,
            "_is_adjacent": (candidate_family in adjacent_families) if candidate_family else False,
        })

    # Rank: adjacent-family candidates first, in adjacency-list order (CIO
    # before Head of IT for a CISO source). Others after, preserving
    # Apollo's natural order (which usually approximates seniority).
    if adjacent_families:
        adjacency_rank = {family: i for i, family in enumerate(adjacent_families)}
        candidates.sort(
            key=lambda c: (0, adjacency_rank[c["_role_family"]])
            if c.get("_role_family") in adjacency_rank
            else (1, 0)
        )

    return candidates[:limit]


def add_as_multithread_suggestions(
    db: Client,
    org_id: str,
    candidates: List[Dict[str, Any]],
    source_lead_id: Optional[str] = None,
    source_company_name: str = "",
) -> Dict[str, Any]:
    """Persist multithread candidates as new suggested leads, tagged with
    `suggestion_reason='multithread_at_<company>'`. Dedups by email +
    linkedin_url against existing leads in the org.

    Returns: {stored: int, duplicates: int, lead_ids: list[str]}.
    """
    reason = (
        f"multithread_at_{source_company_name}"
        if source_company_name
        else "multithread"
    )

    stored: List[str] = []
    duplicates = 0

    for c in candidates:
        email = (c.get("email") or "").strip()
        linkedin = (c.get("linkedin_url") or "").strip()

        # Dedup by email
        if email:
            existing = (
                db.table("leads")
                .select("id")
                .eq("org_id", org_id)
                .eq("email", email)
                .execute()
            )
            if existing.data:
                duplicates += 1
                continue

        # Dedup by linkedin_url
        if linkedin:
            existing = (
                db.table("leads")
                .select("id")
                .eq("org_id", org_id)
                .eq("linkedin_url", linkedin)
                .execute()
            )
            if existing.data:
                duplicates += 1
                continue

        row = {
            "org_id": org_id,
            "email": email,
            "first_name": c.get("first_name", ""),
            "last_name": c.get("last_name", ""),
            "title": c.get("title", ""),
            "company_name": c.get("company_name", ""),
            "company_domain": c.get("company_domain", ""),
            "linkedin_url": linkedin,
            "apollo_id": c.get("apollo_id", ""),
            "source": "multithread",
            "enrichment_status": "suggested",
            "status": "new",
            "suggestion_reason": reason,
        }
        try:
            insert_resp = db.table("leads").insert(row).execute()
            if insert_resp.data:
                stored.append(insert_resp.data[0]["id"])
        except Exception as e:
            logger.warning("Failed to store multithread suggestion: %s", e)

    if stored and source_lead_id:
        log_activity(db, org_id, source_lead_id, "multithread_suggestions_added", {
            "count": len(stored),
            "company": source_company_name,
            "lead_ids": stored,
        })

    return {
        "stored": len(stored),
        "duplicates": duplicates,
        "lead_ids": stored,
    }
