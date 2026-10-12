"""Merge DNS detections into the existing tech_stack output shape.

The legacy `enrich_tech_stack` pipeline (PL → HTML+Claude → Apollo) writes
`enrichment_data["tech_stack"]["technologies_detected"]` as a list of dicts
shaped roughly:

    {"name": str, "category": str, "source"?: str, "detection_methods"?: [str], ...}

DNS is the highest-confidence source (live observable infra). This helper
runs `collect_dns(domain)` and:

  1. For every existing tech that DNS independently confirms (name match),
     appends "dns" to its `detection_methods`. The dashboard's `techTier`
     in lib/dashboard/cyberPressure.ts treats any tech with "dns" in
     detection_methods as `Confirmed`.

  2. For every DNS detection without a matching existing tech, APPENDS a
     new dict (name, category, source="dns", detection_methods=["dns"],
     evidence=...). These show up as new `Confirmed` techs the legacy
     pipeline missed.

Pure function on the tech_stack dict — no DB or network side effects
beyond the DNS lookup. Returns the modified dict (mutates in place +
returns for chaining). Safe to call with `None` or `{"error": ...}`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .collectors.dns_email import collect_dns
from .types import DetectedTech

logger = logging.getLogger(__name__)


def merge_dns_into_tech_stack(
    domain: str,
    tech_stack: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Merge live DNS detections into the tech_stack dict.

    Returns the (mutated) dict. If `tech_stack` is None or an error
    dict, a fresh `{technologies_detected: []}` is created so DNS-only
    techs still surface.
    """
    if tech_stack is None or tech_stack.get("error"):
        # Don't drop DNS findings just because Claude/Apollo failed — still
        # emit them under a fresh dict.
        tech_stack = {"technologies_detected": [], "tech_stack_source": "dns_only"}

    dns_detections: List[DetectedTech] = []
    try:
        dns_detections = collect_dns(domain)
    except Exception as e:  # noqa: BLE001
        logger.warning("DNS collection failed for %s: %s — continuing", domain, e)
        return tech_stack

    if not dns_detections:
        return tech_stack

    existing = tech_stack.get("technologies_detected") or tech_stack.get("technologies") or []
    if not isinstance(existing, list):
        existing = []

    # Build an index of existing techs by lowercased name for matching
    by_name: Dict[str, Dict[str, Any]] = {}
    for entry in existing:
        if isinstance(entry, dict) and entry.get("name"):
            by_name[str(entry["name"]).strip().lower()] = entry

    added = 0
    enriched = 0
    for dns in dns_detections:
        key = dns.name.strip().lower()
        match = by_name.get(key)
        if match is not None:
            # DNS observes live infra, so its category is authoritative when
            # it disagrees with an inferred one — e.g. a prior pass tagged
            # "Mimecast" as generic "security"; the MX record proves it's
            # email_security. Without this the vendor stays mis-categorised
            # (and can vanish from the dossier if the bad category has no
            # render bucket).
            dns_cat = (
                dns.category.value if hasattr(dns.category, "value") else str(dns.category)
            )
            if dns_cat and match.get("category") != dns_cat:
                match["category"] = dns_cat
            methods = match.get("detection_methods") or []
            if not isinstance(methods, list):
                methods = [str(methods)]
            if "dns" not in [str(m).lower() for m in methods]:
                methods.append("dns")
                match["detection_methods"] = methods
                # Append DNS evidence too — useful for the dossier "via"
                # tooltip and audit trail.
                existing_evidence = match.get("dns_evidence") or []
                if not isinstance(existing_evidence, list):
                    existing_evidence = [str(existing_evidence)]
                existing_evidence.append(dns.raw_evidence)
                match["dns_evidence"] = existing_evidence
                enriched += 1
        else:
            existing.append({
                "name": dns.name,
                "category": dns.category.value if hasattr(dns.category, "value") else str(dns.category),
                "source": "dns",
                "detection_methods": ["dns"],
                "dns_evidence": [dns.raw_evidence],
            })
            by_name[key] = existing[-1]
            added += 1

    tech_stack["technologies_detected"] = existing
    # Annotate the source so downstream consumers can tell DNS contributed.
    if tech_stack.get("tech_stack_source"):
        if "dns" not in tech_stack["tech_stack_source"]:
            tech_stack["tech_stack_source"] = f"{tech_stack['tech_stack_source']}+dns"
    else:
        tech_stack["tech_stack_source"] = "dns"

    # Surface a small audit summary so logs make sense
    tech_stack["dns_detections"] = {
        "added": added,
        "enriched_existing": enriched,
        "total_dns_records": len(dns_detections),
    }
    logger.info(
        "DNS merge for %s: +%d new, %d existing enriched (from %d DNS hits)",
        domain, added, enriched, len(dns_detections),
    )
    return tech_stack
