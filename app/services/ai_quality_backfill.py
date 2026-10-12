"""Backfill the AI quality pass for companies missing it.

Audit 2026-06-02: only 52/1128 companies have ai_quality_pass.
The pass currently fires inside lead-level enrichment, so accounts
that were enriched before the column was added (or never re-enriched)
don't have it. This backfill walks companies WHERE ai_quality_pass
IS NULL and runs the pass over their existing enrichment_data — no
new vendor calls, just Haiku over what's already stored.

Cost per company: ~$0.001-0.002 (Haiku 4.5). 1076 companies × $0.0015
≈ $1.62 total. Fire it in batches via the backfill router endpoint.

Idempotent: skips companies that already have a non-null pass unless
`force=True`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from supabase import Client

from app.services.ai_quality_pass import run_quality_pass

logger = logging.getLogger(__name__)


def backfill_one(
    db: Client,
    company_id: str,
    *,
    force: bool = False,
) -> Dict[str, Any]:
    """Run the AI quality pass for one company using its already-stored
    enrichment data + recent signals + threat alerts. Writes the result
    back to companies.ai_quality_pass.

    Returns {ok, company_id, name, signals_in, tech_in} on success;
    {ok: False, error} on skip/failure.
    """
    # 1. Load the company row
    resp = (
        db.table("companies")
        .select(
            "id, name, domain, industry, size_band, technologies, threat_alerts, "
            "predictleads_data, apollo_data, deep_research_data, ai_quality_pass"
        )
        .eq("id", company_id)
        .limit(1)
        .execute()
    )
    if not resp.data:
        return {"ok": False, "error": "not found"}
    c = resp.data[0]

    if not force and c.get("ai_quality_pass"):
        return {"ok": False, "skipped": True, "reason": "already has pass"}

    name = c.get("name") or ""
    domain = (c.get("domain") or "").strip().lower()
    if not domain:
        return {"ok": False, "error": "missing domain"}

    apollo = c.get("apollo_data") or {}

    # 2. Gather signals for this company (recent, capped at 40 per the
    #    pass's MAX_SIGNALS_IN).
    sigs_resp = (
        db.table("signals")
        .select("signal_type, signal_subject_type, inference_level, signal_data, detected_at")
        .eq("company_id", company_id)
        .order("detected_at", desc=True)
        .limit(40)
        .execute()
    )
    signals = sigs_resp.data or []

    # 3. Tech list — companies.technologies is the canonical store.
    technologies_raw = c.get("technologies") or []
    technologies: List[Dict[str, Any]] = (
        technologies_raw if isinstance(technologies_raw, list) else []
    )[:120]

    # 4. Run the pass
    try:
        result = run_quality_pass(
            company_name=name,
            company_domain=domain,
            company_size_band=c.get("size_band"),
            company_industry=c.get("industry"),
            company_revenue=(
                apollo.get("organization_revenue_printed")
                or apollo.get("annual_revenue_printed")
            ),
            is_public_company=bool(apollo.get("publicly_traded_symbol")),
            signals=signals,
            technologies=technologies,
        )
    except Exception as e:
        logger.warning(
            "quality_pass failed during backfill for %s: %s", company_id, e
        )
        return {"ok": False, "error": str(e)}

    # 5. Persist
    try:
        db.table("companies").update(
            {"ai_quality_pass": result}
        ).eq("id", company_id).execute()
    except Exception as e:
        logger.warning("backfill update failed for %s: %s", company_id, e)
        return {"ok": False, "error": f"update failed: {e}"}

    return {
        "ok": True,
        "company_id": company_id,
        "name": name,
        "signals_in": len(signals),
        "tech_in": len(technologies),
    }


def list_pending(db: Client, *, limit: int = 50) -> List[str]:
    """Return company_ids missing an ai_quality_pass. Ordered by
    last_enriched_at desc so the most-actively-monitored accounts
    get backfilled first."""
    resp = (
        db.table("companies")
        .select("id")
        .is_("ai_quality_pass", "null")
        .order("last_enriched_at", desc=True, nullsfirst=False)
        .limit(limit)
        .execute()
    )
    return [r["id"] for r in (resp.data or [])]
