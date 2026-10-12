"""Canonical companies entity layer.

Domains are global — one row per `domain` regardless of org. Org-specific
interpretation (signals, threat_alerts on leads, action_outcomes) stays
on the existing org-scoped tables.

`upsert_company` is called from every enrichment phase that produces
company-level facts. Subsequent enrichments merge non-destructively into
the JSONB blobs so we don't lose intel that only one source captured.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from supabase import Client

from app.services.domain_resolver import clean_domain

logger = logging.getLogger(__name__)


# Fields we keep as scalar columns (display + indexable). All other
# enrichment payloads land in the JSONB columns.
SCALAR_FIELDS = {
    "name", "industry", "size_band", "hq_country", "hq_city",
    "founded_year", "linkedin_url", "description",
}

JSONB_FIELDS = {
    "technologies", "compliance",
    "threat_alerts",  # RETIRED 2026-08-05 — declared but no longer written
    "predictleads_data", "apollo_data", "deep_research_data",
}


def _merge_jsonb(existing: Any, incoming: Any) -> Any:
    """Shallow merge for dicts; replace for lists / scalars when incoming is non-empty.

    Companies accumulate intel over time — a later run that found nothing
    must not erase what an earlier run found. Empty incoming values are dropped.
    """
    if incoming is None:
        return existing
    if isinstance(incoming, list):
        return incoming if incoming else existing
    if isinstance(incoming, dict):
        if not incoming:
            return existing
        out = dict(existing or {})
        for k, v in incoming.items():
            if v is None:
                continue
            if isinstance(v, (dict, list)):
                out[k] = _merge_jsonb(out.get(k), v)
            else:
                out[k] = v
        return out
    return incoming


def upsert_company(
    db: Client,
    domain: str,
    *,
    name: Optional[str] = None,
    industry: Optional[str] = None,
    size_band: Optional[str] = None,
    hq_country: Optional[str] = None,
    hq_city: Optional[str] = None,
    founded_year: Optional[int] = None,
    linkedin_url: Optional[str] = None,
    description: Optional[str] = None,
    technologies: Optional[List[Dict]] = None,
    compliance: Optional[Dict] = None,
    threat_alerts: Optional[List[Dict]] = None,
    predictleads_data: Optional[Dict] = None,
    apollo_data: Optional[Dict] = None,
    deep_research_data: Optional[Dict] = None,
    ai_quality_pass: Optional[Dict] = None,
    credential_exposure: Optional[Dict] = None,
) -> Optional[str]:
    """Upsert a company row keyed by `domain`. Returns the company id.

    JSONB blobs merge non-destructively (existing keys preserved unless
    incoming carries a non-empty replacement). Scalar fields only
    overwrite when the incoming value is non-empty.
    """
    cleaned = clean_domain(domain) if domain else None
    if not cleaned:
        return None

    now = datetime.now(timezone.utc).isoformat()

    existing_q = (
        db.table("companies")
        .select("*")
        .eq("domain", cleaned)
        .execute()
    )
    existing = existing_q.data[0] if existing_q.data else None

    incoming_scalars = {
        "name": name, "industry": industry, "size_band": size_band,
        "hq_country": hq_country, "hq_city": hq_city,
        "founded_year": founded_year, "linkedin_url": linkedin_url,
        "description": description,
    }
    payload: Dict[str, Any] = {}
    for k, v in incoming_scalars.items():
        if v is None or v == "":
            continue
        if existing and existing.get(k) == v:
            continue
        payload[k] = v

    # threat_alerts is NOT written — retired 2026-08-05, the second half of
    # the cve_in_stack removal (see enricher.py). Purging the signals table
    # was not enough: the dashboard's KEV chip reads companies.threat_alerts
    # directly (lib/dashboard/accountSignals.ts), so 1,251 unfounded alerts
    # across 288 companies stayed on screen after the signals were deleted.
    # Ebury was showing four "actively exploited" badges for Ubiquiti UniFi,
    # Splunk and LiteSpeed cPanel with zero matched technologies, on a
    # currency fintech.
    #
    # The column is kept, not dropped: it is the right home for CVE data once
    # the perimeter collector can supply an observed product AND version.
    # Re-enable only when what goes in is evidence, not correlation.
    incoming_jsonb = {
        "technologies": technologies,
        "compliance": compliance,
        "predictleads_data": predictleads_data,
        "apollo_data": apollo_data,
        "deep_research_data": deep_research_data,
        "ai_quality_pass": ai_quality_pass,
        "credential_exposure": credential_exposure,
    }
    for k, v in incoming_jsonb.items():
        if v is None:
            continue
        merged = _merge_jsonb(existing.get(k) if existing else None, v)
        if merged != (existing.get(k) if existing else None):
            payload[k] = merged

    if existing:
        if not payload:
            return existing["id"]
        payload["last_enriched_at"] = now
        payload["updated_at"] = now
        payload["enrichment_version"] = (existing.get("enrichment_version") or 1) + 1
        db.table("companies").update(payload).eq("id", existing["id"]).execute()
        return existing["id"]

    payload["domain"] = cleaned
    payload["last_enriched_at"] = now
    inserted = db.table("companies").insert(payload).execute()
    if inserted.data:
        return inserted.data[0]["id"]
    return None


def get_company_by_domain(db: Client, domain: str) -> Optional[Dict]:
    cleaned = clean_domain(domain) if domain else None
    if not cleaned:
        return None
    res = db.table("companies").select("*").eq("domain", cleaned).execute()
    return res.data[0] if res.data else None


# Signals that bump the matching roll-up boolean on companies. Used by
# `record_intel_observations` so the company intelligence page can read
# "has X" without scanning company_intel.
_SIGNAL_TYPE_TO_ROLLUP_COL: Dict[str, str] = {
    "security_team_expansion": "has_security_team_growth",
    "security_leadership_hire": "has_security_team_growth",
    "hiring_security": "has_security_team_growth",
    "recent_funding": "has_recent_funding",
    "recently_funded": "has_recent_funding",
    "compliance_gap": "has_compliance_signal",
}


def _bump_intel_observation(
    db: Client,
    company_id: str,
    signal_type: str,
    signal_data: Dict[str, Any],
    source: str,
) -> None:
    """One signal observation → one row in company_intel.

    First observation INSERTs; subsequent observations UPDATE the row's
    latest_observed_at + signal_data and increment observed_count. We
    can't use Supabase upsert for the increment because PostgREST's
    ON CONFLICT replaces the row outright; the SELECT-then-UPDATE here
    is racy in the general case but acceptable for an off-band
    intelligence rollup (a missed +1 over the long-run aggregate is not
    a correctness issue).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    cur = (
        db.table("company_intel")
        .select("id, observed_count")
        .eq("company_id", company_id)
        .eq("signal_type", signal_type)
        .limit(1)
        .execute()
    )
    if cur.data:
        prev = cur.data[0]
        db.table("company_intel").update({
            "signal_data": signal_data,
            "source": source,
            "latest_observed_at": now_iso,
            "observed_count": int(prev.get("observed_count") or 1) + 1,
        }).eq("id", prev["id"]).execute()
    else:
        db.table("company_intel").insert({
            "company_id": company_id,
            "signal_type": signal_type,
            "signal_data": signal_data,
            "source": source,
        }).execute()


