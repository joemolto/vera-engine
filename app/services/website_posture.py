"""Phase 5 — website posture signal lift + cheap probe additions.

Two things happen here:

1. **Cheap probes** (no scraping cost, deterministic): security.txt
   and status.{domain} subdomain. These are RFC- or convention-based
   surfaces that are very high signal when they exist.

2. **Boolean → signal lift**: take the booleans the website enricher
   already produces in deep_research_data.security_posture and convert
   them into proper signals with subject=website. These were
   previously buried in JSONB and invisible to the dashboard signal
   timeline.

Plus two derived signals:
   - certification_gap (regulated industry + no confirmed cert)
   - security_proof_gap (sells to regulated buyers + missing trust centre/security page)

All output rows are ready for signal_writer.insert_signals.
"""

from __future__ import annotations

import logging
import socket
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from app.services import signal_payload as sp
from app.services.enrichers.tech_stack import _host_is_public, guarded_get

logger = logging.getLogger(__name__)


# Posture booleans the existing enricher already produces. Each entry
# maps the deep_research_data.security_posture flag → (signal_type,
# friendly headline suffix).
POSTURE_BOOL_SIGNALS: List[tuple] = [
    ("has_trust_centre", "trust_center_present", "has a public trust centre"),
    ("has_security_page", "security_page_present", "has a public security page"),
    ("has_privacy_policy", "privacy_page_present", "has a published privacy policy"),
    ("has_responsible_disclosure", "responsible_disclosure_present", "publishes a responsible-disclosure program"),
    ("has_bug_bounty", "bug_bounty_present", "runs a public bug bounty program"),
]

# Compliance flags from deep_research_data.compliance. Each fires a
# certification_claim_detected signal (subject=website, source=first_party).
COMPLIANCE_FLAGS: List[tuple] = [
    ("iso_27001", "ISO 27001"),
    ("soc_2", "SOC 2"),
    ("pci_dss", "PCI-DSS"),
    ("cyber_essentials", "Cyber Essentials"),
    ("cyber_essentials_plus", "Cyber Essentials+"),
    ("crest", "CREST"),
    ("check", "CHECK"),
]

# Industries where a missing cert is meaningful (the "compliance_gap"
# semantic — same list the existing _create_cyber_signals uses).
_REGULATED_INDUSTRY_KEYWORDS = {
    "financial services", "banking", "insurance", "fintech",
    "investment management", "venture capital",
    "healthcare", "pharmaceuticals",
    "energy", "oil & gas", "utilities",
    "government", "public sector", "defense", "defence", "military",
    "telecommunications", "legal services", "law practice",
    "gambling", "casinos",
}


# ---------------------------------------------------------------------------
# Cheap deterministic probes
# ---------------------------------------------------------------------------

PROBE_TIMEOUT = 5


def probe_security_txt(domain: str) -> Optional[Dict[str, Any]]:
    """RFC 9116 lookup at /.well-known/security.txt.

    Returns a dict with the security contact info if found, None otherwise.
    Tries https first, falls back to http. Cheap — one HTTP request.
    """
    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}/.well-known/security.txt"
        try:
            with httpx.Client(timeout=PROBE_TIMEOUT, follow_redirects=False) as client:
                r = guarded_get(client, url)
                if r is not None and r.status_code == 200 and r.text.strip():
                    contacts = _parse_security_txt(r.text)
                    return {"url": str(r.url), "contacts": contacts, "raw_excerpt": r.text[:400]}
        except (httpx.RequestError, httpx.HTTPError):
            continue
    return None


def _parse_security_txt(text: str) -> List[str]:
    """Extract Contact: lines per RFC 9116."""
    contacts: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("contact:"):
            contacts.append(line.split(":", 1)[1].strip())
    return contacts


def probe_status_subdomain(domain: str) -> Optional[Dict[str, Any]]:
    """Check for status.{domain} as a separate subdomain.

    Most companies host status pages on status.example.com (Statuspage,
    Atlassian, custom). DNS resolution is the cheapest first check;
    fall back to a HEAD request if DNS exists.
    """
    candidate = f"status.{domain}"
    # SSRF guard: only proceed if the subdomain resolves to a public IP —
    # a tenant could point status.theirdomain at an internal address.
    if not _host_is_public(candidate):
        return None
    # DNS exists — verify with a HEAD request to confirm it serves something
    for scheme in ("https", "http"):
        url = f"{scheme}://{candidate}"
        try:
            with httpx.Client(timeout=PROBE_TIMEOUT, follow_redirects=False) as client:
                r = guarded_get(client, url, method="head")
                if r is not None and r.status_code < 400:
                    return {"url": str(r.url)}
                # Some status pages disallow HEAD; try GET as fallback
                r = guarded_get(client, url)
                if r is not None and r.status_code < 400:
                    return {"url": str(r.url)}
        except (httpx.RequestError, httpx.HTTPError):
            continue
    return None


