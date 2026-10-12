"""AI quality pass — backfill endpoint.

Lets the operator call into the engine to run the quality pass over accounts
that don't yet have it (per audit 2026-06-02: 1076/1128 missing).
Processes batches sequentially — Haiku rate limits are generous but
we don't want to thunder them with 1000 concurrent calls.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.database import get_supabase
from app.services.ai_quality_backfill import backfill_one, list_pending

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ai-quality"])


class BackfillRequest(BaseModel):
    """Body for /api/v1/ai-quality/backfill.

    `limit` caps how many companies to process in one call. Default
    50 — call repeatedly until summary.processed == 0 to drain the
    backlog.

    `company_id` (optional) — backfill just one company. Overrides
    `limit` when set.

    `force` — re-run even if the company already has a pass. Useful
    when the pass shape changes and we want to recompute.
    """

    limit: Optional[int] = 50
    company_id: Optional[str] = None
    force: Optional[bool] = False


@router.post("/backfill")
def trigger_backfill(request: BackfillRequest):
    db = get_supabase()

    # Single-company mode
    if request.company_id:
        result = backfill_one(db, request.company_id, force=bool(request.force))
        return {"ok": True, "results": [result]}

    # Batch mode — pull pending IDs then process with mild parallelism.
    # Haiku rate limit (50 req/min on tier 1) accommodates 4 concurrent
    # without strain. The Haiku call inside the pass is the slow part
    # (~1.5s); ThreadPoolExecutor at 4 workers gets us through 50
    # companies in ~20s.
    limit = max(1, min(request.limit or 50, 200))
    ids = list_pending(db, limit=limit)
    if not ids:
        return {"ok": True, "processed": 0, "results": [], "message": "no pending companies"}

    results = []
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="qpass") as pool:
        futures = {
            pool.submit(backfill_one, db, cid, force=bool(request.force)): cid
            for cid in ids
        }
        for fut in as_completed(futures):
            cid = futures[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                logger.warning("backfill thread raised for %s: %s", cid, e)
                results.append({"ok": False, "company_id": cid, "error": str(e)})

    succeeded = sum(1 for r in results if r.get("ok"))
    skipped = sum(1 for r in results if r.get("skipped"))
    failed = sum(1 for r in results if not r.get("ok") and not r.get("skipped"))

    return {
        "ok": True,
        "processed": len(results),
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }


@router.get("/pending")
def count_pending():
    """How many companies still need the pass? Cheap diagnostic so the
    caller knows whether to keep calling /backfill."""
    db = get_supabase()
    resp = (
        db.table("companies")
        .select("id", count="exact", head=True)
        .is_("ai_quality_pass", "null")
        .execute()
    )
    return {"ok": True, "pending": resp.count or 0}


@router.post("/categorize-tech")
def categorize_tech(limit: int = 600):
    """AI-categorise any `tech_category` rows still marked 'pending' and write
    the results back. Meant for an external cron: each run shrinks the backlog, and a tool
    is only ever sent to the AI once. Cheap — bounded by `limit` per run."""
    from app.services.tech_taxonomy import categorize_pending_techs

    db = get_supabase()
    result = categorize_pending_techs(db, limit=limit)
    return {"ok": True, **result}
