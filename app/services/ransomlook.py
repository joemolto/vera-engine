"""Ransomware leak-site victims, from RansomLook.

Replaces a signal that was not doing its job. The old `breach_mention` came
from generic company news and filed "Ford had issues with major safety recall"
and "Tesco had issues with possible shareholder revolt over his pay packet" as
security breaches. Of 17 rows, three were genuinely security-related.

This is the real thing: a named ransomware group has published a named victim
on their leak site, with a discovery timestamp.

Licensing
---------
RansomLook itself is GPL-3.0 and self-hostable, which is the compliant path
for commercial use: run your own instance and point RANSOMLOOK_BASE_URL at it.
The public instance at ransomlook.io is fine for prototyping but is somebody
else's free service; check its terms before relying on it commercially.

Use in product
--------------
Internal timing trigger and dossier context only. Do NOT put "your supplier
just appeared on a leak site" in outbound copy: it reads as ambulance-chasing,
and Vera is sales intelligence for cyber, not an incident feed.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("RANSOMLOOK_BASE_URL", "https://www.ransomlook.io").rstrip("/")
TIMEOUT = 20.0
CACHE_TYPE = "ransomlook_recent"
CACHE_TTL_SECONDS = 6 * 3600

# Pure legal forms only. "Group", "Holdings" and "Co" are deliberately absent:
# they are usually part of the trading name, and stripping them collapses
# "BMS Group" to "BMS", which would match a different company's leak-site
# entry. A false ransomware accusation is not a near miss.
_SUFFIXES = (
    " incorporated", " corporation", " limited",
    " inc", " llc", " ltd", " gmbh", " plc", " corp",
    " ag", " bv", " nv", " pty", " srl", " spa", " sa",
)


@dataclass(frozen=True)
class LeakVictim:
    name: str
    group: str
    discovered: str
    description: str
    link: str


def _normalise(value: str) -> str:
    """Lowercase, strip punctuation and corporate suffixes for comparison."""
    v = (value or "").lower().strip()
    v = re.sub(r"^https?://", "", v)
    v = re.sub(r"^www\.", "", v)
    # A bare domain victim ("clintonhealthaccess.org") compares on its stem.
    if "." in v and " " not in v:
        v = v.split(".")[0]
    v = re.sub(r"[^a-z0-9 ]+", " ", v)
    v = re.sub(r"\s+", " ", v).strip()
    # Strip at most ONE legal form. Chaining them ate "Group" out of
    # "US Installation Group, Inc." and left "us installation".
    for suffix in _SUFFIXES:
        if v.endswith(suffix):
            v = v[: -len(suffix)].strip()
            break
    return v


def fetch_recent_victims(db=None, timeout: float = TIMEOUT) -> List[LeakVictim]:
    """Recent leak-site posts. Cached for 6h — the feed moves slowly."""
    if db is not None:
        try:
            from app.services.enrichers.cache import get_cached
            cached = get_cached(db, "_global", CACHE_TYPE)
            if cached and cached.get("victims"):
                return [LeakVictim(**v) for v in cached["victims"]]
        except Exception as e:  # noqa: BLE001
            logger.debug("ransomlook cache read failed: %s", e)

    try:
        r = httpx.get(f"{BASE_URL}/api/recent", timeout=timeout,
                      headers={"User-Agent": "VeraBot/1.0"})
        if r.status_code != 200:
            logger.warning("RansomLook HTTP %s", r.status_code)
            return []
        rows = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("RansomLook fetch failed: %s", e)
        return []

    victims: List[LeakVictim] = []
    for row in rows if isinstance(rows, list) else []:
        title = (row.get("post_title") or "").strip()
        if not title:
            continue
        victims.append(LeakVictim(
            name=title,
            group=(row.get("group_name") or "unknown").strip(),
            discovered=(row.get("discovered") or "")[:19],
            description=(row.get("description") or "")[:400],
            link=row.get("link") or "",
        ))

    if db is not None and victims:
        try:
            from app.services.enrichers.cache import set_cache
            set_cache(db, "_global", CACHE_TYPE, None,
                      {"victims": [v.__dict__ for v in victims]},
                      ttl_seconds=CACHE_TTL_SECONDS)
        except Exception as e:  # noqa: BLE001
            logger.debug("ransomlook cache write failed: %s", e)

    logger.info("RansomLook: %d recent victims", len(victims))
    return victims


def match_company(
    company_name: Optional[str],
    domain: Optional[str],
    victims: List[LeakVictim],
) -> Optional[LeakVictim]:
    """Exact normalised match only.

    Deliberately not fuzzy. Telling a customer their account was hit by
    ransomware when it was a similarly-named company is not a near miss, it is
    a serious false accusation about a real business.
    """
    targets = {_normalise(x) for x in (company_name, domain) if x}
    targets.discard("")
    if not targets:
        return None
    for v in victims:
        if _normalise(v.name) in targets:
            return v
    return None


def build_breach_signal(
    victim: LeakVictim,
    org_id: str,
    company_id: str,
    company_name: Optional[str] = None,
) -> dict:
    """A `breach_mention` row that has actually earned the name."""
    who = company_name or victim.name
    return {
        "org_id": org_id,
        "company_id": company_id,
        "signal_type": "breach_mention",
        "signal_source": "ransomlook",
        "signal_data": {
            "headline": (
                f"{who} has been named on the {victim.group} "
                f"ransomware leak site"
            ),
            "detail": (
                f"Published by {victim.group}, first seen {victim.discovered}. "
                f"{victim.description}".strip()
            ),
            "entity": who,
            "group": victim.group,
            "discovered": victim.discovered,
            "source_link": f"{BASE_URL}{victim.link}" if victim.link else BASE_URL,
            # Internal timing trigger. Never put this in outbound copy.
            "outbound_safe": False,
        },
        "signal_strength": 0.95,
        "signal_subject_type": "account",
    }
