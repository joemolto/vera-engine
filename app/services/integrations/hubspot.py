"""HubSpot CRM push — org-connected OAuth app.

Connections live in ``org_integrations`` (provider='hubspot'); the
``secret`` column holds JSON ``{access_token, refresh_token, expires_at}``
written by the dashboard's OAuth callback and refreshed here. Access
tokens expire after ~30 minutes — the refresh token is the durable
credential. Tokens are NEVER logged and never leave the server.

Push contract (matches what the dashboard promises the user):
  contact  → upsert Company by domain, upsert Contact by email,
             associate, attach a Note with Vera's signal context.
  company  → upsert Company by domain, attach the Note.

Every function takes org_id and only ever reads that org's connection —
this module runs on the service role, so org scoping is explicit, not
RLS-derived.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import httpx
from supabase import Client

from app.config import settings

logger = logging.getLogger(__name__)

HUBSPOT_BASE = "https://api.hubapi.com"
TIMEOUT = 20


class HubspotNotConnected(Exception):
    """Org has no live HubSpot connection."""


class HubspotAuthError(Exception):
    """Token refresh failed — the org must reconnect."""


class HubspotApiError(Exception):
    """HubSpot rejected a CRM call."""


# ---------------------------------------------------------------------------
# Connection + token refresh
# ---------------------------------------------------------------------------

def _load_connection(db: Client, org_id: str) -> dict:
    rows = (
        db.table("org_integrations")
        .select("id, status, secret, meta")
        .eq("org_id", org_id)
        .eq("provider", "hubspot")
        .limit(1)
        .execute()
        .data
    )
    if not rows or not rows[0].get("secret"):
        raise HubspotNotConnected()
    row = rows[0]
    if row.get("status") != "connected":
        raise HubspotNotConnected()
    return row


def _mark_reconnect_required(db: Client, row_id: str) -> None:
    db.table("org_integrations").update(
        {"status": "reconnect_required", "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", row_id).execute()


def get_access_token(db: Client, org_id: str) -> Tuple[str, dict]:
    """Return a live access token + the connection meta (hub_id etc.),
    refreshing and persisting if the stored one is stale."""
    row = _load_connection(db, org_id)
    try:
        tokens = json.loads(row["secret"])
    except (TypeError, ValueError):
        raise HubspotNotConnected()

    expires_at = tokens.get("expires_at")
    if expires_at:
        try:
            still_valid = datetime.fromisoformat(
                expires_at.replace("Z", "+00:00")
            ) > datetime.now(timezone.utc) + timedelta(seconds=60)
        except ValueError:
            still_valid = False
        if still_valid and tokens.get("access_token"):
            return tokens["access_token"], (row.get("meta") or {})

    # Refresh
    if not settings.HUBSPOT_CLIENT_ID or not settings.HUBSPOT_CLIENT_SECRET:
        raise HubspotAuthError("engine missing HubSpot client credentials")
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        _mark_reconnect_required(db, row["id"])
        raise HubspotAuthError("no refresh token stored")

    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(
            f"{HUBSPOT_BASE}/oauth/v1/token",
            data={
                "grant_type": "refresh_token",
                "client_id": settings.HUBSPOT_CLIENT_ID,
                "client_secret": settings.HUBSPOT_CLIENT_SECRET,
                "refresh_token": refresh_token,
            },
        )
    if resp.status_code >= 400:
        # Revoked / uninstalled — surface as reconnect, never retry-loop.
        logger.warning("HubSpot token refresh failed for org %s (%s)", org_id, resp.status_code)
        _mark_reconnect_required(db, row["id"])
        raise HubspotAuthError("refresh rejected")

    fresh = resp.json()
    new_tokens = {
        "access_token": fresh["access_token"],
        # HubSpot may rotate the refresh token; keep the newest.
        "refresh_token": fresh.get("refresh_token", refresh_token),
        "expires_at": (
            datetime.now(timezone.utc)
            + timedelta(seconds=max(60, int(fresh.get("expires_in", 1800)) - 120))
        ).isoformat(),
    }
    db.table("org_integrations").update(
        {
            "secret": json.dumps(new_tokens),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", row["id"]).execute()
    return new_tokens["access_token"], (row.get("meta") or {})


# ---------------------------------------------------------------------------
# CRM primitives
# ---------------------------------------------------------------------------

def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _search_one(
    client: httpx.Client, token: str, object_type: str, prop: str, value: str
) -> Optional[str]:
    resp = client.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/{object_type}/search",
        headers=_headers(token),
        json={
            "filterGroups": [
                {"filters": [{"propertyName": prop, "operator": "EQ", "value": value}]}
            ],
            "limit": 1,
        },
    )
    if resp.status_code >= 400:
        raise HubspotApiError(f"{object_type} search failed ({resp.status_code})")
    results = resp.json().get("results", [])
    return results[0]["id"] if results else None


def _create(client: httpx.Client, token: str, object_type: str, properties: dict) -> str:
    resp = client.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/{object_type}",
        headers=_headers(token),
        json={"properties": properties},
    )
    if resp.status_code >= 400:
        raise HubspotApiError(f"{object_type} create failed ({resp.status_code}): {resp.text[:200]}")
    return resp.json()["id"]


def upsert_company(client: httpx.Client, token: str, domain: str, name: Optional[str]) -> str:
    existing = _search_one(client, token, "companies", "domain", domain)
    if existing:
        return existing
    props = {"domain": domain}
    if name:
        props["name"] = name
    return _create(client, token, "companies", props)


def upsert_contact(client: httpx.Client, token: str, email: str, props: dict) -> str:
    existing = _search_one(client, token, "contacts", "email", email)
    if existing:
        return existing
    return _create(client, token, "contacts", {"email": email, **props})


def associate_contact_company(
    client: httpx.Client, token: str, contact_id: str, company_id: str
) -> None:
    resp = client.put(
        f"{HUBSPOT_BASE}/crm/v4/objects/contacts/{contact_id}/associations/default/companies/{company_id}",
        headers=_headers(token),
    )
    if resp.status_code >= 400:
        # Non-fatal: both records exist; the association is polish.
        logger.warning("HubSpot association failed (%s)", resp.status_code)


# Published HubSpot-defined association type ids.
NOTE_TO_CONTACT = 202
NOTE_TO_COMPANY = 190


def create_note(
    client: httpx.Client,
    token: str,
    body_html: str,
    contact_id: Optional[str] = None,
    company_id: Optional[str] = None,
) -> None:
    associations = []
    if contact_id:
        associations.append(
            {
                "to": {"id": contact_id},
                "types": [
                    {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": NOTE_TO_CONTACT}
                ],
            }
        )
    if company_id:
        associations.append(
            {
                "to": {"id": company_id},
                "types": [
                    {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": NOTE_TO_COMPANY}
                ],
            }
        )
    resp = client.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/notes",
        headers=_headers(token),
        json={
            "properties": {
                "hs_note_body": body_html[:60_000],
                "hs_timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "associations": associations,
        },
    )
    if resp.status_code >= 400:
        # Non-fatal: the record push succeeded; the note is context.
        logger.warning("HubSpot note create failed (%s): %s", resp.status_code, resp.text[:200])


def _record_url(meta: dict, object_path: str, record_id: str) -> Optional[str]:
    hub_id = (meta or {}).get("hub_id")
    if not hub_id:
        return None
    return f"https://app.hubspot.com/contacts/{hub_id}/record/{object_path}/{record_id}"


def _note_body(headline: str, lines: List[str], vera_url: str) -> str:
    items = "".join(f"<li>{line}</li>" for line in lines) if lines else ""
    parts = [f"<strong>{headline}</strong>"]
    if items:
        parts.append(f"<ul>{items}</ul>")
    parts.append(f'<a href="{vera_url}">Open in Vera</a>')
    return "".join(parts)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def push_contact(
    db: Client,
    org_id: str,
    *,
    email: str,
    first_name: Optional[str],
    last_name: Optional[str],
    title: Optional[str],
    company_name: Optional[str],
    company_domain: Optional[str],
    signal_lines: List[str],
    vera_url: str,
) -> dict:
    token, meta = get_access_token(db, org_id)
    with httpx.Client(timeout=TIMEOUT) as client:
        company_id = None
        if company_domain:
            company_id = upsert_company(client, token, company_domain, company_name)
        props = {}
        if first_name:
            props["firstname"] = first_name
        if last_name:
            props["lastname"] = last_name
        if title:
            props["jobtitle"] = title
        contact_id = upsert_contact(client, token, email, props)
        if company_id:
            associate_contact_company(client, token, contact_id, company_id)
        create_note(
            client,
            token,
            _note_body("Pushed from Vera", signal_lines, vera_url),
            contact_id=contact_id,
            company_id=company_id,
        )
    return {
        "contact_id": contact_id,
        "company_id": company_id,
        "record_url": _record_url(meta, "0-1", contact_id),
    }


def push_company(
    db: Client,
    org_id: str,
    *,
    domain: str,
    name: Optional[str],
    signal_lines: List[str],
    vera_url: str,
) -> dict:
    token, meta = get_access_token(db, org_id)
    with httpx.Client(timeout=TIMEOUT) as client:
        company_id = upsert_company(client, token, domain, name)
        create_note(
            client,
            token,
            _note_body("Pushed from Vera", signal_lines, vera_url),
            company_id=company_id,
        )
    return {
        "company_id": company_id,
        "record_url": _record_url(meta, "0-2", company_id),
    }
