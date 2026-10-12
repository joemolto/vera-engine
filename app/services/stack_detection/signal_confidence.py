"""Confidence cap helper for callers building stack-dependent findings.

Usage:
    finding_confidence = cap_confidence(signal_confidence, stack_confidence)

So a CVE match scored HIGH on a stack tech detected at MEDIUM yields a
MEDIUM finding. Callers (signal builders) own the decision to gate on the
result — this module only computes the cap.

Outreach policy (enforced by signal-firing layer, not here):
    only HIGH findings are allowed to lead outreach.
"""

from .types import Confidence

_RANK = {Confidence.HIGH: 2, Confidence.MEDIUM: 1, Confidence.LOW: 0}
_BY_RANK = {v: k for k, v in _RANK.items()}


def cap_confidence(signal_confidence: Confidence, stack_confidence: Confidence) -> Confidence:
    """Return min(signal, stack) on the HIGH > MEDIUM > LOW scale."""
    return _BY_RANK[min(_RANK[signal_confidence], _RANK[stack_confidence])]
