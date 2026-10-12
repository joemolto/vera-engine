"""DNS collector — resolves MX, SPF, DMARC, NS and maps to vendors.

Highest-confidence external source (observable live infra, not inference).
No API key needed. Per-record-type try/except so one failure returns [] for
that type rather than poisoning the whole collection.

NXDOMAIN, timeout, NoAnswer, and resolver errors all yield [] for the
affected record type — never raise.
"""

import logging
import re
from typing import List, Optional, Tuple

import dns.exception
import dns.resolver

from .._config_loader import vendor_patterns
from ..types import Category, DetectedTech, Source, Tier

logger = logging.getLogger(__name__)

DNS_TIMEOUT = 3.0
DNS_LIFETIME = 5.0


def collect_dns(domain: str, resolver: Optional[dns.resolver.Resolver] = None) -> List[DetectedTech]:
    """Run all DNS lookups for a domain and return matched DetectedTech.

    The `resolver` arg is for tests — production callers omit it.
    """
    if not domain:
        return []

    r = resolver or _default_resolver()

    detections: List[DetectedTech] = []
    detections.extend(_lookup_mx(r, domain))
    detections.extend(_lookup_txt(r, domain))  # SPF + verification tokens
    detections.extend(_lookup_dmarc(r, domain))
    detections.extend(_lookup_ns(r, domain))
    return detections


def _default_resolver() -> dns.resolver.Resolver:
    r = dns.resolver.Resolver()
    r.timeout = DNS_TIMEOUT
    r.lifetime = DNS_LIFETIME
    return r


def _lookup_mx(r: dns.resolver.Resolver, domain: str) -> List[DetectedTech]:
    try:
        answers = r.resolve(domain, "MX")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout, dns.exception.DNSException):
        return []
    except Exception:  # noqa: BLE001
        logger.exception("MX lookup failed for %s", domain)
        return []

    out: List[DetectedTech] = []
    for rr in answers:
        target = str(rr.exchange).rstrip(".").lower()
        match = _match_pattern(target, "MX")
        if match:
            vendor, category = match
            out.append(
                DetectedTech(
                    name=vendor,
                    category=category,
                    source=Source.DNS,
                    raw_evidence=f"MX={target}",
                    source_tier=Tier.A,
                )
            )
    return _dedupe(out)


def _lookup_txt(r: dns.resolver.Resolver, domain: str) -> List[DetectedTech]:
    """Single TXT pull, routes per-pattern: SPF (gated on v=spf1) vs TXT (verification tokens)."""
    try:
        answers = r.resolve(domain, "TXT")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout, dns.exception.DNSException):
        return []
    except Exception:  # noqa: BLE001
        logger.exception("TXT lookup failed for %s", domain)
        return []

    out: List[DetectedTech] = []
    for rr in answers:
        txt = _txt_to_string(rr).lower()
        is_spf = txt.startswith("v=spf1")
        for pattern in vendor_patterns():
            if pattern["match"] not in txt:
                continue
            if is_spf and "SPF" in pattern["record_types"]:
                evidence = f"SPF={pattern['match']}"
            elif (not is_spf) and "TXT" in pattern["record_types"]:
                evidence = f"TXT={pattern['match']}"
            else:
                continue
            out.append(
                DetectedTech(
                    name=pattern["vendor"],
                    category=pattern["category"],
                    source=Source.DNS,
                    raw_evidence=evidence,
                    source_tier=Tier.A,
                )
            )
    return _dedupe(out)


def _lookup_dmarc(r: dns.resolver.Resolver, domain: str) -> List[DetectedTech]:
    try:
        answers = r.resolve(f"_dmarc.{domain}", "TXT")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout, dns.exception.DNSException):
        return []
    except Exception:  # noqa: BLE001
        logger.exception("DMARC lookup failed for %s", domain)
        return []

    out: List[DetectedTech] = []
    for rr in answers:
        txt = _txt_to_string(rr).lower()
        if not txt.startswith("v=dmarc1"):
            continue
        for pattern in vendor_patterns():
            if "DMARC" in pattern["record_types"] and pattern["match"] in txt:
                out.append(
                    DetectedTech(
                        name=pattern["vendor"],
                        category=pattern["category"],
                        source=Source.DNS,
                        raw_evidence=f"DMARC rua/ruf={pattern['match']}",
                        source_tier=Tier.A,
                    )
                )
    return _dedupe(out)


