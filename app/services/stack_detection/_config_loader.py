"""Loads + caches YAML configs at import time."""

from functools import lru_cache
from importlib import resources
from typing import Any, Dict, List

import yaml

from .types import Category


@lru_cache(maxsize=1)
def security_tools() -> Dict[str, Dict[str, Any]]:
    """Canonical tool name -> {category, aliases[]}."""
    raw = _load("security_tools.yaml")
    out: Dict[str, Dict[str, Any]] = {}
    for canonical, entry in raw.items():
        out[canonical] = {
            "category": Category(entry["category"]),
            "aliases": [a.lower() for a in entry.get("aliases", [])],
        }
    return out


@lru_cache(maxsize=1)
def vendor_patterns() -> List[Dict[str, Any]]:
    """Ordered list of DNS pattern rules."""
    raw = _load("vendor_patterns.yaml")
    return [
        {
            "match": p["match"].lower(),
            "vendor": p["vendor"],
            "category": Category(p["category"]),
            "record_types": set(p["record_types"]),
        }
        for p in raw.get("patterns", [])
    ]


@lru_cache(maxsize=1)
def alias_to_canonical() -> Dict[str, str]:
    """Lowercased alias -> canonical tool name. Strict matching only."""
    out: Dict[str, str] = {}
    for canonical, entry in security_tools().items():
        out[canonical.lower()] = canonical
        for alias in entry["aliases"]:
            out[alias] = canonical
    return out


# Product-line suffixes a vendor name commonly carries that we want to strip
# before matching ("Mimecast Secure Email Gateway" -> "Mimecast"). Order
# matters — longest first so "Secure Email Gateway" is stripped before "Email".
_PRODUCT_SUFFIXES = [
    "Secure Email Gateway",
    "Targeted Threat Protection",
    "Application Monitoring",
    "Endpoint Protection",
    "Email Security",
    "Email Protection",
    "Identity Cloud",
    "Cloud Platform",
    "Workforce Identity",
    "Customer Identity",
    "Enterprise Security",
    "Cloud",
    "Inc.",
    "Inc",
    "Ltd.",
    "Ltd",
    "LLC",
    "Corp.",
    "Corporation",
]


@lru_cache(maxsize=4096)
def canonicalize(raw_name: str) -> str | None:
    """Resolve a raw tech name to a canonical name from the dictionary.

    Order:
      1. Exact alias hit (lowercased) — strict.
      2. Strip a known product-line suffix and try again.
      3. Substring match: does any canonical name appear as a whole-word
         prefix of the raw name? (Mimecast Secure Email Gateway -> Mimecast)
         Checked longest-canonical-first so "AWS Glue" doesn't accidentally
         resolve to "AWS" when both could be in the dictionary.
    Returns None if no rule matches — caller decides whether to keep the
    raw name as-is or drop the detection.
    """
    if not raw_name:
        return None
    lower = raw_name.strip().lower()

    aliases = alias_to_canonical()
    hit = aliases.get(lower)
    if hit:
        return hit

    # Suffix-strip pass
    stripped = raw_name.strip()
    for suffix in _PRODUCT_SUFFIXES:
        if stripped.lower().endswith(" " + suffix.lower()):
            candidate = stripped[: -(len(suffix) + 1)].strip()
            hit = aliases.get(candidate.lower())
            if hit:
                return hit

    # Whole-word prefix pass against canonical names (longest first)
    canonicals_by_len = sorted(security_tools().keys(), key=len, reverse=True)
    for canonical in canonicals_by_len:
        cl = canonical.lower()
        if lower == cl:
            return canonical
        if lower.startswith(cl + " "):
            return canonical

    return None


def _load(filename: str) -> Dict[str, Any]:
    path = resources.files("app.services.stack_detection.config").joinpath(filename)
    return yaml.safe_load(path.read_text())
