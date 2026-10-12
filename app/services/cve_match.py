"""Match observed perimeter findings to CVEs, without inventing anything.

Why this is fussy
-----------------
NVD does not express versions the way products report them. Exchange answers
`x-owa-version: 15.1.2507.61` but NVD indexes it as
`cpe:2.3:a:microsoft:exchange_server:2016:cumulative_update_23`. Querying NVD
with the observed build returns only 1999-era CVEs, i.e. silently wrong.

So every product needs a normaliser from "what the server told us" to "what
NVD calls it". Without one we make NO CVE claim. That is the whole discipline
here: cve_in_stack was retired for asserting exposure the evidence could not
support, and a wrong CU mapping would recreate it in a subtler form.

Attribution
-----------
NVD terms require this string wherever CVE data is surfaced:
    "This product uses the NVD API but is not endorsed or certified by the NVD."
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

NVD_API = "https://services.nvd.nist.gov/rest/json/cves/2.0"
NVD_ATTRIBUTION = (
    "This product uses the NVD API but is not endorsed or certified by the NVD."
)
NVD_TIMEOUT = 30.0

# Observed build -> (NVD version, NVD update) for products whose reported
# version differs from the CPE vocabulary.
#
# VERIFIED 2026-08-04 against Microsoft's official build-number list:
# learn.microsoft.com/en-us/exchange/new-features/build-numbers-and-release-dates
# Re-verify from that page before extending. A wrong entry produces a
# confident, specific, false CVE claim — worse than no signal at all.
# Unmapped builds are deliberately dropped, not guessed.
#
# Only the first three segments are keyed. The fourth is the security-update
# revision (15.1.2507.61 = CU23 Oct25SU) and changes with every patch, so
# keying on it would miss almost every host.
EXCHANGE_BUILDS: Dict[str, Tuple[str, str]] = {
    # Exchange Server 2016
    "15.1.2507": ("2016", "cumulative_update_23"),
    "15.1.2375": ("2016", "cumulative_update_22"),
    "15.1.2308": ("2016", "cumulative_update_21"),
    "15.1.2242": ("2016", "cumulative_update_20"),
    "15.1.2176": ("2016", "cumulative_update_19"),
    "15.1.2106": ("2016", "cumulative_update_18"),
    # Exchange Server 2019
    "15.2.1748": ("2019", "cumulative_update_15"),
    "15.2.1544": ("2019", "cumulative_update_14"),
    "15.2.1258": ("2019", "cumulative_update_13"),
    "15.2.1118": ("2019", "cumulative_update_12"),
    "15.2.986": ("2019", "cumulative_update_11"),
    "15.2.922": ("2019", "cumulative_update_10"),
}


@dataclass(frozen=True)
class CveMatch:
    cve_id: str
    published: str
    severity: Optional[str]
    description: str
    is_kev: bool
    matched_cpe: str


def _normalise_exchange(version: str) -> Optional[Tuple[str, str]]:
    """15.1.2507.61 -> ('2016', 'cumulative_update_23')."""
    m = re.match(r"^(\d+\.\d+\.\d+)", version or "")
    if not m:
        return None
    return EXCHANGE_BUILDS.get(m.group(1))


def _normalise_plain(version: str) -> Optional[Tuple[str, str]]:
    """For products NVD indexes by their own version string.

    Verified 2026-08-05: nginx 1.14.0 -> cpe:2.3:a:f5:nginx:1.14.0 returns 11
    CVEs; Apache 2.2.22 -> cpe:2.3:a:apache:http_server:2.2.22 returns 72.
    Exchange is the outlier that needs a build table, not the rule.
    """
    m = re.match(r"^(\d+(?:\.\d+){1,3})", (version or "").strip())
    return (m.group(1), "*") if m else None


# product name -> (cpe vendor, cpe product, normaliser)
# A product absent from this map yields stack intelligence, never a CVE claim.
#
# Microsoft-IIS is deliberately ABSENT. All 12 companies found running it are
# on 10.0, which ships with every supported Windows Server. A CVE claim would
# fire on all of them and separate none of them — a signal with no
# discriminating power is noise wearing a severity badge. Add it only if we
# start seeing genuinely old builds (7.5, 8.5).
NORMALISERS = {
    "Microsoft Exchange": ("microsoft", "exchange_server", _normalise_exchange),
    "nginx": ("f5", "nginx", _normalise_plain),
    "Apache": ("apache", "http_server", _normalise_plain),
}


def build_cpe(product: str, version: str) -> Optional[str]:
    """Exact NVD CPE for an observed product+version, or None if unmappable."""
    entry = NORMALISERS.get(product)
    if not entry or not version:
        return None
    vendor, cpe_product, normalise = entry
    normalised = normalise(version)
    if not normalised:
        logger.info(
            "cve_match: %s build %s not in the verified map — no CVE claim",
            product, version,
        )
        return None
    ver, update = normalised
    return f"cpe:2.3:a:{vendor}:{cpe_product}:{ver}:{update}:*:*:*:*:*:*"


def lookup_cves(
    cpe: str,
    kev_ids: Optional[set] = None,
    api_key: Optional[str] = None,
    timeout: float = NVD_TIMEOUT,
) -> List[CveMatch]:
    """Every CVE NVD associates with this exact CPE."""
    headers = {"apiKey": api_key} if api_key else {}
    try:
        r = httpx.get(
            NVD_API, params={"cpeName": cpe, "resultsPerPage": 200},
            timeout=timeout, headers=headers,
        )
        if r.status_code != 200:
            logger.warning("NVD returned HTTP %s for %s", r.status_code, cpe)
            return []
        payload = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("NVD lookup failed for %s: %s", cpe, e)
        return []

    out: List[CveMatch] = []
    for item in payload.get("vulnerabilities", []):
        cve = item.get("cve") or {}
        cve_id = cve.get("id")
        if not cve_id:
            continue
        severity = None
        for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
            metrics = (cve.get("metrics") or {}).get(key) or []
            if metrics:
                severity = (
                    metrics[0].get("cvssData", {}).get("baseSeverity")
                    or metrics[0].get("baseSeverity")
                )
                break
        desc = next(
            (d.get("value") for d in cve.get("descriptions", [])
             if d.get("lang") == "en"),
            "",
        )
        out.append(CveMatch(
            cve_id=cve_id,
            published=(cve.get("published") or "")[:10],
            severity=severity,
            description=desc[:300],
            is_kev=bool(kev_ids and cve_id in kev_ids),
            matched_cpe=cpe,
        ))
    return out


def build_cve_signals(
    findings,
    org_id: str,
    company_id: str,
    company_name: Optional[str] = None,
    kev_ids: Optional[set] = None,
    api_key: Optional[str] = None,
    max_age_days: int = 90,
) -> List[dict]:
    """Signal rows for versioned findings with recent CVEs.

    Reuses the `cve_in_stack` type, which the retired emitter never earned:
    the version now comes from the company's own host, and the CVE is bound
    to that exact version via a verified CPE. Every claim carries the host
    and the evidence string that produced it, so a reader can check it.

    KEV entries are kept regardless of age (CISA listing them means they are
    exploited now); everything else must be published inside `max_age_days`.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime("%Y-%m-%d")
    rows: List[dict] = []

    for f in findings:
        if not getattr(f, "is_versioned", False):
            continue
        cpe = build_cpe(f.product, f.version)
        if not cpe:
            continue
        matches = lookup_cves(cpe, kev_ids=kev_ids, api_key=api_key)
        recent = [m for m in matches if m.is_kev or m.published >= cutoff]
        if not recent:
            continue

        kev = [m for m in recent if m.is_kev]
        worst = kev[0] if kev else max(recent, key=lambda m: m.published)
        who = company_name or "This company"
        headline = (
            f"{who} is running {f.product} {f.version} on {f.host}, "
            f"exposed to {len(recent)} recent CVE"
            + ("s" if len(recent) != 1 else "")
            + (f" including {len(kev)} on CISA KEV" if kev else "")
        )
        rows.append({
            "org_id": org_id,
            "company_id": company_id,
            "signal_type": "cve_in_stack",
            "signal_source": "perimeter_scan",
            "signal_data": {
                "headline": headline,
                "detail": (
                    f"Observed live on {f.host} ({f.evidence}). "
                    f"Matched via {cpe}. {NVD_ATTRIBUTION}"
                ),
                "host": f.host,
                "product": f.product,
                "version": f.version,
                "cpe": cpe,
                "evidence": f.evidence,
                "kev_count": len(kev),
                "cve_count": len(recent),
                "cves": [
                    {"id": m.cve_id, "published": m.published,
                     "severity": m.severity, "kev": m.is_kev}
                    for m in sorted(recent, key=lambda m: (not m.is_kev, m.published))[:10]
                ],
                "attribution": NVD_ATTRIBUTION,
            },
            # KEV means exploited in the wild, which outranks a merely recent CVE.
            "signal_strength": 0.95 if kev else 0.8,
            "signal_subject_type": "tech",
        })
    return rows


