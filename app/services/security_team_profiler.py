"""Per-account security team headcount profiler.

For each watched account, run one Apollo `mixed_people` search across a
union of security-related titles, scoped to the company's domain. The
returned people are bucketed by role (Leadership / Engineering / SOC-
Ops / GRC / IAM), compared to a size+industry baseline, and the result
persisted to `companies.security_team_profile`.

This is the strongest single input to the Maturity axis of the account
Profile score (cyber lens v1) — a company with ten
security people split sensibly across leadership, engineering, SOC and
GRC reads very differently from one with zero or just a CISO.

Reveal-free by design (spec 2026-06-09): we use Apollo's search
endpoint, which returns counts + titles + linkedin_urls inside the
search response with NO credit charge. We never call `/people/match`
or any reveal endpoint, so no names get added to anyone's billing.

Refresh cadence: 30 days. Security teams don't churn weekly; refresh
on first enrichment + monthly thereafter.

Cost: 0 Apollo credits (search-only), per account per refresh.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from supabase import Client

from app.config import settings

logger = logging.getLogger(__name__)

_APOLLO_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
_TIMEOUT = 30
_REFRESH_AFTER_DAYS = 30
_MAX_PAGE_SIZE = 100  # Apollo cap; one page is plenty for headcount


# ---------------------------------------------------------------------------
# Title universe — what we ask Apollo for.
#
# Wide enough to catch real security people across all role buckets, narrow
# enough that we don't pull in "Sales Engineer" or "DevOps Engineer" by
# accident. The bucketing regex (below) is what assigns role; this list
# is just the search filter.
#
# Rule: cover SaaS + MSSP + consulting + advisory + VC — so titles
# include both employed-at-org and consulting variants ("Security
# Consultant" can apply to either).
# ---------------------------------------------------------------------------
SECURITY_TITLES: List[str] = [
    # Leadership
    "CISO", "Chief Information Security Officer", "Chief Security Officer",
    "VP Security", "VP of Security", "VP Information Security",
    "Head of Security", "Head of Information Security", "Head of Cyber",
    "Director of Security", "Director of Information Security",
    "Director of Cyber Security", "Director of Cybersecurity",
    # Engineering
    "Security Engineer", "Senior Security Engineer", "Staff Security Engineer",
    "Principal Security Engineer", "Application Security Engineer",
    "AppSec Engineer", "Product Security Engineer", "Cloud Security Engineer",
    "Infrastructure Security Engineer", "Security Software Engineer",
    "Security Architect", "Security Developer",
    # Operations / SOC
    "SOC Analyst", "Security Operations Analyst", "SOC Engineer",
    "Security Operations Engineer", "Incident Response Engineer",
    "Incident Response Analyst", "Threat Hunter", "Threat Intelligence Analyst",
    "Detection Engineer", "SIEM Engineer", "Security Analyst",
    # GRC / Compliance
    "Security Compliance Manager", "GRC Analyst", "GRC Manager",
    "Risk Manager", "Information Security Risk Manager",
    "Security Audit Manager", "Compliance Manager",
    "Information Security Compliance Analyst", "Privacy Officer",
    "Data Protection Officer", "DPO",
    # Identity / IAM
    "Identity and Access Management Engineer", "IAM Engineer",
    "Identity Engineer", "IAM Architect", "PAM Engineer",
    "Access Governance Manager", "Identity Manager",
]


# ---------------------------------------------------------------------------
# Bucketing rules — title → role bucket.
#
# First-match wins, evaluated in declaration order. Most-specific
# patterns first; generic catch-alls last.
# ---------------------------------------------------------------------------
BUCKET_LEADERSHIP = "leadership"
BUCKET_ENGINEERING = "engineering"
BUCKET_SOC_OPS = "soc_ops"
BUCKET_GRC = "grc"
BUCKET_IAM = "iam"

ALL_BUCKETS: List[str] = [
    BUCKET_LEADERSHIP, BUCKET_ENGINEERING, BUCKET_SOC_OPS,
    BUCKET_GRC, BUCKET_IAM,
]

# (regex, bucket) — case-insensitive. ORDER MATTERS.
_BUCKET_RULES: List[Tuple[re.Pattern, str]] = [
    # IAM first — "Identity Engineer" should land here, not Engineering
    (re.compile(r"\b(iam|identity|pam|privileged access|access governance)\b", re.I),
     BUCKET_IAM),
    # GRC — risk / compliance / audit / privacy
    (re.compile(r"\b(grc|compliance|risk|audit|privacy|dpo|data protection officer)\b", re.I),
     BUCKET_GRC),
    # Leadership — CISO/CSO/VP/Head/Director of security
    (re.compile(
        r"\b(ciso|cso|chief (information )?security|vp.* security|head of (information )?security|head of cyber|director of (information )?(cyber )?security|director of cybersecurity)\b",
        re.I,
    ), BUCKET_LEADERSHIP),
    # SOC / Ops — detection, incident response, threat
    (re.compile(
        r"\b(soc|security operations|incident response|threat hunt|threat intelligence|detection engineer|siem|security analyst)\b",
        re.I,
    ), BUCKET_SOC_OPS),
    # Engineering catch-all — anything with "security" + an engineering role
    (re.compile(
        r"\b(security|appsec|application security|product security|cloud security|infrastructure security) (engineer|developer|architect|software)\b",
        re.I,
    ), BUCKET_ENGINEERING),
    # Loose engineering catch-all for shorter titles
    (re.compile(r"\bsecurity (engineer|developer|architect)\b", re.I), BUCKET_ENGINEERING),
]


def _bucket_for_title(title: str) -> Optional[str]:
    """Return the role bucket for a title, or None if none matches.

    We deliberately return None for titles that didn't match any rule
    (e.g. "Sales Engineer" if Apollo over-fired) — None entries are
    discarded so they don't bloat the headcount.
    """
    if not title:
        return None
    for pattern, bucket in _BUCKET_RULES:
        if pattern.search(title):
            return bucket
    return None


# ---------------------------------------------------------------------------
# Size + industry baseline.
#
# What does a "normal" security team look like at a company of size X in
# industry Y? These are baseline EXPECTATIONS — under-band reads as
# under-invested (low maturity), in-band reads as proportionate, over-
# band reads as security-driven (high maturity).
#
# Regulated industries (finance, healthcare, energy, defence, gov) get
# a ~30% baseline bump because their compliance + audit needs structurally
# require more GRC + leadership headcount.
# ---------------------------------------------------------------------------
_BASE_BAND_BY_SIZE: List[Tuple[int, Tuple[int, int]]] = [
    # (upper_employee_count_bound, (expected_min, expected_max))
    (50, (0, 1)),         # <50 employees: 0-1 security people is normal
    (200, (1, 3)),        # 50-200: a CISO and maybe 1-2 engineers
    (1_000, (3, 8)),      # 200-1000: leadership + small specialised team
    (5_000, (8, 25)),     # 1000-5000: full security org
    (20_000, (25, 80)),   # 5000-20000: dedicated sub-functions
    (10_000_000, (80, 400)),  # 20000+: enterprise-scale security org
]

_REGULATED_INDUSTRY_KEYWORDS: List[str] = [
    "bank", "banking", "financial services", "insurance",
    "healthcare", "hospital", "medical", "pharmaceutical", "pharma",
    "energy", "utilities", "oil", "gas", "nuclear",
    "defense", "defence", "military", "aerospace",
    "government", "public sector", "federal",
]


def _is_regulated_industry(industry: Optional[str]) -> bool:
    if not industry:
        return False
    lower = industry.lower()
    return any(kw in lower for kw in _REGULATED_INDUSTRY_KEYWORDS)


def _expected_band(
    employee_count: Optional[int], industry: Optional[str],
) -> Tuple[int, int]:
    """Return (expected_min, expected_max) headcount for this company.

    Defaults to the smallest band when employee_count is missing — better
    to under-promise than over-claim a company is below baseline based
    on no data.
    """
    n = employee_count or 0
    for upper, band in _BASE_BAND_BY_SIZE:
        if n <= upper:
            lo, hi = band
            break
    else:
        lo, hi = _BASE_BAND_BY_SIZE[-1][1]
    if _is_regulated_industry(industry):
        # Bump by ~30% on both ends.
        lo = int(lo * 1.3)
        hi = int(hi * 1.3)
    return (lo, hi)


# ---------------------------------------------------------------------------
# Score 0-50 (Maturity contribution).
#
# Sliding read against the expected band:
#   total = 0                          → 0     (zero security presence)
#   total < expected_min               → linear ramp 5 → 25 across the gap
#   expected_min <= total <= expected_max → 30-40 (in-band)
#   total > expected_max               → ramp 40 → 50 cap (security-driven over-investment)
# ---------------------------------------------------------------------------
def _score_for_total(total: int, band: Tuple[int, int]) -> int:
    lo, hi = band
    if total == 0:
        return 0
    if total < lo:
        if lo <= 0:
            return 30
        ratio = total / lo  # 0..1
        return int(5 + ratio * 20)  # 5 .. 25
    if total <= hi:
        # In-band — scale from 30 (at lo) to 40 (at hi). Even hi gets 40,
        # not 50 — 50 is reserved for over-band investment.
        span = max(1, hi - lo)
        ratio = (total - lo) / span  # 0..1
        return int(30 + ratio * 10)  # 30 .. 40
    # Over-band
    overshoot = total - hi
    headroom = max(1, hi)
    ratio = min(1.0, overshoot / headroom)
    return int(40 + ratio * 10)  # 40 .. 50


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def refresh_security_team_profile(
    db: Client,
    *,
    company_id: str,
    force: bool = False,
) -> Optional[Dict[str, Any]]:
    """Refresh `companies.security_team_profile` for one company.

    Returns the persisted profile dict, or None on hard failure
    (Apollo unreachable + no cached data). Soft failures (Apollo
    returned nothing) keep whatever's currently cached.
    """
    company = _fetch_company(db, company_id)
    if not company:
        logger.warning(
            "refresh_security_team_profile: company %s not found", company_id,
        )
        return None

    cached = company.get("security_team_profile")
    if not force and _is_fresh(cached):
        return cached

    domain = (company.get("domain") or "").strip().lower()
    if not domain:
        logger.info(
            "refresh_security_team_profile: company %s has no domain — skipping",
            company_id,
        )
        return cached

    people = _apollo_search(domain=domain)
    if not people and not cached:
        # First-time fetch failed entirely — return None so caller knows
        # we haven't established a profile yet.
        return None
    if not people:
        # Refresh failed but we have cached data — keep what we have.
        return cached

    profile = _profile_from_people(
        people,
        employee_count=_parse_size_band(company.get("size_band")),
        industry=company.get("industry"),
    )
    _store(db, company_id=company_id, profile=profile)
    return profile


def _profile_from_people(
    people: List[Dict[str, Any]],
    *,
    employee_count: Optional[int],
    industry: Optional[str],
) -> Dict[str, Any]:
    """Bucket + count + score. Pure function for ease of testing."""
    counts = {b: 0 for b in ALL_BUCKETS}
    titles_seen: List[str] = []
    for p in people:
        if not isinstance(p, dict):
            continue
        title = (p.get("title") or "").strip()
        bucket = _bucket_for_title(title)
        if bucket is None:
            continue
        counts[bucket] += 1
        if title and title not in titles_seen:
            titles_seen.append(title)

    total = sum(counts.values())
    band = _expected_band(employee_count, industry)
    score = _score_for_total(total, band)

    # Flag buckets that look thin vs what we'd expect at this size — used
    # by the dossier visual to highlight gaps ("⚠ no IAM").
    gap_buckets: List[str] = []
    if total >= 5:
        # Once an account has any real security headcount, missing a
        # whole bucket starts being informative.
        for b, c in counts.items():
            if c == 0:
                gap_buckets.append(b)

    return {
        "counts_by_bucket": counts,
        "total": total,
        "top_titles": titles_seen[:15],
        "expected_band": list(band),
        "score": score,
        "gap_buckets": gap_buckets,
        "is_regulated_industry": _is_regulated_industry(industry),
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Apollo + persistence helpers
# ---------------------------------------------------------------------------
def _apollo_search(*, domain: str) -> List[Dict[str, Any]]:
    """One Apollo mixed_people search across the full security-title
    union, scoped to this domain. Search is free; no reveals."""
    api_key = settings.APOLLO_API_KEY
    if not api_key:
        logger.warning("Apollo search skipped — APOLLO_API_KEY not set")
        return []

    body = {
        "person_titles": SECURITY_TITLES,
        "q_organization_domains_list": [domain],
        "per_page": _MAX_PAGE_SIZE,
        "page": 1,
    }
    try:
        res = httpx.post(
            _APOLLO_URL,
            json=body,
            headers={"X-Api-Key": api_key},
            timeout=_TIMEOUT,
        )
        res.raise_for_status()
        payload = res.json()
    except Exception as e:
        logger.warning("Apollo security-team search failed for %s: %s", domain, e)
        return []

    people = payload.get("people") or payload.get("contacts") or []
    if not isinstance(people, list):
        return []
    return people


def _fetch_company(db: Client, company_id: str) -> Optional[Dict[str, Any]]:
    try:
        res = (
            db.table("companies")
            .select("id, domain, name, size_band, industry, security_team_profile")
            .eq("id", company_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.warning("_fetch_company failed for %s: %s", company_id, e)
        return None
    rows = res.data or []
    return rows[0] if rows else None


def _parse_size_band(raw: Any) -> Optional[int]:
    """Parse companies.size_band into an employee count.

    Two formats live in this column depending on which Apollo path
    populated it:
      - Clean int as string  : "660"        -> 660
      - Range "lo,hi"        : "1001,5000"  -> 3000 (midpoint)
    Returns None when neither parses.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        pass
    if "," in s:
        parts = s.split(",")
        if len(parts) == 2:
            try:
                lo = int(parts[0].strip())
                hi = int(parts[1].strip())
                return (lo + hi) // 2
            except (TypeError, ValueError):
                return None
    return None


def _store(db: Client, *, company_id: str, profile: Dict[str, Any]) -> None:
    try:
        db.table("companies").update(
            {"security_team_profile": profile}
        ).eq("id", company_id).execute()
    except Exception as e:
        logger.warning(
            "Persist security_team_profile failed for %s: %s", company_id, e,
        )


def _is_fresh(cached: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(cached, dict):
        return False
    ts = cached.get("refreshed_at")
    if not isinstance(ts, str):
        return False
    try:
        last = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    return (datetime.now(timezone.utc) - last) < timedelta(days=_REFRESH_AFTER_DAYS)
