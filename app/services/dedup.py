"""Lead deduplication logic."""

import logging

from supabase import Client

from app.models.schemas import LeadInput
from typing import Any, Dict, List, Set, Tuple

logger = logging.getLogger(__name__)


def deduplicate_leads(
    db: Client,
    org_id: str,
    leads: List[LeadInput],
) -> Tuple[List[LeadInput], int]:
    """Check which leads already exist for this org by email or linkedin_url.

    Returns (new_leads, duplicate_count).
    """
    if not leads:
        return [], 0

    # Collect all emails and linkedin_urls to check
    emails = [lead.email.lower() for lead in leads if lead.email]
    linkedin_urls = [lead.linkedin_url for lead in leads if lead.linkedin_url]

    existing_emails: Set[str] = set()
    existing_linkedin: Set[str] = set()

    try:
        # Check existing emails
        if emails:
            result = (
                db.table("leads")
                .select("email")
                .eq("org_id", org_id)
                .in_("email", emails)
                .execute()
            )
            existing_emails = {row["email"].lower() for row in result.data if row.get("email")}

        # Check existing linkedin_urls
        if linkedin_urls:
            result = (
                db.table("leads")
                .select("linkedin_url")
                .eq("org_id", org_id)
                .in_("linkedin_url", linkedin_urls)
                .execute()
            )
            existing_linkedin = {row["linkedin_url"] for row in result.data if row.get("linkedin_url")}

    except Exception as e:
        logger.error("Dedup lookup error: %s", e)
        # On error, proceed without dedup — DB unique constraint is the safety net
        return leads, 0

    new_leads = []
    for lead in leads:
        # Check email match
        if lead.email and lead.email.lower() in existing_emails:
            continue
        # Check linkedin_url match
        if lead.linkedin_url and lead.linkedin_url in existing_linkedin:
            continue
        new_leads.append(lead)

    duplicate_count = len(leads) - len(new_leads)

    logger.info(
        "Dedup for org %s: %d total, %d new, %d duplicates",
        org_id, len(leads), len(new_leads), duplicate_count,
    )
    return new_leads, duplicate_count


def deduplicate_leads_for_discovery(
    db: Client,
    org_id: str,
    leads: List[LeadInput],
) -> Tuple[List[LeadInput], List[Dict[str, Any]], int]:
    """Partition leads into new and re-discovered for discovery pipeline.

    Unlike deduplicate_leads() which drops duplicates, this returns:
    - new_leads: LeadInput objects not yet in the database
    - existing_leads: DB rows for leads Apollo found again (pool/suggested/filtered/enriched)
    - duplicate_count: how many were matched to existing records
    """
    if not leads:
        return [], [], 0

    emails = [lead.email.lower() for lead in leads if lead.email]
    linkedin_urls = [lead.linkedin_url for lead in leads if lead.linkedin_url]

    existing_by_email: Dict[str, Dict[str, Any]] = {}
    existing_by_linkedin: Dict[str, Dict[str, Any]] = {}

    try:
        if emails:
            result = (
                db.table("leads")
                .select("id, email, linkedin_url, enrichment_status, times_discovered, pre_enrichment_data, company_domain")
                .eq("org_id", org_id)
                .in_("email", emails)
                .execute()
            )
            for row in result.data or []:
                if row.get("email"):
                    existing_by_email[row["email"].lower()] = row

        if linkedin_urls:
            result = (
                db.table("leads")
                .select("id, email, linkedin_url, enrichment_status, times_discovered, pre_enrichment_data, company_domain")
                .eq("org_id", org_id)
                .in_("linkedin_url", linkedin_urls)
                .execute()
            )
            for row in result.data or []:
                if row.get("linkedin_url"):
                    existing_by_linkedin[row["linkedin_url"]] = row

    except Exception as e:
        logger.error("Discovery dedup lookup error: %s", e)
        return leads, [], 0

    new_leads: List[LeadInput] = []
    existing_leads: List[Dict[str, Any]] = []
    seen_existing_ids: Set[str] = set()

    for lead in leads:
        existing_row = None
        if lead.email and lead.email.lower() in existing_by_email:
            existing_row = existing_by_email[lead.email.lower()]
        elif lead.linkedin_url and lead.linkedin_url in existing_by_linkedin:
            existing_row = existing_by_linkedin[lead.linkedin_url]

        if existing_row:
            if existing_row["id"] not in seen_existing_ids:
                seen_existing_ids.add(existing_row["id"])
                existing_leads.append(existing_row)
        else:
            new_leads.append(lead)

    duplicate_count = len(existing_leads)
    logger.info(
        "Discovery dedup for org %s: %d total, %d new, %d re-discovered",
        org_id, len(leads), len(new_leads), duplicate_count,
    )
    return new_leads, existing_leads, duplicate_count