def record_intel_observations(
    db: Client,
    company_id: str,
    org_id: str,
    signal_rows: Optional[List[Dict[str, Any]]],
) -> None:
    """Roll an enrichment's outputs into the global intelligence layer.

    For each unique signal_type in `signal_rows`:
      - Upserts (company_id, signal_type) into company_intel, bumping
        observed_count.
      - Flips the matching roll-up boolean on companies to TRUE.

    Also bumps companies.enrichment_count and recomputes
    watched_by_org_count from DISTINCT(org_id) on the leads table.

    Best-effort: each step is try/except'd so a schema mismatch or
    transient DB error degrades to a logged warning instead of breaking
    the enrichment that produced the signals.
    """
    if not company_id:
        return

    seen: set = set()
    rollups: Dict[str, bool] = {}
    for row in (signal_rows or []):
        sig_type = row.get("signal_type")
        if not sig_type or sig_type in seen:
            continue
        seen.add(sig_type)
        try:
            _bump_intel_observation(
                db,
                company_id,
                sig_type,
                row.get("signal_data") or {},
                row.get("signal_source") or "enrichment",
            )
        except Exception as e:
            logger.warning(
                "company_intel upsert failed (%s, %s): %s",
                company_id, sig_type, e,
            )
        col = _SIGNAL_TYPE_TO_ROLLUP_COL.get(sig_type)
        if col:
            rollups[col] = True

    # Bump counts on the companies row
    try:
        cur = (
            db.table("companies")
            .select("enrichment_count")
            .eq("id", company_id)
            .limit(1)
            .execute()
        )
        cur_row = cur.data[0] if cur.data else {}
        new_enrich = int(cur_row.get("enrichment_count") or 0) + 1

        # Recompute watched_by_org_count from authoritative source.
        # The current lead's company_id may not have been written yet
        # (the enricher batches the update in the same call), so we
        # include `org_id` explicitly in the set.
        distinct = (
            db.table("leads")
            .select("org_id")
            .eq("company_id", company_id)
            .execute()
        )
        org_ids = {
            r.get("org_id")
            for r in (distinct.data or [])
            if r.get("org_id")
        }
        org_ids.add(org_id)

        db.table("companies").update({
            "enrichment_count": new_enrich,
            "watched_by_org_count": len(org_ids),
            "last_enriched_at": datetime.now(timezone.utc).isoformat(),
            **rollups,
        }).eq("id", company_id).execute()
    except Exception as e:
        logger.warning(
            "companies rollup update failed for %s: %s", company_id, e
        )


