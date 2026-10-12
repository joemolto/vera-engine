"""Phase 6 — compound cyber-pressure signals.

Atomic signals (one keyword, one boolean, one event) get composed into
higher-value compound signals that reflect *workflows* or *buying
motions*. A compound signal is what makes the dashboard feel
intelligent rather than a list of events.

Every compound signal has subject=account and inference=inferred
because by definition it's hypothesised from multiple atomic signals.
The strength is bounded — a compound signal can never be more
confident than its weakest required input.

Inputs: the list of signal rows emitted earlier in this enrichment
run (cyber, job-keyword, website-posture, person-level).
Output: additional signal rows to insert.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from app.services import signal_payload as sp

logger = logging.getLogger(__name__)


# Each compound rule has:
#   - signal_type: the compound type fired
#   - headline + detail: human copy
#   - any_of_groups: List[Set[str]] — at least one match required from
#     EACH group (logical AND-of-OR). All groups must produce at least
#     one match for the rule to fire.
COMPOUND_RULES = [
    {
        "signal_type": "enterprise_readiness_pressure",
        "headline": "Enterprise readiness pressure",
        "detail": "Sells (or moving) into enterprise, with a public security proof gap. Procurement friction likely.",
        "any_of_groups": [
            {"enterprise_page_detected", "enterprise_customer_detected", "regulated_customer_detected"},
            {"public_proof_not_visible"},
        ],
    },
    {
        "signal_type": "customer_security_review_pressure",
        "headline": "Customer security review pressure",
        "detail": "Multiple signals that customer security reviews are scaling — moving upmarket + GRC/questionnaire workflow keywords.",
        "any_of_groups": [
            {"enterprise_page_detected", "enterprise_customer_detected", "regulated_customer_detected"},
            {"job_mentions_security_questionnaires", "job_mentions_vendor_risk", "enterprise_trust_pressure"},
        ],
    },
    {
        "signal_type": "post_funding_security_maturity_push",
        "headline": "Post-funding security maturity push",
        "detail": "Funding event + security/GRC hiring. Typical pattern: board pushes security spend post-raise.",
        "any_of_groups": [
            {"recently_funded", "recent_acquisition"},
            {"hiring_security", "hiring_grc", "security_team_expansion", "person_security_leader", "compliance_workflow_pressure"},
        ],
    },
    {
        "signal_type": "upmarket_security_friction",
        "headline": "Upmarket security friction",
        "detail": "Enterprise sales hiring + missing public security proof. Sales team will hit security-review delays.",
        "any_of_groups": [
            {"hiring_sales_bd", "person_commercial_leader"},
            {"public_proof_not_visible"},
        ],
    },
    {
        "signal_type": "compliance_operationalisation",
        "headline": "Compliance operationalisation underway",
        "detail": "Multiple compliance/audit keywords + GRC hiring. Moving compliance from one-off project to ongoing workflow.",
        "any_of_groups": [
            {"job_mentions_audit", "compliance_workflow_pressure"},
            {"hiring_grc", "person_grc_leader", "job_mentions_security_questionnaires"},
        ],
    },
    {
        "signal_type": "security_team_scaling",
        "headline": "Security team scaling",
        "detail": "Active security headcount growth + leadership in place. Team-build phase — workflow/tooling decisions live.",
        "any_of_groups": [
            {"security_team_expansion", "hiring_security"},
            {"person_security_leader", "security_leadership_hire"},
        ],
    },
    {
        "signal_type": "cloud_security_scaling",
        "headline": "Cloud security scaling",
        "detail": "Cloud-security keywords across job posts + security/hiring activity. Cloud is moving from infra to security concern.",
        "any_of_groups": [
            {"cloud_security_pressure", "job_mentions_cloud_security"},
            {"hiring_security", "person_security_leader", "security_team_expansion"},
        ],
    },
    {
        "signal_type": "revenue_blocker_likely",
        "headline": "Revenue blocker likely",
        "detail": "Selling to regulated/enterprise + weak public security proof + commercial-team scale. Security gap is a deal blocker.",
        "any_of_groups": [
            {"hiring_sales_bd", "person_commercial_leader"},
            {"regulated_customer_detected", "enterprise_customer_detected", "enterprise_page_detected"},
            {"public_proof_not_visible"},
        ],
    },
]


def derive_compound_signals(
    org_id: str,
    lead_id: str,
    company_name: Optional[str],
    atomic_rows: List[Dict[str, Any]],
    expires_days: int = 180,
) -> List[Dict[str, Any]]:
    """Derive compound signal rows from the atomic set just emitted.

    Args:
        atomic_rows: every row produced by this enrichment run (cyber,
            job-keyword, website-posture, person-level). We only look
            at signal_type — the rule engine doesn't peek into
            signal_data.

    Returns rows ready for signal_writer.insert_signals.
    """
    fired_types: Set[str] = {r["signal_type"] for r in atomic_rows}
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=expires_days)
    ).isoformat()

    compound_rows: List[Dict[str, Any]] = []
    for rule in COMPOUND_RULES:
        if not _rule_matches(rule, fired_types):
            continue
        contributing = _collect_contributing(rule, fired_types)
        compound_rows.append({
            "org_id": org_id,
            "lead_id": lead_id,
            "signal_type": rule["signal_type"],
            "signal_source": "compound_derivation",
            "signal_data": sp.build_signal_payload(
                headline=(
                    f"{company_name}: {rule['headline']}"
                    if company_name
                    else rule["headline"]
                ),
                entity=company_name,
                detail=rule["detail"],
                contributing_signals=sorted(contributing),
            ),
            "signal_strength": _compound_strength(len(contributing)),
            "expires_at": expires_at,
            # Explicit classification (also in defaults map; defensive)
            "signal_subject_type": "account",
            "inference_level": "inferred",
            "source_quality": "inferred_from_multiple_sources",
        })

    return compound_rows


def _rule_matches(rule: Dict[str, Any], fired: Set[str]) -> bool:
    """Every any_of_groups entry must intersect `fired` at least once
    (AND-of-OR)."""
    for group in rule.get("any_of_groups", []):
        if not (group & fired):
            return False
    return True


def _collect_contributing(rule: Dict[str, Any], fired: Set[str]) -> Set[str]:
    out: Set[str] = set()
    for group in rule.get("any_of_groups", []):
        out |= group & fired
    return out


def _compound_strength(contributing_count: int) -> float:
    """More contributing atomic signals → higher confidence, capped at 0.9.

    Floor at 0.55 to keep compounds visibly above weak atomic signals
    but below high-confidence direct evidence.
    """
    if contributing_count <= 2:
        return 0.55
    if contributing_count == 3:
        return 0.7
    return min(0.9, 0.7 + 0.05 * (contributing_count - 3))
