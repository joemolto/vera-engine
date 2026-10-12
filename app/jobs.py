"""Background job queue (arq + Redis).

Heavy work (enrichment now; monitor passes next) runs on a separate worker
process instead of as a FastAPI BackgroundTask inside the API process, so a
burst of enrichment can't starve the API and the dashboard stays fast. Jobs
live in Redis (Upstash), survive an API restart, and the worker scales
independently of the web tier.

Enqueue side (API): `enqueue_enrichment(...)` pushes a job, falling back to
in-process execution when REDIS_URL isn't set (local dev) or Redis is
unreachable — so behaviour is identical with or without the queue.

Worker side: `arq app.jobs.WorkerSettings`.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, List

from arq import create_pool
from arq.connections import RedisSettings

from app.config import settings

logger = logging.getLogger(__name__)


# JSON (not arq's default pickle) so the worker can never be tricked into
# executing a crafted payload off the queue — a bad message just fails to
# parse. All our job args are JSON-native (ids/strings/lists). The enqueue
# side and the worker MUST use the same pair.
def _job_serializer(obj: Any) -> bytes:
    return json.dumps(obj).encode()


def _job_deserializer(data: bytes) -> Any:
    return json.loads(data)


def _redis_settings() -> RedisSettings:
    # A placeholder DSN keeps import safe when REDIS_URL is unset (local dev):
    # from_dsn only parses, it never connects. A `rediss://` URL sets TLS.
    return RedisSettings.from_dsn(settings.REDIS_URL or "redis://localhost:6379")


# ---------------------------------------------------------------------------
# Enqueue (API side)
# ---------------------------------------------------------------------------

def _enqueue_sync(name: str, *args: Any, **kwargs: Any) -> None:
    """Push one job. Runs its own event loop so it's callable from the sync
    FastAPI endpoints; enqueues are infrequent, so a short-lived pool is fine."""
    async def _run() -> None:
        pool = await create_pool(
            _redis_settings(),
            job_serializer=_job_serializer,
            job_deserializer=_job_deserializer,
        )
        try:
            await pool.enqueue_job(name, *args, **kwargs)
        finally:
            await pool.close()
    asyncio.run(_run())


def _enqueue_many_sync(name: str, args_list: List[tuple]) -> None:
    """Push many jobs over ONE pooled connection. A pool create is a TLS
    handshake + round-trip to Upstash, so doing it once for a whole seed batch
    (vs once per job) keeps the enqueuing endpoint's latency flat."""
    async def _run() -> None:
        pool = await create_pool(
            _redis_settings(),
            job_serializer=_job_serializer,
            job_deserializer=_job_deserializer,
        )
        try:
            for args in args_list:
                await pool.enqueue_job(name, *args)
        finally:
            await pool.close()
    asyncio.run(_run())


def enqueue_enrichment(
    background_tasks,
    db,
    org_id: str,
    lead_ids: List[str],
    sources: List[str],
    mode: str = "cyber_company_gtm",
) -> str:
    """Queue a lead-enrichment batch on the worker. Falls back to an in-process
    FastAPI background task when the queue isn't available, so callers behave
    identically with or without Redis. Returns 'queued' or 'in_process'."""
    if settings.REDIS_URL:
        try:
            _enqueue_sync("enrich_leads", org_id, lead_ids, sources, mode)
            return "queued"
        except Exception as e:  # noqa: BLE001
            logger.warning("enqueue enrich failed, running in-process: %s", e)
    from app.services.enricher import run_full_enrichment
    background_tasks.add_task(run_full_enrichment, db, org_id, lead_ids, sources, mode)
    return "in_process"


def enqueue_research(org_id: str, domain: str, background_tasks=None) -> str:
    """Queue a company research snapshot (base enrich + one-off monitor pass)
    on the worker. Falls back to a FastAPI background task when one is supplied,
    otherwise runs inline — for callers that are already off the request path
    (e.g. another background task). Returns 'queued' or 'in_process'."""
    if settings.REDIS_URL:
        try:
            _enqueue_sync("research_enrich", org_id, domain)
            return "queued"
        except Exception as e:  # noqa: BLE001
            logger.warning("enqueue research failed, running in-process: %s", e)
    if background_tasks is not None:
        background_tasks.add_task(_run_research_enrich, org_id, domain)
    else:
        _run_research_enrich(org_id, domain)
    return "in_process"


def enqueue_lite_batch(domains: List[str]) -> str:
    """Queue fast firmographic passes for a seed batch, so cards paint in
    seconds. Falls back to inline when Redis is unavailable. Best-effort."""
    if settings.REDIS_URL and domains:
        try:
            _enqueue_many_sync("lite_enrich", [(d,) for d in domains])
            return "queued"
        except Exception as e:  # noqa: BLE001
            logger.warning("batch enqueue lite failed, running in-process: %s", e)
    for d in domains:
        _run_lite_enrich(d)
    return "in_process"


