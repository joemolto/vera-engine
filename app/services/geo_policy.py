"""Client geography policy — deterministic geo gate for the threat surface.

A UK-only client (icp_config.target_locations = ["United Kingdom"]) was
getting US incidents ("U.S. bank discloses lapse", "New York Life data
breach") because threat→org matching only checked industry/tech overlap
and never geography — then an LLM wrote a relevance_reason rationalising
it in. Same class of bug as the tier cap: client policy enforced on some
surfaces, not others.

This is the single place geo policy is decided. It is deliberately
CONSERVATIVE — it only excludes a threat when ALL of:
  • the client targets a restricted set of countries (not global), AND
  • the threat text carries a STRONG marker of a country the client
    does NOT target, AND
  • it carries NO marker of a country the client DOES target, AND
  • it is not a global technical threat (CVE / CISA-KEV) — those affect
    everyone regardless of where the news originates.
When in doubt it KEEPS the threat (a missed exclusion is cheap; wrongly
dropping a relevant global threat is not).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Canonical country → strong, low-false-positive text markers (lowercased).
# Deliberately omits ambiguous tokens (e.g. bare "us"/"sec"/"cisa") that
# collide with ordinary words or global tech.
_COUNTRY_MARKERS: Dict[str, set] = {
    "united kingdom": {
        "united kingdom", "u.k.", " uk ", "(uk)", "british", "britain",
        "england", "scotland", "wales", "london", "fca", "ncsc",
        "bank of england", "hmrc", " ico ", "gov.uk",
    },
    "united states": {
        "united states", "u.s.", "u.s ", " usa", "american", "washington",
        "new york", "california", "texas", "congress", "white house",
        "the irs", " ftc ", "wall street", "federal reserve",
    },
    "canada": {"canada", "canadian", "ontario", "quebec"},
    "australia": {"australia", "australian", "sydney", "melbourne", "acsc"},
    "germany": {"germany", "german", "berlin", "bafin"},
    "france": {"france", "french", "paris"},
    "india": {"india", "indian", "mumbai", "bengaluru"},
}

# Map a free-text target_locations entry to a canonical country key.
_LOCATION_ALIASES: Dict[str, str] = {
    "uk": "united kingdom", "u.k.": "united kingdom",
    "united kingdom": "united kingdom", "england": "united kingdom",
    "britain": "united kingdom", "great britain": "united kingdom",
    "gb": "united kingdom",
    "us": "united states", "usa": "united states", "u.s.": "united states",
    "united states": "united states", "america": "united states",
    "canada": "canada", "australia": "australia",
    "germany": "germany", "france": "france", "india": "india",
}

# Target values that mean "no geo restriction" — never gate these.
_GLOBAL_TOKENS = {"global", "worldwide", "international", "anywhere", "any", ""}


def target_countries(icp_config: Optional[Dict[str, Any]]) -> set:
    """Canonical country set the client targets. Empty == unrestricted."""
    locs = (icp_config or {}).get("target_locations") or []
    if isinstance(locs, str):
        locs = [locs]
    out: set = set()
    for loc in locs:
        if not isinstance(loc, str):
            continue
        key = loc.strip().lower()
        if key in _GLOBAL_TOKENS:
            return set()  # explicit global → unrestricted
        out.add(_LOCATION_ALIASES.get(key, key))
    return out


def _countries_present(text: str) -> set:
    t = f" {text.lower()} "
    return {c for c, markers in _COUNTRY_MARKERS.items()
            if any(m in t for m in markers)}


def geo_excluded(
    *,
    text: str,
    icp_config: Optional[Dict[str, Any]],
    is_global_threat: bool = False,
) -> Tuple[bool, str]:
    """Should this threat be dropped for this client on geography grounds?

    Returns (excluded, reason). Conservative: excludes only a clearly
    foreign localized incident for a geo-restricted client.
    """
    if is_global_threat:
        return False, "global threat (CVE/KEV) — geo-agnostic"

    targets = target_countries(icp_config)
    if not targets:
        return False, "client has no geo restriction"

    present = _countries_present(text or "")
    if not present:
        return False, "no country markers in threat — kept"

    if present & targets:
        return False, f"threat references a target country ({present & targets})"

    foreign = present - targets
    if foreign:
        return True, (
            f"threat is about {sorted(foreign)}; client targets "
            f"{sorted(targets)} only"
        )
    return False, "no foreign marker"


def is_global_threat(threat: Dict[str, Any], analysed: Dict[str, Any]) -> bool:
    """Global technical threats (CVE / CISA-KEV / actively-exploited vulns)
    affect everyone regardless of where the article was written — never
    geo-gate these.
    """
    if (threat or {}).get("source") == "cisa_kev":
        return True
    a = analysed or {}
    if (a.get("threat_type") or "").lower() == "vulnerability":
        return True
    if a.get("is_actively_exploited"):
        return True
    title = ((threat or {}).get("title") or "").lower()
    return "cve-" in title
