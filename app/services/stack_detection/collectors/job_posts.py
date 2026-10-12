"""Job-post collector.

Extracts named security tools from already-scraped job-post text via the
curated alias dictionary. Independent corroboration source: if they hire for
it, they probably run it.

Tier B (per resolver design): a hiring mention is weaker than live infra,
so a single job-post hit yields MEDIUM confidence — not HIGH.
"""

import logging
import re
from typing import Dict, Iterable, List, Set, Tuple

from .._config_loader import alias_to_canonical, canonicalize, security_tools  # noqa: F401
from ..types import DetectedTech, Source, Tier

logger = logging.getLogger(__name__)

# Word-boundary matcher built per-alias. Pre-compile + memoize.
_ALIAS_PATTERNS: Dict[str, re.Pattern] = {}


def _pattern_for(alias: str) -> re.Pattern:
    pat = _ALIAS_PATTERNS.get(alias)
    if pat is None:
        pat = re.compile(r"\b" + re.escape(alias) + r"\b", re.IGNORECASE)
        _ALIAS_PATTERNS[alias] = pat
    return pat


def collect_job_posts(job_post_texts: Iterable[str]) -> List[DetectedTech]:
    """Scan job-post text bodies for named tools.

    Args:
        job_post_texts: iterable of raw text blobs (one per posting).

    Returns one DetectedTech per (canonical_tool, posting_index_first_seen).
    Multiple postings mentioning the same tool collapse to a single record
    with the count encoded in raw_evidence.
    """
    if not job_post_texts:
        return []

    canonicalizer = alias_to_canonical()
    tools = security_tools()

    hits: Dict[str, Tuple[int, Set[str]]] = {}
    for text in job_post_texts:
        if not text:
            continue
        found_this_posting: Set[str] = set()
        try:
            for alias_lower, canonical in canonicalizer.items():
                if canonical in found_this_posting:
                    continue
                if _pattern_for(alias_lower).search(text):
                    found_this_posting.add(canonical)
        except Exception:  # noqa: BLE001 — collector isolation
            logger.exception("job-post collector: skipping malformed text")
            continue

        for canonical in found_this_posting:
            count, aliases_seen = hits.get(canonical, (0, set()))
            aliases_seen.add(canonical)
            hits[canonical] = (count + 1, aliases_seen)

    out: List[DetectedTech] = []
    for canonical, (count, _aliases) in hits.items():
        out.append(
            DetectedTech(
                name=canonical,
                category=tools[canonical]["category"],
                source=Source.JOB_POST,
                raw_evidence=f"mentioned_in={count}_postings",
                source_tier=Tier.B,
            )
        )
    return out
