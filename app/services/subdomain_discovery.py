"""Find the hostnames a company actually publishes, via Certificate Transparency.

Why CT and not a wordlist
-------------------------
The perimeter collector originally guessed 24 hostnames (`vpn.`, `mail.`, …)
and found something on only 16% of accounts. The limit was discovery, not
fingerprinting: Arqiva alone publishes 246 hostnames, and we were checking two
dozen guesses. CT gives the real list.

Why crt.sh and not subfinder
----------------------------
subfinder (MIT) is the better tool, but it is a Go binary the server would also
need installed, and a missing binary fails silently — discovery would just
return nothing and no one would notice, which is the failure mode this whole
codebase spent 2026-08-04 removing. crt.sh is crt.sh: the same primary source
subfinder leans on, reachable over plain HTTP from anywhere the engine runs.

Certificate Transparency is public infrastructure. Every CA is required to
log every certificate it issues, precisely so anyone can audit them. Reading
that log is not reconnaissance against the company; it is reading a public
register the company's own CA wrote to.

Cost of the choice: crt.sh takes ~40s for a busy domain, so results are cached
hard (14 days — CT changes slowly) and the wordlist is always unioned in as a
floor so a crt.sh outage degrades to the old behaviour rather than to nothing.
"""

from __future__ import annotations

import logging
from typing import Optional, Set

import httpx

logger = logging.getLogger(__name__)

CRTSH_URL = "https://crt.sh/"
CRTSH_TIMEOUT = 60.0
CACHE_TYPE = "subdomains"
CACHE_TTL_SECONDS = 14 * 24 * 3600

# Always probed even when CT returns nothing. Cheap, and covers hosts that
# never got a public certificate (plain-HTTP legacy kit, internal CAs).
FALLBACK_HOSTNAMES = (
    "vpn", "vpn2", "remote", "access", "connect", "gateway", "gw",
    "mail", "mail2", "webmail", "owa", "exchange", "autodiscover",
    "portal", "sso", "adfs", "citrix", "extranet", "secure",
    "mft", "files", "transfer", "ftp",
)

# Hostnames worth probing first when a domain publishes hundreds. Ordering
# matters because callers cap how many they probe.
EDGE_HINTS = (
    "vpn", "remote", "portal", "mail", "owa", "exchange", "citrix", "sso",
    "adfs", "access", "connect", "extranet", "gateway", "secure", "mft",
    "transfer", "files", "webmail", "autodiscover",
)

# Never worth probing: wildcards and environments that are not the estate.
_SKIP_PREFIXES = ("*", "_")


def _from_crtsh(domain: str, timeout: float = CRTSH_TIMEOUT) -> Set[str]:
    try:
        r = httpx.get(
            CRTSH_URL,
            params={"q": f"%.{domain}", "output": "json"},
            timeout=timeout,
            headers={"User-Agent": "VeraBot/1.0"},
        )
        if r.status_code != 200:
            logger.info("crt.sh HTTP %s for %s", r.status_code, domain)
            return set()
        rows = r.json()
    except Exception as e:  # noqa: BLE001
        logger.info("crt.sh lookup failed for %s: %s", domain, type(e).__name__)
        return set()

    out: Set[str] = set()
    for row in rows:
        for name in (row.get("name_value") or "").split("\n"):
            name = name.strip().lower().lstrip("*.")
            if not name or "@" in name:
                continue
            if name.startswith(_SKIP_PREFIXES):
                continue
            if name == domain or name.endswith("." + domain):
                out.add(name)
    return out


def rank_for_probing(hosts: Set[str], domain: str) -> list:
    """Edge-looking hostnames first, then shortest (usually most central)."""
    def key(h: str):
        label = h[: -(len(domain) + 1)] if h.endswith("." + domain) else h
        first = label.split(".")[-1] if label else ""
        return (not any(hint in first for hint in EDGE_HINTS), len(h), h)

    return sorted(hosts, key=key)


def discover_subdomains(
    domain: str,
    db=None,
    use_cache: bool = True,
    timeout: float = CRTSH_TIMEOUT,
) -> list:
    """Ranked hostnames for `domain`, CT-derived plus the wordlist floor.

    `db` is optional so the function stays usable from scripts and tests; pass
    a Supabase client to get the 14-day cache.
    """
    if not domain:
        return []
    domain = domain.strip().lower().lstrip(".")

    cached = None
    if db is not None and use_cache:
        try:
            from app.services.enrichers.cache import get_cached
            cached = get_cached(db, domain, CACHE_TYPE)
        except Exception as e:  # noqa: BLE001
            logger.debug("subdomain cache read failed for %s: %s", domain, e)

    if cached and cached.get("hosts"):
        hosts = set(cached["hosts"])
        logger.info("subdomains %s: %d from cache", domain, len(hosts))
    else:
        hosts = _from_crtsh(domain, timeout=timeout)
        if db is not None and hosts:
            try:
                from app.services.enrichers.cache import set_cache
                set_cache(
                    db, domain, CACHE_TYPE, None,
                    {"hosts": sorted(hosts)}, ttl_seconds=CACHE_TTL_SECONDS,
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("subdomain cache write failed for %s: %s", domain, e)
        logger.info("subdomains %s: %d from crt.sh", domain, len(hosts))

    hosts |= {f"{p}.{domain}" for p in FALLBACK_HOSTNAMES}
    return rank_for_probing(hosts, domain)
