"""Cyber threat intelligence monitoring endpoints."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter

from app.database import get_supabase
from app.models.schemas import ThreatAlertUpdateRequest, ThreatMonitorRequest
from app.services.threat_intel.monitor import (
    run_threat_monitoring,
    run_threat_monitoring_for_org,
)

logger = logging.getLogger(__name__)

# Active Threats card hides anything older than this. Threats stay in the DB
# (so we have a historical record + the cross-reference can still match
# against them) but the dashboard only shows fresh ones — after a week, a CVE
# is either patched or actively exploited, so it's no longer "news".
THREAT_ALERT_DISPLAY_DAYS = 7

router = APIRouter(tags=["threat_intel"])


@router.post("/monitor")
def trigger_threat_monitoring(request: Optional[ThreatMonitorRequest] = None):
    """Trigger a threat monitoring run for all organisations.

    Meant for an external daily cron. Fetches threats from NCSC, NVD, CISA KEV, ICO
    and matches them against all org ICPs.
    """
    db = get_supabase()
    since_hours = 48
    if request and request.since_hours:
        since_hours = request.since_hours

    logger.info("Threat monitoring triggered (since_hours=%d)", since_hours)
    result = run_threat_monitoring(db, since_hours=since_hours)
    return result


@router.post("/monitor/{org_id}")
def trigger_threat_monitoring_for_org(
    org_id: str,
    request: Optional[ThreatMonitorRequest] = None,
):
    """Trigger threat monitoring for a single organisation.

    Useful for testing.
    """
    db = get_supabase()
    since_hours = 48
    if request and request.since_hours:
        since_hours = request.since_hours

    logger.info("Threat monitoring triggered for org %s (since_hours=%d)", org_id, since_hours)
    result = run_threat_monitoring_for_org(db, org_id, since_hours=since_hours)
    return result


@router.get("/alerts/{org_id}")
def get_threat_alerts(
    org_id: str,
    status: Optional[str] = None,
    urgency: Optional[str] = None,
    limit: int = 20,
    days: int = THREAT_ALERT_DISPLAY_DAYS,
):
    """Get threat alerts for an organisation.

    Only returns alerts detected in the last `days` days (default 7) so the
    dashboard stays fresh. Old alerts are kept in the DB for historical
    record but hidden from the API. Pass `days=0` to bypass the filter.
    """
    db = get_supabase()

    query = db.table("threat_alerts").select(
        "*, threat_intel(title, source, severity, url, ai_summary, published_at)"
    ).eq("org_id", org_id).order("detected_at", desc=True).limit(limit)

    if status:
        query = query.eq("status", status)
    if urgency:
        query = query.eq("urgency", urgency)
    if days and days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        query = query.gte("detected_at", cutoff)

    result = query.execute()

    return {
        "org_id": org_id,
        "alerts": result.data or [],
        "count": len(result.data or []),
        "filtered_to_last_days": days if days > 0 else None,
    }


@router.patch("/alerts/{alert_id}")
def update_threat_alert(alert_id: str, request: ThreatAlertUpdateRequest):
    """Update a threat alert status."""
    db = get_supabase()

    update_data = {"status": request.status}

    if request.status == "seen":
        update_data["seen_at"] = datetime.now(timezone.utc).isoformat()
    elif request.status == "actioned":
        update_data["actioned_at"] = datetime.now(timezone.utc).isoformat()

    try:
        result = db.table("threat_alerts").update(
            update_data
        ).eq("id", alert_id).eq("org_id", request.org_id).execute()

        if not result.data:
            return {"error": "Alert not found or access denied", "status": 404}

        return {"status": "updated", "alert": result.data[0]}
    except Exception as e:
        logger.error("Failed to update alert %s: %s", alert_id, e)
        return {"error": str(e)}


@router.get("/feed")
def get_threat_feed(
    severity: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 20,
):
    """Get the raw threat intelligence feed (last 7 days).

    Dashboard can show this as a 'Threat Landscape' section.
    """
    db = get_supabase()

    query = db.table("threat_intel").select(
        "*"
    ).order("published_at", desc=True).limit(limit)

    if severity:
        query = query.eq("severity", severity)
    if source:
        query = query.eq("source", source)

    result = query.execute()

    return {
        "threats": result.data or [],
        "count": len(result.data or []),
    }
