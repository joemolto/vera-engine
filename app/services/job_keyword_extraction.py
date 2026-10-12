"""Phase 4 — job description keyword extraction.

Engine already pulls raw job postings (title + description) into
``enrichment_data["jobs"]["raw_postings"]`` from LinkedIn, Apollo,
Indeed, and the website careers page. Each posting is a dict shaped
``{"title": str, "description": str, "source": str}``.

This module scans the JD text for the keyword patterns defined in the
2026-05-28 spec, fires per-keyword `job_mentions_*` signals when found,
and rolls them up into five pressure categories that are stronger
signals for cyber buying motion than the individual keywords alone.

Design notes:
- Regex over JD text. No LLM. Deterministic + cheap + reproducible.
- Patterns are word-boundary aware so "SOC analyst" isn't matched as
  "SOC 2" — important since the same letters appear in unrelated
  security contexts.
- Each signal carries a snippet (≤180 char excerpt around the match)
  so the dashboard can render evidence inline.
- We deduplicate per signal_type per lead — one `job_mentions_soc2`
  signal per enrichment, not one per matching posting.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.services import signal_payload as sp

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Atomic keyword patterns.
#
# Each entry: (signal_type, list_of_regex_patterns). Patterns use
# word boundaries where the keyword is ambiguous. Order doesn't matter —
# we OR them per signal type.
# ---------------------------------------------------------------------------

KEYWORD_PATTERNS: List[Tuple[str, List[re.Pattern]]] = [
    (
        "job_mentions_soc2",
        [
            re.compile(r"\bSOC[\s\-]?2\b", re.IGNORECASE),
            re.compile(r"\bSOC\s*II\b", re.IGNORECASE),
            re.compile(r"Service Organization Control", re.IGNORECASE),
        ],
    ),
    (
        "job_mentions_iso27001",
        [
            re.compile(r"ISO[\s/\-]?27001", re.IGNORECASE),
            re.compile(r"ISO[\s/\-]?27\d{3}", re.IGNORECASE),
        ],
    ),
    (
        "job_mentions_security_questionnaires",
        [
            re.compile(r"security questionnaire", re.IGNORECASE),
            re.compile(r"customer security review", re.IGNORECASE),
            re.compile(r"prospect security review", re.IGNORECASE),
            re.compile(r"due\s*diligence questionnaire", re.IGNORECASE),
            re.compile(r"\bRFP\b.*security", re.IGNORECASE),
        ],
    ),
    (
        "job_mentions_vendor_risk",
        [
            re.compile(r"vendor risk", re.IGNORECASE),
            re.compile(r"third[\s\-]?party risk", re.IGNORECASE),
            re.compile(r"\bTPRM\b", re.IGNORECASE),
            re.compile(r"supplier risk", re.IGNORECASE),
        ],
    ),
    (
        "job_mentions_audit",
        [
            re.compile(r"\baudit(?:s|ing|ed)?\b", re.IGNORECASE),
            re.compile(r"evidence collection", re.IGNORECASE),
            re.compile(r"control testing", re.IGNORECASE),
            re.compile(r"control evidence", re.IGNORECASE),
            re.compile(r"\bSOX\b", re.IGNORECASE),
        ],
    ),
    (
        "job_mentions_cloud_security",
        [
            re.compile(r"cloud security", re.IGNORECASE),
            re.compile(r"\bCNAPP\b", re.IGNORECASE),
            re.compile(r"\bCSPM\b", re.IGNORECASE),
            re.compile(r"\bCWPP\b", re.IGNORECASE),
            re.compile(r"\b(AWS|Azure|GCP)\b.*security", re.IGNORECASE),
            re.compile(r"\bWiz\b", re.IGNORECASE),
            re.compile(r"\bOrca Security\b", re.IGNORECASE),
            re.compile(r"\bLacework\b", re.IGNORECASE),
            re.compile(r"\bPalo Alto.*Prisma\b", re.IGNORECASE),
        ],
    ),
    (
        "job_mentions_identity",
        [
            re.compile(r"\bIAM\b", re.IGNORECASE),
            re.compile(r"identity\s+and\s+access", re.IGNORECASE),
            re.compile(r"\bOkta\b", re.IGNORECASE),
            re.compile(r"\bEntra\b", re.IGNORECASE),
            re.compile(r"\bAzure AD\b", re.IGNORECASE),
            re.compile(r"\bPing Identity\b", re.IGNORECASE),
            re.compile(r"\bAuth0\b", re.IGNORECASE),
            re.compile(r"\bSSO\b", re.IGNORECASE),
            re.compile(r"\bSAML\b", re.IGNORECASE),
            re.compile(r"\bSCIM\b", re.IGNORECASE),
        ],
    ),
    (
        "job_mentions_siem",
        [
            re.compile(r"\bSIEM\b", re.IGNORECASE),
            re.compile(r"\bSplunk\b", re.IGNORECASE),
            re.compile(r"\bSentinel\b(?!\s*Edge)", re.IGNORECASE),  # Microsoft Sentinel, not Sentinel Edge
            re.compile(r"\bQRadar\b", re.IGNORECASE),
            re.compile(r"\bSumo Logic\b", re.IGNORECASE),
            re.compile(r"\bChronicle\b", re.IGNORECASE),
            re.compile(r"\bExabeam\b", re.IGNORECASE),
            re.compile(r"\bSecuronix\b", re.IGNORECASE),
        ],
    ),
    (
        "job_mentions_edr",
        [
            re.compile(r"\bEDR\b", re.IGNORECASE),
            re.compile(r"\bXDR\b", re.IGNORECASE),
            re.compile(r"\bCrowdStrike\b", re.IGNORECASE),
            re.compile(r"\bFalcon\b.*endpoint", re.IGNORECASE),
            re.compile(r"\bSentinelOne\b", re.IGNORECASE),
            re.compile(r"Defender\s+for\s+Endpoint", re.IGNORECASE),
            re.compile(r"\bCarbon Black\b", re.IGNORECASE),
            re.compile(r"\bSophos\b.*endpoint", re.IGNORECASE),
        ],
    ),
    (
        "job_mentions_vuln_mgmt",
        [
            re.compile(r"vulnerability management", re.IGNORECASE),
            re.compile(r"\bVMaaS\b", re.IGNORECASE),
            re.compile(r"\bTenable\b", re.IGNORECASE),
            re.compile(r"\bQualys\b", re.IGNORECASE),
            re.compile(r"\bRapid7\b", re.IGNORECASE),
            re.compile(r"\bNessus\b", re.IGNORECASE),
            re.compile(r"\bSnyk\b", re.IGNORECASE),
            re.compile(r"patch management", re.IGNORECASE),
        ],
    ),
    (
        "job_mentions_incident_response",
        [
            re.compile(r"incident response", re.IGNORECASE),
            re.compile(r"\bIR\s+playbook", re.IGNORECASE),
            re.compile(r"\btabletop", re.IGNORECASE),
            re.compile(r"\bSOAR\b", re.IGNORECASE),
            re.compile(r"breach response", re.IGNORECASE),
            re.compile(r"\bSOC\b(?:[\s,\.])", re.IGNORECASE),  # SOC followed by punctuation
        ],
    ),
    (
        "job_mentions_devsecops",
        [
            re.compile(r"\bDevSecOps\b", re.IGNORECASE),
            re.compile(r"\bAppSec\b", re.IGNORECASE),
            re.compile(r"application security", re.IGNORECASE),
            re.compile(r"secure\s+SDLC", re.IGNORECASE),
            re.compile(r"code security", re.IGNORECASE),
            re.compile(r"\bSAST\b", re.IGNORECASE),
            re.compile(r"\bDAST\b", re.IGNORECASE),
            re.compile(r"\bSCA\b\s+scan", re.IGNORECASE),
        ],
    ),
]


# ---------------------------------------------------------------------------
# Pressure-category rollups — see brief 2026-05-28 ("Roll these up into
# pressure categories"). Compound signals are stronger than the atomic
# keyword count because they imply a *workflow*, not just a tool.
# ---------------------------------------------------------------------------

PRESSURE_CATEGORIES: List[Tuple[str, List[str]]] = [
    (
        "compliance_workflow_pressure",
        [
            "job_mentions_soc2",
            "job_mentions_iso27001",
            "job_mentions_audit",
            "job_mentions_security_questionnaires",
            "job_mentions_vendor_risk",
        ],
    ),
    (
        "security_operations_pressure",
        [
            "job_mentions_siem",
            "job_mentions_edr",
            "job_mentions_incident_response",
            "job_mentions_vuln_mgmt",
        ],
    ),
    (
        "cloud_security_pressure",
        ["job_mentions_cloud_security"],
    ),
    (
        "appsec_pressure",
        ["job_mentions_devsecops"],
    ),
    (
        "enterprise_trust_pressure",
        [
            "job_mentions_security_questionnaires",
            "job_mentions_vendor_risk",
            "job_mentions_audit",
        ],
    ),
]


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def extract_job_signals(
    org_id: str,
    lead_id: str,
    company_name: Optional[str],
    raw_postings: List[Dict[str, Any]],
    expires_days: int = 90,
) -> List[Dict[str, Any]]:
    """Return signal rows for keyword matches across the raw job postings.

    Args:
        org_id: Org owning the lead.
        lead_id: Lead the signals attach to.
        company_name: For headline rendering.
        raw_postings: As stored under enrichment_data["jobs"]["raw_postings"].
        expires_days: Signal TTL in days.

    Returns:
        A list of dicts ready for ``signal_writer.insert_signals``.
        Auto-classification (subject=job, source=first_party, observed)
        is applied at write time — we don't need to set it here.
    """
    if not raw_postings:
        return []

    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=expires_days)
    ).isoformat()

    # Track which atomic signal types fired so we can roll up to pressure cats
    fired_atomic: Dict[str, Dict[str, Any]] = {}
    posting_count = len(raw_postings)

    for signal_type, patterns in KEYWORD_PATTERNS:
        match_details = _scan_postings(raw_postings, patterns)
        if not match_details:
            continue
        fired_atomic[signal_type] = match_details

    rows: List[Dict[str, Any]] = []

    # Atomic signal rows
    for signal_type, details in fired_atomic.items():
        snippet = details["snippet"]
        match_count = details["count"]
        matched_titles = details["titles"][:3]  # don't dump every title
        readable = signal_type.replace("job_mentions_", "").replace("_", " ")
        rows.append({
            "org_id": org_id,
            "lead_id": lead_id,
            "signal_type": signal_type,
            "signal_source": "jobs_keyword_extraction",
            "signal_data": sp.build_signal_payload(
                headline=(
                    f"{company_name} job posts mention {readable}"
                    if company_name
                    else f"Job posts mention {readable}"
                ),
                entity=company_name,
                detail=(
                    f"Found in {match_count} of {posting_count} postings"
                    + (
                        f" (e.g. {', '.join(matched_titles)})"
                        if matched_titles
                        else ""
                    )
                ),
                source_url=details.get("source_url"),
                # Roles where the keyword appeared — useful for the
                # dashboard to render "Why it matters" with context.
                matched_titles=details["titles"],
                match_count=match_count,
            ),
            "signal_strength": _strength_for_match(match_count, posting_count),
            "expires_at": expires_at,
            # Override subject + level explicitly (also in defaults map,
            # but defensive — same data lives in two places by design).
            "signal_subject_type": "job",
            "inference_level": "observed",
            "source_quality": "first_party",
            "snippet": snippet,
        })

    # Pressure-category rollups — fire when ≥2 atomic signals in the
    # category triggered (one keyword could be a false positive; two
    # is a pattern).
    fired_types = set(fired_atomic.keys())
    for category, member_types in PRESSURE_CATEGORIES:
        members_present = [t for t in member_types if t in fired_types]
        if len(members_present) < 2:
            continue
        readable_members = ", ".join(
            t.replace("job_mentions_", "").replace("_", " ") for t in members_present
        )
        rows.append({
            "org_id": org_id,
            "lead_id": lead_id,
            "signal_type": category,
            "signal_source": "jobs_keyword_extraction",
            "signal_data": sp.build_signal_payload(
                headline=(
                    f"{company_name} job posts indicate {category.replace('_', ' ')}"
                    if company_name
                    else f"Job posts indicate {category.replace('_', ' ')}"
                ),
                entity=company_name,
                detail=(
                    f"Multiple keyword signals: {readable_members}"
                ),
                contributing_signals=members_present,
            ),
            "signal_strength": min(0.9, 0.5 + 0.1 * len(members_present)),
            "expires_at": expires_at,
            "signal_subject_type": "account",  # rollup is account-level
            "inference_level": "derived",
            "source_quality": "inferred_from_multiple_sources",
        })

    return rows


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------

def _scan_postings(
    postings: List[Dict[str, Any]],
    patterns: List[re.Pattern],
) -> Optional[Dict[str, Any]]:
    """Scan all postings; return aggregate details or None if no match."""
    matched_titles: List[str] = []
    first_snippet: Optional[str] = None
    first_source_url: Optional[str] = None
    count = 0
    for p in postings:
        if not isinstance(p, dict):
            continue
        title = (p.get("title") or "").strip()
        description = (p.get("description") or "").strip()
        haystack = f"{title}\n{description}"
        if not haystack.strip():
            continue
        for pat in patterns:
            m = pat.search(haystack)
            if m:
                count += 1
                if title and title not in matched_titles:
                    matched_titles.append(title)
                if first_snippet is None:
                    first_snippet = _snippet_around(haystack, m.start(), m.end())
                if first_source_url is None and p.get("url"):
                    first_source_url = p.get("url")
                break  # one match per posting is enough
    if count == 0:
        return None
    return {
        "count": count,
        "titles": matched_titles,
        "snippet": first_snippet,
        "source_url": first_source_url,
    }


def _snippet_around(text: str, start: int, end: int, padding: int = 80) -> str:
    """Return ≤180 char excerpt centred on the match."""
    s = max(0, start - padding)
    e = min(len(text), end + padding)
    excerpt = text[s:e].replace("\n", " ").strip()
    # Add ellipses if truncated
    if s > 0:
        excerpt = "…" + excerpt
    if e < len(text):
        excerpt = excerpt + "…"
    return excerpt[:200]


def _strength_for_match(match_count: int, posting_count: int) -> float:
    """Match volume → strength band, capped to 0.9.

    1 match = weak (0.5). 3+ matches across postings = strong (>=0.8).
    Higher when the keyword appears across multiple distinct postings,
    not just one outlier.
    """
    if posting_count == 0:
        return 0.5
    ratio = match_count / max(posting_count, 1)
    if match_count >= 3 or ratio >= 0.3:
        return 0.85
    if match_count >= 2:
        return 0.7
    return 0.55