PERIMETER_CACHE_TYPE = "perimeter_scan"
PERIMETER_TTL_SECONDS = 7 * 24 * 3600

KEV_URL = (
    "https://www.cisa.gov/sites/default/files/feeds/"
    "known_exploited_vulnerabilities.json"
)
KEV_TTL_SECONDS = 6 * 3600
_kev_cache: Dict[str, Any] = {"ids": None, "fetched_at": 0.0}


def kev_ids(timeout: float = 30.0) -> set:
    """CISA's actively-exploited CVE IDs, cached in-process for 6 hours.

    The catalogue is ~1,660 entries and moves a few times a week. Fetching it
    per account would add a megabyte of traffic to every monitor pass for data
    that barely changes. Returns an empty set on failure, which downgrades KEV
    flagging rather than blocking the scan.
    """
    import time

    now = time.time()
    if _kev_cache["ids"] is not None and now - _kev_cache["fetched_at"] < KEV_TTL_SECONDS:
        return _kev_cache["ids"]
    try:
        r = httpx.get(KEV_URL, timeout=timeout)
        if r.status_code == 200:
            ids = {v["cveID"] for v in r.json().get("vulnerabilities", [])}
            _kev_cache.update({"ids": ids, "fetched_at": now})
            return ids
    except Exception as e:  # noqa: BLE001
        logger.warning("KEV catalogue fetch failed: %s", e)
    return _kev_cache["ids"] or set()


