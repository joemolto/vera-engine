"""Credential exposure enrichment — LeakRadar stealer-log / dark-web check.

LeakRadar (https://api.leakradar.io) indexes hundreds of billions of
plain-text credentials harvested from infostealer malware logs, combolists
and dark-web dumps. We query by company domain to answer one question per
account: how exposed is this company in stealer logs, and has anything
surfaced recently?

COST MODEL: searches and reports are FREE and unlimited on LeakRadar. Only
*revealing* a plain-text password costs an "unlock". Vera NEVER unlocks — we
surface the *exposure* (counts, categories, recency, whether passwords were
captured), never the credentials themselves. So this enricher never touches
the unlock quota. We pass auto_unlock=False everywhere as belt-and-braces.

WHAT WE PULL (all free):
  GET  /search/domain/{domain}    → category totals + password-strength summary
  POST /search/advanced           → count of EMPLOYEE creds *surfaced* in the
       (email_domain, added_from)   last 90 days — the recency signal that
                                     drives the Risk score

Returns a compact summary persisted to ``companies.credential_exposure`` and
projected into Risk signals by ``company_enricher``:
  - ``credential_leak``              employees, recency-gated → dominant Risk
  - ``credential_leak_supply_chain`` third parties            → secondary Risk
Customer exposure is stored for the dossier but never scored (2026-06-18).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import httpx
from supabase import Client

from app.config import settings
from app.services.enrichers.cache import get_cached, set_cache

logger = logging.getLogger(__name__)

BASE_URL = "https://api.leakradar.io"
REQUEST_TIMEOUT = 30.0
RECENT_WINDOW_DAYS = 90
# Reports change slowly day-to-day and searches are free, so a short TTL is
# just politeness — hot accounts (2-day cadence) re-check every pass, warmer
# ones reuse the cached summary.
_CACHE_TTL_SECONDS = 3 * 86400
_CACHE_TYPE = "credential_exposure"


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.LEAK_RADAR_API_KEY}",
        "Accept": "application/json",
        "User-Agent": "Vera/1.0",
    }


def _empty_summary(domain: str) -> Dict[str, Any]:
    return {
        "source": "leakradar",
        "domain": domain,
        "employees_compromised": 0,
        "third_parties_compromised": 0,
        "customers_compromised": 0,
        "recent_employees_90d": 0,
        "most_recent_surfaced": None,
        "employee_passwords_captured": False,
        "third_party_passwords_captured": 0,
        "has_recent_exposure": False,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _domain_report(client: httpx.Client, domain: str) -> Dict[str, Any]:
    """GET /search/domain/{domain} — category totals + password summaries (free)."""
    resp = client.get(
        f"{BASE_URL}/search/domain/{domain}",
        headers=_headers(),
    )
    resp.raise_for_status()
    return resp.json() or {}


def _employee_search(
    client: httpx.Client, domain: str, since: Optional[str]
) -> Tuple[int, Optional[str], bool]:
    """POST /search/advanced filtered to corporate-email credentials (free).

    ``email_domain={domain}`` matches stealer-log records whose leaked email is
    @domain — i.e. the company's own staff. For B2B targets (banks, insurers,
    etc.) the corporate domain is staff-only, so this is the true "employee
    credentials in stealer logs" count — and it's the metric the report's
    narrower URL-based ``employees_compromised`` undersells.

    When ``since`` is given, ``added_from`` (LeakRadar's *discovery* date)
    restricts to creds that surfaced on/after that date — exactly the "anything
    worth surfacing in the last 3 months" test. ``total`` is authoritative; we
    read item ``added_at`` for the most-recent label and ``password_strength``
    to flag (never reveal) that passwords were captured. ``auto_unlock=False``
    guarantees we never spend quota.
    """
    body: Dict[str, Any] = {"email_domain": [domain]}
    if since:
        body["added_from"] = since
    resp = client.post(
        f"{BASE_URL}/search/advanced",
        headers=_headers(),
        params={"page": 1, "page_size": 50, "auto_unlock": False},
        json=body,
    )
    resp.raise_for_status()
    data = resp.json() or {}
    total = int(data.get("total") or 0)
    items = [it for it in (data.get("items") or []) if isinstance(it, dict)]
    dates = [it.get("added_at") for it in items if it.get("added_at")]
    most_recent = max(dates) if dates else None
    passwords_captured = any(it.get("password_strength") is not None for it in items)
    return total, most_recent, passwords_captured


def _collect_recent_employees(
    client: httpx.Client, domain: str, since: str, cap: int = 1000
) -> Tuple[list, int, Optional[str], bool]:
    """Paginate recent employee records for scoring AND the raw_artifacts archive.

    Same email_domain + added_from filter as _employee_search, but pages
    through all matching records (capped) so we can archive the masked detail
    (emails, urls, sources, dates, password-strength) — never plain-text.
    `total` is the API's authoritative count (may exceed the records we keep
    when capped). auto_unlock=False guarantees we never spend quota.
    """
    records: list = []
    total = 0
    page = 1
    page_size = 100
    while len(records) < cap:
        resp = client.post(
            f"{BASE_URL}/search/advanced",
            headers=_headers(),
            params={"page": page, "page_size": page_size, "auto_unlock": False},
            json={"email_domain": [domain], "added_from": since},
        )
        resp.raise_for_status()
        data = resp.json() or {}
        if page == 1:
            total = int(data.get("total") or 0)
        items = [it for it in (data.get("items") or []) if isinstance(it, dict)]
        records.extend(items)
        if len(items) < page_size:
            break
        page += 1
    dates = [it.get("added_at") for it in records if it.get("added_at")]
    most_recent = max(dates) if dates else None
    passwords_captured = any(it.get("password_strength") is not None for it in records)
    return records, total, most_recent, passwords_captured


def _report_int(report: Dict[str, Any], key: str) -> int:
    try:
        return int(report.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _password_total(report: Dict[str, Any], key: str) -> int:
    summary = report.get(key)
    if isinstance(summary, dict):
        return int(summary.get("total_pass") or 0)
    return 0


def _password_total(report: Dict[str, Any], key: str) -> int:
    summary = report.get(key)
    if isinstance(summary, dict):
        return int(summary.get("total_pass") or 0)
    return 0


def check_credential_exposure(
    db: Client, domain: str, *, force_refresh: bool = False
) -> Optional[Dict[str, Any]]:
    """Return a credential-exposure summary for `domain`, or None on failure.

    Cached per domain (3-day TTL). Returns a zeroed summary (not None) when the
    domain is clean, so the dossier can honestly show "no exposure" rather than
    a missing panel. Returns None only on a hard error / missing API key, so
    callers leave any prior summary untouched.

    force_refresh skips the cache read and forces a live fetch — used by the
    backfill so the raw payloads get archived to raw_artifacts even when the
    summary is already cached.
    """
    domain = (domain or "").lower().strip()
    if not domain:
        return None
    if not settings.LEAK_RADAR_API_KEY:
        logger.info(
            "LEAK_RADAR_API_KEY not set — skipping credential exposure for %s", domain
        )
        return None

    if not force_refresh:
        cached = get_cached(db, domain, _CACHE_TYPE)
        if cached is not None:
            return cached

    since = (
        datetime.now(timezone.utc) - timedelta(days=RECENT_WINDOW_DAYS)
    ).date().isoformat()

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            # The domain report is the authoritative source of category
            # totals — a hard failure here aborts (we keep any prior summary).
            try:
                report = _domain_report(client, domain)
            except httpx.HTTPStatusError as e:
                status = e.response.status_code if e.response is not None else None
                if status == 404:
                    # Domain not present in any leak set — explicitly clean.
                    summary = _empty_summary(domain)
                    set_cache(db, domain, _CACHE_TYPE, None, summary, ttl_seconds=_CACHE_TTL_SECONDS)
                    return summary
                logger.warning(
                    "LeakRadar domain report failed for %s (status %s): %s", domain, status, e
                )
                return None

            # Employee exposure comes from the email_domain advanced search
            # (the true "staff creds in stealer logs" count), not the report's
            # narrower employees_compromised. Both calls are best-effort: if
            # advanced search fails (e.g. a date-format quirk), degrade to zero
            # employee counts rather than nuking the whole read — the dossier
            # still shows the report's third-party / customer exposure.
            try:
                employees_total, _md, _pw_all = _employee_search(client, domain, None)
            except Exception as e:
                logger.warning(
                    "LeakRadar employee (all-time) lookup failed for %s: %s", domain, e
                )
                employees_total = 0
            recent_records: list = []
            try:
                recent_records, recent_employees, most_recent, recent_pw = (
                    _collect_recent_employees(client, domain, since)
                )
            except Exception as e:
                logger.warning(
                    "LeakRadar employee recency lookup failed for %s (totals kept): %s",
                    domain, e,
                )
                recent_employees, most_recent, recent_pw = 0, None, False
    except Exception as e:
        logger.warning("Credential exposure check errored for %s: %s", domain, e)
        return None

    # Archive the raw payloads to raw_artifacts (best-effort — never blocks the
    # summary). Masked detail only: the report's category breakdown + the recent
    # employee records (emails, urls, sources, dates, password-strength). We
    # never request or store plain-text passwords. persist_raw dedupes by
    # content hash, so unchanged data on a later pass writes nothing new.
    try:
        from app.services.raw_artifacts import persist_raw

        persist_raw(
            db, domain, "leakradar", report,
            source_endpoint="GET /search/domain/{domain}",
        )
        persist_raw(
            db, domain, "leakradar",
            {
                "query": {"email_domain": domain, "added_from": since},
                "total": recent_employees,
                "records": recent_records,
            },
            source_endpoint="POST /search/advanced (recent employees)",
        )
    except Exception as e:
        logger.warning("LeakRadar raw archive failed for %s: %s", domain, e)

    third_parties = _report_int(report, "third_parties_compromised")
    customers = _report_int(report, "customers_compromised")
    # All-time can lag a fresh recency page; never report fewer all-time than
    # recent (they cover the same email_domain set).
    employees = max(employees_total, recent_employees)

    summary = {
        "source": "leakradar",
        "domain": domain,
        "employees_compromised": employees,
        "third_parties_compromised": third_parties,
        "customers_compromised": customers,
        "recent_employees_90d": recent_employees,
        "most_recent_surfaced": most_recent,
        "employee_passwords_captured": recent_pw,
        "third_party_passwords_captured": _password_total(report, "third_parties_passwords"),
        "has_recent_exposure": recent_employees > 0,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    set_cache(db, domain, _CACHE_TYPE, None, summary, ttl_seconds=_CACHE_TTL_SECONDS)
    return summary


def build_signals(
    *, org_id: str, company_id: str, summary: Optional[Dict[str, Any]], now_iso: str
) -> list:
    """Project a credential-exposure summary into Risk signal rows.

    Two scored categories (2026-06-18): employee creds surfaced in the last
    90 days are the DOMINANT Risk driver — recency-gated, because "surfaced in
    the last three months" is the whole point; third-party / supply-chain
    exposure is a secondary driver. Customer exposure is held on the company
    row for the dossier but never scored. We surface the EXPOSURE, never the
    credentials. Shared by enrich_company_only and the backfill script.
    """
    ce = summary or {}
    rows = []
    recent_emp = int(ce.get("recent_employees_90d") or 0)
    emp_total = int(ce.get("employees_compromised") or 0)
    tp_total = int(ce.get("third_parties_compromised") or 0)

    if recent_emp > 0:
        pw = " — passwords captured" if ce.get("employee_passwords_captured") else ""
        when = ce.get("most_recent_surfaced")
        when_label = (
            f", most recent {when[:7]}" if isinstance(when, str) and len(when) >= 7 else ""
        )
        plural = "s" if recent_emp != 1 else ""
        rows.append(
            {
                "org_id": org_id,
                "company_id": company_id,
                "signal_type": "credential_leak",
                "signal_source": "leakradar",
                "signal_data": {
                    "headline": (
                        f"{recent_emp} employee credential{plural} surfaced in stealer "
                        f"logs in the last 90 days{when_label} "
                        f"({emp_total} total exposed){pw}"
                    ),
                    "recent_employees": recent_emp,
                    "total_employees": emp_total,
                    "most_recent_surfaced": when,
                    "passwords_captured": bool(ce.get("employee_passwords_captured")),
                    "category": "employees",
                },
                "signal_strength": 0.95,
                "signal_subject_type": "account",
                "detected_at": now_iso,
            }
        )

    if tp_total > 0:
        plural = "s" if tp_total != 1 else ""
        rows.append(
            {
                "org_id": org_id,
                "company_id": company_id,
                "signal_type": "credential_leak_supply_chain",
                "signal_source": "leakradar",
                "signal_data": {
                    "headline": (
                        f"{tp_total} third-party / supply-chain credential{plural} "
                        f"exposed in stealer logs"
                    ),
                    "total_third_parties": tp_total,
                    "category": "third_parties",
                },
                "signal_strength": 0.7,
                "signal_subject_type": "account",
                "detected_at": now_iso,
            }
        )
    return rows


_CRED_SIGNAL_TYPES = ["credential_leak", "credential_leak_supply_chain"]


def refresh_credential_signals(
    db: Client,
    *,
    org_id: str,
    company_id: str,
    summary: Optional[Dict[str, Any]],
    now_iso: Optional[str] = None,
) -> int:
    """Replace this company's credential-exposure signals with a fresh set.

    Surgically deletes only the prior credential_leak / credential_leak_supply_
    chain account rows (lead_id null) for the company, then inserts from
    build_signals. Keeps the signal set in sync as counts change between passes
    without touching funding / news / posture signals. Returns rows inserted.

    Used by the monitor pass and the backfill. enrich_company_only doesn't call
    this — it already deletes + rewrites all account signals in one shot.
    """
    from app.services.signal_writer import insert_signals

    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    try:
        (
            db.table("signals")
            .delete()
            .eq("org_id", org_id)
            .eq("company_id", company_id)
            .is_("lead_id", "null")
            .in_("signal_type", _CRED_SIGNAL_TYPES)
            .execute()
        )
    except Exception as e:
        logger.warning(
            "Clearing prior credential signals failed for company %s: %s", company_id, e
        )

    rows = build_signals(
        org_id=org_id, company_id=company_id, summary=summary, now_iso=now_iso
    )
    if not rows:
        return 0
    try:
        return insert_signals(db, rows)
    except Exception as e:
        logger.warning(
            "Inserting credential signals failed for company %s: %s", company_id, e
        )
        return 0