# ---------------------------------------------------------------------------
# Page probes — deterministic checks of conventional URL paths.
#
# These close the Posture gaps Sonar can't reliably see (DPA pages,
# subprocessor lists, /trust, /compliance) and generate the five signal
# types that existed in the schema with no generator (2026-06-11).
#
# False-positive discipline (product rule #11): a probe only fires when
# ALL of these hold — HTTP 200, the response was NOT redirected back to
# the homepage, the page text contains a type-specific keyword, and the
# site is not a catch-all (SPAs that 200 every path are detected first
# and skip page probes entirely).
# ---------------------------------------------------------------------------

# signal_type → (paths to try, required keywords (any), headline suffix,
# strength). Paths are tried in order; first hit wins.
PAGE_PROBES: List[tuple] = [
    (
        "compliance_page_present",
        ("/compliance", "/trust/compliance", "/legal/compliance", "/security/compliance"),
        ("compliance", "soc 2", "soc2", "iso 27001", "iso27001", "certification"),
        "publishes a compliance page",
        0.9,
    ),
    (
        "dpa_present",
        ("/dpa", "/legal/dpa", "/data-processing-agreement",
         "/legal/data-processing-agreement", "/legal/data-processing-addendum"),
        ("data processing",),
        "publishes a data processing agreement",
        0.9,
    ),
    (
        "subprocessors_present",
        ("/subprocessors", "/legal/subprocessors", "/sub-processors",
         "/legal/sub-processors", "/trust/subprocessors"),
        ("subprocessor", "sub-processor"),
        "discloses its subprocessors",
        0.9,
    ),
    (
        "enterprise_page_detected",
        ("/enterprise",),
        ("enterprise",),
        "has a dedicated enterprise page",
        0.8,
    ),
    (
        "api_or_developer_platform",
        ("/developers", "/developer", "/docs/api", "/api/docs"),
        ("api",),
        "publishes API / developer documentation",
        0.8,
    ),
    # Fill-ins for the Sonar booleans — only consulted when the deep
    # research flag came back false/absent (see call site below).
    (
        "trust_center_present",
        ("/trust",),
        ("trust", "security", "compliance"),
        "has a public trust centre",
        0.85,
    ),
    (
        "security_page_present",
        ("/security",),
        ("security",),
        "has a public security page",
        0.85,
    ),
]

# A path no real site should serve. If it returns 200, the site 200s
# everything (SPA catch-all) and page-content probes are meaningless.
_CATCHALL_CHECK_PATH = "/zz-vera-posture-probe-check"

_PROBE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; VeraPostureCheck/1.0)",
}


def probe_posture_pages(
    domain: str, skip_types: Optional[set] = None,
) -> Dict[str, Dict[str, Any]]:
    """Run the PAGE_PROBES against a domain.

    Returns {signal_type: {url, suffix, strength}} for each probe that
    passed all checks. `skip_types` lets the caller drop probes whose
    signal is already confirmed by another source (e.g. Sonar said the
    trust centre exists — no need to re-probe).
    """
    skip = skip_types or set()
    results: Dict[str, Dict[str, Any]] = {}
    if not _host_is_public(domain):
        return {}
    try:
        with httpx.Client(
            timeout=PROBE_TIMEOUT,
            follow_redirects=False,
            headers=_PROBE_HEADERS,
        ) as client:
            base = f"https://{domain}"
            # Catch-all detection — one request decides whether page
            # content can be trusted at all on this site.
            try:
                r = guarded_get(client, base + _CATCHALL_CHECK_PATH)
                if r is not None and r.status_code == 200:
                    logger.info(
                        "Posture probes skipped for %s — site 200s unknown paths (catch-all)",
                        domain,
                    )
                    return {}
            except (httpx.RequestError, httpx.HTTPError):
                # Domain unreachable over https — nothing to probe.
                return {}

            for signal_type, paths, keywords, suffix, strength in PAGE_PROBES:
                if signal_type in skip:
                    continue
                for path in paths:
                    hit = _probe_one_page(client, base, path, keywords)
                    if hit:
                        results[signal_type] = {
                            "url": hit,
                            "suffix": suffix,
                            "strength": strength,
                        }
                        break
    except Exception as e:
        logger.warning("Posture page probes failed for %s: %s", domain, e)
    return results


