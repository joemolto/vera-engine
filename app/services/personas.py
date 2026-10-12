"""Buyer-profile (persona) CRUD + auto-migration from legacy flat icp_config.

Personas live inside `organisations.icp_config.personas` as an array of
objects. Each persona is a self-contained set of filters Apollo can search
against — multiple personas means the engine runs Apollo once per persona
during discovery, and scoring picks the best-matching persona per lead.

Backwards-compatibility note: orgs that existed before this feature
landed have flat `target_titles`, `target_industries` etc. on
icp_config. read_personas() lifts those into a single "Default profile"
persona on first read and persists the migration. Legacy flat fields are
left intact during the migration so any consumer the refactor missed
still reads them.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional, TypedDict

from supabase import Client

logger = logging.getLogger(__name__)


class Persona(TypedDict, total=False):
    """One buyer profile. All filters optional — empty list = no filter."""
    id: str
    name: str
    active: bool
    target_titles: List[str]
    target_seniorities: List[str]
    target_industries: List[str]
    target_locations: List[str]
    target_company_size: List[str]
    target_revenue_range: Optional[str]
    target_funding_stages: List[str]
    target_tech_uses: List[str]
    target_tech_excludes: List[str]


PERSONA_FIELDS = (
    "target_titles",
    "target_seniorities",
    "target_industries",
    "target_locations",
    "target_company_size",
    "target_funding_stages",
    "target_tech_uses",
    "target_tech_excludes",
)


# Persona caps per subscription tier. Bypass for specific customers via the
# organisations.persona_limit_override column (NULL = use this map).
PERSONA_LIMIT_BY_TIER: Dict[str, int] = {
    "scout": 3,
    "growth": 5,
    "scale": 10,
}
DEFAULT_PERSONA_LIMIT = PERSONA_LIMIT_BY_TIER["scout"]


class PersonaLimitExceeded(Exception):
    """Raised when an org tries to add a persona beyond its tier cap.
    Carries the tier + limit so the API layer can surface useful info."""

    def __init__(self, tier: str, limit: int, current: int):
        super().__init__(f"Tier {tier!r} allows {limit} profiles; org already has {current}.")
        self.tier = tier
        self.limit = limit
        self.current = current


def _empty_persona(name: str = "Default profile") -> Persona:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "active": True,
        "target_titles": [],
        "target_seniorities": [],
        "target_industries": [],
        "target_locations": [],
        "target_company_size": [],
        "target_revenue_range": None,
        "target_funding_stages": [],
        "target_tech_uses": [],
        "target_tech_excludes": [],
    }


def _migrate_flat_to_persona(icp_config: Dict[str, Any]) -> Persona:
    """Build personas[0] from the legacy flat target_* fields.

    Called once per org during the first /icp/personas GET. We snapshot
    the existing values into a 'Default profile' persona but leave the
    flat fields in place — any engine code path the refactor missed will
    still find them.
    """
    persona = _empty_persona()
    for field in PERSONA_FIELDS:
        value = icp_config.get(field)
        if isinstance(value, list):
            persona[field] = value  # type: ignore[literal-required]
    return persona


def read_personas(db: Client, org_id: str) -> List[Persona]:
    """Return the org's persona array, auto-migrating on first read.

    If `icp_config.personas` is missing or empty, build a single persona
    from the flat fields and persist it. Subsequent reads return the
    array as stored.
    """
    org_resp = (
        db.table("organisations")
        .select("icp_config")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    if not org_resp.data:
        raise ValueError(f"Organisation {org_id} not found")

    icp_config = org_resp.data[0].get("icp_config") or {}
    personas = icp_config.get("personas")

    if personas and isinstance(personas, list):
        return personas

    migrated = [_migrate_flat_to_persona(icp_config)]
    _persist_personas(db, org_id, migrated, snapshot_previous=False)
    logger.info("Auto-migrated org %s flat ICP into 1 persona", org_id)
    return migrated


def _persist_personas(
    db: Client,
    org_id: str,
    personas: List[Persona],
    snapshot_previous: bool = True,
) -> None:
    """Write the personas array back into icp_config.personas.

    Also dual-writes the legacy flat target_* fields as the union of all
    active personas' values. This keeps every existing engine consumer
    (vera_brain, multithread, enricher) working unchanged — they continue
    to read `icp_config.target_titles` etc., which now reflects the
    combined ICP. Phase 2 (per-persona fan-out) will replace this with
    true iteration; until then, the union is the correct conservative
    behaviour.

    snapshot_previous=True copies the whole icp_config into
    icp_config_previous so /icp/revert has something to roll back to.
    Disabled during the initial auto-migration since the legacy state is
    still the same data, just reshaped.
    """
    org_resp = (
        db.table("organisations")
        .select("icp_config")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    if not org_resp.data:
        raise ValueError(f"Organisation {org_id} not found")

    current = org_resp.data[0].get("icp_config") or {}
    new_config = {**current, "personas": personas, **_union_flat_fields(personas)}

    update: Dict[str, Any] = {"icp_config": new_config}
    if snapshot_previous:
        update["icp_config_previous"] = current

    db.table("organisations").update(update).eq("id", org_id).execute()


def _union_flat_fields(personas: List[Persona]) -> Dict[str, Any]:
    """Compute the union of all ACTIVE personas' fields, for dual-write
    into the legacy flat icp_config keys.

    Inactive personas are excluded — they're explicitly muted by the user,
    so their filters should not leak into the engine's search calls.
    """
    flat: Dict[str, list] = {field: [] for field in PERSONA_FIELDS}
    for p in personas:
        if not p.get("active", True):
            continue
        for field in PERSONA_FIELDS:
            for v in (p.get(field) or []):
                if v not in flat[field]:
                    flat[field].append(v)
    return flat


def _persona_limit_for_org(db: Client, org_id: str) -> tuple[str, int]:
    """Return (tier, max_personas) for the org. Checks per-org override
    first, falls back to the tier map. Treats missing tier as 'scout'."""
    org_resp = (
        db.table("organisations")
        .select("tier, persona_limit_override")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    if not org_resp.data:
        raise ValueError(f"Organisation {org_id} not found")

    row = org_resp.data[0]
    override = row.get("persona_limit_override")
    if override:
        return (row.get("tier") or "scout", int(override))

    tier = row.get("tier") or "scout"
    return (tier, PERSONA_LIMIT_BY_TIER.get(tier, DEFAULT_PERSONA_LIMIT))


def add_persona(db: Client, org_id: str, name: str) -> Persona:
    """Append a new empty persona to the org. Returns the created persona.

    Raises PersonaLimitExceeded when the org is already at its tier cap.
    The API layer maps this to a 402-ish response shape with upgrade info.
    """
    personas = read_personas(db, org_id)
    tier, limit = _persona_limit_for_org(db, org_id)
    if len(personas) >= limit:
        raise PersonaLimitExceeded(tier=tier, limit=limit, current=len(personas))

    new_persona = _empty_persona(name=name or "New profile")
    personas.append(new_persona)
    _persist_personas(db, org_id, personas)
    logger.info("Org %s added persona %s (%d/%d on %s)",
                org_id, new_persona["id"], len(personas), limit, tier)
    return new_persona


def update_persona(
    db: Client,
    org_id: str,
    persona_id: str,
    fields: Dict[str, Any],
) -> Persona:
    """Apply partial updates to one persona. Returns the updated persona.

    Only known fields are written — unknown keys silently ignored so
    bad client payloads can't corrupt the row.
    """
    allowed = {"name", "active", "target_revenue_range", *PERSONA_FIELDS}
    clean = {k: v for k, v in fields.items() if k in allowed}

    personas = read_personas(db, org_id)
    for i, p in enumerate(personas):
        if p.get("id") == persona_id:
            personas[i] = {**p, **clean}
            _persist_personas(db, org_id, personas)
            logger.info("Org %s updated persona %s (%d fields)",
                        org_id, persona_id, len(clean))
            return personas[i]
    raise ValueError(f"Persona {persona_id} not found in org {org_id}")


def delete_persona(db: Client, org_id: str, persona_id: str) -> List[Persona]:
    """Remove a persona. Returns the remaining list.

    Refuses to remove the last persona — every org keeps at least one.
    """
    personas = read_personas(db, org_id)
    if len(personas) <= 1:
        raise ValueError("Cannot delete the last persona — every org needs one")

    remaining = [p for p in personas if p.get("id") != persona_id]
    if len(remaining) == len(personas):
        raise ValueError(f"Persona {persona_id} not found in org {org_id}")

    _persist_personas(db, org_id, remaining)
    logger.info("Org %s deleted persona %s", org_id, persona_id)
    return remaining


def active_personas(db: Client, org_id: str) -> List[Persona]:
    """Personas marked active=True. Engine consumers use this — not all
    personas — so a user can mute a persona without losing its config."""
    return [p for p in read_personas(db, org_id) if p.get("active", True)]
