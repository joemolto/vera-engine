"""Client sector-relevance policy — deterministic gate for the threat surface.

Sibling of geo_policy. The LLM `analyse_threat` over-tags
`affected_industries` (it labelled an EdTech ransomware story "saas"),
so a deterministic industry intersection then "matched" it to a SaaS/
fintech client and an LLM wrote a relevance_reason rationalising it.
Same class of bug: client policy enforced on some surfaces, not this one.

CONSERVATIVE — excludes a threat for a client only when ALL of:
  • the client has target industries configured, AND
  • the threat text carries a STRONG marker of an off-target sector
    (e.g. education / edtech), AND
  • the threat text references NONE of the client's target industries,
    AND
  • it is not a global technical threat (CVE / CISA-KEV — geo_policy's
    is_global_threat; those are sector-agnostic).
When unsure it KEEPS the threat (a missed exclusion is cheap; dropping a
genuinely relevant threat is not).
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# Off-target sectors that recur as false positives, with strong,
# low-false-positive markers (lowercased, space-padded matching).
_OFF_SECTOR_MARKERS: Dict[str, set] = {
    "education": {
        "edtech", "ed tech", "ed-tech", "education provider",
        "education firm", "schools", "school district", "k-12", "k12",
        "university", "universities", "student data", "students",
        "higher education", "learning management", "instructure",
        "canvas lms", "powerschool", "blackboard",
    },
    "agriculture": {"agriculture", "farming", "agritech", "crop "},
    "hospitality": {"hospitality", "hotel chain", "restaurant chain"},
    "construction": {"construction firm", "construction company"},
}

# Words that signal a client's target industry is actually present in the
# threat text. Built from icp_config.target_industries / target_verticals;
# each multi-word industry also contributes its individual words so
# "financial services" matches an article saying "financial".
_STOPWORDS = {"and", "the", "of", "services", "service", "tech",
              "technology", "software", "&"}


def _target_industry_tokens(icp_config: Optional[Dict[str, Any]]) -> set:
    icp = icp_config or {}
    inds = list(icp.get("target_industries") or [])
    inds += list(icp.get("target_verticals") or [])
    tokens: set = set()
    for ind in inds:
        if not isinstance(ind, str):
            continue
        phrase = ind.strip().lower()
        if len(phrase) >= 3:
            tokens.add(phrase)
        for w in phrase.replace("&", " ").split():
            if len(w) >= 4 and w not in _STOPWORDS:
                tokens.add(w)
    return tokens


def _off_sectors_present(text: str) -> set:
    t = f" {text.lower()} "
    return {s for s, markers in _OFF_SECTOR_MARKERS.items()
            if any(m in t for m in markers)}


def sector_excluded(
    *,
    text: str,
    icp_config: Optional[Dict[str, Any]],
    is_global_threat: bool = False,
) -> Tuple[bool, str]:
    """Should this threat be dropped for this client on sector grounds?

    Returns (excluded, reason). Conservative — see module docstring.
    """
    if is_global_threat:
        return False, "global threat (CVE/KEV) — sector-agnostic"

    targets = _target_industry_tokens(icp_config)
    if not targets:
        return False, "client has no target industries configured"

    off = _off_sectors_present(text or "")
    if not off:
        return False, "no off-sector markers in threat — kept"

    t = f" {(text or '').lower()} "
    if any(tok in t for tok in targets):
        return False, "threat also references a target industry — kept"

    return True, (
        f"threat is about {sorted(off)}; client targets only "
        f"{sorted(list(targets))[:6]} and the text references none of them"
    )
