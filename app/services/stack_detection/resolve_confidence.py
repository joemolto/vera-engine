"""Fuses DetectedTech from multiple collectors into ResolvedTech with confidence.

Rules (with job-post tiered B):
  - 2+ independent Sources agree on a tech       -> HIGH
  - 1 Tier-A detection (DNS or PL-strong)        -> HIGH
  - PL-only, all Tier B                          -> MEDIUM
  - Job-post-only                                -> MEDIUM
  - Uncorroborated low signal                    -> LOW (dropped from output)

Conflict rule (category disagreement on same tech name):
  Highest-tier source wins. Ties broken by source precedence:
  DNS > PREDICTLEADS > JOB_POST. The losing detection's evidence is still
  preserved in the evidence tuple so reviewers see the conflict.
"""

from collections import defaultdict
from typing import Iterable, List, Tuple

from .collectors.dns_email import collect_dns
from .collectors.job_posts import collect_job_posts
from .collectors.predictleads import collect_predictleads
from .types import Category, Confidence, DetectedTech, ResolvedTech, Source, Tier

# Source precedence for category-conflict tie-breaking (most authoritative first)
_SOURCE_PRECEDENCE = {Source.DNS: 0, Source.PREDICTLEADS: 1, Source.JOB_POST: 2}


def resolve(detections: Iterable[DetectedTech]) -> List[ResolvedTech]:
    """Pure function: group detections by tech name, score, return ResolvedTech."""
    by_name: dict[str, List[DetectedTech]] = defaultdict(list)
    for d in detections:
        by_name[d.name].append(d)

    out: List[ResolvedTech] = []
    for name, group in by_name.items():
        resolved = _resolve_one(name, group)
        if resolved.confidence == Confidence.LOW:
            continue
        out.append(resolved)
    return out


def resolve_stack(
    domain: str,
    pl_technologies: list | None = None,
    job_post_texts: list | None = None,
) -> List[ResolvedTech]:
    """End-to-end: run all three collectors for `domain` and resolve.

    PL data and job-post text are passed in (already fetched by upstream
    enrichers). DNS is fetched live here. Each collector is isolated so a
    failure in one yields [] from that source, not a crash overall.
    """
    detections: List[DetectedTech] = []
    detections.extend(collect_predictleads(pl_technologies or []))
    detections.extend(collect_dns(domain))
    detections.extend(collect_job_posts(job_post_texts or []))
    return resolve(detections)


def _resolve_one(name: str, group: List[DetectedTech]) -> ResolvedTech:
    distinct_sources = {d.source for d in group}
    has_tier_a = any(d.source_tier == Tier.A for d in group)

    if len(distinct_sources) >= 2:
        confidence = Confidence.HIGH
    elif has_tier_a:
        confidence = Confidence.HIGH
    else:
        # Single-source, all Tier B. PL-only or job-post-only -> MEDIUM.
        only_source = next(iter(distinct_sources))
        if only_source in (Source.PREDICTLEADS, Source.JOB_POST):
            confidence = Confidence.MEDIUM
        else:
            confidence = Confidence.LOW

    category = _resolve_category(group)
    sources = tuple(sorted(distinct_sources, key=lambda s: _SOURCE_PRECEDENCE[s]))
    evidence = tuple(f"[{d.source.value}] {d.raw_evidence}" for d in group)

    return ResolvedTech(
        name=name,
        category=category,
        confidence=confidence,
        sources=sources,
        evidence=evidence,
    )


def _resolve_category(group: List[DetectedTech]) -> Category:
    """Highest-tier source wins; tie-break on source precedence."""
    def rank(d: DetectedTech) -> Tuple[int, int]:
        tier_rank = 0 if d.source_tier == Tier.A else 1
        return tier_rank, _SOURCE_PRECEDENCE[d.source]

    winner = min(group, key=rank)
    return winner.category
