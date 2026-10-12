"""Confidence-scored tech-stack detection.

External sources only. Fuses PredictLeads + DNS + job-post extraction into
ResolvedTech records with HIGH/MEDIUM confidence. Callers cap signal
confidence with the stack confidence before firing a finding — only HIGH
findings are eligible to lead outreach.
"""

from .collectors.dns_email import collect_dns
from .merge import merge_dns_into_tech_stack
from .resolve_confidence import resolve_stack
from .signal_confidence import cap_confidence
from .types import (
    Category,
    Confidence,
    DetectedTech,
    ResolvedTech,
    Source,
    Tier,
)

__all__ = [
    "Category",
    "Confidence",
    "DetectedTech",
    "ResolvedTech",
    "Source",
    "Tier",
    "cap_confidence",
    "collect_dns",
    "merge_dns_into_tech_stack",
    "resolve_stack",
]
