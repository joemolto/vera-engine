"""
Monthly enrichment sweep — re-verifies the profile facts on WATCHED accounts.

The weekly monitor detects change (hiring, funding, stack deltas, news,
credentials, social); it never re-verifies what IS. Revenue, headcount,
full tech stack, email posture and the deep-research narrative drift
silently after the on-add enrichment. This sweep re-runs the full
enrichment for every monitored account whose profile is older than
STALE_DAYS, so the profile stays honest without a rep clicking
anything. (2026-07-17: monthly cadence.)

Scope and cost:
  - Monitored accounts only (monitoring_active = true). Research-mode
    accounts keep their one-time snapshot — the deliberate
    decoupled-monitoring design, and it caps spend.
  - STALE_DAYS guard makes the sweep idempotent: re-firing the cron in
    the same month re-enriches nothing that's already fresh.
  - Sequential on purpose — roughly $3-5/account across vendors; a burst
    of parallel enrichments is a rate-limit and bill surprise.

Called by POST /watchlist/enrich-sweep (external cron, monthly). Never raises:
every account is try/excepted and the summary reports failures.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from supabase import Client

from app.services.company_enricher import enrich_company_only

logger = logging.getLogger(__name__)

STALE_DAYS = 25  # a monthly cron that re-fires never double-spends


def run_enrichment_sweep(db: Client) -> Dict[str, Any]:
    """Re-enrich stale monitored accounts across every org. Returns a
    summary dict (also logged) for the cron's execution record."""
    started = datetime.now(timezone.utc)
    cutoff = (started - timedelta(days=STALE_DAYS)).isoformat()

    rows = (
        db.table("watchlist")
        .select("org_id, company_domain, company_name")
        .eq("status", "active")
        .eq("monitoring_active", True)
        .execute()
    ).data or []

    # Never re-enrich a lapsed org's book. Churn deliberately KEEPS
    # watchlist rows (the re-engagement asset), which made this monthly
    # sweep a forever-bill for every org that ever churned (2026-08-23
    # lapsed-org audit — the largest recurring leak found).
    from app.services.plan_limits import REVOKED_STATUSES
    org_ids = sorted({r["org_id"] for r in rows if r.get("org_id")})
    lapsed: set = set()
    if org_ids:
        for o in (db.table("organisations").select("id, subscription_status")
                  .in_("id", org_ids).execute().data or []):
            if (o.get("subscription_status") or "").lower() in REVOKED_STATUSES:
                lapsed.add(o["id"])
    rows = [r for r in rows if r.get("org_id") not in lapsed]

    # Stale check against the shared companies cache — one query.
    domains = sorted({
        (r.get("company_domain") or "").lower()
        for r in rows if r.get("company_domain")
    })
    fresh: set = set()
    if domains:
        res = (
            db.table("companies")
            .select("domain, last_enriched_at")
            .in_("domain", domains)
            .gte("last_enriched_at", cutoff)
            .execute()
        )
        fresh = {(c.get("domain") or "").lower() for c in (res.data or [])}

    enriched: List[str] = []
    skipped = 0
    failures: List[str] = []
    seen: set = set()
    for r in rows:
        domain = (r.get("company_domain") or "").lower()
        if not domain or domain in seen:
            continue
        seen.add(domain)
        if domain in fresh:
            skipped += 1
            continue
        try:
            enrich_company_only(
                db,
                r["org_id"],
                domain,
                company_name_hint=r.get("company_name"),
            )
            enriched.append(domain)
        except Exception as e:
            failures.append(domain)
            logger.warning("enrich-sweep: %s failed: %s", domain, e)

    summary = {
        "status": "completed",
        "enriched": len(enriched),
        "skipped_fresh": skipped,
        "failed": len(failures),
        "failed_domains": failures[:20],
        "duration_s": int((datetime.now(timezone.utc) - started).total_seconds()),
    }
    logger.info("enrich-sweep summary: %s", summary)
    return summary