def refresh_perimeter_signals(
    db,
    *,
    org_id: str,
    company_id: str,
    domain: str,
    company_name: Optional[str] = None,
    kev_ids: Optional[set] = None,
    api_key: Optional[str] = None,
) -> int:
    """Scan the company's perimeter and replace its cve_in_stack signals.

    Mirrors refresh_credential_signals: surgically deletes only this company's
    prior account-level cve_in_stack rows, then inserts the fresh set, so the
    signals track reality as they patch (or stop patching) between passes.

    Cache-gated to 7 days. Internet-facing infrastructure changes slowly and
    the scan is the slowest thing in the pass (Certificate Transparency alone
    takes ~40s on a busy domain), so re-running it every monitor pass would
    cost minutes per account for data that rarely moves.

    Returns rows inserted. Never raises: a perimeter failure must not take
    down the monitor pass.
    """
    from app.services.enrichers.cache import get_cached, set_cache
    from app.services.perimeter import scan_perimeter
    from app.services.signal_writer import insert_signals

    try:
        if get_cached(db, domain, PERIMETER_CACHE_TYPE):
            logger.debug("perimeter %s: within 7-day cache, skipping", domain)
            return 0
    except Exception:  # noqa: BLE001
        pass  # cache unavailable is not a reason to skip the scan

    try:
        findings = scan_perimeter(domain, db=db)
    except Exception as e:  # noqa: BLE001
        logger.warning("perimeter scan failed for %s: %s", domain, e)
        return 0

    rows = build_cve_signals(
        findings,
        org_id=org_id,
        company_id=company_id,
        company_name=company_name,
        kev_ids=kev_ids,
        api_key=api_key,
    )

    try:
        (
            db.table("signals").delete()
            .eq("org_id", org_id)
            .eq("company_id", company_id)
            .is_("lead_id", "null")
            .eq("signal_type", "cve_in_stack")
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("clearing prior cve_in_stack failed for %s: %s", domain, e)

    written = 0
    if rows:
        try:
            written = insert_signals(db, rows) or 0
        except Exception as e:  # noqa: BLE001
            logger.warning("writing cve_in_stack failed for %s: %s", domain, e)

    # Cache AFTER the work, and cache even on an empty result — "we looked and
    # found nothing" is worth 7 days too, otherwise clean companies get
    # rescanned every pass forever.
    try:
        set_cache(
            db, domain, PERIMETER_CACHE_TYPE, None,
            {"findings": len(findings), "signals": written},
            ttl_seconds=PERIMETER_TTL_SECONDS,
        )
    except Exception:  # noqa: BLE001
        pass

    if written:
        logger.info("perimeter %s: %d cve_in_stack signals written", domain, written)
    return written


def match_findings(
    findings,
    kev_ids: Optional[set] = None,
    api_key: Optional[str] = None,
) -> Dict[str, List[CveMatch]]:
    """Map versioned, normalisable findings to their CVEs.

    Findings without a version, or whose product has no verified normaliser,
    are skipped entirely rather than matched loosely.
    """
    results: Dict[str, List[CveMatch]] = {}
    for f in findings:
        if not getattr(f, "is_versioned", False):
            continue
        cpe = build_cpe(f.product, f.version)
        if not cpe:
            continue
        matches = lookup_cves(cpe, kev_ids=kev_ids, api_key=api_key)
        if matches:
            results[f"{f.host}|{f.product} {f.version}"] = matches
    return results
