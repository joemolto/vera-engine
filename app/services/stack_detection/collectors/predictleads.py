"""PredictLeads collector.

Wraps the existing _fetch_technology() shape from
app/services/enrichers/predictleads.py:

    {"name": str, "category": str, "behind_firewall": bool, "source": str}

The `source` field is PredictLeads' own detection-method attribution
("DNS" | "JobPost" | "HtmlSourceCode" | "DataAnalysis" | "unknown" | ...).
We carry it through as DetectedTech.pl_detection_method and use it to assign
tier — PL is only Tier A when PL itself observed something concrete.
"""

import logging
from typing import Any, Dict, List

from .._config_loader import canonicalize, security_tools
from ..types import Category, DetectedTech, Source, Tier

logger = logging.getLogger(__name__)

# PL detection methods that count as observable evidence on PL's side.
# Anything else (DataAnalysis, partner-graph inference, unknown) -> Tier B.
_STRONG_PL_METHODS = {"dns", "jobpost", "htmlsourcecode"}


def collect_predictleads(pl_technologies: List[Dict[str, Any]]) -> List[DetectedTech]:
    """Convert PL technologies into DetectedTech.

    Input is the `technologies` list from _fetch_technology()'s return value.
    Falls back to [] on any unexpected shape — never raises.
    """
    if not pl_technologies:
        return []

    tools = security_tools()
    out: List[DetectedTech] = []

    for entry in pl_technologies:
        try:
            raw_name = (entry.get("name") or "").strip()
            if not raw_name:
                continue

            canonical = canonicalize(raw_name)
            if canonical:
                category = tools[canonical]["category"]
                name = canonical
            else:
                # Unknown to our dictionary — keep it but mark OTHER. The
                # resolver still benefits from cross-source agreement.
                name = raw_name
                category = _map_pl_category(entry.get("category"))

            # `detection_methods` is the full provenance list (DNS, HtmlSourceCode,
            # JobPost, Connection). `source` is the strongest one for back-compat.
            methods = entry.get("detection_methods") or []
            if not methods:
                # Older cached entries / fallback path
                legacy = (entry.get("source") or "unknown").strip()
                methods = [legacy] if legacy else []
            pl_method = methods[0] if methods else "unknown"
            tier = (
                Tier.A
                if any(m.lower() in _STRONG_PL_METHODS for m in methods)
                else Tier.B
            )

            evidence = f"PredictLeads(methods={'+'.join(methods) or 'unknown'})"
            if entry.get("behind_firewall"):
                evidence += ",behind_firewall"

            out.append(
                DetectedTech(
                    name=name,
                    category=category,
                    source=Source.PREDICTLEADS,
                    raw_evidence=evidence,
                    source_tier=tier,
                    pl_detection_method=pl_method,
                )
            )
        except Exception:  # noqa: BLE001 — collector isolation
            logger.exception("predictleads collector: skipping malformed entry")
            continue

    return out


def _map_pl_category(pl_category: str | None) -> Category:
    """Best-effort map of PL's free-text category to our enum."""
    if not pl_category:
        return Category.OTHER
    c = pl_category.lower()
    if "security" in c and "email" in c:
        return Category.EMAIL_SECURITY
    if "cdn" in c or "waf" in c:
        return Category.WAF_CDN
    if "endpoint" in c or "edr" in c:
        return Category.EDR
    if "siem" in c or "logging" in c:
        return Category.SIEM
    if "identity" in c or "iam" in c or "sso" in c:
        return Category.IAM
    if "cloud" in c or "iaas" in c:
        return Category.CLOUD
    return Category.OTHER
