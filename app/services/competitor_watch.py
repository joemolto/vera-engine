"""Competitor watch — posts by an org's competitors + the security people
who engaged with them.

One harvestapi/linkedin-company-posts call per competitor returns a FLAT
dataset mixing record types (verified live 2026-07-07): `type` is "post"
(content, postedAt.date, linkedinUrl, engagement), "reaction" (postId,
actor{name, position, linkedinUrl}) or "comment" (postId, actor,
commentary). We join engagers to posts via postId, keep only actors whose
position looks security-flavoured, and upsert one competitor_feed row per
(org, post).

Zero-config: orgs with icp_config.client_context.competitors use that
list; otherwise we fall back to a curated set for their sector. Unknown
company slugs fail soft (logged, skipped).

Costs: pay-per-event actor; ~5 posts + <=100 reactions + <=30 comments per
competitor per scan = cents. Caps below are the spend ceiling.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from supabase import Client

from app.config import settings

logger = logging.getLogger(__name__)

_APIFY_BASE = "https://api.apify.com/v2"
_ACTOR = "harvestapi~linkedin-company-posts"
_TIMEOUT = 240.0

MAX_COMPETITORS = 6
MAX_POSTS = 5
MAX_REACTIONS = 100
MAX_COMMENTS = 30
LOOKBACK_DAYS = 14

# Known competitor -> LinkedIn company slug. Best-effort: a wrong slug
# fails soft (actor returns nothing) and is logged. zerofox verified live
# 2026-07-07; extend as orgs configure new competitor names.
COMPETITOR_SLUGS: Dict[str, str] = {
    "crowdstrike": "crowdstrike",
    "sentinelone": "sentinelone",
    "arctic wolf": "arctic-wolf-networks",
    "esentire": "esentire",
    "expel": "expel-io",
    "red canary": "red-canary",
    "recorded future": "recorded-future",
    "digital shadows": "digital-shadows",
    "flashpoint": "flashpoint-intel",
    "zerofox": "zerofox",
}

# Website domains for logo rendering in the dashboard (DuckDuckGo favicon
# service, same mechanism as the accounts list). Unknown names fall back
# to `<name-with-dashes>.com`, which is right often enough and fails soft
# to an initial in the UI.
COMPETITOR_DOMAINS: Dict[str, str] = {
    "crowdstrike": "crowdstrike.com",
    "sentinelone": "sentinelone.com",
    "arctic wolf": "arcticwolf.com",
    "esentire": "esentire.com",
    "expel": "expel.com",
    "red canary": "redcanary.com",
    "recorded future": "recordedfuture.com",
    "digital shadows": "digitalshadows.com",
    "flashpoint": "flashpoint.io",
    "zerofox": "zerofox.com",
}


def _domain_for(name: str) -> str:
    key = (name or "").strip().lower()
    return COMPETITOR_DOMAINS.get(key) or key.replace(" ", "") + ".com"

# Curated zero-config defaults by sector, keyed off wizard/client context.
SECTOR_COMPETITORS: Dict[str, List[str]] = {
    "mdr": ["CrowdStrike", "SentinelOne", "Arctic Wolf", "eSentire", "Expel", "Red Canary"],
    "threat_intel": ["Recorded Future", "ZeroFox", "Flashpoint", "Digital Shadows"],
}

# Security-flavoured titles. Two tiers: _LEADER_RE marks decision-makers,
# _SECURITY_RE keeps senior ICs too (the feed shows both, leaders first).
_SECURITY_RE = re.compile(
    r"(?i)\b(ciso|cso|chief information security|chief security|infosec|"
    r"information security|cyber ?security|security (officer|director|lead|leader|"
    r"manager|architect|engineer|analyst|operations|specialist)|threat intel|"
    r"soc analyst|soc manager|grc|dfir|incident response|penetration|red team)\b"
)
_LEADER_RE = re.compile(
    r"(?i)\b(ciso|cso|chief|head of|vp|vice president|director|founder|owner)\b"
)


def _slug_for(name: str) -> str:
    key = (name or "").strip().lower()
    return COMPETITOR_SLUGS.get(key) or key.replace(" ", "-")


def resolve_competitors(org: Dict[str, Any]) -> List[str]:
    """Configured list first; else curated sector defaults; else empty."""
    icp = org.get("icp_config") or {}
    cc = icp.get("client_context") or {}
    names = cc.get("competitors")
    if isinstance(names, list) and names:
        return [str(n) for n in names if n][:MAX_COMPETITORS]

    # Zero-config: infer sector from client context / onboarding answers.
    blob = " ".join(
        str(v) for v in (
            cc.get("sector"),
            cc.get("sells_what"),
            (icp.get("onboarding") or {}).get("answers"),
        ) if v
    ).lower()
    if any(w in blob for w in ("threat intel", "dark web", "exposure intel")):
        return SECTOR_COMPETITORS["threat_intel"][:MAX_COMPETITORS]
    if any(w in blob for w in ("mdr", "managed detection", "soc outsourc", "managed security")):
        return SECTOR_COMPETITORS["mdr"][:MAX_COMPETITORS]
    return []


def _fetch_company_posts(slug: str) -> List[Dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    with httpx.Client(timeout=_TIMEOUT) as c:
        res = c.post(
            f"{_APIFY_BASE}/acts/{_ACTOR}/run-sync-get-dataset-items",
            params={"token": settings.APIFY_API_KEY, "timeout": 220},
            json={
                "targetUrls": [f"https://www.linkedin.com/company/{slug}"],
                "maxPosts": MAX_POSTS,
                "postedLimitDate": since,
                "scrapeReactions": True,
                "maxReactions": MAX_REACTIONS,
                "scrapeComments": True,
                "maxComments": MAX_COMMENTS,
            },
        )
        res.raise_for_status()
        items = res.json()
    return items if isinstance(items, list) else []


def _parse_engager(rec: Dict[str, Any], kind: str, competitor: str) -> Optional[Dict[str, Any]]:
    actor = rec.get("actor")
    if not isinstance(actor, dict):
        return None
    position = (actor.get("position") or "").strip()
    if not position or not _SECURITY_RE.search(position):
        return None
    # The competitor's own people engage with their employer's posts
    # constantly — they're not prospects. Title mentions the competitor
    # (e.g. "VP @ SentinelOne") = drop.
    if competitor.lower() in position.lower():
        return None
    return {
        "name": (actor.get("name") or "").strip(),
        "title": position[:160],
        "linkedin_url": actor.get("linkedinUrl") or "",
        # LinkedIn CDN photo. These URLs expire after some weeks — the UI
        # must fall back to initials on load error, never break.
        "picture_url": actor.get("pictureUrl") or "",
        "kind": kind,
        "is_leader": bool(_LEADER_RE.search(position)),
    }


def scan_competitor(db: Client, org_id: str, competitor: str) -> int:
    """Scan one competitor for one org. Returns rows upserted."""
    slug = _slug_for(competitor)
    try:
        items = _fetch_company_posts(slug)
    except Exception as e:
        msg = str(e)
        if settings.APIFY_API_KEY:
            msg = msg.replace(settings.APIFY_API_KEY, "***")
        logger.warning("competitor-watch: fetch failed for %s (%s): %s", competitor, slug, msg)
        return 0
    if not items:
        logger.info("competitor-watch: no items for %s (slug %s — wrong slug or quiet company)",
                    competitor, slug)
        return 0

    posts: Dict[str, Dict[str, Any]] = {}
    engagers_by_post: Dict[str, List[Dict[str, Any]]] = {}
    for rec in items:
        if not isinstance(rec, dict):
            continue
        rtype = rec.get("type")
        if rtype == "post":
            pid = str(rec.get("id") or rec.get("entityId") or "")
            if pid:
                posts[pid] = rec
        elif rtype in ("reaction", "comment"):
            pid = str(rec.get("postId") or "")
            eng = _parse_engager(rec, "comment" if rtype == "comment" else "reaction", competitor)
            if pid and eng:
                engagers_by_post.setdefault(pid, []).append(eng)

    written = 0
    for pid, post in posts.items():
        url = post.get("linkedinUrl") or post.get("shareLinkedinUrl")
        if not url:
            continue
        posted_at = None
        pa = post.get("postedAt")
        if isinstance(pa, dict):
            posted_at = pa.get("date")
        engagers = engagers_by_post.get(pid, [])
        # Same person can appear twice (reaction + comment) — keep one,
        # preferring the comment (richer). Then leaders first, bounded.
        seen: Dict[str, Dict[str, Any]] = {}
        for e in engagers:
            k = e["linkedin_url"] or e["name"]
            if k not in seen or e["kind"] == "comment":
                seen[k] = e
        engagers = list(seen.values())
        engagers.sort(key=lambda e: (not e["is_leader"], e["name"]))
        row = {
            "org_id": org_id,
            "competitor": competitor,
            "competitor_domain": _domain_for(competitor),
            "post_url": url,
            "post_text": (post.get("content") or "")[:1000],
            "posted_at": posted_at,
            "engagement": post.get("engagement") or {},
            "security_engagers": engagers[:25],
            "engager_count": len(engagers),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            db.table("competitor_feed").upsert(row, on_conflict="org_id,post_url").execute()
            written += 1
        except Exception as e:
            logger.warning("competitor-watch: upsert failed for %s: %s", url, e)
    logger.info("competitor-watch: %s — %d posts, %d with security engagers",
                competitor, written,
                sum(1 for p in posts if engagers_by_post.get(p)))
    return written


def scan_org(db: Client, org: Dict[str, Any]) -> Dict[str, Any]:
    """Scan all competitors for one org row (id + icp_config needed)."""
    org_id = str(org["id"])
    competitors = resolve_competitors(org)
    if not competitors:
        return {"org_id": org_id, "competitors": 0, "rows": 0}
    rows = 0
    for name in competitors:
        rows += scan_competitor(db, org_id, name)
    return {"org_id": org_id, "competitors": len(competitors), "rows": rows}
