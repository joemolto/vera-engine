"""Job-board collection — read a company's real vacancies from source.

Why this exists (2026-07-21): job postings are the strongest cyber
buying trigger ("they're hiring a Vuln Mgt lead" beats "they had creds
leaked"), but Vera only parsed jobs once, at enrichment, and the weekly
monitor saw nothing but PredictLeads' coarse counters. Measured result:
8 `hiring_security` signals across 83 accounts in 90 days, while a single
company's public board carries 13 live security roles.

Most companies publish their vacancies through an applicant tracking
system (ATS) whose job list is deliberately public so job boards can
consume it. We read that directly: free, real-time, and it carries the
posting DATE, which is what turns "they have a role open" into "they
posted it on Tuesday" or "they still can't fill it after five months".

Detection recognises every major platform even where we have no adapter,
so the coverage report can say how much of the watchlist sits on Workday
and friends before we decide to build those.

Nothing here classifies a role as security. Titles go to the existing
Claude cyber taxonomy + exclude list downstream. No keyword shortcuts:
that is exactly how the old `is_hiring_technical → hiring_security` bug
counted backend developers as security hires.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = 12
_UA = "Mozilla/5.0 (compatible; VeraBot/1.0)"

# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

# platform -> regexes that capture the board token from a careers-page link.
# Ordered by how unambiguous the pattern is.
ATS_PATTERNS: List[Tuple[str, str]] = [
    ("greenhouse", r"boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)"),
    ("greenhouse", r"job-boards\.greenhouse\.io/([a-z0-9_-]+)"),
    ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([a-z0-9_-]+)"),
    ("lever", r"jobs\.(?:eu\.)?lever\.co/([a-z0-9_-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)"),
    ("workable", r"apply\.workable\.com/([a-z0-9_-]+)"),
    ("smartrecruiters", r"careers\.smartrecruiters\.com/([a-zA-Z0-9_-]+)"),
    ("recruitee", r"([a-z0-9-]+)\.recruitee\.com"),
    ("teamtailor", r"([a-z0-9-]+)\.teamtailor\.com"),
    ("personio", r"([a-z0-9-]+)\.jobs\.personio\.(?:de|com)"),
    ("workday", r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com"),
    ("successfactors", r"([a-z0-9-]+)\.(?:successfactors|sapsf)\.(?:com|eu)"),
    ("taleo", r"([a-z0-9-]+)\.taleo\.net"),
    ("icims", r"([a-z0-9-]+)\.icims\.com"),
]

# Platforms we can actually read today. Everything else is detected and
# reported so the coverage number is honest about what's left.
SUPPORTED = {"greenhouse", "lever", "ashby"}

_CAREERS_PATHS = [
    "/careers", "/jobs", "/company/careers", "/about/careers",
    "/careers/jobs", "/work-with-us", "/join-us", "/life", "/en/careers",
    "/about-us/careers", "/vacancies", "/",
]

# Plenty of companies host careers on a subdomain and never link the ATS
# from the marketing site's HTML (JS-rendered boards, mostly).
_CAREERS_HOSTS = ["careers.{d}", "jobs.{d}", "apply.{d}"]


def _get(url: str) -> Optional[str]:
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as c:
            r = c.get(url, headers={"User-Agent": _UA})
            if r.status_code == 200 and r.text:
                # Redirect target counts as evidence too (careers pages very
                # often 302 straight onto the ATS).
                return str(r.url) + "\n" + r.text
    except Exception:
        return None
    return None


def _match_ats(blob: str) -> Optional[Dict[str, str]]:
    for platform, pattern in ATS_PATTERNS:
        m = re.search(pattern, blob, re.I)
        if not m:
            continue
        token = m.group(1)
        # Guard against matching the vendor's own marketing pages.
        if token.lower() in {"www", "jobs", "careers", "api", "boards", "app", "help", "support"}:
            continue
        return {"platform": platform, "token": token}
    return None


def _slugs(company_name: str) -> List[str]:
    """Plausible board tokens derived from the company name."""
    base = re.sub(r"[^a-z0-9 ]", "", (company_name or "").lower()).strip()
    if not base:
        return []
    words = base.split()
    joined = "".join(words)
    out = [joined]
    # Drop common suffixes: "Allica Bank" -> "allica"
    if len(words) > 1 and words[-1] in {
        "bank", "group", "plc", "ltd", "limited", "inc", "llc", "holdings",
        "insurance", "international", "technologies", "solutions", "labs",
    }:
        out.append("".join(words[:-1]))
    if len(words) > 1:
        out.append(words[0])
    return [s for s in dict.fromkeys(out) if len(s) > 2][:3]


def detect_ats(domain: str, company_name: str = "") -> Optional[Dict[str, str]]:
    """Work out which ATS a company uses. Cheap, and done once per company.

    Two strategies: read their careers page and look at where the apply
    links point (authoritative), then fall back to guessing the board token
    from the company name and confirming it against the live API.
    """
    for path in _CAREERS_PATHS:
        blob = _get(f"https://{domain}{path}")
        if not blob:
            continue
        hit = _match_ats(blob)
        if hit:
            hit["via"] = "careers_page"
            return hit

    for host in _CAREERS_HOSTS:
        blob = _get(f"https://{host.format(d=domain)}")
        if not blob:
            continue
        hit = _match_ats(blob)
        if hit:
            hit["via"] = "careers_subdomain"
            return hit

    # Fallback: guess the token, then prove it by calling the API.
    for slug in _slugs(company_name):
        for platform in ("greenhouse", "lever", "ashby"):
            if _probe(platform, slug):
                return {"platform": platform, "token": slug, "via": "name_guess"}
    return None


def _probe(platform: str, token: str) -> bool:
    url = _API[platform](token)
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            return c.get(url, headers={"User-Agent": _UA}).status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Adapters — normalise each ATS into one shape
# ---------------------------------------------------------------------------

_API = {
    "greenhouse": lambda t: f"https://boards-api.greenhouse.io/v1/boards/{t}/jobs",
    "lever": lambda t: f"https://api.lever.co/v0/postings/{t}?mode=json",
    "ashby": lambda t: f"https://api.ashbyhq.com/posting-api/job-board/{t}",
}


def _iso(value: Any) -> Optional[str]:
    """Normalise a date that may be ISO text or epoch millis."""
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        return str(value)
    except Exception:
        return None


def _norm(external_id: Any, title: str, location: Any, url: str,
          published: Any, updated: Any) -> Optional[Dict[str, Any]]:
    title = (title or "").strip()
    if not title:
        return None
    if isinstance(location, dict):
        location = location.get("name") or location.get("locationName")
    return {
        "external_id": str(external_id),
        "title": title,
        "location": (str(location).strip() if location else None),
        "url": url,
        "first_published": _iso(published),
        "updated_at": _iso(updated),
    }


def _greenhouse(token: str) -> List[Dict[str, Any]]:
    with httpx.Client(timeout=TIMEOUT) as c:
        data = c.get(_API["greenhouse"](token), headers={"User-Agent": _UA}).raise_for_status().json()
    out = []
    for j in data.get("jobs", []):
        n = _norm(j.get("id"), j.get("title"), j.get("location"),
                  j.get("absolute_url", ""), j.get("first_published"), j.get("updated_at"))
        if n:
            out.append(n)
    return out


def _lever(token: str) -> List[Dict[str, Any]]:
    with httpx.Client(timeout=TIMEOUT) as c:
        data = c.get(_API["lever"](token), headers={"User-Agent": _UA}).raise_for_status().json()
    out = []
    for j in data if isinstance(data, list) else []:
        n = _norm(j.get("id"), j.get("text"), (j.get("categories") or {}).get("location"),
                  j.get("hostedUrl", ""), j.get("createdAt"), j.get("createdAt"))
        if n:
            out.append(n)
    return out


def _ashby(token: str) -> List[Dict[str, Any]]:
    with httpx.Client(timeout=TIMEOUT) as c:
        data = c.get(_API["ashby"](token), headers={"User-Agent": _UA}).raise_for_status().json()
    out = []
    for j in data.get("jobs", []):
        n = _norm(j.get("id"), j.get("title"), j.get("location"),
                  j.get("jobUrl", ""), j.get("publishedAt"), j.get("publishedAt"))
        if n:
            out.append(n)
    return out


ADAPTERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}


def fetch_ats_jobs(platform: str, token: str) -> Optional[List[Dict[str, Any]]]:
    """Read every live vacancy from a company's ATS. None = read failed
    (which is NOT the same as 'no vacancies' — callers must keep them apart)."""
    fn = ADAPTERS.get(platform)
    if not fn:
        return None
    try:
        return fn(token)
    except Exception as e:  # noqa: BLE001
        logger.warning("job_boards: %s fetch failed for %s: %s", platform, token, e)
        return None