def _probe_one_page(client: httpx.Client, base: str, path: str, keywords) -> Optional[str]:
    """GET one candidate path. Returns the final URL on a confident hit."""
    r = guarded_get(client, base + path)
    if r is None or r.status_code != 200:
        return None
    # Redirected back to the homepage = the path doesn't really exist.
    final_path = httpx.URL(str(r.url)).path.rstrip("/")
    if final_path in ("", "/"):
        return None
    text = (r.text or "")[:60000].lower()
    if not any(kw in text for kw in keywords):
        return None
    return str(r.url)


# ---------------------------------------------------------------------------
# Main entry point — call from the enricher orchestrator.
# ---------------------------------------------------------------------------

def create_website_posture_signals(
    org_id: str,
    lead_id: str,
    domain: str,
    company_name: Optional[str],
    enrichment_data: Dict[str, Any],
    industry: Optional[str] = None,
    expires_days: int = 180,
) -> List[Dict[str, Any]]:
    """Build website-posture signal rows for this lead.

    Caller is responsible for clearing prior rows of the same types
    (we don't have a db handle here — keeps the function pure +
    testable). See enricher._create_website_posture_signals.
    """
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=expires_days)
    ).isoformat()
    rows: List[Dict[str, Any]] = []

    deep = enrichment_data.get("deep_research") or {}
    posture = deep.get("security_posture") or {}
    compliance = deep.get("compliance") or {}

    # 1. Lift the booleans
    for source_key, signal_type, suffix in POSTURE_BOOL_SIGNALS:
        if _flag_is_true(posture.get(source_key)):
            rows.append({
                "org_id": org_id,
                "lead_id": lead_id,
                "signal_type": signal_type,
                "signal_source": "website",
                "signal_data": sp.build_signal_payload(
                    headline=(
                        f"{company_name} {suffix}" if company_name else suffix.capitalize()
                    ),
                    entity=company_name,
                    detail=None,
                ),
                "signal_strength": 0.85,
                "expires_at": expires_at,
            })

    # 2. Compliance certifications — one per-cert signal type each
    #    (certification_iso_27001, certification_soc_2, ...) so reps can
    #    filter by framework and the dossier can correlate "claims ISO"
    #    with "job posts mention ISO". Replaced the generic
    #    certification_claim_detected on 2026-06-11; legacy rows of the
    #    generic type still score via the profile recipe.
    for flag_key, label in COMPLIANCE_FLAGS:
        if _flag_is_true(compliance.get(flag_key)):
            rows.append({
                "org_id": org_id,
                "lead_id": lead_id,
                "signal_type": f"certification_{flag_key}",
                "signal_source": "website",
                "signal_data": sp.build_signal_payload(
                    headline=(
                        f"{company_name} claims {label} certification"
                        if company_name
                        else f"Claims {label} certification"
                    ),
                    entity=company_name,
                    detail=None,
                    certification=label,
                    certification_key=flag_key,
                ),
                "signal_strength": 0.9,
                "expires_at": expires_at,
            })

    # 3. security.txt probe (free, RFC standard)
    sec_txt = probe_security_txt(domain)
    if sec_txt and sec_txt.get("contacts"):
        rows.append({
            "org_id": org_id,
            "lead_id": lead_id,
            "signal_type": "security_contact_present",
            "signal_source": "security_txt_probe",
            "signal_data": sp.build_signal_payload(
                headline=(
                    f"{company_name} publishes a security.txt contact"
                    if company_name
                    else "security.txt contact published"
                ),
                entity=company_name,
                detail=", ".join(sec_txt["contacts"][:3]),
                source_url=sec_txt.get("url"),
                contacts=sec_txt["contacts"],
            ),
            "signal_strength": 0.95,
            "expires_at": expires_at,
            "snippet": sec_txt.get("raw_excerpt"),
        })

    # 4. status.{domain} probe
    status_page = probe_status_subdomain(domain)
    if status_page:
        rows.append({
            "org_id": org_id,
            "lead_id": lead_id,
            "signal_type": "status_page_present",
            "signal_source": "dns_probe",
            "signal_data": sp.build_signal_payload(
                headline=(
                    f"{company_name} runs a public status page"
                    if company_name
                    else "Public status page detected"
                ),
                entity=company_name,
                detail=None,
                source_url=status_page.get("url"),
            ),
            "signal_strength": 0.9,
            "expires_at": expires_at,
        })

    # 4b. Page probes — deterministic path checks for the posture pages
    #     Sonar can't reliably see. Skips types Sonar already confirmed
    #     (no duplicate rows; signal_writer dedupe would collapse them
    #     anyway, but skipping saves the HTTP requests).
    sonar_confirmed = {
        signal_type
        for source_key, signal_type, _ in POSTURE_BOOL_SIGNALS
        if _flag_is_true(posture.get(source_key))
    }
    page_hits = probe_posture_pages(domain, skip_types=sonar_confirmed)
    for signal_type, hit in page_hits.items():
        rows.append({
            "org_id": org_id,
            "lead_id": lead_id,
            "signal_type": signal_type,
            "signal_source": "web_probe",
            "signal_data": sp.build_signal_payload(
                headline=(
                    f"{company_name} {hit['suffix']}"
                    if company_name
                    else hit["suffix"].capitalize()
                ),
                entity=company_name,
                detail=None,
                source_url=hit["url"],
            ),
            "signal_strength": hit["strength"],
            "expires_at": expires_at,
        })

    # 5. Derived signals — public_proof_not_visible
    #
    # IMPORTANT: this is a VISIBILITY signal, not a deficiency claim. We do
    # not know whether the company holds certifications internally — only
    # that they don't surface them publicly. Large regulated companies
    # almost always have SOC 2 / ISO 27001 in place; many keep them behind
    # NDA/procurement walls.
    #
    # `certification_gap` was deleted entirely (redundant with this one,
    # and its name carried the wrong implication).
    industry_lc = (industry or "").lower()
    is_regulated = any(kw in industry_lc for kw in _REGULATED_INDUSTRY_KEYWORDS)
    has_any_cert = any(_flag_is_true(compliance.get(k)) for k, _ in COMPLIANCE_FLAGS)
    has_trust = _flag_is_true(posture.get("has_trust_centre"))
    has_sec_page = _flag_is_true(posture.get("has_security_page"))

    # Fire when the public surface lacks BOTH certs AND a trust/security
    # page. This is the rep's friction signal: they'll have to chase the
    # info during discovery rather than reference it up front.
    if is_regulated and not has_any_cert and not has_trust and not has_sec_page:
        rows.append({
            "org_id": org_id,
            "lead_id": lead_id,
            "signal_type": "public_proof_not_visible",
            "signal_source": "derived",
            "signal_data": sp.build_signal_payload(
                headline=(
                    f"No public security or compliance proof visible for {company_name}"
                    if company_name
                    else "No public security or compliance proof visible"
                ),
                entity=company_name,
                detail=(
                    "Operates in a regulated industry but has no public trust "
                    "centre, security page, or certification claim on their "
                    "website. They likely hold the standard frameworks "
                    "internally (SOC 2 / ISO 27001 typical for this profile) "
                    "— just kept behind procurement. Discovery question, not "
                    "a sales claim."
                ),
                industry=industry,
                # Inference grounding so AI quality pass / dashboard can
                # frame this honestly
                inference_basis="absence_of_public_evidence",
            ),
            "signal_strength": 0.6,
            "expires_at": expires_at,
        })

    return rows


