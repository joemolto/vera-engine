"""
Competitor watch — scan competitors' LinkedIn posts + who engaged.

POST /scan {org_id} runs one org synchronously; {all: true} fans out over
every org with resolvable competitors as a background task (each org is a
handful of pay-per-event actor calls, minutes not seconds). Mounted under
/api/v1/competitors with the shared-key dependency; triggered by a weekly
cron or manually. The dashboard reads the competitor_feed table directly
(RLS org-scoped).
"""

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from app.database import get_supabase
from app.services.competitor_watch import resolve_competitors, scan_org

logger = logging.getLogger(__name__)

router = APIRouter()


class ScanRequest(BaseModel):
    org_id: Optional[UUID] = None
    all: bool = False


def _scan_all() -> None:
    from app.services.plan_limits import REVOKED_STATUSES

    db = get_supabase()
    orgs = (
        db.table("organisations")
        .select("id, name, icp_config, subscription_status")
        .execute()
    ).data or []
    for org in orgs:
        # Lapsed orgs get no Apify spend (2026-08-23 lapsed-org audit).
        if (org.get("subscription_status") or "").lower() in REVOKED_STATUSES:
            continue
        if not resolve_competitors(org):
            continue
        try:
            result = scan_org(db, org)
            logger.info("competitor-watch: %s -> %s", org.get("name"), result)
        except Exception:
            logger.exception("competitor-watch: scan failed for %s", org.get("name"))


@router.post("/scan")
def scan(request: ScanRequest, background: BackgroundTasks):
    if request.all:
        background.add_task(_scan_all)
        return {"started": True, "mode": "all"}
    if not request.org_id:
        raise HTTPException(status_code=422, detail="Provide org_id or all=true.")
    db = get_supabase()
    org = (
        db.table("organisations")
        .select("id, name, icp_config")
        .eq("id", str(request.org_id))
        .single()
        .execute()
    ).data
    if not org:
        raise HTTPException(status_code=404, detail="Org not found.")
    return scan_org(db, org)