def enqueue_research_batch(org_id: str, domains: List[str]) -> str:
    """Queue research for many domains in a single Redis pool (onboarding seed).
    Falls back to running them inline when Redis is unavailable. Callers off the
    request path (this runs inside a background task) so inline is acceptable.
    Returns 'queued' or 'in_process'."""
    if settings.REDIS_URL and domains:
        try:
            _enqueue_many_sync("research_enrich", [(org_id, d) for d in domains])
            return "queued"
        except Exception as e:  # noqa: BLE001
            logger.warning("batch enqueue research failed, running in-process: %s", e)
    for d in domains:
        _run_research_enrich(org_id, d)
    return "in_process"


def enqueue_monitor_pass(org_id: str, domain: str, background_tasks=None) -> str:
    """Queue a one-off monitor pass (Phases A-E) on the worker. Same fallback
    contract as enqueue_research. Returns 'queued' or 'in_process'."""
    if settings.REDIS_URL:
        try:
            _enqueue_sync("monitor_pass", org_id, domain)
            return "queued"
        except Exception as e:  # noqa: BLE001
            logger.warning("enqueue monitor pass failed, running in-process: %s", e)
    if background_tasks is not None:
        background_tasks.add_task(_run_monitor_pass, org_id, domain)
    else:
        _run_monitor_pass(org_id, domain)
    return "in_process"


# ---------------------------------------------------------------------------
# Tasks (worker side)
# ---------------------------------------------------------------------------

async def ping(ctx: dict) -> str:
    """Trivial job for smoke-testing the worker end to end."""
    logger.info("jobs.ping received")
    return "pong"


async def enrich_leads(
    ctx: dict,
    org_id: str,
    lead_ids: List[str],
    sources: List[str],
    mode: str = "cyber_company_gtm",
) -> dict:
    """Run the enrichment pipeline for a batch of leads. The pipeline is sync
    (blocking vendor calls), so offload it to a thread to keep the worker's
    event loop free for other jobs."""
    from app.database import get_supabase
    from app.services.enricher import run_full_enrichment
    db = get_supabase()
    await asyncio.to_thread(run_full_enrichment, db, org_id, lead_ids, sources, mode)
    return {"org_id": org_id, "leads": len(lead_ids)}


def _run_research_enrich(org_id: str, domain: str) -> None:
    """Company research snapshot: base enrich then a one-off monitor pass.
    Best-effort — the monitor pass runs even if the base enrich fails, since it
    backfills research itself. This is the single source of truth shared by the
    queue task and the no-Redis fallback."""
    from app.database import get_supabase
    try:
        from app.services.company_enricher import enrich_company_only
        enrich_company_only(get_supabase(), org_id, domain)
    except Exception as e:  # noqa: BLE001
        logger.error("research enrich failed for %s/%s: %s", org_id, domain, e)
    from app.services.watchlist.monitor import run_one_off_monitor_pass
    run_one_off_monitor_pass(org_id, domain)


def _run_monitor_pass(org_id: str, domain: str) -> None:
    """One-off monitor pass (Phases A-E) for a freshly added account."""
    from app.services.watchlist.monitor import run_one_off_monitor_pass
    run_one_off_monitor_pass(org_id, domain)


def _run_lite_enrich(domain: str) -> None:
    """Fast, cache-first firmographic pass (name/industry/size/HQ) so a brand-new
    account's card looks real in seconds, while the heavy research_enrich streams
    the deep signals behind it. Best-effort."""
    from app.database import get_supabase
    try:
        from app.services.company_enricher import enrich_company_lite
        enrich_company_lite(get_supabase(), domain)
    except Exception as e:  # noqa: BLE001
        logger.warning("lite enrich failed for %s: %s", domain, e)


async def research_enrich(ctx: dict, org_id: str, domain: str) -> dict:
    """Worker task: company research snapshot. The pipeline is sync (blocking
    vendor calls), so offload it to a thread to keep the event loop free."""
    await asyncio.to_thread(_run_research_enrich, org_id, domain)
    return {"org_id": org_id, "domain": domain}


async def monitor_pass(ctx: dict, org_id: str, domain: str) -> dict:
    """Worker task: one-off monitor pass for a freshly added account."""
    await asyncio.to_thread(_run_monitor_pass, org_id, domain)
    return {"org_id": org_id, "domain": domain}


async def lite_enrich(ctx: dict, domain: str) -> dict:
    """Worker task: fast firmographic pass for onboarding first-paint."""
    await asyncio.to_thread(_run_lite_enrich, domain)
    return {"domain": domain}


class WorkerSettings:
    """Run with: `arq app.jobs.WorkerSettings`."""
    functions = [ping, enrich_leads, research_enrich, monitor_pass, lite_enrich]
    redis_settings = _redis_settings()
    job_serializer = _job_serializer
    job_deserializer = _job_deserializer
    max_jobs = 12         # concurrent jobs. Box (2 vCPU/8GB) handles it — work is
                          # I/O-bound (vendor waits). Real ceiling is vendor rate
                          # limits, not the host; push toward 16 only if they cope.
    job_timeout = 900     # 15 min ceiling per batch
    keep_result = 3600    # keep results 1h for debugging
    max_tries = 3         # retry a failed job a couple of times
