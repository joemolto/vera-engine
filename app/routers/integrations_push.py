"""Push a contact or account into an org's connected apps (HubSpot / Slack).

Called from the dashboard through the clara-api edge function, which has
already verified the caller's session belongs to org_id. This router runs
on the service role, so the ONE rule that matters: every entity read is
explicitly scoped to org_id before anything leaves Vera. A lead that
isn't the org's, or a domain the org doesn't watch, is a 404 — never a
push.

Rate limited per org (30/min) so a client-side bug can never spam a
customer's CRM or Slack channel in a loop.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.database import get_supabase
from app.services.rate_limit import check_and_record
from app.services.integrations import hubspot, slack_push

logger = logging.getLogger(__name__)

router = APIRouter()

DASHBOARD_BASE = settings.DASHBOARD_URL

# Backend vendor names must never reach a customer surface (product rule).
# Signal headlines are usually clean, but scrub defensively before they
# land in a CRM note or Slack channel.
_VENDOR_RE = re.compile(
    r"\b(PredictLeads|Apollo\.io|Apollo|Apify|Jina|Tavily|Findymail|LeakRadar)\b",
    re.IGNORECASE,
)


def _scrub(text: str) -> str:
    return _VENDOR_RE.sub("Vera", text)


def _headline(row: dict) -> str:
    data = row.get("signal_data") or {}
    if isinstance(data, dict) and data.get("headline"):
        return _scrub(str(data["headline"]))
    return str(row.get("signal_type", "signal")).replace("_", " ")


def _signal_lines(db, *, org_id: str, lead_id: Optional[str], company_id: Optional[str]) -> List[str]:
    """Freshest signals for the entity — union of the lead and company
    axes (every signals read must cover both), org-scoped, top 5."""
    rows: List[dict] = []
    if lead_id:
        rows += (
            db.table("signals")
            .select("signal_type, signal_data, detected_at")
            .eq("org_id", org_id)
            .eq("lead_id", lead_id)
            .order("detected_at", desc=True)
            .limit(5)
            .execute()
            .data
            or []
        )
    if company_id:
        rows += (
            db.table("signals")
            .select("signal_type, signal_data, detected_at")
            .eq("org_id", org_id)
            .eq("company_id", company_id)
            .order("detected_at", desc=True)
            .limit(5)
            .execute()
            .data
            or []
        )
    rows.sort(key=lambda r: r.get("detected_at") or "", reverse=True)
    lines: List[str] = []
    for r in rows:
        line = _headline(r)
        if line not in lines:
            lines.append(line)
        if len(lines) >= 5:
            break
    return lines


def _narrative(company: dict) -> Optional[str]:
    """The cached facts-only "why this account matters" paragraph,
    trimmed to a Slack-friendly length at a sentence boundary."""
    wtm = company.get("why_this_matters") or {}
    text = wtm.get("paragraph") if isinstance(wtm, dict) else None
    if not text:
        return None
    text = _scrub(str(text)).strip()
    if len(text) <= 600:
        return text
    cut = text[:600]
    dot = cut.rfind(". ")
    return (cut[: dot + 1] if dot > 200 else cut).strip()


_SECURITY_TITLE_RE = re.compile(
    r"ciso|cso\b|security|infosec|information security|cyber", re.IGNORECASE
)


def _contact_line(company: dict) -> Optional[str]:
    """Name the best contact Vera has found: security-titled first,
    highest icp_score wins. Facts only — name and title, no invented
    reasoning."""
    contacts = company.get("key_contacts") or []
    if not isinstance(contacts, list) or not contacts:
        return None
    def score(c: dict) -> tuple:
        title = str(c.get("title") or "")
        return (
            1 if _SECURITY_TITLE_RE.search(title) else 0,
            float(c.get("icp_score") or 0),
        )
    best = max(contacts, key=score)
    name = str(best.get("name") or "").strip()
    title = _scrub(str(best.get("title") or "").strip())
    if not name:
        return None
    line = f"*Who I'd start with:* {name}"
    if title:
        line += f" — {title}"
    return line


def _intro(entity_label: str, lines: List[str]) -> str:
    """First-person opener led by the freshest signal."""
    if lines:
        first = lines[0].rstrip(".")
        return f"Hey — {first}."
    return f"Hey — here's the latest picture on {entity_label}."


class PushRequest(BaseModel):
    org_id: str
    provider: str  # 'hubspot' | 'slack'
    entity_type: str  # 'contact' | 'company'
    entity_id: str  # lead uuid for contacts, domain for companies


@router.post("/push")
def push(payload: PushRequest):
    if payload.provider not in ("hubspot", "slack"):
        raise HTTPException(status_code=400, detail="unknown_provider")
    if payload.entity_type not in ("contact", "company"):
        raise HTTPException(status_code=400, detail="unknown_entity_type")

    db = get_supabase()

    # Lapsed orgs can't push to their tools — same 429 cap shape as the
    # plan doors so the dashboard's reactivate modal opens (2026-08-23).
    from app.services.plan_limits import org_lapsed
    if org_lapsed(payload.org_id, db):
        raise HTTPException(status_code=429, detail={
            "error": "solo_limit_reached", "cap": "accounts",
            "message": "Reactivate your subscription to push to your tools.",
        })

    check_and_record(
        db, payload.org_id, bucket_key="integrations_push", max_per_minute=30
    )

    if payload.entity_type == "contact":
        return _push_contact(db, payload)
    return _push_company(db, payload)


def _push_contact(db, payload: PushRequest) -> dict:
    # Ownership gate: the lead must belong to this org. 404 otherwise.
    leads = (
        db.table("leads")
        .select("id, org_id, email, first_name, last_name, title, company_name, company_domain, company_id")
        .eq("id", payload.entity_id)
        .eq("org_id", payload.org_id)
        .limit(1)
        .execute()
        .data
    )
    if not leads:
        raise HTTPException(status_code=404, detail="not_found")
    lead = leads[0]

    company_id = lead.get("company_id")
    lines = _signal_lines(
        db, org_id=payload.org_id, lead_id=lead["id"], company_id=company_id
    )
    company_row: dict = {}
    if company_id:
        rows = (
            db.table("companies")
            .select("why_this_matters")
            .eq("id", company_id)
            .limit(1)
            .execute()
            .data
        )
        company_row = rows[0] if rows else {}
    name = " ".join(x for x in [lead.get("first_name"), lead.get("last_name")] if x) or lead.get("email") or "Contact"
    subtitle_bits = [b for b in [lead.get("title"), lead.get("company_name")] if b]
    vera_url = f"{DASHBOARD_BASE}/dashboard/prospects/{lead['id']}"
    contact_intro = f"Hey — worth a look at {name}"
    if lead.get("title"):
        contact_intro += f", {lead['title']}"
    if lead.get("company_name"):
        contact_intro += f" at {lead['company_name']}"
    contact_intro = _scrub(contact_intro + ".")

    if payload.provider == "hubspot":
        if not lead.get("email"):
            # HubSpot dedupes by email; without one we'd create junk records.
            raise HTTPException(status_code=400, detail="email_required")
        try:
            result = hubspot.push_contact(
                db,
                payload.org_id,
                email=lead["email"],
                first_name=lead.get("first_name"),
                last_name=lead.get("last_name"),
                title=lead.get("title"),
                company_name=lead.get("company_name"),
                company_domain=lead.get("company_domain"),
                signal_lines=lines,
                vera_url=vera_url,
            )
        except hubspot.HubspotNotConnected:
            raise HTTPException(status_code=400, detail="not_connected")
        except hubspot.HubspotAuthError:
            raise HTTPException(status_code=409, detail="reconnect_required")
        except hubspot.HubspotApiError as e:
            logger.warning("HubSpot push failed for org %s: %s", payload.org_id, e)
            raise HTTPException(status_code=502, detail="provider_error")
        return {"ok": True, "record_url": result.get("record_url")}

    # Slack
    try:
        slack_push.push_card(
            db,
            payload.org_id,
            heading=name,
            subtitle=" · ".join(subtitle_bits) or None,
            signal_lines=lines,
            vera_url=vera_url,
            intro=contact_intro,
            narrative=_narrative(company_row),
        )
    except slack_push.SlackNotConnected:
        raise HTTPException(status_code=400, detail="not_connected")
    except slack_push.SlackApiError as e:
        logger.warning("Slack push failed for org %s: %s", payload.org_id, e)
        raise HTTPException(status_code=502, detail="provider_error")
    return {"ok": True}


def _push_company(db, payload: PushRequest) -> dict:
    domain = payload.entity_id.strip().lower()
    # Ownership gate: the org must have this domain on its watchlist
    # (any status — research or monitored both count as "theirs").
    watch = (
        db.table("watchlist")
        .select("id")
        .eq("org_id", payload.org_id)
        .eq("company_domain", domain)
        .limit(1)
        .execute()
        .data
    )
    if not watch:
        raise HTTPException(status_code=404, detail="not_found")

    companies = (
        db.table("companies")
        .select("id, name, domain, why_this_matters, key_contacts")
        .eq("domain", domain)
        .limit(1)
        .execute()
        .data
    )
    company = companies[0] if companies else {}
    company_name = company.get("name") or domain
    lines = _signal_lines(
        db, org_id=payload.org_id, lead_id=None, company_id=company.get("id")
    )
    vera_url = f"{DASHBOARD_BASE}/dashboard/accounts/{domain}"

    if payload.provider == "hubspot":
        try:
            result = hubspot.push_company(
                db,
                payload.org_id,
                domain=domain,
                name=company_name,
                signal_lines=lines,
                vera_url=vera_url,
            )
        except hubspot.HubspotNotConnected:
            raise HTTPException(status_code=400, detail="not_connected")
        except hubspot.HubspotAuthError:
            raise HTTPException(status_code=409, detail="reconnect_required")
        except hubspot.HubspotApiError as e:
            logger.warning("HubSpot push failed for org %s: %s", payload.org_id, e)
            raise HTTPException(status_code=502, detail="provider_error")
        return {"ok": True, "record_url": result.get("record_url")}

    try:
        slack_push.push_card(
            db,
            payload.org_id,
            heading=company_name,
            subtitle=domain,
            signal_lines=lines[1:] if len(lines) > 1 else lines,
            vera_url=vera_url,
            intro=_intro(company_name, lines),
            narrative=_narrative(company),
            contact_line=_contact_line(company),
        )
    except slack_push.SlackNotConnected:
        raise HTTPException(status_code=400, detail="not_connected")
    except slack_push.SlackApiError as e:
        logger.warning("Slack push failed for org %s: %s", payload.org_id, e)
        raise HTTPException(status_code=502, detail="provider_error")
    return {"ok": True}
