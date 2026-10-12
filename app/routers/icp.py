"""ICP (buyer-profile) endpoints powering the Settings → Buyer profiles card.

Six endpoints:
  - GET  /icp/personas?org_id=...        → array (auto-migrates legacy flat icp_config on first call)
  - POST /icp/personas                    → add a new persona
  - PUT  /icp/personas/{persona_id}       → update a persona's fields
  - DELETE /icp/personas/{persona_id}     → remove (refuses if last)
  - GET  /icp/vocabularies                → controlled lists for dropdowns
                                            (seniorities, funding stages, size buckets,
                                            revenue brackets, cyber tech keywords)

All endpoints sit behind the shared X-API-Key dependency (registered in
main.py). The org_id comes in the body or query param like every other
dashboard endpoint.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.database import get_supabase
from app.services.activity import log_activity
from app.services.apollo_vocabularies import all_vocabularies
from app.services.personas import (
    PersonaLimitExceeded,
    _persona_limit_for_org,
    add_persona,
    delete_persona,
    read_personas,
    update_persona,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/icp", tags=["icp"])


# ---------------------------------------------------------------------------
# Vocabularies — static, no auth scope needed beyond the X-API-Key
# ---------------------------------------------------------------------------

@router.get("/vocabularies")
def get_vocabularies() -> Dict[str, Any]:
    """Controlled lists for the BuyerProfilesCard dropdowns.

    Returned shape is consumed directly by the dashboard's useIcpVocabularies()
    hook. Cached client-side for the session.
    """
    return all_vocabularies()


# ---------------------------------------------------------------------------
# Personas — CRUD on organisations.icp_config.personas
# ---------------------------------------------------------------------------

@router.get("/personas")
def list_personas(org_id: UUID = Query(...)) -> Dict[str, Any]:
    """Return the org's persona array plus the per-tier cap.

    Auto-migrates the legacy flat ICP config into a single 'Default
    profile' persona on first call. Subsequent calls return the array
    as stored. The `limit` field tells the dashboard how many profiles
    this tier allows so it can render the usage indicator + gate the
    Add button without an extra round-trip.
    """
    db = get_supabase()
    try:
        personas = read_personas(db, str(org_id))
        tier, limit = _persona_limit_for_org(db, str(org_id))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {
        "personas": personas,
        "tier": tier,
        "limit": limit,
        "used": len(personas),
    }


class AddPersonaRequest(BaseModel):
    org_id: UUID
    name: str = Field(..., min_length=1, max_length=80)


@router.post("/personas")
def create_persona(request: AddPersonaRequest) -> Dict[str, Any]:
    """Append a new empty persona. Returns the created persona only —
    fetch the full list separately if you need it.

    Returns HTTP 402 (payment required) when the org is at its tier cap,
    with a body the dashboard parses to render the upgrade modal:
        {"detail": "...", "code": "persona_limit_exceeded",
         "tier": "scout", "limit": 3, "current": 3}
    """
    db = get_supabase()
    try:
        persona = add_persona(db, str(request.org_id), request.name)
    except PersonaLimitExceeded as e:
        raise HTTPException(
            status_code=402,
            detail={
                "message": str(e),
                "code": "persona_limit_exceeded",
                "tier": e.tier,
                "limit": e.limit,
                "current": e.current,
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    log_activity(db, str(request.org_id), None, "persona_created", {
        "persona_id": persona["id"],
        "name": persona["name"],
    })
    return {"persona": persona}


class UpdatePersonaRequest(BaseModel):
    org_id: UUID
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    active: Optional[bool] = None
    target_titles: Optional[List[str]] = None
    target_seniorities: Optional[List[str]] = None
    target_industries: Optional[List[str]] = None
    target_locations: Optional[List[str]] = None
    target_company_size: Optional[List[str]] = None
    target_revenue_range: Optional[str] = None
    target_funding_stages: Optional[List[str]] = None
    target_tech_uses: Optional[List[str]] = None
    target_tech_excludes: Optional[List[str]] = None


@router.put("/personas/{persona_id}")
def patch_persona(persona_id: UUID, request: UpdatePersonaRequest) -> Dict[str, Any]:
    """Partial update — only fields explicitly set are written.

    The dashboard sends the whole row on every save (debounced), so in
    practice every field is set. The Optional+None pattern just lets a
    curl caller send a subset without losing the other fields.
    """
    fields = request.model_dump(exclude_unset=True, exclude={"org_id"})
    db = get_supabase()
    try:
        persona = update_persona(db, str(request.org_id), str(persona_id), fields)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    log_activity(db, str(request.org_id), None, "persona_updated", {
        "persona_id": str(persona_id),
        "fields_changed": list(fields.keys()),
    })
    return {"persona": persona}


# ---------------------------------------------------------------------------
# Brain mode — per-org control over how autonomous the agent loop is
# ---------------------------------------------------------------------------

BRAIN_MODES = ("auto", "surface_only", "off")


class BrainModeRequest(BaseModel):
    org_id: UUID
    brain_mode: Literal["auto", "surface_only", "off"]


@router.get("/brain-mode")
def get_brain_mode(org_id: UUID = Query(...)) -> Dict[str, str]:
    """Return the org's current brain_mode."""
    db = get_supabase()
    resp = (
        db.table("organisations")
        .select("brain_mode")
        .eq("id", str(org_id))
        .limit(1)
        .execute()
    )
    if not resp.data:
        raise HTTPException(status_code=404, detail="Organisation not found")
    return {"brain_mode": resp.data[0].get("brain_mode") or "auto"}


@router.put("/brain-mode")
def set_brain_mode(request: BrainModeRequest) -> Dict[str, str]:
    """Update the org's brain_mode. Takes effect on the next scheduled
    brain run — no need to restart anything."""
    db = get_supabase()
    resp = (
        db.table("organisations")
        .update({"brain_mode": request.brain_mode})
        .eq("id", str(request.org_id))
        .execute()
    )
    if not resp.data:
        raise HTTPException(status_code=404, detail="Organisation not found")
    log_activity(db, str(request.org_id), None, "brain_mode_changed", {
        "new_mode": request.brain_mode,
    })
    return {"brain_mode": request.brain_mode}


@router.delete("/personas/{persona_id}")
def remove_persona(
    persona_id: UUID,
    org_id: UUID = Query(...),
) -> Dict[str, List[Dict[str, Any]]]:
    """Delete a persona. Refuses to remove the last one — every org keeps
    at least one persona so the engine always has filters to search with."""
    db = get_supabase()
    try:
        remaining = delete_persona(db, str(org_id), str(persona_id))
    except ValueError as e:
        # ValueError covers both "not found" and "can't delete last" — return
        # 400 since both are client-resolvable rather than 404 vs 409.
        raise HTTPException(status_code=400, detail=str(e))

    log_activity(db, str(org_id), None, "persona_deleted", {
        "persona_id": str(persona_id),
        "remaining_count": len(remaining),
    })
    return {"personas": remaining}
