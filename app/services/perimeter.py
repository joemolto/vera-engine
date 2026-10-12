"""Perimeter fingerprinting — what a company actually exposes to the internet.

Why this exists
---------------
`cve_in_stack` was retired on 2026-08-04 because PredictLeads tech data is
vendor-level with no versions (0 versions across 44,620 entries), so it could
never support a CVE claim. This module produces the missing half: product AND
version, observed live on hosts the company actually exposes.

Licensing
---------
Deliberately built with ZERO third-party data providers. Subdomains come from
our own DNS resolution of a wordlist; product and version come from our own
HTTP response headers and public banner endpoints. Shodan's terms are not
publicly readable and SSLMate's free tier is evaluation-only, so neither is
used.

What this is NOT
----------------
Passive reconnaissance only, the same requests a browser makes:
  - No port scanning. We speak HTTPS to 443 and nothing else.
  - No authentication attempts, ever. A 401 is recorded as "this product is
    here", never retried with credentials.
  - No vulnerability probing. We read banners; we do not test whether any
    CVE actually exploits. Presence of a version is reported as presence.
"""

from __future__ import annotations

import logging
import re
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; VeraBot/1.0)"
HTTP_TIMEOUT = 6.0
MAX_WORKERS = 12

# Ceiling on hosts probed per domain. CT can return hundreds; discovery ranks
# edge-looking names first so the cap sheds the least useful, but coverage
# above this is genuinely not attempted and callers should not read a clean
# result on a large estate as "nothing exposed".
MAX_HOSTS = 120

# Hostnames where exploitable edge kit actually lives. The apex is usually
# behind a CDN and tells you nothing — you cannot proxy a VPN or a mail
# server through Cloudflare, which is exactly why these names are worth
# resolving.
EDGE_HOSTNAMES = (
    "vpn", "vpn2", "remote", "access", "connect", "gateway", "gw",
    "mail", "mail2", "webmail", "owa", "exchange", "autodiscover",
    "portal", "sso", "adfs", "citrix", "extranet", "secure",
    "mft", "files", "transfer", "ftp",
)

# Product banners. Path -> (product, cpe_vendor, cpe_product, markers).
# Every one of these is a public, unauthenticated landing page.
#
# `markers` must identify the PRODUCT and must never be a string the server
# could echo back from our own request. A redirect Location repeats the path
# you asked for, so a marker of "my.policy" on /my.policy matches anything
# that redirects — Exchange sending /my.policy to /owa/my.policy "detected"
# F5 BIG-IP on a host with no F5 on it. Session cookies are the strongest
# markers available: SVPNCOOKIE, NSC_, DSID and BIGipServer are set by one
# product each and cannot be produced by accident.
#
# This is the same unfounded-inference failure that killed cve_in_stack. Any
# new marker must be product-identifying, not request-echoing.
BANNER_PATHS: Dict[str, tuple] = {
    "/owa/": (
        "Microsoft Exchange", "microsoft", "exchange_server",
        ("auth/logon.aspx", "outlook web app", "x-owa-version"),
    ),
    "/remote/login": (
        "Fortinet FortiGate SSL-VPN", "fortinet", "fortios",
        ("svpncookie", "fortinet", "fgt_lang"),
    ),
    "/vpn/index.html": (
        "Citrix Gateway", "citrix", "netscaler_gateway",
        ("nsc_", "citrix", "vpn/js/"),
    ),
    "/dana-na/": (
        "Ivanti Connect Secure", "ivanti", "connect_secure",
        ("dsid", "welcome.cgi", "pulse secure", "ivanti"),
    ),
    "/+CSCOE+/logon.html": (
        "Cisco ASA WebVPN", "cisco", "adaptive_security_appliance_software",
        ("webvpn", "cisco"),
    ),
    "/my.policy": (
        "F5 BIG-IP APM", "f5", "big-ip_access_policy_manager",
        ("bigipserver", "f5 networks", "lastmrh_session"),
    ),
}

