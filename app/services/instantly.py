"""Instantly API integration — push leads and sync stats."""

import json
import logging
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.services.activity import log_activity
from typing import List, Optional

logger = logging.getLogger(__name__)

INSTANTLY_BASE_URL = "https://api.instantly.ai/api/v2"


def _get_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.INSTANTLY_API_KEY}",
        "Content-Type": "application/json",
    }


def push_leads(
    db,
    org_id: str,
    campaign_id: str,
    lead_ids: Optional[List[str]] = None,
    min_score: float = 60.0,
) -> dict:
    """Push qualified leads to an Instantly campaign.

    Fetches leads above min_score that haven't been sequenced yet.
    """
    # Fetch leads to push
    query = (
        db.table("leads")
        .select("*")
        .eq("org_id", org_id)
        .eq("status", "qualified")
        .gte("total_score", min_score)
    )
    if lead_ids:
        query = query.in_("id", lead_ids)

    leads_result = query.execute()
    leads = leads_result.data

    if not leads:
        return {"pushed_count": 0, "skipped_count": 0, "pushed_lead_ids": [], "errors": []}

    pushed_ids: List[str] = []
    errors: List[str] = []
    now = datetime.now(timezone.utc).isoformat()

    for lead in leads:
        try:
            # Build signal summary for custom variables
            signal_types = [s.get("type", "") for s in (lead.get("signals") or [])]
            signal_summary = ", ".join(signal_types) if signal_types else "None"

            # Format for Instantly API
            instantly_payload = {
                "campaign_id": campaign_id,
                "email": lead["email"],
                "first_name": lead.get("first_name", ""),
                "last_name": lead.get("last_name", ""),
                "company_name": lead.get("company_name", ""),
                "custom_variables": {
                    "title": lead.get("title", ""),
                    "company_name": lead.get("company_name", ""),
                    "signal_summary": signal_summary,
                    "score": str(lead.get("total_score", 0)),
                },
            }

            with httpx.Client(timeout=10) as client:
                response = client.post(
                    f"{INSTANTLY_BASE_URL}/leads",
                    headers=_get_headers(),
                    json=instantly_payload,
                )
                response.raise_for_status()

            # Update lead in database
            db.table("leads").update({
                "status": "sequenced",
                "instantly_campaign_id": campaign_id,
                "updated_at": now,
            }).eq("id", lead["id"]).execute()

            pushed_ids.append(lead["id"])

            log_activity(db, org_id, lead["id"], "lead_sequenced", {
                "campaign_id": campaign_id,
                "score": lead.get("total_score"),
            })

        except Exception as e:
            error_msg = f"Error pushing lead {lead['email']}: {e}"
            logger.error(error_msg)
            errors.append(error_msg)

    # Update campaign leads_added count
    try:
        campaign = (
            db.table("campaigns")
            .select("id, leads_added")
            .eq("instantly_campaign_id", campaign_id)
            .eq("org_id", org_id)
            .execute()
        )
        if campaign.data:
            db.table("campaigns").update({
                "leads_added": campaign.data[0]["leads_added"] + len(pushed_ids),
                "updated_at": "now()",
            }).eq("id", campaign.data[0]["id"]).execute()
    except Exception as e:
        logger.error("Failed to update campaign leads_added: %s", e)

    return {
        "pushed_count": len(pushed_ids),
        "skipped_count": len(leads) - len(pushed_ids),
        "pushed_lead_ids": pushed_ids,
        "errors": errors,
    }


def sync_campaign_stats(db, org_id: str) -> dict:
    """Pull campaign analytics from Instantly and update local records."""
    campaigns_result = (
        db.table("campaigns")
        .select("id, instantly_campaign_id, name")
        .eq("org_id", org_id)
        .not_.is_("instantly_campaign_id", "null")
        .execute()
    )

    if not campaigns_result.data:
        return {"campaigns_synced": 0, "errors": []}

    synced = 0
    errors: List[str] = []

    for campaign in campaigns_result.data:
        try:
            with httpx.Client(timeout=10) as client:
                response = client.get(
                    f"{INSTANTLY_BASE_URL}/campaigns/{campaign['instantly_campaign_id']}/analytics",
                    headers=_get_headers(),
                )
                response.raise_for_status()
                stats = response.json()

            db.table("campaigns").update({
                "emails_sent": stats.get("emails_sent", 0),
                "replies_received": stats.get("replies", 0),
                "updated_at": "now()",
            }).eq("id", campaign["id"]).execute()

            synced += 1

        except Exception as e:
            error_msg = f"Error syncing campaign {campaign['name']}: {e}"
            logger.error(error_msg)
            errors.append(error_msg)

    log_activity(db, org_id, None, "stats_synced", {
        "campaigns_synced": synced,
    })

    return {"campaigns_synced": synced, "errors": errors}
