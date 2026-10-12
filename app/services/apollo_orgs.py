"""Apollo mixed_companies search wrapper.

Used by the suggestions topup flow to pull candidate companies matching
a client's ICP (target_industries, target_locations, target_company_size).
Returns a small list of normalised candidate rows the router can insert
straight into the watchlist table with status='pending'.

Distinct from `enrichers.tech_stack._fetch_apollo_org_data` which enriches
a known domain. This one DISCOVERS new companies from criteria.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30
_APOLLO_URL = "https://api.apollo.io/api/v1/mixed_companies/search"


def _merge_buckets(payload: Any) -> List[Dict[str, Any]]:
    """Merge BOTH mixed_companies/search response buckets, deduped by domain.

    `organizations` = net-new companies (id = org id, primary_domain);
    `accounts` = companies already saved in the Apollo workspace (their
    `id` is an ACCOUNT id — the org id lives in organization_id, and the
    domain field is `domain`). Reading one bucket OR the other (the old
    behaviour) collapsed results to single digits because our workspace
    holds thousands of saved accounts, so page 1 lands mostly in the
    accounts bucket. Found via a 2026-08-19 bug report; same fix
    already live in the market radar and the suggest-accounts edge fn.
    """
    if not isinstance(payload, dict):
        return []
    merged: List[Dict[str, Any]] = []
    seen: set = set()

    def _key(row: Dict[str, Any]) -> str:
        dom = str(row.get("primary_domain") or row.get("domain") or row.get("website_url") or "")
        return dom.replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "").lower()

    for o in payload.get("organizations") or []:
        if isinstance(o, dict):
            k = _key(o)
            if k and k not in seen:
                seen.add(k)
                merged.append(o)
    for a in payload.get("accounts") or []:
        if isinstance(a, dict):
            k = _key(a)
            if k and k not in seen:
                seen.add(k)
                # Normalise: expose the ORG id under `id` and the domain
                # under `primary_domain` so downstream mapping is uniform.
                merged.append({**a, "id": a.get("organization_id"), "primary_domain": a.get("domain") or a.get("primary_domain")})
    return merged


def search_by_name(query: str, limit: int = 8) -> List[Dict[str, Any]]:
    """Typeahead resolver: fuzzy-match a typed company name to real orgs.

    Powers the onboarding "add your accounts" picker. The user types "Monzo"
    (or "Monzoo", or "HSBC"), Apollo fuzzy-matches, and we hand back real
    candidates with logo + domain so they PICK the right company rather than
    us guessing a domain from a possibly-misspelt string. Robust to typos
    and name/domain mismatches by construction — the match comes from Apollo,
    the choice from the user.
    """
    q = (query or "").strip()
    if not q or not settings.APOLLO_API_KEY:
        return []
    body = {"q_organization_name": q, "page": 1, "per_page": max(1, min(limit, 10))}
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            res = client.post(_APOLLO_URL, json=body, headers={"X-Api-Key": settings.APOLLO_API_KEY})
            res.raise_for_status()
            orgs = _merge_buckets(res.json())
    except Exception as e:  # noqa: BLE001
        logger.warning("Apollo name search failed for %r: %s", q, e)
        return []

    out: List[Dict[str, Any]] = []
    for o in orgs:
        if not isinstance(o, dict):
            continue
        domain = (o.get("primary_domain") or o.get("website_url") or "").strip()
        if "://" in domain:
            domain = domain.split("://", 1)[1]
        if "/" in domain:
            domain = domain.split("/", 1)[0]
        domain = domain.lower().strip()
        name = (o.get("name") or "").strip()
        if not domain or "." not in domain or not name:
            continue
        out.append({
            "name": name,
            "domain": domain,
            "logo_url": o.get("logo_url"),
            "industry": o.get("industry"),
            "hq_country": o.get("organization_country") or o.get("country"),
        })
    return out


def search_companies(
    *,
    industries: Optional[List[str]] = None,
    locations: Optional[List[str]] = None,
    employee_ranges: Optional[List[str]] = None,
    per_page: int = 25,
    page: int = 1,
) -> List[Dict[str, Any]]:
    """Search Apollo for orgs matching the given ICP criteria.

    Returns a list of dicts shaped for the suggestions table:
      {domain, name, industry, hq_country, hq_city, size_band, matched_reasons}
    where matched_reasons is a list[str] of chip-level "why suggested"
    tokens — e.g. ["industry: healthcare", "size: 1000-5000", "UK"].

    Empty list on auth failure or zero results — caller decides whether
    to log/alert.
    """
    if not settings.APOLLO_API_KEY:
        logger.warning("Apollo search skipped — APOLLO_API_KEY not set")
        return []

    body: Dict[str, Any] = {
        "per_page": per_page,
        "page": page,
    }
    if industries:
        body["q_organization_keyword_tags"] = industries
    if locations:
        body["organization_locations"] = locations
    if employee_ranges:
        body["organization_num_employees_ranges"] = employee_ranges

    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            res = client.post(
                _APOLLO_URL,
                json=body,
                headers={"X-Api-Key": settings.APOLLO_API_KEY},
            )
            res.raise_for_status()
            payload = res.json()
    except Exception as e:
        logger.warning("Apollo mixed_companies/search failed: %s", e)
        return []

    orgs = _merge_buckets(payload)
    if not orgs:
        return []

    out: List[Dict[str, Any]] = []
    for o in orgs:
        if not isinstance(o, dict):
            continue
        domain = (o.get("primary_domain") or o.get("website_url") or "").strip()
        if "://" in domain:
            domain = domain.split("://", 1)[1]
        if "/" in domain:
            domain = domain.split("/", 1)[0]
        domain = domain.lower().strip()
        if not domain or "." not in domain:
            continue

        name = (o.get("name") or "").strip()
        if not name:
            continue

        # Hard-drop employment / staffing agencies. They carry the same
        # industry keyword tags as the sectors they recruit FOR (an FS
        # recruiter tags 'financial services'), so they leak into results
        # for every sector and aren't buyers. SIC 736x = employment
        # agencies / help-supply; NAICS 5613 = employment services. A real
        # operating company in the target sector won't carry these codes.
        if _is_staffing(o.get("sic_codes"), o.get("naics_codes")):
            logger.info("Apollo search: dropped staffing/recruitment org '%s'", name)
            continue

        # Build chip-level "why suggested" tokens from fields the search
        # response actually returns. Apollo's mixed_companies/search
        # response doesn't include flat industry / country / employee
        # count — it returns richer per-company facts instead:
        # organization_revenue_printed, headcount growth rates,
        # owned_by_organization, founded_year, sic_codes / naics_codes.
        # Use these to populate per-card chips that say something
        # concrete about THIS company, rather than echoing back the
        # filter values which would be identical for every card.
        reasons: List[str] = []

        revenue_printed = o.get("organization_revenue_printed")
        if isinstance(revenue_printed, str) and revenue_printed:
            reasons.append(f"Revenue ${revenue_printed}")

        growth_12m = o.get("organization_headcount_twelve_month_growth")
        if isinstance(growth_12m, (int, float)) and growth_12m > 0.05:
            # Only surface positive growth — declining headcount isn't
            # an opener. Round to nearest percent for readability.
            reasons.append(f"+{round(growth_12m * 100)}% headcount (12m)")

        owned_by = o.get("owned_by_organization")
        if isinstance(owned_by, dict):
            parent = owned_by.get("name")
            if parent:
                reasons.append(f"Subsidiary of {parent}")

        founded = o.get("founded_year")
        if isinstance(founded, int) and founded >= 2020:
            reasons.append(f"Founded {founded}")

        # Industry from SIC code — rough bucket, only the major
        # 2-digit category. Better than nothing when we have no
        # other concrete fact.
        if not any("Revenue" in r or "headcount" in r for r in reasons):
            sic_codes = o.get("sic_codes")
            if isinstance(sic_codes, list) and sic_codes:
                bucket = _sic_bucket(str(sic_codes[0]))
                if bucket:
                    reasons.append(bucket)

        out.append(
            {
                "domain": domain,
                "name": name,
                "matched_reasons": reasons[:4],
            }
        )
    return out


# Coarse SIC code bucketing for the rare org that has no revenue
# / growth / parent data. Just the 2-digit major industry — better
# than an empty card. Only the SIC buckets relevant to the original
# pilot ICP are mapped; others fall through to None.
_SIC_BUCKETS: Dict[str, str] = {
    "60": "Banking",
    "61": "Financial services",
    "62": "Financial services",
    "63": "Insurance",
    "64": "Insurance",
    "65": "Real estate",
    "80": "Healthcare",
    "28": "Pharmaceutical",
    "49": "Utilities / energy",
    "47": "Government",
    "37": "Defense",
}


def _sic_bucket(sic_code: str) -> Optional[str]:
    if not sic_code or len(sic_code) < 2:
        return None
    return _SIC_BUCKETS.get(sic_code[:2])


# Employment / staffing classification codes. These are the recruiters
# that match a sector's keyword tags without being buyers in it.
#   SIC 7361 (employment agencies), 7363 (help supply services) → "736"
#   NAICS 5613 (employment services: placement, exec search, temp staffing)
def _is_staffing(sic_codes: Any, naics_codes: Any) -> bool:
    sics = [str(s) for s in (sic_codes or []) if s]
    naics = [str(n) for n in (naics_codes or []) if n]
    if any(s.startswith("736") for s in sics):
        return True
    if any(n.startswith("5613") for n in naics):
        return True
    return False