# Headers that carry a version for a SPECIFIC product. The generic `Server`
# header is deliberately absent: on a host running IIS in front of Exchange,
# Server reports IIS, and borrowing that number for Exchange (or F5) invents
# a version the evidence never supported.
PRODUCT_VERSION_HEADERS = ("x-owa-version",)

# Response headers that leak a product and often a version.
HEADER_PRODUCTS = {
    "x-owa-version": ("Microsoft Exchange", "microsoft", "exchange_server"),
    "x-aspnet-version": ("Microsoft ASP.NET", "microsoft", "asp.net"),
}

# Server-header names that are infrastructure plumbing, not products anyone
# runs or patches. Surfaced by the 2026-08-05 survey as pure noise:
# Microsoft-HTTPAPI is always 2.0 (the Windows HTTP stack), awselb is an AWS
# load balancer, Fly and DPS are hosting layers. Reporting them pads the
# output and implies we found something when we found a CDN.
SERVER_DENYLIST = {
    "microsoft-httpapi", "awselb", "fly", "dps", "cloudflare",
    "cloudfront", "akamaighost", "envoy", "istio-envoy", "amazons3",
}

# Product names we expect to come out of the `Server:` header. Unlike
# BANNER_PATHS these are parsed dynamically, so this set is documentation
# rather than configuration — it records what the 2026-08-05 survey actually
# saw, and lets the CVE matcher's tests check that every normaliser
# corresponds to something this collector can really emit.
SERVER_HEADER_PRODUCTS = {"nginx", "Apache", "Microsoft-IIS", "SecureTransport"}

_SERVER_RE = re.compile(r"^([A-Za-z0-9_\-\. ]+?)[/ ]v?([0-9]+(?:\.[0-9]+)*)")
_VERSION_RE = re.compile(r"([0-9]+(?:\.[0-9]+){1,3})")


@dataclass(frozen=True)
class PerimeterFinding:
    """One product observed on one internet-facing host."""

    host: str
    ip: str
    product: str
    version: Optional[str]
    cpe: Optional[str]
    evidence: str

    @property
    def is_versioned(self) -> bool:
        """Only versioned findings can carry a defensible CVE claim."""
        return bool(self.version)


def _resolve(host: str) -> Optional[str]:
    try:
        return socket.gethostbyname(host)
    except Exception:  # noqa: BLE001
        return None


def _cpe(vendor: str, product: str, version: Optional[str]) -> str:
    base = f"cpe:2.3:a:{vendor}:{product}"
    return f"{base}:{version}" if version else f"{base}:*"


def _probe_host(host: str, ip: str) -> List[PerimeterFinding]:
    """One HTTPS GET of the root, then the public banner paths."""
    out: List[PerimeterFinding] = []
    headers = {"User-Agent": USER_AGENT}

    try:
        root = httpx.get(
            f"https://{host}", timeout=HTTP_TIMEOUT, follow_redirects=True,
            headers=headers, verify=False,  # noqa: S501 - self-signed edge kit is normal
        )
    except Exception:  # noqa: BLE001
        return out

    # Server: header — "Microsoft-IIS/10.0", "nginx/1.24.0"
    server = root.headers.get("server", "")
    m = _SERVER_RE.match(server.strip())
    if m and m.group(1).strip().lower() not in SERVER_DENYLIST:
        name, version = m.group(1).strip(), m.group(2)
        vendor = name.split("-")[0].split("/")[0].lower()
        out.append(PerimeterFinding(
            host=host, ip=ip, product=name, version=version,
            cpe=_cpe(vendor, name.lower().replace(" ", "_"), version),
            evidence=f"Server: {server}",
        ))

    for header, (product, vendor, cpe_product) in HEADER_PRODUCTS.items():
        raw = root.headers.get(header)
        if not raw:
            continue
        vm = _VERSION_RE.search(raw)
        version = vm.group(1) if vm else None
        out.append(PerimeterFinding(
            host=host, ip=ip, product=product, version=version,
            cpe=_cpe(vendor, cpe_product, version),
            evidence=f"{header}: {raw}",
        ))

    for path, (product, vendor, cpe_product, markers) in BANNER_PATHS.items():
        try:
            r = httpx.get(
                f"https://{host}{path}", timeout=HTTP_TIMEOUT,
                follow_redirects=False, headers=headers, verify=False,  # noqa: S501
            )
        except Exception:  # noqa: BLE001
            continue
        # 401 means the product is there and asking for auth. We record it
        # and move on — we never send credentials.
        if r.status_code not in (200, 301, 302, 401):
            continue

        # Search Location, Set-Cookie and header names for a product marker.
        # Body only on non-redirects; a redirect has no meaningful body.
        haystack = " ".join([
            r.headers.get("location", ""),
            r.headers.get("set-cookie", ""),
            " ".join(r.headers.keys()),
            (r.text or "")[:4000] if r.status_code in (200, 401) else "",
        ]).lower()

        hit = next((m for m in markers if m in haystack), None)
        if not hit:
            continue

        # Version only from a header belonging to THIS product.
        version = None
        for h in PRODUCT_VERSION_HEADERS:
            raw = r.headers.get(h)
            if raw:
                vm = _VERSION_RE.search(raw)
                if vm:
                    version = vm.group(1)
                    break
        corroborated_by = f"marker '{hit}'"

        out.append(PerimeterFinding(
            host=host, ip=ip, product=product, version=version,
            cpe=_cpe(vendor, cpe_product, version),
            evidence=f"GET {path} -> HTTP {r.status_code}, {corroborated_by}",
        ))

    return out