def upsert_company_from_enrichment(
    db: Client, domain: str, enrichment_data: Dict, lead: Optional[Dict] = None,
) -> Optional[str]:
    """Convenience: extract company-level facts out of an enrichment payload
    and write them through `upsert_company`. Used at the tail of every
    enrichment run so the canonical row stays in sync with the lead row."""
    if not domain:
        return None

    tech = (enrichment_data.get("tech_stack") or {})
    techs = tech.get("technologies_detected") if isinstance(tech, dict) else None

    deep = enrichment_data.get("deep_research") or {}
    pl_company = enrichment_data.get("predictleads_company") or {}

    return upsert_company(
        db,
        domain,
        name=(lead or {}).get("company_name") or pl_company.get("name"),
        industry=(lead or {}).get("company_industry") or pl_company.get("industry"),
        size_band=(lead or {}).get("company_size") or pl_company.get("size"),
        hq_country=(lead or {}).get("company_location") or pl_company.get("location"),
        founded_year=pl_company.get("founded_year"),
        linkedin_url=pl_company.get("linkedin_url"),
        description=pl_company.get("description") or deep.get("company_description"),
        technologies=techs,
        compliance=deep.get("compliance") if isinstance(deep, dict) else None,
        threat_alerts=enrichment_data.get("threat_alerts"),
        predictleads_data={
            "company": enrichment_data.get("predictleads_company"),
            "news": enrichment_data.get("predictleads_news"),
            "tech": enrichment_data.get("predictleads_tech"),
            "jobs": enrichment_data.get("predictleads_jobs"),
            "financing": enrichment_data.get("predictleads_financing"),
            "similar": enrichment_data.get("predictleads_similar"),
        },
        apollo_data=enrichment_data.get("apollo"),
        deep_research_data=deep if isinstance(deep, dict) else None,
        ai_quality_pass=enrichment_data.get("ai_quality_pass"),
    )
