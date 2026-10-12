"""Enrichment cache layer — keyed by (domain, type, mode), per-source TTL.

Existing call sites (website/jobs/news/tech_stack/companies_house) keep working
unchanged — the new ttl_seconds kwarg is optional and falls back to the DB's
7-day default when omitted.

PredictLeads + deep research wire in explicit per-source TTLs because their
freshness needs differ (news/jobs change daily, tech/financing change rarely).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from supabase import Client

logger = logging.getLogger(__name__)


# Per-source TTL recommendations. Callers may pass ttl_seconds directly to
# override; this map is the canonical reference for "how stale is acceptable
# for this kind of data". Keep here so it lives next to the cache module.
TTL_SECONDS = {
    "predictleads_technology": 7 * 86400,
    "predictleads_financing": 7 * 86400,
    "predictleads_similar_companies": 7 * 86400,
    "predictleads_company_info": 7 * 86400,
    "predictleads_jobs": 24 * 3600,
    "predictleads_news_events": 24 * 3600,
    "deep_research": 7 * 86400,
    "website": 7 * 86400,
    "tech_stack": 7 * 86400,
    "jobs": 24 * 3600,
    "news": 24 * 3600,
    "companies_house": 30 * 86400,
    # Apollo org enrichment is credit-free but rate-limited; the payload is
    # identical whichever org asks about a domain.
    "apollo_org": 7 * 86400,
    # Apify LinkedIn scrapes are pay-per-item. Raw actor payloads are cached
    # and re-normalised on read so caller-side freshness filters still apply.
    # Person scrapes align with the 6-day per-org monitor cadence.
    "linkedin_posts": 24 * 3600,
    "linkedin_profile": 6 * 86400,
    "linkedin_person_posts": 6 * 86400,
    "linkedin_person_activity": 6 * 86400,
}


def get_cached(
    db: Client,
    domain: str,
    enrichment_type: str,
    enrichment_mode: Optional[str] = None,
    max_age_seconds: Optional[int] = None,
) -> Optional[dict]:
    """Return cached enrichment result if valid (not expired), else None.

    max_age_seconds: optional stricter freshness bound on top of the row's
    TTL. Rejects rows whose fetched_at is older than now - max_age_seconds,
    so a caller can demand fresher data than the type's default TTL without
    busting the cache for everyone else.
    """
    try:
        # Mode is normalised to "" (never NULL): NULLs are pairwise distinct
        # in Postgres unique indexes, which let the upsert insert a duplicate
        # row on every write instead of updating (a one-off cleanup collapsed
        # the historical NULL rows). Order by fetched_at so that, if legacy dupes
        # ever reappear, the freshest row wins instead of a random one.
        result = (
            db.table("enrichment_cache")
            .select("result_data, expires_at, fetched_at")
            .eq("domain", domain.lower())
            .eq("enrichment_type", enrichment_type)
            .eq("enrichment_mode", enrichment_mode or "")
            .order("fetched_at", desc=True)
            .limit(1)
            .execute()
        )

        if not result.data:
            return None

        row = result.data[0]
        expires_at = row.get("expires_at")

        if expires_at:
            now = datetime.now(timezone.utc).isoformat()
            if now > expires_at:
                logger.debug("Cache expired for %s/%s", domain, enrichment_type)
                return None

        if max_age_seconds is not None:
            fetched_at = row.get("fetched_at")
            if not fetched_at:
                return None  # can't prove freshness → treat as miss
            fetched_dt = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - fetched_dt).total_seconds()
            if age > max_age_seconds:
                logger.debug(
                    "Cache too old for %s/%s (%.0fs > %ds)",
                    domain, enrichment_type, age, max_age_seconds,
                )
                return None

        logger.info("Cache hit for %s/%s", domain, enrichment_type)
        return row["result_data"]

    except Exception as e:
        logger.error("Cache lookup error: %s", e)
        return None


def set_cache(
    db: Client,
    domain: str,
    enrichment_type: str,
    enrichment_mode: Optional[str],
    result_data: dict,
    ttl_seconds: Optional[int] = None,
) -> None:
    """Store enrichment result in cache.

    ttl_seconds: how long this row should be considered fresh.
      - None  → fall back to TTL_SECONDS[enrichment_type] if known, else
                let the DB default (7 days) apply by leaving expires_at unset.
      - int   → set expires_at = now + ttl_seconds.

    The DB column has a 7-day default so omitting expires_at still produces a
    valid row, but explicit values let callers ensure shorter freshness for
    daily-volatile sources like jobs/news.
    """
    try:
        row = {
            "domain": domain.lower(),
            "enrichment_type": enrichment_type,
            # "" not NULL — see get_cached; NULL dodges the unique constraint
            # and turns the upsert into an insert-a-duplicate-every-time.
            "enrichment_mode": enrichment_mode or "",
            "result_data": result_data,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

        ttl = ttl_seconds if ttl_seconds is not None else TTL_SECONDS.get(enrichment_type)
        if ttl is not None:
            row["expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=ttl)
            ).isoformat()

        db.table("enrichment_cache").upsert(
            row,
            on_conflict="domain,enrichment_type,enrichment_mode",
        ).execute()
        logger.debug(
            "Cache set for %s/%s (ttl=%s)",
            domain, enrichment_type, ttl if ttl else "db_default",
        )
    except Exception as e:
        logger.error("Cache write error: %s", e)
