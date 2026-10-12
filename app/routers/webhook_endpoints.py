"""Webhook endpoint management + delivery worker trigger.

Two flavours of endpoints in this module:
1. Management endpoints (gated by internal X-API-Key, used by the
   dashboard) for CRUD + replay.
2. The deliver-pending trigger (also internal), which flushes queued
   deliveries on demand. The built-in scheduler already runs the same
   delivery every 30 seconds.
"""

import logging
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.database import get_supabase
from app.services import webhooks as webhooks_service
from app.services.api_keys import org_has_api_access
from app.services.webhooks import EVENT_TYPES

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook-endpoints", tags=["webhook-endpoints"])


class CreateEndpointRequest(BaseModel):
    org_id: UUID
    url: str = Field(..., min_length=10)
    name: str = Field(default="", max_length=120)
    events: Optional[List[str]] = None


@router.get("")
def list_endpoints(org_id: UUID):
    """List webhook endpoints for an org. Secrets included — frontend is
    responsible for masking before display (eye-toggle pattern).

    Webhooks share API gating: Growth + Scale only. Scout orgs get
    tier_allowed=false and an empty list, so the dashboard can show
    an upgrade prompt instead of the create-endpoint UI."""
    db = get_supabase()
    if not org_has_api_access(db, str(org_id)):
        return {"data": [], "tier_allowed": False}
    return {
        "data": webhooks_service.list_endpoints(db, str(org_id)),
        "tier_allowed": True,
    }


@router.post("")
def create_endpoint(payload: CreateEndpointRequest):
    """Create a webhook endpoint. Returns the row including the freshly-
    generated secret (only time it's returned in full — frontend shows
    in a copy-once modal then masks)."""
    db = get_supabase()
    if not org_has_api_access(db, str(payload.org_id)):
        raise HTTPException(
            status_code=403,
            detail="Webhook integrations are included on every plan.",
        )
    if payload.events:
        invalid = [e for e in payload.events if e not in EVENT_TYPES]
        if invalid:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "unknown_event_types",
                    "invalid": invalid,
                    "available": list(EVENT_TYPES),
                },
            )
    try:
        record = webhooks_service.create_endpoint(
            db,
            org_id=str(payload.org_id),
            url=payload.url,
            name=payload.name,
            events=payload.events,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return record


@router.delete("/{endpoint_id}")
def delete_endpoint(endpoint_id: UUID, org_id: UUID):
    """Delete a webhook endpoint. Cascades to all deliveries."""
    db = get_supabase()
    if not webhooks_service.delete_endpoint(db, str(org_id), str(endpoint_id)):
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return {"status": "deleted", "endpoint_id": str(endpoint_id)}


@router.get("/{endpoint_id}/deliveries")
def list_deliveries(
    endpoint_id: UUID,
    org_id: UUID,
    limit: int = 25,
):
    """Recent deliveries for a webhook endpoint — used by the dashboard
    debug view."""
    db = get_supabase()
    resp = (
        db.table("webhook_deliveries")
        .select("id, event_type, status, attempt_count, response_status, "
                "duration_ms, created_at, last_attempt_at, delivered_at, "
                "next_attempt_at, response_body")
        .eq("endpoint_id", str(endpoint_id))
        .eq("org_id", str(org_id))
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return {"data": resp.data or []}


@router.post("/deliveries/{delivery_id}/replay")
def replay_delivery(delivery_id: UUID, org_id: UUID):
    """Re-queue a failed/dead delivery for another attempt."""
    db = get_supabase()
    if not webhooks_service.replay_delivery(db, str(org_id), str(delivery_id)):
        raise HTTPException(status_code=404, detail="Delivery not found")
    return {"status": "queued", "delivery_id": str(delivery_id)}


@router.post("/{endpoint_id}/test")
def send_test(endpoint_id: UUID, org_id: UUID):
    """Send a synthetic webhook.test event to this endpoint and return
    the immediate result. Powers the 'Send test' button on the dashboard
    settings page. Org-scoped: only the owner can test their endpoint."""
    db = get_supabase()
    result = webhooks_service.send_test_delivery(
        db, str(org_id), str(endpoint_id)
    )
    if not result.get("success") and result.get("error") == "endpoint not found":
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return result


@router.post("/deliver-pending")
def deliver_pending():
    """Worker trigger: deliver any pending webhook deliveries that are due.
    The built-in scheduler runs the same delivery every 30 seconds."""
    db = get_supabase()
    return webhooks_service.deliver_pending(db)