def _flag_is_true(v: Any) -> bool:
    """Tolerant truthiness — engine sometimes stores bool, sometimes 'true' string."""
    if v in (True, "true", "True", "yes", "Yes", 1):
        return True
    return False


# ---------------------------------------------------------------------------
# Company-keyed backfill (spec 2026-06-09)
#
# Mirrors create_website_posture_signals but writes signals keyed on
# company_id rather than lead_id. Used by the /accounts/website-posture/
# refresh endpoint to bring Posture-axis coverage from "only-where-a-lead-
# exists" to "every watchlist account." Going forward, the per-lead path
# continues to write lead_id-keyed rows on lead enrichment; the scorer
# unions both paths so duplicates cap-out per signal_type without
# double-counting.
# ---------------------------------------------------------------------------
def create_website_posture_signals_for_company(
    org_id: str,
    company_id: str,
    domain: str,
    company_name: Optional[str],
    deep_research_data: Dict[str, Any],
    industry: Optional[str] = None,
    expires_days: int = 180,
) -> List[Dict[str, Any]]:
    """Same projection as create_website_posture_signals, but signal rows
    are keyed by company_id. Used for the account-level backfill where no
    representative lead exists.

    deep_research_data is the companies.deep_research_data jsonb blob
    (the same one Perplexity writes during enrichment). We wrap it into
    the {deep_research: ...} shape the underlying projector expects.
    """
    # Build a "fake" enrichment_data shape so we can reuse the original
    # function's projection logic — minus the lead_id binding.
    enrichment_data = {"deep_research": deep_research_data or {}}

    # Delegate to the original projector with a sentinel lead_id, then
    # rewrite the rows to be company-keyed. We do this rather than copy-
    # paste the 150-line projection so behaviour stays in lock-step with
    # the lead-keyed path (one source of truth for posture detection).
    placeholder = "__company_backfill__"
    rows = create_website_posture_signals(
        org_id=org_id,
        lead_id=placeholder,
        domain=domain,
        company_name=company_name,
        enrichment_data=enrichment_data,
        industry=industry,
        expires_days=expires_days,
    )
    for r in rows:
        r.pop("lead_id", None)
        r["company_id"] = company_id
    return rows
