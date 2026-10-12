"""Internal management endpoints for per-tenant API keys.

These are called by the dashboard (not the public API), so they're
gated by the existing internal X-API-Key shared secret applied at
router registration in app/main.py — no Bearer auth here.

Endpoints:
  GET    /api-keys?org_id=...           list active keys
  POST   /api-keys                      create a new key (returns plaintext once)
  DELETE /api-keys/{key_id}?org_id=...  revoke a key
  POST   /api-keys/{key_id}/rotate      rotate (revoke + create new with same name)
"""

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.database import get_supabase
from app.services import api_keys as api_keys_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api-keys", tags=["api-keys-management"])


class CreateKeyRequest(BaseModel):
    org_id: UUID
    name: str = Field(..., min_length=1, max_length=120)
    created_by: Optional[UUID] = None


class RotateKeyRequest(BaseModel):
    org_id: UUID
    created_by: Optional[UUID] = None


@router.get("")
def list_keys(org_id: UUID):
    """List active API keys for an org. Plaintext keys are NEVER returned —
    only prefix + metadata."""
    db = get_supabase()
    if not api_keys_service.org_has_api_access(db, str(org_id)):
        # Returning [] keeps the dashboard UI tidy (no error toast) while
        # the upgrade prompt is shown by the FE.
        return {"data": [], "tier_allowed": False}
    keys = api_keys_service.list_api_keys(db, str(org_id))
    return {"data": keys, "tier_allowed": True}


@router.post("")
def create_key(request: CreateKeyRequest):
    """Create a new key. Response includes the plaintext `full_key` —
    this is the only time it's ever returned. The dashboard must show
    a copy-once modal."""
    db = get_supabase()
    org_id = str(request.org_id)
    if not api_keys_service.org_has_api_access(db, org_id):
        raise HTTPException(
            status_code=403,
            detail="API access is included on Vera Solo, Pro and Team plans.",
        )
    record = api_keys_service.create_api_key(
        db,
        org_id=org_id,
        name=request.name,
        created_by=str(request.created_by) if request.created_by else None,
    )
    return record


@router.delete("/{key_id}")
def revoke_key(key_id: UUID, org_id: UUID):
    """Revoke a key. Idempotent."""
    db = get_supabase()
    ok = api_keys_service.revoke_api_key(db, str(org_id), str(key_id))
    if not ok:
        # 404 vs already-revoked is intentionally indistinguishable —
        # avoids leaking key existence to a wrong org_id.
        raise HTTPException(status_code=404, detail="Key not found or already revoked")
    return {"status": "revoked", "key_id": str(key_id)}


@router.post("/{key_id}/rotate")
def rotate_key(key_id: UUID, request: RotateKeyRequest):
    """Revoke a key and create a new one with the same name. Returns
    the new key's plaintext `full_key` — show once."""
    db = get_supabase()
    org_id = str(request.org_id)
    if not api_keys_service.org_has_api_access(db, org_id):
        raise HTTPException(
            status_code=403,
            detail="API access is included on Vera Solo, Pro and Team plans.",
        )
    new_key = api_keys_service.rotate_api_key(
        db,
        org_id=org_id,
        key_id=str(key_id),
        created_by=str(request.created_by) if request.created_by else None,
    )
    if new_key is None:
        raise HTTPException(status_code=404, detail="Key not found")
    return new_key
