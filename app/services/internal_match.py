"""Internal-first account matching — recommend from the companies we've
ALREADY enriched before spending an Apollo call.

The `companies` table is global per-domain (public company facts: industry,
size, HQ, signals, profile score). For a new Solo org we can match their
ICP straight against it: free, instant, recruiter-free (the stored industry
is a clean canonical taxonomy, not Apollo's fuzzy keyword tags), and often
already deeply researched. Apollo stays as the cold-start fallback for ICPs
the pool doesn't cover yet.

Tenant isolation: this reads `companies` (shared facts) only — never
`watchlist` — so it can't leak which org watches which account.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from supabase import Client

logger = logging.getLogger(__name__)

# Wizard industry label → the canonical stored `companies.industry` labels
# (LinkedIn/Apollo taxonomy as it actually appears in our DB). Selecting
# only these by construction excludes 'staffing & recruiting' and other
# noise — recruiters can't appear unless explicitly mapped (they aren't).
INDUSTRY_MAP: Dict[str, List[str]] = {
    "Financial services": [
        "financial services", "banking", "capital markets",
        "investment banking", "investment management",
        "venture capital & private equity",
    ],
    "Insurance": ["insurance"],
    "Healthcare": [
        "hospital & health care", "health, wellness & fitness",
        "medical devices", "biotechnology", "pharmaceuticals",
    ],
    "Retail / e-commerce": ["retail", "consumer services", "luxury goods & jewelry"],
    "Energy / utilities": ["oil & energy", "utilities", "renewables & environment"],
    "Public sector / government": ["government administration", "civic & social organization"],
    "Software / SaaS": [
        "information technology & services", "internet",
        "information services", "computer games",
    ],
    "Manufacturing": [
        "machinery", "electrical/electronic manufacturing",
        "automotive", "chemicals",
    ],
    "Legal": ["law practice", "legal services"],
    "Education": [
        "higher education", "education management",
        "primary/secondary education", "e-learning",
    ],
}

# Wizard geography → lowercase substrings to match against `hq_country`
# (which is messy: "United Kingdom" / "London, England, United Kingdom").
GEO_PATTERNS: Dict[str, List[str]] = {
    "UK": ["united kingdom", "england", "scotland", "wales", "northern ireland"],
    "United States": ["united states", " usa"],
    "Canada": ["canada"],
    "Europe (EU)": [
        "germany", "france", "netherlands", "ireland", "spain", "italy",
        "sweden", "belgium", "poland", "denmark", "finland", "portugal",
        "austria", "luxembourg",
    ],
    "APAC": [
        "singapore", "australia", "india", "japan", "hong kong", "china",
        "new zealand", "malaysia", "korea",
    ],
    "Middle East": [
        "united arab emirates", "saudi", "israel", "qatar", "dubai",
        "abu dhabi", "bahrain", "kuwait",
    ],
    "Global": [],
}


# Security / cyber industries are NEVER suggested as targets. Vera's
# customers ARE cyber vendors — MSSPs and other security companies are
# peers/competitors, not buyers (generalises an early client's "never
# suggest cybersec companies as prospects" rule). Hard exclusion regardless of ICP.
SECURITY_EXCLUDE = {"computer & network security", "security & investigations"}


def _stored_industries(wizard_industries: List[str]) -> List[str]:
    """Map ICP industry labels to the canonical `companies.industry` values.

    Tolerant of short tokens: the auto-ICP often stores 'government' rather than
    the full wizard label 'Public sector / government', so an exact-key miss
    falls back to a token match against the map's keys and values before giving
    up. Without this, 'government' matched no stored industry and the caller
    fell through to a fuzzy vendor keyword search (returning Shell / Barclays)."""
    out: set[str] = set()
    for w in wizard_industries or []:
        wl = str(w).strip().lower()
        if not wl:
            continue
        # 1. Exact wizard-label key (case-insensitive).
        exact = next((v for k, v in INDUSTRY_MAP.items() if k.lower() == wl), None)
        if exact:
            out.update(exact)
            continue
        # 2. Token match — the ICP token appears in a full label or one of its
        #    canonical values. Guarded to >=4 chars so tiny tokens can't over-match.
        if len(wl) >= 4:
            token_hits = [
                v for k, vals in INDUSTRY_MAP.items() for v in [vals]
                if wl in k.lower() or any(wl in cv for cv in vals)
            ]
            if token_hits:
                for vals in token_hits:
                    out.update(vals)
                continue
        # 3. Unknown — match the raw token directly (legacy behaviour).
        out.add(wl)
    return [s for s in out if s]


def canonical_industries(industries: List[str]) -> List[str]:
    """Public wrapper — canonical `companies.industry` labels for an ICP's
    industries. Reused for the vendor fallback so it filters on precise sector
    labels ('government administration') instead of a fuzzy word ('government')."""
    return _stored_industries(industries)


def _parse_size(size_band: Any) -> Optional[int]:
    """First integer in a stored size_band ('340', '4000', '1001,5000')."""
    if size_band is None:
        return None
    m = re.search(r"\d+", str(size_band))
    return int(m.group()) if m else None


def _size_bands(employee_ranges: List[str]) -> List[tuple[int, int]]:
    """Parse normalised 'min,max' (or 'min-max') ranges into int tuples.
    The top band is treated as open-ended."""
    bands: List[tuple[int, int]] = []
    for r in employee_ranges or []:
        nums = re.findall(r"\d+", str(r))
        if not nums:
            continue
        lo = int(nums[0])
        hi = int(nums[1]) if len(nums) > 1 else lo
        if hi >= 100000:  # "10000-100000" → 10k+, don't exclude the giants
            hi = 10**9
        bands.append((lo, hi))
    return bands


def _size_match(size_band: Any, bands: List[tuple[int, int]]) -> bool:
    if not bands:
        return True  # no size filter selected
    n = _parse_size(size_band)
    if n is None:
        return True  # unparseable → don't drop on size alone
    return any(lo <= n <= hi for lo, hi in bands)


def _geo_match(hq_country: Any, wizard_geos: List[str]) -> bool:
    if not wizard_geos or "Global" in wizard_geos:
        return True
    patterns: List[str] = []
    for g in wizard_geos:
        mapped = GEO_PATTERNS.get(g)
        if mapped:
            patterns.extend(mapped)
        elif str(g).strip():
            # The ICP often stores the raw location ("United Kingdom") rather
            # than the wizard key ("UK"). Match the string itself so an unmapped
            # geo doesn't silently disable the filter — that key-miss let a US
            # company through a UK-only ICP.
            patterns.append(str(g).strip().lower())
    if not patterns:
        return True  # truly nothing to match on → don't drop on geo
    hq = (str(hq_country or "")).lower()
    return any(p in hq for p in patterns)


def _profile_composite(profile_score: Any) -> float:
    if isinstance(profile_score, dict):
        v = profile_score.get("composite")
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def search_internal_companies(
    db: Client,
    *,
    industries: List[str],
    locations: List[str],
    employee_ranges: List[str],
    exclude_domains: set[str],
    limit: int,
) -> List[Dict[str, Any]]:
    """Match the org's ICP against companies we've already enriched.

    Returns shaped rows ready to seed as suggestions, ranked so the
    already-researched ones (have a profile score) come first — those give
    a brand-new user instant value with no enrichment wait.
    """
    stored = _stored_industries(industries)
    if not stored:
        return []

    try:
        rows = (
            db.table("companies")
            .select("domain, name, industry, size_band, hq_country, profile_score")
            .in_("industry", stored)
            .limit(400)
            .execute()
            .data
        ) or []
    except Exception as e:
        logger.warning("internal company search failed: %s", e)
        return []

    bands = _size_bands(employee_ranges)
    matched: List[Dict[str, Any]] = []
    for r in rows:
        dom = (r.get("domain") or "").strip().lower()
        if not dom or dom in exclude_domains:
            continue
        # Never recommend a security/cyber company as a target.
        if str(r.get("industry") or "").strip().lower() in SECURITY_EXCLUDE:
            continue
        if not _geo_match(r.get("hq_country"), locations):
            continue
        if not _size_match(r.get("size_band"), bands):
            continue
        has_profile = isinstance(r.get("profile_score"), dict) and bool(r["profile_score"])
        reasons = [str(r.get("industry"))] if r.get("industry") else []
        if has_profile:
            reasons.append("Already researched by Vera")
        matched.append({
            "domain": dom,
            "name": (r.get("name") or "").strip(),
            "industry": r.get("industry"),
            "size_band": r.get("size_band"),
            "hq_country": r.get("hq_country"),
            "matched_reasons": reasons,
            "_has_profile": has_profile,
            "_score": _profile_composite(r.get("profile_score")),
        })

    # Already-researched first, then by profile composite.
    matched.sort(key=lambda m: (m["_has_profile"], m["_score"]), reverse=True)
    # Drop any without a usable name now that ranking is done.
    return [m for m in matched if m["name"]][:limit]