def _lookup_ns(r: dns.resolver.Resolver, domain: str) -> List[DetectedTech]:
    try:
        answers = r.resolve(domain, "NS")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout, dns.exception.DNSException):
        return []
    except Exception:  # noqa: BLE001
        logger.exception("NS lookup failed for %s", domain)
        return []

    out: List[DetectedTech] = []
    for rr in answers:
        target = str(rr.target).rstrip(".").lower()
        match = _match_pattern(target, "NS")
        if match:
            vendor, category = match
            out.append(
                DetectedTech(
                    name=vendor,
                    category=category,
                    source=Source.DNS,
                    raw_evidence=f"NS={target}",
                    source_tier=Tier.A,
                )
            )
    return _dedupe(out)


def _match_pattern(value: str, record_type: str) -> Optional[Tuple[str, Category]]:
    for pattern in vendor_patterns():
        if record_type not in pattern["record_types"]:
            continue
        if pattern["match"] in value:
            return pattern["vendor"], pattern["category"]
    return None


def _txt_to_string(rr) -> str:
    # dnspython TXT rdata exposes .strings as list[bytes]
    parts = []
    for s in getattr(rr, "strings", []):
        parts.append(s.decode("utf-8", errors="ignore") if isinstance(s, bytes) else str(s))
    return "".join(parts).strip()


def _dedupe(items: List[DetectedTech]) -> List[DetectedTech]:
    """Same vendor/category from multiple records of one type collapses."""
    seen = set()
    out = []
    for d in items:
        key = (d.name, d.category)
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Public email-security posture — for the free report lead magnet. Lightweight:
# does the domain publish SPF / DMARC, what is the DMARC enforcement policy, and
# which mail provider runs the MX. All observable public DNS; never raises.
# ---------------------------------------------------------------------------
_MX_PROVIDERS: List[Tuple[str, str]] = [
    ("google", "Google Workspace"),
    ("googlemail", "Google Workspace"),
    ("protection.outlook", "Microsoft 365"),
    ("outlook", "Microsoft 365"),
    ("microsoft", "Microsoft 365"),
    ("pphosted", "Proofpoint"),
    ("proofpoint", "Proofpoint"),
    ("mimecast", "Mimecast"),
    ("messagelabs", "Symantec / Broadcom"),
    ("barracuda", "Barracuda"),
    ("zoho", "Zoho"),
    ("amazonaws", "Amazon SES"),
    ("mailgun", "Mailgun"),
    ("sendgrid", "SendGrid"),
    ("cisco", "Cisco"),
]


def _mx_provider(targets: List[str]) -> Optional[str]:
    joined = " ".join(targets)
    for needle, label in _MX_PROVIDERS:
        if needle in joined:
            return label
    return None


def email_security_posture(
    domain: str, resolver: Optional[dns.resolver.Resolver] = None
) -> dict:
    """SPF / DMARC / mail-provider posture for a domain (public DNS only).

    Returns {spf: bool, dmarc: bool, dmarc_policy: 'none'|'quarantine'|'reject'
    |None, mx_provider: str|None}. Never raises; missing pieces stay falsy.
    """
    if not domain:
        return {}
    r = resolver or _default_resolver()
    out: dict = {"spf": False, "dmarc": False, "dmarc_policy": None, "mx_provider": None}

    try:  # SPF — any TXT starting v=spf1
        for rr in r.resolve(domain, "TXT"):
            if _txt_to_string(rr).lower().startswith("v=spf1"):
                out["spf"] = True
                break
    except Exception:  # noqa: BLE001
        pass

    try:  # DMARC — _dmarc TXT v=dmarc1, parse p=
        for rr in r.resolve(f"_dmarc.{domain}", "TXT"):
            txt = _txt_to_string(rr).lower()
            if txt.startswith("v=dmarc1"):
                out["dmarc"] = True
                m = re.search(r"\bp=\s*(none|quarantine|reject)", txt)
                out["dmarc_policy"] = m.group(1) if m else None
                break
    except Exception:  # noqa: BLE001
        pass

    try:  # Mail provider from MX
        targets = [
            str(rr.exchange).rstrip(".").lower() for rr in r.resolve(domain, "MX")
        ]
        out["mx_provider"] = _mx_provider(targets)
    except Exception:  # noqa: BLE001
        pass

    return out
