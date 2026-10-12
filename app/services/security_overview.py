"""The security picture of a company, assembled from public evidence only.

This is the body of the public free report. It answers the five questions a
cyber seller actually opens a conversation with:

    Do they publish a security or trust page?
    Do they have a security team, and how big?
    Is that a lot or a little for their size and sector?
    Are they missing whole disciplines (no GRC, no IAM)?
    Are they hiring security people right now?

Every one of those works on essentially any domain, which is the point. The
perimeter scan is the opposite: it says something on roughly a third of
domains and produces a CVE-grade claim on about one in thirty (measured
2026-08-04 across a 30-domain sample of the signal-bearing corpus). It rides
along here as a bonus section, never as the thing the report stands on.

Contact-safe by construction. The team profiler uses Apollo's *search*
endpoint, which returns counts and titles with no credit charge and no
reveal, and we surface only counts, role mix and job titles. No names, no
LinkedIn URLs, no emails ever leave this module — that is what the paid
product is for.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Union of practitioner and leadership hiring terms. Deliberately wider than
# `enricher._SECURITY_ROLE_KEYWORDS` (practitioner-only) because the question
# is "have they hired in security", which a Head of Security req answers just
# as well as a SOC analyst req. Kept local rather than imported: enricher.py is
# the lead-enrichment monolith and importing it here would risk a cycle.
SECURITY_ROLE_KEYWORDS = frozenset({
    "security engineer", "security analyst", "security architect",
    "security operations", "soc analyst", "soc engineer",
    "devsecops", "appsec", "application security",
    "incident response", "threat hunter", "vulnerability management",
    "penetration tester", "pentester",
    "ciso", "chief information security", "head of security",
    "head of information security", "security manager", "director of security",
    "grc", "compliance manager", "risk manager",
    "identity and access", "iam engineer",
})

# How the profiler's internal bucket keys read in a report.
BUCKET_LABELS = {
    "leadership": "Leadership",
    "engineering": "Security engineering",
    "soc_ops": "Security operations",
    "grc": "Governance, risk & compliance",
    "iam": "Identity & access",
}

# Posture probes worth showing a stranger, in the order they matter.
POSTURE_LABELS = [
    ("security_page_present", "Security page"),
    ("trust_center_present", "Trust centre"),
    ("compliance_page_present", "Compliance page"),
    ("subprocessors_present", "Subprocessor list"),
    ("dpa_present", "Data processing agreement"),
]

RECENT_POSTING_DAYS = 90


def _is_security_title(title: str) -> bool:
    return any(kw in (title or "").lower() for kw in SECURITY_ROLE_KEYWORDS)


def _posted_within(raw: Any, days: int) -> bool:
    """True when a posting date parses and falls inside the window.

    Unparseable or absent dates return False: an undated posting is not
    evidence of *recent* hiring, and guessing is how the old cve_in_stack
    signal earned its retirement.
    """
    if not raw:
        return False
    text = str(raw).strip().replace("Z", "+00:00")
    for parse in (
        lambda s: datetime.fromisoformat(s),
        lambda s: datetime.strptime(s[:10], "%Y-%m-%d"),
    ):
        try:
            dt = parse(text)
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc) - timedelta(days=days)
    return False


def _team_verdict(total: int, band: List[int]) -> str:
    """One plain sentence comparing headcount to the size+sector baseline.

    The raw number is trivia. "Four, where eight to fifteen is typical for a
    company this size" is a finding, and it is the single most quotable line
    in the whole report.
    """
    if not band or len(band) != 2:
        return ""
    lo, hi = band
    if total == 0:
        return f"No security-titled staff found. Companies this size typically carry {lo} to {hi}."
    if total < lo:
        return f"{total} security staff, below the {lo} to {hi} typical for a company this size and sector."
    if total > hi:
        return f"{total} security staff, above the {lo} to {hi} typical for a company this size and sector."
    return f"{total} security staff, in line with the {lo} to {hi} typical for a company this size and sector."


def _collect_posture(domain: str) -> Dict[str, Any]:
    """Which security-assurance surfaces the company actually publishes."""
    published: List[Dict[str, str]] = []
    missing: List[str] = []
    try:
        from app.services.website_posture import probe_posture_pages
        hits = probe_posture_pages(domain) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("[overview] posture probes failed for %s: %s", domain, e)
        hits = {}

    for signal_type, label in POSTURE_LABELS:
        hit = hits.get(signal_type)
        if hit and hit.get("url"):
            published.append({"label": label, "url": hit["url"]})
        else:
            missing.append(label)

    security_txt = None
    try:
        from app.services.website_posture import probe_security_txt
        security_txt = probe_security_txt(domain)
    except Exception as e:  # noqa: BLE001
        logger.warning("[overview] security.txt probe failed for %s: %s", domain, e)

    return {
        "published": published,
        "missing": missing,
        "has_security_txt": bool(security_txt),
        # A company that publishes nothing is itself the finding, so callers
        # can render the absence rather than an empty block.
        "publishes_nothing": not published and not security_txt,
    }


def _collect_team(
    db, *, company_id: Optional[str], domain: str,
) -> Optional[Dict[str, Any]]:
    """Security headcount and role mix, vs the size+sector baseline.

    The baseline is RECOMPUTED here from the company's current size rather
    than read off the cached profile, and the comparison is withheld entirely
    when that size won't parse.

    Two failure modes make this necessary, both seen on bodycote.com:
      - `_expected_band` falls back to the smallest band on a missing
        headcount. Sane for an internal maturity score, a falsehood in a
        report: "5 security staff, above the 0 to 1 typical".
      - Profiles cache for 30 days, so a band computed while `size_band` was
        null outlives the null. Bodycote's size arrived minutes after its
        profile was built and the stale [0, 1] would have shipped anyway.

    Headcount and role mix are observed facts and always survive; only the
    comparison is conditional.
    """
    if not company_id:
        return None
    try:
        from app.services.security_team_profiler import refresh_security_team_profile
        profile = refresh_security_team_profile(db, company_id=company_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("[overview] team profile failed for %s: %s", domain, e)
        return None
    if not profile:
        return None

    band: List[int] = []
    try:
        from app.services.companies import get_company_by_domain
        from app.services.security_team_profiler import _expected_band, _parse_size_band

        company = get_company_by_domain(db, domain) or {}
        headcount = _parse_size_band(company.get("size_band"))
        if headcount:
            band = list(_expected_band(headcount, company.get("industry")))
        else:
            logger.info(
                "[overview] %s size_band %r does not parse — no baseline claim",
                domain, company.get("size_band"),
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("[overview] baseline lookup failed for %s: %s", domain, e)

    counts = profile.get("counts_by_bucket") or {}
    total = int(profile.get("total") or 0)
    return {
        "total": total,
        "by_area": [
            {"label": BUCKET_LABELS.get(k, k), "count": int(v or 0)}
            for k, v in counts.items()
        ],
        "expected_band": band,
        "gaps": [BUCKET_LABELS.get(b, b) for b in (profile.get("gap_buckets") or [])],
        "titles": list(profile.get("top_titles") or [])[:8],
        "verdict": _team_verdict(total, band),
        "is_regulated_industry": bool(profile.get("is_regulated_industry")),
    }


def _collect_hiring(job_postings: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Open security roles, and how many went up in the last 90 days.

    Apollo's organisation enrichment already fetched these on the cheap path
    and threw them away. Counts and titles only; a job ad is public, but we
    are not turning it into a route to the hiring manager.
    """
    roles = [
        p for p in (job_postings or [])
        if isinstance(p, dict) and _is_security_title(p.get("title") or "")
    ]
    recent = [p for p in roles if _posted_within(p.get("posted_date"), RECENT_POSTING_DAYS)]
    return {
        "open_security_roles": len(roles),
        "recent_90d": len(recent),
        "titles": [
            {"title": (p.get("title") or "").strip(), "location": (p.get("location") or "").strip()}
            for p in roles[:6]
            if (p.get("title") or "").strip()
        ],
    }