def scan_perimeter(
    domain: str,
    hostnames: Optional[tuple] = None,
    max_workers: int = MAX_WORKERS,
    db=None,
    max_hosts: int = MAX_HOSTS,
) -> List[PerimeterFinding]:
    """Resolve edge hostnames for `domain` and fingerprint what answers.

    Hostnames come from Certificate Transparency when available (see
    subdomain_discovery), falling back to the guessed wordlist. Discovery was
    the binding constraint: guessing 24 names found something on 16% of
    accounts while Arqiva alone publishes 246 real hostnames.

    `max_hosts` caps probing per domain. Discovery is ranked edge-first, so
    the cap drops the least interesting names, but it IS a cap — a domain
    publishing thousands is not fully covered, by design.

    Returns every observation, versioned or not. Callers that intend to make
    a CVE claim must filter on `is_versioned` — an unversioned product is
    evidence of presence, never evidence of exposure.
    """
    if not domain:
        return []
    domain = domain.strip().lower().lstrip(".")

    if hostnames is not None:
        candidates = [f"{n}.{domain}" for n in hostnames]
    else:
        try:
            from app.services.subdomain_discovery import discover_subdomains
            candidates = discover_subdomains(domain, db=db)
        except Exception as e:  # noqa: BLE001
            logger.warning("subdomain discovery failed for %s: %s", domain, e)
            candidates = [f"{n}.{domain}" for n in EDGE_HOSTNAMES]

    if len(candidates) > max_hosts:
        logger.info(
            "perimeter %s: capping %d candidate hosts to %d",
            domain, len(candidates), max_hosts,
        )
        candidates = candidates[:max_hosts]

    live: List[tuple] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_resolve, h): h for h in candidates}
        for fut in as_completed(futures):
            host = futures[fut]
            try:
                ip = fut.result()
            except Exception:  # noqa: BLE001
                ip = None
            if ip:
                live.append((host, ip))

    if not live:
        return []

    findings: List[PerimeterFinding] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_probe_host, h, ip) for h, ip in live]
        for fut in as_completed(futures):
            try:
                findings.extend(fut.result())
            except Exception as e:  # noqa: BLE001
                logger.debug("perimeter probe failed: %s", e)

    # Dedupe: the same product can answer on several hostnames.
    seen = set()
    unique: List[PerimeterFinding] = []
    for f in sorted(findings, key=lambda x: (x.product, x.version or "", x.host)):
        key = (f.product, f.version)
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)

    logger.info(
        "perimeter %s: %d hosts live, %d findings (%d versioned)",
        domain, len(live), len(unique),
        sum(1 for f in unique if f.is_versioned),
    )
    return unique