def _collect_perimeter(db, domain: str, run_scan: bool) -> Dict[str, Any]:
    """What their internet-facing estate publishes, and what answers on it.

    Unversioned kit is reported, not discarded. "Citrix Gateway on
    remote.example.com, version not disclosed" is a legitimate opener even
    though it can never carry a CVE claim, and holding those back was what
    made the perimeter section look far thinner than it is.
    """
    out: Dict[str, Any] = {
        "hostnames_published": 0, "edge_kit": [], "scanned": False,
    }
    try:
        from app.services.subdomain_discovery import discover_subdomains
        hosts = discover_subdomains(domain, db=db)
        out["hostnames_published"] = len(hosts)
    except Exception as e:  # noqa: BLE001
        logger.warning("[overview] subdomain discovery failed for %s: %s", domain, e)

    if not run_scan:
        return out

    try:
        from app.services.perimeter import scan_perimeter
        findings = scan_perimeter(domain, db=db)
    except Exception as e:  # noqa: BLE001
        logger.warning("[overview] perimeter scan failed for %s: %s", domain, e)
        return out

    out["scanned"] = True
    out["edge_kit"] = [
        {
            "host": f.host,
            "product": f.product,
            "version": f.version,
            "versioned": f.is_versioned,
        }
        for f in findings
    ]
    return out


def build_security_overview(
    db,
    *,
    domain: str,
    company_id: Optional[str] = None,
    job_postings: Optional[List[Dict[str, Any]]] = None,
    run_perimeter_scan: bool = True,
) -> Dict[str, Any]:
    """Assemble the public security picture for one domain.

    Slow by design (crt.sh alone takes ~40s on a busy estate), so callers
    must run this off the request path. Every section degrades to empty
    rather than raising: a partial report still beats no report.
    """
    domain = (domain or "").strip().lower()
    if not domain:
        raise ValueError("domain is required")

    return {
        "posture": _collect_posture(domain),
        "team": _collect_team(db, company_id=company_id, domain=domain),
        "hiring": _collect_hiring(job_postings),
        "perimeter": _collect_perimeter(db, domain, run_perimeter_scan),
    }
