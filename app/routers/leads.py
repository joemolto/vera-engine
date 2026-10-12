import json
import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from app.database import get_supabase
from app.services.api_auth import hosted_only
from app.models.schemas import (
    IngestResponse,
    LeadInput,
    LeadIngestRequest,
    ManualLeadRequest,
    StatusUpdateRequest,
    VALID_TRANSITIONS,
)
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from app.services.activity import log_activity
from app.services.brief_generator import generate_brief, generate_briefs_for_org
from app.services.claude import call_claude
from app.services.client_context import get_client_context as _fetch_client_context, format_competitors, format_product_names
from app.services.dedup import deduplicate_leads, deduplicate_leads_for_discovery
from app.services.domain_resolver import clean_domain, resolve_company_domain
from app.services.enricher import run_full_enrichment
from app.services.enrichers.predictleads import pre_enrich_domain
from app.services.linkedin_verifier import scrape_linkedin_profile
from app.services.outcomes import VALID_OUTCOMES, record_from_lead
from app.services.scorer import score_leads
from app.services.usage import check_enrichment_allowed, record_enrichment, record_discovery_scan
from typing import Any, Dict, List, Optional
from uuid import UUID

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/leads", tags=["leads"])


# ---------------------------------------------------------------------------
# Per-tier suggestion caps now live in ONE shared module so /discover and
# the Vera Brain enforce the IDENTICAL ceiling. Re-deriving this locally is
# exactly what let a Scout org reach 20 suggestions (cap enforced here but
# not in the Brain). Single source of truth: app/services/tiers.py.
from app.services.tiers import (  # noqa: E402
    SUGGESTION_TARGETS,
    DEFAULT_SUGGESTION_TARGET,
    get_suggestion_target as _get_suggestion_target,
)


@router.post("/ingest", response_model=IngestResponse)
def ingest_leads(request: LeadIngestRequest):
    """Ingest a batch of leads from Apollo or an external workflow with deduplication.

    Apollo leads (source="apollo") are auto-filtered to the org's tier-aware
    suggestion target. Other sources currently aren't supported by the live
    product (CSV upload was removed) but the endpoint preserves the
    `enrichment_status` if a caller explicitly sets one.
    """
    db = get_supabase()
    org_id = str(request.org_id)

    new_leads, duplicate_count = deduplicate_leads(db, org_id, request.leads)

    new_lead_ids: List[str] = []
    suggested_lead_ids: List[str] = []

    for lead in new_leads:
        # Default to "suggested" — only Apollo is a live source.
        enrichment_status = lead.enrichment_status or "suggested"

        row = {
            "org_id": org_id,
            "email": lead.email,
            "first_name": lead.first_name,
            "last_name": lead.last_name,
            "title": lead.title,
            "company_name": lead.company_name,
            "company_domain": lead.company_domain,
            "company_size": lead.company_size,
            "company_industry": lead.company_industry,
            "company_location": lead.company_location,
            "linkedin_url": lead.linkedin_url,
            "phone": lead.phone,
            "apollo_id": lead.apollo_id,
            "source": lead.source,
            "status": "new",
            "enrichment_status": enrichment_status,
        }
        try:
            result = db.table("leads").insert(row).execute()
            if result.data:
                lead_id = result.data[0]["id"]
                new_lead_ids.append(lead_id)
                if enrichment_status == "suggested":
                    suggested_lead_ids.append(lead_id)
                log_activity(db, org_id, lead_id, "lead_sourced", {
                    "email": lead.email,
                    "source": lead.source,
                    "company": lead.company_name,
                })
        except Exception as e:
            error_msg = str(e)
            if "23505" in error_msg or "duplicate" in error_msg.lower():
                duplicate_count += 1
                logger.debug("Duplicate caught on insert: %s", lead.email)
            else:
                logger.error("Error inserting lead %s: %s", lead.email, e)

    # Auto-filter Apollo suggestions down to the org's tier-specific target
    # (Scout 5 / Growth 20 / Scale 75). See SUGGESTION_TARGETS.
    suggestions_shown = None
    suggestions_filtered = None

    if suggested_lead_ids:
        target = _get_suggestion_target(db, org_id)
        shown, filtered = _auto_filter_suggestions(
            db, org_id, suggested_lead_ids, top_n=target,
        )
        suggestions_shown = shown
        suggestions_filtered = filtered

    return IngestResponse(
        new_count=len(new_lead_ids),
        duplicate_count=duplicate_count,
        new_lead_ids=new_lead_ids,
        suggestions_shown=suggestions_shown,
        suggestions_filtered=suggestions_filtered,
    )


def _auto_filter_suggestions(
    db, org_id: str, suggested_lead_ids: List[str], top_n: int = 5,
) -> tuple:
    """Filter suggested leads to the best N. Returns (shown_count, filtered_count)."""
    # Fetch the suggested leads
    leads_result = (
        db.table("leads")
        .select("id, first_name, last_name, title, company_name, company_industry, company_size")
        .eq("org_id", org_id)
        .in_("id", suggested_lead_ids)
        .execute()
    )
    leads = leads_result.data or []
    if not leads:
        return 0, 0

    # Step 1: ICP pre-filter
    passed = []
    icp_filtered_ids = []
    for lead in leads:
        industry = (lead.get("company_industry") or "").lower()
        if industry in _DISQUALIFY_INDUSTRIES:
            icp_filtered_ids.append(lead["id"])
        else:
            passed.append(lead)

    # Mark ICP-filtered leads
    for lid in icp_filtered_ids:
        db.table("leads").update({"enrichment_status": "filtered"}).eq("id", lid).execute()

    # If top_n or fewer pass, show them all
    if len(passed) <= top_n:
        return len(passed), len(icp_filtered_ids)

    # Step 2: Claude picks the best N
    try:
        client_context = _get_client_context(db, org_id)
        services = ", ".join(client_context.get("services_offered", [])) or "N/A"
        client_name = client_context.get("client_name", "the client")

        lead_list = "\n".join(
            f"- ID: {l['id']} | {l.get('first_name', '')} {l.get('last_name', '')}, "
            f"{l.get('title', '')} at {l.get('company_name', '')} "
            f"(Industry: {l.get('company_industry', 'Unknown')}, Size: {l.get('company_size', 'Unknown')})"
            for l in passed
        )

        prompt = f"""You are selecting the best prospects for {client_name} who sells {services}.

Here are {len(passed)} potential prospects from Apollo:
{lead_list}

Select exactly {top_n} that are most likely to need {client_name}'s services. Prioritise:
- Senior decision makers (CEO, Founder, MD, CRO, Head of Sales)
- Companies in the 10-200 employee sweet spot
- Companies whose industry is clearly cybersecurity related
- Avoid very small companies (1-2 people) unless the founder is clearly relevant
- Avoid companies that seem like consulting/freelance operations

Respond ONLY with valid JSON:
{{"selected_ids": ["id1", "id2", "id3", "id4", "id5"]}}"""

        result = call_claude(prompt)
        selected_ids = set(result.get("selected_ids", [])) if "error" not in result else set()
    except Exception as e:
        logger.warning("Claude suggestion filtering failed: %s — using first %d", e, top_n)
        selected_ids = set()

    # Fallback: if Claude failed or returned nothing, take first N
    if not selected_ids:
        selected_ids = {l["id"] for l in passed[:top_n]}

    # Mark selected as "suggested", rest as "filtered"
    claude_filtered = 0
    for lead in passed:
        if lead["id"] in selected_ids:
            # Already "suggested" from ingest, no update needed
            pass
        else:
            db.table("leads").update({"enrichment_status": "filtered"}).eq("id", lead["id"]).execute()
            claude_filtered += 1

    return len(selected_ids), len(icp_filtered_ids) + claude_filtered


# ---------------------------------------------------------------------------
# Apollo Discovery — store all leads, classify into suggested/pool/filtered
# ---------------------------------------------------------------------------

class DiscoverRequest(BaseModel):
    org_id: UUID
    leads: List[LeadInput]


@router.post("/discover")
def discover_leads(request: DiscoverRequest):
    """Receive Apollo leads, pre-enrich all, store all, Claude picks top 5.

    Flow:
    1. Partition into new leads vs re-discovered existing leads
    2. ICP pre-filter new leads (industry disqualify)
    3. Lightweight PredictLeads scan on all remaining (new + re-discovered domains)
    4. Update re-discovered leads: times_discovered, last_discovered_at, re-scan
    5. Claude classifies ALL leads: suggested (top N) / pool / filtered
    6. Store all new leads; update existing pool leads
    """
    db = get_supabase()
    org_id = str(request.org_id)
    now = datetime.now(timezone.utc).isoformat()
    total_received = len(request.leads)
    batch_id = f"disc_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    # 2 credits per discovery scan (flat, regardless of batch size).
    from app.services import credits as _credits
    _credits.charge(
        db,
        org_id=org_id,
        action="discovery_scan",
        related_id=batch_id,
        metadata={"leads_received": total_received},
    )

    # Step 0: How many suggestion slots are available?
    # Each tier has its own steady-state target — Scout 5, Growth 20, Scale 75.
    # See SUGGESTION_TARGETS at the top of this file.
    suggestion_target = _get_suggestion_target(db, org_id)
    existing_result = (
        db.table("leads")
        .select("id", count="exact")
        .eq("org_id", org_id)
        .eq("enrichment_status", "suggested")
        .execute()
    )
    existing_suggestions = existing_result.count if existing_result.count else 0
    needed = max(0, suggestion_target - existing_suggestions)

    # Step 1: Partition into new vs re-discovered
    new_leads, existing_leads, rediscovered_count = deduplicate_leads_for_discovery(
        db, org_id, request.leads,
    )

    # Step 2: ICP pre-filter new leads (industry disqualify + critical_rules)
    client_context = _get_client_context(db, org_id)
    icp_passed = []
    icp_filtered_leads: List[tuple] = []  # (lead, reason)
    for lead in new_leads:
        filter_reason = _check_discovery_icp_filter(lead, client_context)
        if filter_reason:
            icp_filtered_leads.append((lead, filter_reason))
        else:
            icp_passed.append(lead)

    # Step 3: Pre-enrich all domains (new passed + re-discovered)
    all_domains: set = set()
    for lead in icp_passed:
        if lead.company_domain:
            all_domains.add(lead.company_domain)
    for lead in existing_leads:
        if lead.get("company_domain"):
            all_domains.add(lead["company_domain"])

    pre_enrichments = _pre_enrich_domains(all_domains)

    # Step 4: Update re-discovered existing leads
    _update_rediscovered_leads(db, existing_leads, pre_enrichments, now, batch_id)

    # Step 5: Store ICP-filtered leads immediately (no Claude needed)
    filtered_ids = _store_filtered_leads(db, org_id, icp_filtered_leads, batch_id, now)

    # Step 6: Claude classifies all ICP-passed leads
    if not icp_passed:
        return {
            "total_received": total_received,
            "new_leads": 0,
            "rediscovered": rediscovered_count,
            "icp_filtered": len(icp_filtered_leads),
            "pre_enriched": len(pre_enrichments),
            "suggestions_created": 0,
            "pool_created": 0,
            "filtered_created": len(filtered_ids),
            "existing_suggestions": existing_suggestions,
            "batch_id": batch_id,
        }

    classifications = _claude_classify_leads(
        db, org_id, icp_passed, pre_enrichments, top_n=needed,
    )

    # Step 7: Store all classified leads
    suggested_ids = []
    pool_ids = []
    claude_filtered_ids = []

    for lead, classification in zip(icp_passed, classifications):
        lead_pre = pre_enrichments.get(lead.company_domain or "", {})
        status = classification.get("status", "pool")
        reason = classification.get("reason", "")

        row = {
            "org_id": org_id,
            "email": lead.email,
            "first_name": lead.first_name,
            "last_name": lead.last_name,
            "title": lead.title,
            "company_name": lead.company_name,
            "company_domain": lead.company_domain,
            "company_size": lead.company_size,
            "company_industry": lead.company_industry,
            "company_location": lead.company_location,
            "linkedin_url": lead.linkedin_url,
            "phone": lead.phone,
            "apollo_id": lead.apollo_id,
            "source": "apollo",
            "status": "new",
            "enrichment_status": status,
            "pre_enrichment_data": lead_pre.get("raw_data", {}),
            "discovery_batch_id": batch_id,
            "discovery_date": now,
            "last_discovered_at": now,
        }
        # Ground-truth persona attribution: when the per-persona fan-out
        # search surfaced this lead, the profile whose query found them is
        # the correct match — write it straight to matched_persona_id
        # rather than waiting for the post-scoring matcher's guess. Skip
        # the synthesised union sentinel (legacy flat configs) — it isn't
        # a real persona UUID and the column is typed UUID.
        if lead.sourced_persona_id and lead.sourced_persona_id != "__union__":
            row["matched_persona_id"] = lead.sourced_persona_id

        if status == "suggested":
            row["suggestion_reason"] = reason
        else:
            row["rejection_reason"] = reason

        try:
            result = db.table("leads").insert(row).execute()
            if result.data:
                lead_id = result.data[0]["id"]
                if status == "suggested":
                    suggested_ids.append(lead_id)
                elif status == "pool":
                    pool_ids.append(lead_id)
                else:
                    claude_filtered_ids.append(lead_id)
                log_activity(db, org_id, lead_id, "lead_sourced", {
                    "email": lead.email,
                    "source": "apollo_discovery",
                    "company": lead.company_name,
                    "classification": status,
                    "batch_id": batch_id,
                })
        except Exception as e:
            error_msg = str(e)
            if "23505" not in error_msg and "duplicate" not in error_msg.lower():
                logger.error("Error inserting discovered lead %s: %s", lead.email, e)

    # Record discovery scan usage
    record_discovery_scan(db, org_id)

    # Auto-promote pool leads if suggestion slots remain unfilled
    # Use the same per-tier target as Step 0 above.
    promoted_ids: List[str] = []
    total_suggested_now = existing_suggestions + len(suggested_ids)
    still_needed = max(0, suggestion_target - total_suggested_now)

    if still_needed > 0:
        promoted_ids = _promote_from_pool(db, org_id, still_needed)
        if promoted_ids:
            suggested_ids.extend(promoted_ids)
            logger.info("Promoted %d pool leads to suggested for org %s", len(promoted_ids), org_id)

    return {
        "total_received": total_received,
        "new_leads": len(icp_passed) + len(icp_filtered_leads),
        "rediscovered": rediscovered_count,
        "icp_filtered": len(icp_filtered_leads),
        "pre_enriched": len(pre_enrichments),
        "suggestions_created": len(suggested_ids),
        "pool_created": len(pool_ids),
        "filtered_created": len(filtered_ids) + len(claude_filtered_ids),
        "existing_suggestions": existing_suggestions,
        "promoted_from_pool": len(promoted_ids),
        "suggestion_ids": suggested_ids,
        "pool_ids": pool_ids,
        "batch_id": batch_id,
    }


class DiscoverAutoRequest(BaseModel):
    org_id: UUID


@router.post("/discover/auto")
def discover_auto(request: DiscoverAutoRequest):
    """One-call discovery: fan out Apollo once per active buyer profile,
    then run the normal /discover classification pipeline.

    Collapses the 3-step workflow dance (Fetch ICP Params → Apollo Search →
    Discover Leads — a single union search) into one engine call where
    per-persona fan-out is the default. /apollo-params and /discover are
    untouched, so this is purely additive: point an external discovery
    workflow here when ready, roll back by pointing it back. The Apollo searches
    consume no credits; the tier cap + Claude classifier in /discover
    remain the gate on what actually gets enriched.
    """
    db = get_supabase()
    org_id = str(request.org_id)

    org_resp = (
        db.table("organisations")
        .select("icp_config")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    if not org_resp.data:
        raise HTTPException(status_code=404, detail=f"Organisation {org_id} not found")
    icp_config = org_resp.data[0].get("icp_config") or {}

    from app.services.discovery import run_persona_fanout

    fanout = run_persona_fanout(icp_config, total_budget=25, source="apollo")
    if not fanout["leads"]:
        return {
            "status": "no_results",
            "apollo_people_found": 0,
            "per_persona": fanout["per_persona"],
            "discover": None,
        }

    discover_result = discover_leads(DiscoverRequest(
        org_id=request.org_id,
        leads=fanout["leads"],
    ))
    return {
        "status": "ok",
        "apollo_people_found": fanout["count"],
        "per_persona": fanout["per_persona"],
        "discover": discover_result,
    }


def _promote_from_pool(db, org_id: str, count: int) -> List[str]:
    """Promote the best pool leads to suggested status to fill empty slots.

    Picks pool leads ordered by times_discovered (most frequently found = most relevant),
    then by created_at (newest first). Returns list of promoted lead IDs.
    """
    pool_result = (
        db.table("leads")
        .select("id, first_name, last_name, company_name")
        .eq("org_id", org_id)
        .eq("enrichment_status", "pool")
        .order("times_discovered", desc=True)
        .order("created_at", desc=True)
        .limit(count)
        .execute()
    )

    if not pool_result.data:
        return []

    promoted_ids = []
    now = datetime.now(timezone.utc).isoformat()

    for lead in pool_result.data:
        db.table("leads").update({
            "enrichment_status": "suggested",
            "updated_at": now,
        }).eq("id", lead["id"]).execute()

        promoted_ids.append(lead["id"])
        logger.info("Promoted pool lead %s %s (%s) to suggested",
                     lead.get("first_name", ""), lead.get("last_name", ""), lead.get("company_name", ""))

    return promoted_ids


# ---------------------------------------------------------------------------
# Pre-enrichment helpers for /discover
# ---------------------------------------------------------------------------

_PRE_ENRICH_TIMEOUT = 10  # seconds per domain


def _pre_enrich_domains(domains: set) -> Dict[str, Dict]:
    """Run lightweight PredictLeads pre-enrichment on a set of domains in parallel."""
    results: Dict[str, Dict] = {}
    if not domains:
        return results

    with ThreadPoolExecutor(max_workers=min(len(domains), 25)) as executor:
        futures = {
            domain: executor.submit(pre_enrich_domain, domain)
            for domain in domains
        }
        for domain, future in futures.items():
            try:
                result = future.result(timeout=_PRE_ENRICH_TIMEOUT)
                if result:
                    results[domain] = result
            except FuturesTimeoutError:
                logger.debug("Pre-enrichment timed out for %s", domain)
            except Exception as e:
                logger.debug("Pre-enrichment failed for %s: %s", domain, e)

    logger.info("Pre-enriched %d/%d domains for discovery", len(results), len(domains))
    return results


def _update_rediscovered_leads(
    db, existing_leads: List[Dict], pre_enrichments: Dict[str, Dict],
    now: str, batch_id: str,
) -> None:
    """Update existing leads that Apollo found again."""
    for lead in existing_leads:
        lead_id = lead["id"]
        domain = lead.get("company_domain") or ""
        times = (lead.get("times_discovered") or 1) + 1

        update = {
            "times_discovered": times,
            "last_discovered_at": now,
            "discovery_batch_id": batch_id,
            "updated_at": now,
        }

        # Re-scan: store fresh PredictLeads data, keep old for diffing
        new_pre = pre_enrichments.get(domain, {})
        if new_pre.get("raw_data"):
            old_pre = lead.get("pre_enrichment_data") or {}
            if old_pre:
                update["previous_pre_enrichment_data"] = old_pre
            update["pre_enrichment_data"] = new_pre["raw_data"]

        db.table("leads").update(update).eq("id", lead_id).execute()


def _store_filtered_leads(
    db, org_id: str, filtered: List[tuple], batch_id: str, now: str,
) -> List[str]:
    """Store ICP-disqualified leads as filtered. Each item is (LeadInput, reason)."""
    ids = []
    for lead, reason in filtered:
        row = {
            "org_id": org_id,
            "email": lead.email,
            "first_name": lead.first_name,
            "last_name": lead.last_name,
            "title": lead.title,
            "company_name": lead.company_name,
            "company_domain": lead.company_domain,
            "company_size": lead.company_size,
            "company_industry": lead.company_industry,
            "company_location": lead.company_location,
            "linkedin_url": lead.linkedin_url,
            "phone": lead.phone,
            "apollo_id": lead.apollo_id,
            "source": "apollo",
            "status": "new",
            "enrichment_status": "filtered",
            "rejection_reason": reason,
            "discovery_batch_id": batch_id,
            "discovery_date": now,
            "last_discovered_at": now,
        }
        try:
            result = db.table("leads").insert(row).execute()
            if result.data:
                ids.append(result.data[0]["id"])
        except Exception as e:
            error_msg = str(e)
            if "23505" not in error_msg and "duplicate" not in error_msg.lower():
                logger.error("Error inserting filtered lead %s: %s", lead.email, e)
    return ids


def _build_lead_summary(index: int, lead: LeadInput, pre: Optional[Dict]) -> str:
    """Build a text summary for one lead, combining Apollo data + PredictLeads intel."""
    summary = pre.get("summary", {}) if pre else {}

    parts = [
        f"Lead {index}: {lead.first_name or ''} {lead.last_name or ''}, "
        f"{lead.title or 'Unknown title'} at {lead.company_name or 'Unknown'}"
    ]
    parts.append(f"  Industry: {lead.company_industry or 'Unknown'} | Size: {lead.company_size or 'Unknown'}")

    if summary.get("description"):
        parts.append(f"  Description: {summary['description'][:200]}")

    # Location cross-reference: Apollo vs PredictLeads
    apollo_location = lead.company_location or "Unknown"
    pl_location = summary.get("location") or ""
    parts.append(f"  Apollo location: {apollo_location}")
    if pl_location:
        parts.append(f"  PredictLeads location: {pl_location}")
        # Flag mismatch if PredictLeads location suggests a different country
        apollo_lower = apollo_location.lower()
        pl_lower = pl_location.lower()
        location_match = "yes"
        if pl_lower and apollo_lower != "unknown":
            # Check if they broadly agree on country
            apollo_countries = {"united kingdom", "uk", "gb", "england", "scotland", "wales"}
            us_countries = {"united states", "us", "usa", "america"}
            apollo_is_uk = any(c in apollo_lower for c in apollo_countries)
            apollo_is_us = any(c in apollo_lower for c in us_countries)
            pl_is_uk = any(c in pl_lower for c in apollo_countries)
            pl_is_us = any(c in pl_lower for c in us_countries)
            if (apollo_is_uk and pl_is_us) or (apollo_is_us and pl_is_uk):
                location_match = "MISMATCH — Apollo says {}, PredictLeads says {}".format(apollo_location, pl_location)
            elif apollo_is_uk == pl_is_uk and apollo_is_us == pl_is_us:
                location_match = "yes"
            else:
                location_match = "uncertain"
        parts.append(f"  Location match: {location_match}")
    else:
        parts.append(f"  Location match: unverified (no PredictLeads data)")

    # Timing signals
    signals = []
    if summary.get("has_recent_funding"):
        signals.append("Recent funding")
    if summary.get("has_recent_acquisition"):
        signals.append("Recent acquisition")
    if summary.get("has_recent_product_launch"):
        signals.append("Recent product launch")
    if summary.get("has_recent_partnership"):
        signals.append("Recent partnership")
    parts.append(f"  Timing signals: {', '.join(signals) if signals else 'None detected'}")

    # Tech stack
    total_tech = summary.get("total_tech_detected", 0)
    bfw_count = summary.get("behind_firewall_count", 0)
    parts.append(f"  Total tech detected: {total_tech} (behind firewall: {bfw_count})")

    security_tools = summary.get("security_tools", [])
    if security_tools:
        parts.append(f"  Security tools: {', '.join(security_tools)}")
    elif summary.get("has_no_security_tools"):
        parts.append("  Security tools: NONE detected (potential buying signal)")
    else:
        parts.append("  Security tools: No tech data available")

    if summary.get("behind_firewall_techs"):
        parts.append(f"  Behind-firewall tech: {', '.join(summary['behind_firewall_techs'][:5])}")

    if summary.get("parent_company"):
        parts.append(f"  Parent company: {summary['parent_company']}")

    return "\n".join(parts)


def _claude_classify_leads(
    db, org_id: str, leads: List[LeadInput],
    pre_enrichments: Dict[str, Dict], top_n: int = DEFAULT_SUGGESTION_TARGET,
) -> List[Dict[str, str]]:
    """Claude classifies ALL leads: suggested (top N), pool, or filtered.

    Returns a list parallel to `leads`, each item:
        {"status": "suggested"|"pool"|"filtered", "reason": "..."}

    The `top_n` arg is the per-tier suggestion target, passed by /discover
    based on the org's tier. The default exists for safety only.
    """
    num_leads = len(leads)
    fallback = [{"status": "pool", "reason": "Claude classification unavailable"}] * num_leads
    if top_n > 0:
        for i in range(min(top_n, num_leads)):
            fallback[i] = {"status": "suggested", "reason": "Selected by default — Claude unavailable"}

    try:
        client_context = _get_client_context(db, org_id)
        client_name = client_context.get("client_name", "the client")
        what_they_sell = client_context.get("what_they_sell", "") or ", ".join(client_context.get("services_offered", [])) or "N/A"
        product_names = format_product_names(client_context) or "N/A"
        ideal_triggers = client_context.get("ideal_triggers", "") or "N/A"
        disqualify_signals = client_context.get("disqualify_signals", "") or "N/A"
        competitors = format_competitors(client_context) or "N/A"
        client_positioning = client_context.get("client_positioning", "") or "N/A"

        # Build summaries
        summaries = []
        for i, lead in enumerate(leads):
            pre = pre_enrichments.get(lead.company_domain or "")
            summaries.append(_build_lead_summary(i, lead, pre))

        all_summaries = "\n\n".join(summaries)

        # Build critical rules section
        critical_rules = client_context.get("critical_rules", "")
        critical_rules_section = ""
        if critical_rules:
            critical_rules_section = f"""
CRITICAL RULES FOR THIS CLIENT:
{critical_rules}

These rules override all other selection criteria. Any lead that violates these rules must be filtered out, regardless of how good the other signals look.
"""

        prompt = f"""You are Vera, an AI GTM agent for {client_name}.
They sell: {what_they_sell}
Products: {product_names}
Their ideal customer: {client_positioning}
Their ideal triggers: {ideal_triggers}
Competitors to watch for: {competitors}
Disqualify if: {disqualify_signals}
{critical_rules_section}
Here are {num_leads} potential prospects with lightweight intelligence:

{all_summaries}

Classify EVERY lead into one of three categories:
- "suggested" — Top {top_n} leads to research NOW. Strong signals, clear fit, good timing.
- "pool" — Decent prospect but not top priority right now. Worth keeping for later.
- "filtered" — Poor fit, wrong industry, too small/large, or matches a disqualify signal.

{"Select exactly " + str(top_n) + " as suggested." if top_n > 0 else "Do not select any as suggested — all slots are filled."}

IMPORTANT — Location verification:
Each lead has a "Location match" field. If it says "MISMATCH", the company may not actually be in the target geography. NEVER suggest a lead with a location mismatch — demote to pool with the reason noting the location uncertainty. Only suggest leads with confirmed or unverified locations.

For each lead, provide a one-sentence reason. For suggested leads, explain WHY they're worth pursuing NOW (reference specific signals). For pool leads, note what's missing. For filtered leads, explain the disqualify reason.

Respond ONLY with valid JSON:
{{
    "leads": [
        {{"lead_index": 0, "status": "suggested", "reason": "No SIEM detected, just hired a CTO, Series A 3 months ago — perfect timing"}},
        {{"lead_index": 1, "status": "pool", "reason": "Good ICP fit but no timing signals — revisit next quarter"}},
        {{"lead_index": 2, "status": "filtered", "reason": "Already uses CrowdStrike and Splunk — mature security stack"}}
    ]
}}"""

        result = call_claude(prompt)
        if "error" not in result:
            items = result.get("leads", [])
            # Build a lookup by lead_index
            index_map: Dict[int, Dict[str, str]] = {}
            for item in items:
                idx = item.get("lead_index")
                if isinstance(idx, int) and 0 <= idx < num_leads:
                    index_map[idx] = {
                        "status": item.get("status", "pool"),
                        "reason": item.get("reason", ""),
                    }

            # Build result list parallel to leads
            classifications = []
            suggested_count = 0
            for i in range(num_leads):
                if i in index_map:
                    cl = index_map[i]
                    # Enforce suggested cap
                    if cl["status"] == "suggested":
                        if suggested_count < top_n:
                            suggested_count += 1
                        else:
                            cl["status"] = "pool"
                    classifications.append(cl)
                else:
                    classifications.append({"status": "pool", "reason": "Not classified by Vera"})

            return classifications

    except Exception as e:
        logger.warning("Claude lead classification failed: %s — using fallback", e)

    return fallback


# ---------------------------------------------------------------------------
# Lead pool endpoint
# ---------------------------------------------------------------------------

@router.get("/pool")
def get_pool_leads(org_id: str, has_new_signals: bool = False, limit: int = 50):
    """Return pool leads, optionally filtered to those with new PredictLeads signals.

    When has_new_signals=true, returns pool leads where pre_enrichment_data differs
    from previous_pre_enrichment_data (i.e. re-discovered with signal changes).
    """
    db = get_supabase()

    query = (
        db.table("leads")
        .select(
            "id, first_name, last_name, title, company_name, company_domain, "
            "company_industry, company_size, linkedin_url, enrichment_status, "
            "rejection_reason, suggestion_reason, times_discovered, "
            "last_discovered_at, discovery_batch_id, pre_enrichment_data, "
            "previous_pre_enrichment_data, created_at"
        )
        .eq("org_id", org_id)
        .eq("enrichment_status", "pool")
        .order("last_discovered_at", desc=True)
        .limit(limit)
    )

    leads_result = query.execute()
    pool_leads = leads_result.data or []

    if has_new_signals:
        # Filter to leads where PredictLeads data changed between scans
        pool_leads = [
            lead for lead in pool_leads
            if _has_signal_changes(lead)
        ]

    return {
        "pool_leads": pool_leads,
        "total": len(pool_leads),
    }


def _has_signal_changes(lead: Dict) -> bool:
    """Check if a lead's pre-enrichment signals changed since last scan."""
    current = lead.get("pre_enrichment_data") or {}
    previous = lead.get("previous_pre_enrichment_data") or {}

    # No previous scan — treat first re-discovery as a new signal
    if not previous and current:
        return (lead.get("times_discovered") or 1) > 1

    if not current or not previous:
        return False

    # Compare news events — new funding, acquisition, etc.
    cur_news = current.get("predictleads_news") or {}
    prev_news = previous.get("predictleads_news") or {}
    for signal in ("has_recent_funding", "has_recent_acquisition",
                   "has_recent_product_launch", "has_recent_partnership"):
        if cur_news.get(signal) and not prev_news.get(signal):
            return True

    # Compare tech count — significant change
    cur_tech = current.get("predictleads_tech") or {}
    prev_tech = previous.get("predictleads_tech") or {}
    cur_count = cur_tech.get("total_detected", 0)
    prev_count = prev_tech.get("total_detected", 0)
    if abs(cur_count - prev_count) >= 3:
        return True

    # Compare behind-firewall techs
    cur_bfw = set(cur_tech.get("behind_firewall_techs") or [])
    prev_bfw = set(prev_tech.get("behind_firewall_techs") or [])
    if cur_bfw != prev_bfw:
        return True

    return False


@router.get("/apollo-params/{org_id}")
def get_apollo_params(org_id: str):
    """Return ICP config formatted for Apollo search. Called by an external discovery workflow before each run."""
    db = get_supabase()

    org_result = (
        db.table("organisations")
        .select("name, icp_config, client_context")
        .eq("id", org_id)
        .execute()
    )
    if not org_result.data:
        raise HTTPException(status_code=404, detail="Organisation not found")

    org = org_result.data[0]
    icp = org.get("icp_config") or {}
    ctx = org.get("client_context") or {}

    # Additive, non-breaking: every legacy flat field below is unchanged
    # (it's still the union, for any workflow not yet migrated). `personas`
    # is a new array — one ready-to-POST Apollo payload per active buyer
    # profile — so an external discovery workflow can loop and run one search per
    # profile instead of the single union search. Prefer the simpler
    # POST /leads/discover/auto, which does this server-side; this field
    # exists for workflows that build their own Apollo calls.
    from app.services.apollo_payload import build_search_payload
    from app.services.discovery import _active_personas_from_icp

    personas_out = []
    for p in _active_personas_from_icp(icp):
        if p.get("id") == "__union__":
            break  # legacy flat config — no real personas to expose
        personas_out.append({
            "persona_id": p.get("id"),
            "persona_name": p.get("name"),
            "apollo_payload": build_search_payload(p, page=1, per_page=25),
        })

    return {
        "org_id": org_id,
        "org_name": org.get("name", ""),
        "target_titles": icp.get("target_titles", []),
        "target_industries": icp.get("target_industries", []),
        "target_company_size": icp.get("target_company_size", []),
        "target_locations": icp.get("target_locations", []),
        "target_description": icp.get("target_description", ""),
        "signals_to_detect": icp.get("signals_to_detect", []),
        "critical_rules": icp.get("critical_rules") or ctx.get("critical_rules", ""),
        "disqualify_signals": ctx.get("disqualify_signals", []),
        "competitors": ctx.get("competitors", []),
        "personas": personas_out,
    }


class NaturalLanguageICPRequest(BaseModel):
    org_id: UUID
    nl_query: str = Field(..., min_length=3, max_length=500)


class ICPRevertRequest(BaseModel):
    org_id: UUID


_NL_ICP_PROMPT = """You are translating a sales operator's natural-language ICP request into structured search parameters for the Apollo people-search API.

## What the user wants to target
{nl_query}

## Their current ICP (for context — preserve any fields the user didn't mention)
- target_titles: {current_titles}
- target_industries: {current_industries}
- target_locations: {current_locations}
- target_company_size: {current_company_size}

## Translation rules

1. Output JSON with ONLY the fields the user explicitly mentioned. Fields you don't include will be preserved from the current ICP.
2. **target_titles**: array of job titles. Use canonical forms (e.g. "Chief Information Security Officer" plus "CISO" — Apollo matches both). Include common variants.
3. **target_industries**: array of industry strings. Be liberal with synonyms — "financial services" should expand to ["Financial Services", "Banking", "Capital Markets"]. "fintech" → ["Financial Technology", "Fintech"]. Apollo's vocab is fuzzy, so include 2-4 plausible matches per concept the user mentioned.
4. **target_locations**: array of locations. Use Apollo's preferred format: "City, State, Country" for US (e.g. "Boston, Massachusetts, United States"), "City, Country" elsewhere ("London, United Kingdom"). If the user says just "Boston", expand to the full form.
5. **target_company_size**: only include if user mentioned company size. Use Apollo's range strings ("11,50", "51,100", "101,200", "201,500", "501,1000", "1001,5000", "5001,10000").
6. **change_summary**: a one-sentence human-readable description of what changed, in past tense. Example: "Switched targeting from healthcare CISOs in NY to banking CISOs in Boston."

## Output

Reply with valid JSON only:
{{
  "target_titles": [...],          // omit if user didn't mention titles
  "target_industries": [...],       // omit if user didn't mention industries
  "target_locations": [...],        // omit if user didn't mention locations
  "target_company_size": [...],     // omit if user didn't mention company size
  "change_summary": "..."           // always include
}}"""


@router.post("/icp/natural-language")
def update_icp_via_natural_language(request: NaturalLanguageICPRequest):
    """Translate a natural-language ICP query into icp_config changes,
    snapshot the previous config for one-step undo, and trigger an immediate
    Apollo people-search so results land in the suggestions pool within ~30s.

    Cost: ~$0.001 (one Claude Haiku call). Apollo people-search is free.
    Per-lead enrichment is NOT triggered here — suggestions land in the pool
    and the user enriches selectively via the existing UI.
    """
    db = get_supabase()
    org_id = str(request.org_id)
    nl_query = request.nl_query.strip()

    org_resp = (
        db.table("organisations")
        .select("icp_config, client_context")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    if not org_resp.data:
        raise HTTPException(status_code=404, detail=f"Organisation {org_id} not found")
    current_icp = org_resp.data[0].get("icp_config") or {}

    prompt = _NL_ICP_PROMPT.format(
        nl_query=nl_query,
        current_titles=json.dumps(current_icp.get("target_titles", [])),
        current_industries=json.dumps(current_icp.get("target_industries", [])),
        current_locations=json.dumps(current_icp.get("target_locations", [])),
        current_company_size=json.dumps(current_icp.get("target_company_size", [])),
    )

    try:
        llm_result = call_claude(prompt)
    except Exception as e:
        logger.warning("NL ICP translation failed: %s", e)
        raise HTTPException(status_code=502, detail=f"LLM translation failed: {e}")

    if not isinstance(llm_result, dict):
        raise HTTPException(status_code=502, detail="LLM returned an unexpected response shape")

    change_summary = (llm_result.get("change_summary") or "").strip() or "Updated ICP from natural-language query."

    # Build the new icp_config: preserve everything from current, only replace
    # the specific fields the LLM returned. This is the safe merge — fields
    # the user didn't mention stay exactly as they were.
    new_icp = dict(current_icp)
    delta_fields = ("target_titles", "target_industries", "target_locations", "target_company_size")
    delta_applied: Dict[str, Any] = {}
    for field in delta_fields:
        if field in llm_result and isinstance(llm_result[field], list):
            cleaned = [v for v in llm_result[field] if isinstance(v, str) and v.strip()]
            if cleaned:
                new_icp[field] = cleaned
                delta_applied[field] = cleaned

    if not delta_applied:
        raise HTTPException(
            status_code=400,
            detail="Could not extract any ICP fields from that query. Try mentioning titles, industries, or locations explicitly.",
        )

    # Snapshot current → previous (for one-step undo via /icp/revert), then apply
    db.table("organisations").update({
        "icp_config_previous": current_icp,
        "icp_config": new_icp,
    }).eq("id", org_id).execute()

    log_activity(db, org_id, None, "icp_updated_via_nl", {
        "nl_query": nl_query,
        "change_summary": change_summary,
        "delta_applied": delta_applied,
    })

    # Fire an immediate Apollo people-search with the new params and feed
    # results into the existing /discover pipeline (which dedupes, classifies
    # via Claude, and inserts as suggestions). All free until the user
    # chooses to enrich a specific lead.
    apollo_search_result = _run_apollo_search_for_icp(new_icp)
    suggestions_added = 0
    if apollo_search_result["leads"]:
        try:
            discover_response = discover_leads(DiscoverRequest(
                org_id=request.org_id,
                leads=apollo_search_result["leads"],
            ))
            suggestions_added = (
                discover_response.get("suggested_count", 0)
                if isinstance(discover_response, dict)
                else 0
            )
        except Exception as e:
            logger.warning("Discover failed after NL ICP update: %s", e)

    # Detect why suggestions_added might be 0 even when Apollo returned hits —
    # most often the pool is at cap and /discover correctly held off rather
    # than overflow it.
    pool_state: Dict[str, Any] = {}
    try:
        suggestion_target = _get_suggestion_target(db, org_id)
        existing_resp = (
            db.table("leads")
            .select("id", count="exact")
            .eq("org_id", org_id)
            .eq("enrichment_status", "suggested")
            .execute()
        )
        existing_suggestions = existing_resp.count if existing_resp.count else 0
        pool_state = {
            "current_size": existing_suggestions,
            "target_size": suggestion_target,
            "is_full": existing_suggestions >= suggestion_target,
        }
    except Exception as e:
        logger.debug("Couldn't fetch pool state for response context: %s", e)

    return {
        "status": "icp_updated",
        "previous_icp": current_icp,
        "new_icp": new_icp,
        "delta_applied": delta_applied,
        "change_summary": change_summary,
        "search_results": {
            "apollo_people_found": apollo_search_result["count"],
            "suggestions_added": suggestions_added,
            "pool": pool_state,
        },
    }


@router.get("/icp/status/{org_id}")
def get_icp_status(org_id: str):
    """Return whether the org has a revertable ICP snapshot, plus a summary
    of the most recent change. Polled by the dashboard on page load so a
    persistent "Revert previous ICP" button can be shown when needed —
    surviving page refreshes that would lose in-memory banner state.
    """
    db = get_supabase()

    org_resp = (
        db.table("organisations")
        .select("icp_config_previous")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    if not org_resp.data:
        raise HTTPException(status_code=404, detail=f"Organisation {org_id} not found")

    has_previous = org_resp.data[0].get("icp_config_previous") is not None

    latest_change: Optional[Dict[str, Any]] = None
    if has_previous:
        # Pull the most recent icp_updated_via_nl activity row for context
        activity_resp = (
            db.table("activity_log")
            .select("activity_data, created_at")
            .eq("org_id", org_id)
            .eq("activity_type", "icp_updated_via_nl")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if activity_resp.data:
            row = activity_resp.data[0]
            data = row.get("activity_data") or {}
            latest_change = {
                "summary": data.get("change_summary") or "",
                "nl_query": data.get("nl_query") or "",
                "applied_at": row.get("created_at"),
            }

    return {
        "has_previous_icp": has_previous,
        "latest_change": latest_change,
    }


@router.post("/icp/revert")
def revert_icp(request: ICPRevertRequest):
    """Restore the previously-saved ICP config (single-step undo).

    Swaps `icp_config_previous` back into `icp_config`. After revert,
    `icp_config_previous` is cleared — there's no redo. If the user wants
    to redo, they re-run the natural-language search.
    """
    db = get_supabase()
    org_id = str(request.org_id)

    org_resp = (
        db.table("organisations")
        .select("icp_config, icp_config_previous")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    if not org_resp.data:
        raise HTTPException(status_code=404, detail=f"Organisation {org_id} not found")

    previous = org_resp.data[0].get("icp_config_previous")
    if not previous:
        raise HTTPException(
            status_code=409,
            detail="No previous ICP to revert to — the most recent change has already been undone (or never happened).",
        )

    current = org_resp.data[0].get("icp_config") or {}

    db.table("organisations").update({
        "icp_config": previous,
        "icp_config_previous": None,
    }).eq("id", org_id).execute()

    log_activity(db, org_id, None, "icp_reverted", {
        "reverted_from": current,
        "reverted_to": previous,
    })

    return {
        "status": "icp_reverted",
        "restored_icp": previous,
    }


def _run_apollo_search_for_icp(icp_config: Dict[str, Any]) -> Dict[str, Any]:
    """Run Apollo people-search for the org's ICP and return LeadInput-shaped
    rows ready to feed into /discover.

    Now fans out one search PER active buyer profile (see
    app.services.discovery) instead of a single union search, so every
    returned person genuinely belongs to a profile rather than a
    cross-product of all of them. Legacy flat configs (no `personas`
    array) fall back to a single synthesised union search — identical to
    the previous behaviour.

    Apollo people-search consumes no credits — only personal-data reveals
    (people-match) cost credits — so N profile searches is still free.
    Source label stays "vera_brain_apollo" so NL-originated leads keep
    their existing provenance.
    """
    from app.services.discovery import run_persona_fanout

    result = run_persona_fanout(
        icp_config, total_budget=25, source="vera_brain_apollo"
    )
    return {"count": result["count"], "leads": result["leads"]}


@router.post("/manual", response_model=IngestResponse)
def add_manual_lead(request: ManualLeadRequest):
    """Add a single lead manually (LinkedIn DM, referral, event, etc.)."""
    db = get_supabase()
    org_id = str(request.org_id)

    # Check for existing
    existing = (
        db.table("leads")
        .select("id")
        .eq("org_id", org_id)
        .eq("email", request.email)
        .execute()
    )
    if existing.data:
        return IngestResponse(new_count=0, duplicate_count=1, new_lead_ids=[])

    row = {
        "org_id": org_id,
        "email": request.email,
        "first_name": request.first_name,
        "last_name": request.last_name,
        "title": request.title,
        "company_name": request.company_name,
        "company_domain": request.company_domain,
        "company_size": request.company_size,
        "company_industry": request.company_industry,
        "company_location": request.company_location,
        "linkedin_url": request.linkedin_url,
        "phone": request.phone,
        "source": request.source,
        "status": "new",
    }

    try:
        result = db.table("leads").insert(row).execute()
        lead_id = result.data[0]["id"]
        log_activity(db, org_id, lead_id, "lead_sourced", {
            "email": request.email,
            "source": request.source,
            "notes": request.notes,
        })
        return IngestResponse(new_count=1, duplicate_count=0, new_lead_ids=[lead_id])
    except Exception as e:
        logger.error("Error adding manual lead: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/{lead_id}/status")
def update_lead_status(lead_id: str, request: StatusUpdateRequest):
    """Update a lead's status with transition validation."""
    db = get_supabase()
    org_id = str(request.org_id)

    # Fetch current lead
    lead_result = (
        db.table("leads")
        .select("id, status")
        .eq("id", lead_id)
        .eq("org_id", org_id)
        .execute()
    )
    if not lead_result.data:
        raise HTTPException(status_code=404, detail="Lead not found")

    current_status = lead_result.data[0]["status"]
    new_status = request.status

    # Validate transition
    allowed = VALID_TRANSITIONS.get(current_status, [])
    if new_status not in allowed:
        logger.warning(
            "Invalid status transition %s -> %s for lead %s (allowing anyway)",
            current_status, new_status, lead_id,
        )

    # Update status
    db.table("leads").update({
        "status": new_status,
        "updated_at": "now()",
    }).eq("id", lead_id).execute()

    # If meeting booked, increment campaign counter
    if new_status == "meeting":
        lead_data = (
            db.table("leads")
            .select("instantly_campaign_id")
            .eq("id", lead_id)
            .execute()
        )
        campaign_id = lead_data.data[0].get("instantly_campaign_id") if lead_data.data else None
        if campaign_id:
            campaign = (
                db.table("campaigns")
                .select("id, meetings_booked")
                .eq("instantly_campaign_id", campaign_id)
                .eq("org_id", org_id)
                .execute()
            )
            if campaign.data:
                db.table("campaigns").update({
                    "meetings_booked": campaign.data[0]["meetings_booked"] + 1,
                    "updated_at": "now()",
                }).eq("id", campaign.data[0]["id"]).execute()

    log_activity(db, org_id, lead_id, "status_updated", {
        "from": current_status,
        "to": new_status,
        "notes": request.notes,
    })

    return {"lead_id": lead_id, "previous_status": current_status, "new_status": new_status}


# ---------------------------------------------------------------------------
# Mark reached out — manual outreach tracking until email-send integration
# ---------------------------------------------------------------------------

class MarkReachedOutRequest(BaseModel):
    org_id: UUID


@router.post("/{lead_id}/mark-reached-out")
def mark_reached_out(lead_id: str, request: MarkReachedOutRequest):
    """Manually flag that the rep has reached out to this lead.

    Sets outreach_at = now() and bumps status to 'contacted' (only if
    the lead is still in new/qualified — never downgrade a further-along
    status like 'meeting'). The lead drops out of the "Act on today"
    queue because that feed filters out anything with outreach_at set.

    Idempotent: re-marking a lead that's already been reached out to is
    a no-op (we keep the original outreach_at — the rep cares about the
    FIRST touch, same pattern as first_hot_at).
    """
    db = get_supabase()
    org_id = str(request.org_id)

    result = (
        db.table("leads")
        .select("id, status, outreach_at")
        .eq("id", lead_id)
        .eq("org_id", org_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Lead not found")

    row = result.data[0]
    if row.get("outreach_at"):
        # Already reached out — preserve the first-touch timestamp.
        return {
            "lead_id": lead_id,
            "outreach_at": row["outreach_at"],
            "already_reached_out": True,
        }

    now = datetime.now(timezone.utc).isoformat()
    update: Dict[str, Any] = {"outreach_at": now, "updated_at": now}
    # Only advance status forward — don't clobber 'meeting'/'won'/etc.
    if row.get("status") in ("new", "qualified", None):
        update["status"] = "contacted"

    db.table("leads").update(update).eq("id", lead_id).execute()

    log_activity(db, org_id, lead_id, "lead_reached_out", {
        "outreach_at": now,
        "via": "manual_dashboard_button",
    })

    return {
        "lead_id": lead_id,
        "outreach_at": now,
        "already_reached_out": False,
    }


# ---------------------------------------------------------------------------
# Sales brief generation
# ---------------------------------------------------------------------------

class BriefRequest(BaseModel):
    org_id: UUID


class BatchBriefRequest(BaseModel):
    org_id: UUID


def _get_client_context(db, org_id: str) -> dict:
    """Fetch full client context including client_context JSONB and icp_config."""
    ctx = _fetch_client_context(db, org_id)
    if not ctx or not ctx.get("company_name"):
        raise HTTPException(status_code=404, detail="Organisation not found")
    return ctx


@router.post("/{lead_id}/brief")
def generate_lead_brief(lead_id: str, request: BriefRequest):
    """Generate a sales brief for a single lead."""
    db = get_supabase()
    org_id = str(request.org_id)

    # Fetch lead with org isolation
    lead_result = (
        db.table("leads")
        .select("*")
        .eq("id", lead_id)
        .eq("org_id", org_id)
        .execute()
    )
    if not lead_result.data:
        raise HTTPException(status_code=404, detail="Lead not found")

    lead = lead_result.data[0]

    if not lead.get("enrichment_data"):
        raise HTTPException(status_code=400, detail="Lead has no enrichment data — run enrichment first")

    client_context = _get_client_context(db, org_id)
    brief = generate_brief(db, org_id, lead, client_context)

    if "error" in brief:
        raise HTTPException(status_code=500, detail=f"Brief generation failed: {brief['error']}")

    return {"lead_id": lead_id, "brief": brief}


@router.post("/briefs/generate")
def generate_batch_briefs(request: BatchBriefRequest):
    """Generate sales briefs for all enriched leads in an org that don't have one yet."""
    db = get_supabase()
    org_id = str(request.org_id)

    client_context = _get_client_context(db, org_id)
    result = generate_briefs_for_org(db, org_id, client_context)

    return result


# ---------------------------------------------------------------------------
# Quick enrich from LinkedIn URL
# ---------------------------------------------------------------------------

class QuickEnrichRequest(BaseModel):
    """Quick-enrich accepts EITHER a LinkedIn person URL (creates +
    enriches the lead, with company side-upserted) OR a `domain`
    (company-only enrichment, no lead). Exactly one path is taken per
    call — the route dispatches on which field is populated.
    """
    org_id: UUID
    linkedin_url: Optional[str] = None
    domain: Optional[str] = None
    company_domain: Optional[str] = None
    company_name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    title: Optional[str] = None
    email: Optional[str] = None


def _extract_lead_from_profile(profile: Dict[str, Any], linkedin_url: str) -> Dict[str, str]:
    """Extract lead fields from a scraped LinkedIn profile."""
    # First name — try dedicated fields, then split fullName/name
    first_name = (
        profile.get("firstName") or profile.get("first_name") or ""
    )
    last_name = (
        profile.get("lastName") or profile.get("last_name") or ""
    )

    # If no dedicated first/last, try splitting fullName or name
    if not first_name and not last_name:
        full_name = profile.get("fullName") or profile.get("name") or ""
        if full_name:
            parts = full_name.strip().split(" ", 1)
            first_name = parts[0]
            last_name = parts[1] if len(parts) > 1 else ""

    title = (
        profile.get("headline") or profile.get("title")
        or profile.get("currentTitle") or ""
    )
    company_name = (
        profile.get("companyName") or profile.get("company")
        or profile.get("currentCompany") or ""
    )

    # Try to extract from current experience if top-level fields are empty
    if not company_name or not title:
        for key in ("experience", "experiences", "positions", "workExperience"):
            exp_list = profile.get(key)
            if isinstance(exp_list, list) and exp_list:
                first_exp = exp_list[0]
                if isinstance(first_exp, dict):
                    if not company_name:
                        company_name = (
                            first_exp.get("companyName") or first_exp.get("company")
                            or first_exp.get("organizationName") or ""
                        )
                    if not title:
                        title = first_exp.get("title") or first_exp.get("position") or ""
                break

    # Try to get domain from profile data
    company_domain = ""
    website = (
        profile.get("companyWebsite") or profile.get("website")
        or profile.get("companyUrl") or ""
    )
    if website and "linkedin.com" not in website:
        company_domain = clean_domain(website) or ""

    return {
        "first_name": first_name,
        "last_name": last_name,
        "title": title,
        "company_name": company_name,
        "company_domain": company_domain,
        "linkedin_url": linkedin_url,
    }




def _apollo_people_match(
    first_name: str,
    last_name: str,
    linkedin_url: str = "",
    company_domain: str = "",
    company_name: str = "",
    apollo_id: str = "",
) -> Optional[Dict[str, Any]]:
    """Look up a person in Apollo to find their email and enrich contact data.

    Returns dict with email, phone, title, etc. or None if not found.

    `apollo_id` (Apollo's person id) is the STRONGEST matcher: when we already
    have it from discovery / the live people search, Apollo returns that exact
    person with their real full name + photo. Matching only on a discovered,
    obfuscated name ("Partho C.") is lossy and can echo the obfuscation back —
    which is why an unlock could land a surname-less, photo-less contact.
    """
    from app.config import settings

    if not settings.APOLLO_API_KEY:
        logger.warning("Apollo API key not set — skipping people match")
        return None

    headers = {"X-Api-Key": settings.APOLLO_API_KEY}
    payload: Dict[str, Any] = {
        "first_name": first_name,
        "last_name": last_name,
    }
    if apollo_id:
        payload["id"] = apollo_id
    if linkedin_url:
        payload["linkedin_url"] = linkedin_url
    if company_domain:
        payload["domain"] = company_domain
    if company_name:
        payload["organization_name"] = company_name

    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(
                "https://api.apollo.io/api/v1/people/match",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        person = data.get("person") or {}
        if not person:
            logger.info("Apollo people match: no match for %s %s", first_name, last_name)
            return None

        logger.info("Apollo people match: found %s %s — email=%s",
                     first_name, last_name, person.get("email", "N/A"))
        org = person.get("organization") or {}
        # Apollo returns either `industry` (string) or `industries` (list).
        # Take whichever is present — the array is the modern shape.
        industry = (
            org.get("industry")
            or (org.get("industries") or [None])[0]
            or ""
        )
        # Apollo's `estimated_num_employees` is the canonical headcount field.
        # It comes back as int sometimes, string others; coerce to string for
        # the `company_size` column which is TEXT.
        size_raw = org.get("estimated_num_employees")
        size = str(size_raw) if size_raw is not None else ""
        # Location: Apollo provides separate city/state/country and a combined
        # `headquarters_address` blob. Prefer the structured fields when
        # available, otherwise fall back to the combined string.
        loc_parts = [
            org.get("city") or "",
            org.get("state") or "",
            org.get("country") or "",
        ]
        location = ", ".join(p for p in loc_parts if p) or (org.get("headquarters_address") or "")

        # Company description — Apollo's most underused field. The structured
        # `industry` is null for ~30% of small/early-stage orgs (an AI startup can be
        # listed only as the parent industry "Computer Software" while Apollo's
        # `short_description` says "AI-powered enterprise resource planning
        # platform"). Pulling description + keywords gives the ICP gate the
        # actual business description it needs to classify reliably.
        description_parts: List[str] = []
        for key in ("short_description", "seo_description", "description"):
            v = org.get(key)
            if isinstance(v, str) and v.strip():
                description_parts.append(v.strip())
        # Apollo's `keywords` is a list of business-tag strings (e.g.
        # ["artificial intelligence", "saas", "erp"]). Append them as a
        # comma-joined string so the haystack can match individual tokens.
        keywords = org.get("keywords")
        if isinstance(keywords, list) and keywords:
            kw_str = ", ".join(k for k in keywords if isinstance(k, str) and k.strip())
            if kw_str:
                description_parts.append(kw_str)
        company_description = " | ".join(description_parts)

        founded_year = org.get("founded_year")
        founded = str(founded_year) if founded_year is not None else ""

        return {
            "email": person.get("email") or "",
            # Apollo's own verdict on its address ('verified'/'unverified'/
            # 'extrapolated'…). Most are 'unverified' — callers must not
            # treat every Apollo email as equally trustworthy.
            "email_status": person.get("email_status") or "",
            "phone": person.get("phone_number") or "",
            # Apollo's revealed identity. Callers that matched on an
            # obfuscated name (e.g. "Ronald T.") need these to store the
            # person's real full name.
            "first_name": person.get("first_name") or "",
            "last_name": person.get("last_name") or "",
            "linkedin_url": person.get("linkedin_url") or "",
            "photo_url": person.get("photo_url") or "",
            "title": person.get("title") or "",
            "company_name": org.get("name") or "",
            "company_domain": org.get("primary_domain") or "",
            "company_industry": industry,
            "company_size": size,
            "company_location": location,
            "company_description": company_description,
            "company_founded_year": founded,
            "apollo_id": person.get("id") or "",
        }
    except Exception as e:
        logger.warning("Apollo people match failed for %s %s: %s", first_name, last_name, e)
        return None


# ---------------------------------------------------------------------------
# Apollo phone reveal — opt-in, charges 1 enrichment credit per success
# ---------------------------------------------------------------------------
# Standard people-match returns email but not phone. Phone reveal is a
# separate Apollo credit (charged by Apollo only on success — misses are
# free). We mirror that: 1 Vera enrichment credit charged on success only.
#
# Apollo's reveal can be sync (phone returned in the same response) or
# async (returns a request_id, requires polling /people/phone_enrichment_status).


def _extract_phone_from_person(person: Dict[str, Any]) -> Optional[str]:
    """Pull a phone number out of an Apollo person object — they ship it in
    multiple shapes depending on which endpoint and which payload variant.
    Tries the most common locations in order and returns the first hit."""
    if not isinstance(person, dict):
        return None

    # Direct field
    direct = person.get("phone_number")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    # Array of numbers — first one wins. Apollo's array entries are usually
    # {raw_number, sanitized_number, type} dicts but can be plain strings.
    phones = person.get("phone_numbers") or []
    if isinstance(phones, list) and phones:
        first = phones[0]
        if isinstance(first, dict):
            for key in ("sanitized_number", "raw_number", "number"):
                v = first.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        elif isinstance(first, str) and first.strip():
            return first.strip()

    return None


def _poll_apollo_phone_status(
    request_id: str,
    max_attempts: int = 7,
    sleep_seconds: float = 2.0,
) -> Optional[str]:
    """Poll Apollo's async phone-enrichment status endpoint until the reveal
    completes or times out. Total wait budget: ~14 seconds (7 × 2s)."""
    import time
    from app.config import settings as _settings

    url = "https://api.apollo.io/api/v1/people/phone_enrichment_status"

    headers = {"X-Api-Key": _settings.APOLLO_API_KEY}

    for attempt in range(max_attempts):
        time.sleep(sleep_seconds)
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(
                    url,
                    json={"request_id": request_id},
                    headers=headers,
                )
                if not resp.is_success:
                    logger.warning(
                        "Apollo phone status poll attempt %d returned HTTP %d",
                        attempt + 1, resp.status_code,
                    )
                    continue
                data = resp.json()
        except Exception as e:
            logger.warning("Apollo phone status poll attempt %d errored: %s", attempt + 1, e)
            continue

        status = (data.get("status") or data.get("state") or "").lower()
        if status in ("complete", "completed", "success", "ready", "found"):
            # Phone may live at the top level or nested under person/phone_numbers
            phone = (
                data.get("phone_number")
                or data.get("phone")
                or _extract_phone_from_person(data)
                or _extract_phone_from_person(data.get("person") or {})
            )
            return phone or None
        if status in ("failed", "error", "not_found", "no_match"):
            return None

    logger.info("Apollo phone reveal timed out after %d polls for request %s", max_attempts, request_id)
    return None


def _apollo_reveal_phone(
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    linkedin_url: Optional[str] = None,
    company_domain: Optional[str] = None,
    company_name: Optional[str] = None,
    apollo_id: Optional[str] = None,
) -> Optional[str]:
    """Reveal a phone number for a contact via Apollo's people-match endpoint.

    Pass any combination of identifiers — Apollo prefers `id` (apollo_id) or
    `linkedin_url`. Returns the phone string on success, None if not found
    or on error. Apollo only charges credits on successful reveals.
    """
    from app.config import settings as _settings

    if not _settings.APOLLO_API_KEY:
        logger.warning("APOLLO_API_KEY not set — skipping phone reveal")
        return None

    headers = {"X-Api-Key": _settings.APOLLO_API_KEY}
    payload: Dict[str, Any] = {
        "reveal_phone_number": True,
    }
    # Identifying fields — at least one needed; combine for best match rate
    if apollo_id:
        payload["id"] = apollo_id
    if linkedin_url:
        payload["linkedin_url"] = linkedin_url
    if first_name:
        payload["first_name"] = first_name
    if last_name:
        payload["last_name"] = last_name
    if company_domain:
        payload["domain"] = company_domain
    if company_name:
        payload["organization_name"] = company_name

    try:
        with httpx.Client(timeout=20) as client:
            resp = client.post(
                "https://api.apollo.io/api/v1/people/match",
                json=payload,
                headers=headers,
            )
            if not resp.is_success:
                logger.warning(
                    "Apollo phone reveal returned HTTP %d: %s",
                    resp.status_code, resp.text[:200],
                )
                return None
            data = resp.json()
    except Exception as e:
        logger.warning("Apollo phone reveal request failed: %s", e)
        return None

    person = data.get("person") or {}
    if not person:
        return None

    # Sync path: phone arrived in the immediate response
    phone = _extract_phone_from_person(person)
    if phone:
        return phone

    # Async path: phone reveal queued, follow the request_id to status endpoint
    enrichment = (
        data.get("phone_enrichment")
        or person.get("phone_enrichment")
        or {}
    )
    request_id = enrichment.get("request_id") if isinstance(enrichment, dict) else None
    if request_id:
        return _poll_apollo_phone_status(request_id)

    return None


class RevealPhoneRequest(BaseModel):
    org_id: UUID


@router.post("/{lead_id}/reveal-phone")
def reveal_phone(lead_id: str, request: RevealPhoneRequest):
    """Reveal a phone number for an existing lead via Apollo. Charges 1
    enrichment credit on success only — misses are free (Apollo doesn't
    bill us for misses, we don't bill the customer).

    Idempotent: if the lead already has a phone, returns it without
    contacting Apollo and without charging a credit.
    """
    db = get_supabase()
    org_id = str(request.org_id)

    lead_resp = (
        db.table("leads")
        .select("id, org_id, first_name, last_name, linkedin_url, company_domain, company_name, apollo_id, phone")
        .eq("id", lead_id)
        .eq("org_id", org_id)
        .limit(1)
        .execute()
    )
    if not lead_resp.data:
        raise HTTPException(status_code=404, detail="Lead not found")
    lead = lead_resp.data[0]

    # Already revealed — no charge, just return what we have
    if lead.get("phone"):
        return {
            "status": "already_revealed",
            "phone": lead["phone"],
            "credit_charged": False,
        }

    # Verify the org has an enrichment credit available before spending one
    allowed, usage_info = check_enrichment_allowed(db, org_id, 1)
    if not allowed:
        raise HTTPException(status_code=429, detail={
            "error": "enrichment_limit_reached",
            "message": f"Monthly enrichment limit reached ({usage_info['used']}/{usage_info['limit']}). Resets on {usage_info['resets_on']}.",
            "usage": usage_info,
        })

    phone = _apollo_reveal_phone(
        first_name=lead.get("first_name"),
        last_name=lead.get("last_name"),
        linkedin_url=lead.get("linkedin_url"),
        company_domain=lead.get("company_domain"),
        company_name=lead.get("company_name"),
        apollo_id=lead.get("apollo_id"),
    )

    if not phone:
        # Apollo didn't find a phone — no credit charged, customer-friendly default
        log_activity(db, org_id, lead_id, "phone_reveal_attempted", {
            "result": "not_found",
            "credit_charged": False,
        })
        return {
            "status": "not_found",
            "phone": None,
            "credit_charged": False,
        }

    # Persist + charge the credit
    db.table("leads").update({"phone": phone}).eq("id", lead_id).execute()
    record_enrichment(db, org_id, 1)
    log_activity(db, org_id, lead_id, "phone_revealed", {
        "source": "apollo",
        "credit_charged": True,
    })

    return {
        "status": "revealed",
        "phone": phone,
        "credit_charged": True,
    }


# ---------------------------------------------------------------------------
# Multithreading: find other decision-makers at the same company
# ---------------------------------------------------------------------------
# Two-step flow:
#   1. POST /{lead_id}/find-others-at-company  → returns preview candidates
#      (no DB write). Apollo people-search is free; we just look.
#   2. POST /{lead_id}/multithread-add  → takes selected candidates and
#      stores them as suggested leads tagged
#      `suggestion_reason='multithread_at_<company>'` so the dashboard can
#      surface them as "you found these new contacts at companies you've
#      already engaged with."


class FindOthersRequest(BaseModel):
    org_id: UUID


class MultithreadAddRequest(BaseModel):
    org_id: UUID
    candidates: List[Dict[str, Any]] = Field(default_factory=list)


@router.post("/{lead_id}/find-others-at-company")
def find_others_at_company_endpoint(lead_id: str, request: FindOthersRequest):
    """Preview other ICP-fit decision-makers at the same company as this
    lead. No DB write — Apollo people-search consumes no credits.

    Returns all candidates (up to the search limit) after filtering out
    anyone already in the pool at this domain. The user picks which to
    add via /multithread-add.
    """
    db = get_supabase()
    org_id = str(request.org_id)

    lead_resp = (
        db.table("leads")
        .select("id, first_name, last_name, title, company_domain, company_name")
        .eq("id", lead_id)
        .eq("org_id", org_id)
        .limit(1)
        .execute()
    )
    if not lead_resp.data:
        raise HTTPException(status_code=404, detail="Lead not found")
    lead = lead_resp.data[0]

    company_domain = lead.get("company_domain") or ""
    if not company_domain:
        raise HTTPException(
            status_code=400,
            detail="Lead has no company_domain — can't search for colleagues at this company.",
        )

    from app.services.multithread import find_others_at_company

    full_name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip()
    candidates = find_others_at_company(
        db=db,
        org_id=org_id,
        company_domain=company_domain,
        company_name=lead.get("company_name") or "",
        source_lead_title=lead.get("title") or "",
        exclude_names=[full_name] if full_name else None,
        limit=10,
    )

    return {
        "company_name": lead.get("company_name") or "",
        "company_domain": company_domain,
        "candidates": candidates,
        "total_found": len(candidates),
    }


def _reveal_candidate_to_lead(
    db,
    org_id: str,
    *,
    background_tasks: BackgroundTasks,
    apollo_id: str,
    first_name: str,
    last_name: str,
    title: str,
    linkedin_url: str,
    company_domain: str,
    company_name: str,
) -> Optional[str]:
    """Reveal one colleague (from the free find-others search) into an
    actionable person_unlock lead, mirroring the People-tab unlock: Apollo
    people-match reveal → create/dedupe the lead → record the unlock →
    fire background enrichment. Returns the lead_id, or None if the reveal
    failed. The caller enforces the unlock cap BEFORE calling (it slices the
    candidate list to the remaining allowance), so this never spends past it.
    """
    match = _apollo_people_match(
        first_name=first_name,
        last_name=last_name,
        linkedin_url=linkedin_url,
        company_domain=company_domain,
        company_name=company_name,
        apollo_id=apollo_id or "",
    )
    if not match:
        return None

    email = (match.get("email") or "").strip()
    domain = (company_domain or match.get("company_domain") or "").lower()

    # Dedupe by revealed email — reuse an existing lead rather than duplicate.
    existing_lead_id: Optional[str] = None
    if email:
        try:
            ex = (
                db.table("leads").select("id")
                .eq("org_id", org_id).eq("email", email).limit(1).execute()
            )
            if ex.data:
                existing_lead_id = ex.data[0]["id"]
        except Exception as e:  # noqa: BLE001
            logger.warning("find-others reveal: dedupe lookup failed: %s", e)

    if existing_lead_id:
        lead_id = existing_lead_id
        try:
            db.table("leads").update({
                "source": "person_unlock",
                "first_name": match.get("first_name") or first_name,
                "last_name": match.get("last_name") or last_name,
                "photo_url": match.get("photo_url") or "",
            }).eq("id", lead_id).execute()
        except Exception as e:  # noqa: BLE001
            logger.warning("find-others reveal: mark deduped lead failed: %s", e)
    else:
        lead_row = {
            "org_id": org_id,
            "email": email,
            "first_name": match.get("first_name") or first_name,
            "last_name": match.get("last_name") or last_name,
            "title": match.get("title") or title or "",
            "company_name": match.get("company_name") or company_name or "",
            "company_domain": domain,
            "company_size": match.get("company_size") or "",
            "company_industry": match.get("company_industry") or "",
            "company_location": match.get("company_location") or "",
            "linkedin_url": match.get("linkedin_url") or linkedin_url or "",
            "photo_url": match.get("photo_url") or "",
            "phone": match.get("phone") or "",
            "source": "person_unlock",
            "status": "new",
        }
        try:
            inserted = db.table("leads").insert(lead_row).execute()
            lead_id = inserted.data[0]["id"]
        except Exception as e:  # noqa: BLE001
            logger.error("find-others reveal: lead create failed: %s", e)
            return None
        # Record the unlock (no-op charge for capped Solo orgs — their cap is
        # counted from the person_unlock lead we just created).
        from app.services import credits as _credits
        _credits.charge(
            db, org_id=org_id, action="person_unlock", related_id=apollo_id or None,
            metadata={"domain": domain, "source": "multithread"},
        )

    # Per-org unlock record (revealed identity + lead link). Lazy import
    # avoids the account_suggestions <-> leads import cycle.
    try:
        from app.routers.account_suggestions import _upsert_people_cache
        _upsert_people_cache(
            db, org_id, apollo_id, domain,
            {
                "apollo_id": apollo_id,
                "first_name": match.get("first_name") or first_name,
                "last_name": match.get("last_name") or last_name,
                "title": match.get("title") or title or "",
                "linkedin_url": match.get("linkedin_url") or linkedin_url or "",
                "email": email or None,
                "phone_numbers": [{"raw_number": match["phone"]}] if match.get("phone") else None,
            },
            lead_id=lead_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("find-others reveal: people_cache upsert failed: %s", e)

    # New leads get the full enrichment pass; deduped ones were already enriched.
    if not existing_lead_id:
        from app.jobs import enqueue_enrichment
        enqueue_enrichment(
            background_tasks, db, org_id, [lead_id],
            ["website", "tech_stack", "news", "jobs"], "cyber_company_gtm",
        )
    return lead_id


@router.post("/{lead_id}/multithread-add")
def multithread_add(
    lead_id: str, request: MultithreadAddRequest, background_tasks: BackgroundTasks,
):
    """Reveal + research the selected colleagues and return where to navigate.

    Each reveal is a contact unlock (deducted from the org's monthly cap),
    exactly like the People-tab unlock — Apollo match → person_unlock lead →
    background enrichment. The rep lands on the first revealed contact; the
    rest become real researched prospects. Reveals only up to the remaining
    unlock allowance and reports when the cap stopped it, so we never spend
    past the cap.
    """
    db = get_supabase()
    org_id = str(request.org_id)

    lead_resp = (
        db.table("leads")
        .select("id, company_name, company_domain")
        .eq("id", lead_id)
        .eq("org_id", org_id)
        .limit(1)
        .execute()
    )
    if not lead_resp.data:
        raise HTTPException(status_code=404, detail="Lead not found")
    src = lead_resp.data[0]
    company_domain = (src.get("company_domain") or "").lower()
    company_name = src.get("company_name") or ""

    candidates = request.candidates or []
    if not candidates:
        raise HTTPException(status_code=400, detail="No candidates provided")

    # Reveal at most the remaining monthly unlock allowance (uncapped orgs
    # are unlimited). Checked up front so we never spend past the cap.
    from app.services.plan_limits import _org_is_capped, current_usage, effective_limits
    remaining: Optional[int] = None
    if _org_is_capped(org_id, db):
        cap = effective_limits(org_id, db)["unlocks_per_month"]
        used = current_usage(org_id, db)["unlocks_this_month"]
        remaining = max(0, cap - used)

    to_reveal = candidates if remaining is None else candidates[:remaining]
    cap_reached = remaining is not None and len(candidates) > len(to_reveal)

    lead_ids: List[str] = []
    failed = 0
    for c in to_reveal:
        lid = _reveal_candidate_to_lead(
            db, org_id, background_tasks=background_tasks,
            apollo_id=str(c.get("apollo_id") or ""),
            first_name=c.get("first_name") or "",
            last_name=c.get("last_name") or "",
            title=c.get("title") or "",
            linkedin_url=c.get("linkedin_url") or "",
            company_domain=company_domain or (c.get("company_domain") or "").lower(),
            company_name=company_name or (c.get("company_name") or ""),
        )
        if lid:
            lead_ids.append(lid)
        else:
            failed += 1

    if lead_ids:
        from app.services.multithread import log_activity
        log_activity(db, org_id, lead_id, "multithread_contacts_revealed", {
            "count": len(lead_ids), "company": company_name, "lead_ids": lead_ids,
        })

    return {
        "revealed": len(lead_ids),
        "lead_ids": lead_ids,
        "primary_lead_id": lead_ids[0] if lead_ids else None,
        "failed": failed,
        "cap_reached": cap_reached,
        "requested": len(candidates),
    }


# ---------------------------------------------------------------------------
# Pre-enrichment ICP gate
# ---------------------------------------------------------------------------
# Cheap string-match check that runs after Apollo people-match (so we have
# industry data) but before the expensive run_full_enrichment + score_leads
# stages. Saves ~$0.18-0.69 per disqualified lead in PredictLeads + Tavily +
# deep_research + Anthropic + Findymail calls that would otherwise burn for
# a prospect who's clearly not in the org's ICP.
#
# Three-state result:
#   "fit"       — proceed with full enrichment + scoring
#   "miss"      — clear ICP miss, create lead as disqualified, skip downstream
#   "ambiguous" — unknown, default to fit (give benefit of doubt — better to
#                 over-enrich than wrongly reject a real prospect)


import re as _icp_re

# Tokens dropped when deriving keywords from target_industries — too generic
# to be a useful match. Without this, "computer software" would match every
# software company in the world for any client whose target_industries
# happens to mention "software" anywhere.
_ICP_STOPWORDS = {
    "and", "or", "the", "a", "of",
    "computer", "computers", "software", "services", "service",
    "technology", "technologies", "tech",
    "internet", "online", "digital", "general", "company",
    "companies", "platform", "platforms", "industry", "industries",
    "solutions", "products", "consulting", "consultancy",
}


def _icp_needle_set(icp_config: Dict[str, Any]) -> set:
    """Build the set of tokens that count as ICP-fit signal for this org.

    Combines:
      - Each target_industry phrase (full string)
      - Each significant word inside a target_industry phrase, after dropping
        generic stopwords. Catches "computer & network security" via
        "security" when Vera's target list has "information security",
        "managed security", etc.
      - Each target_keyword (treated as-is, no stopword filtering — a client
        who explicitly lists "software" as a target keyword should match it)
    """
    needles: set = set()
    for ti in (icp_config.get("target_industries") or []):
        if not isinstance(ti, str):
            continue
        ti_low = ti.lower().strip()
        if ti_low:
            needles.add(ti_low)
        for word in _icp_re.split(r"[\W_]+", ti_low):
            if len(word) >= 4 and word not in _ICP_STOPWORDS:
                needles.add(word)
    for kw in (icp_config.get("target_keywords") or []):
        if isinstance(kw, str) and kw.strip():
            needles.add(kw.lower().strip())
    return needles


def _build_icp_haystack(
    lead_fields: Dict[str, str],
    profile: Optional[Dict[str, Any]] = None,
) -> str:
    """Combine every free signal we have about the prospect's company into
    one lowercase string the needle set can be tested against.

    The LinkedIn profile (already scraped at Step 1) usually contains enough
    context to classify even when Apollo's industry field is null. The
    headline, currentPosition, about, and first-experience-description fields
    are all reliable signal sources for the AI-vs-cyber, fintech-vs-marketing
    distinctions the gate makes.
    """
    parts: List[str] = [
        (lead_fields.get("company_industry") or "").lower(),
        (lead_fields.get("company_name") or "").lower(),
        (lead_fields.get("title") or "").lower(),
        # Apollo's company description + keywords — single highest-signal
        # source for what the company actually does (e.g. an AI startup's
        # "AI-powered enterprise resource planning platform" lives here, not
        # in the structured `industry` field).
        (lead_fields.get("company_description") or "").lower(),
    ]

    if isinstance(profile, dict):
        for key in ("headline", "about", "summary", "description"):
            v = profile.get(key)
            if isinstance(v, str) and v:
                parts.append(v.lower())

        cp = profile.get("currentPosition") or {}
        if isinstance(cp, dict):
            parts.append((cp.get("position") or "").lower())
            parts.append((cp.get("companyName") or "").lower())
            parts.append((cp.get("description") or "").lower())

        # First experience entry — companies usually describe themselves there
        for key in ("experience", "experiences", "positions", "workExperience"):
            exp_list = profile.get(key)
            if isinstance(exp_list, list) and exp_list:
                first = exp_list[0]
                if isinstance(first, dict):
                    parts.append((first.get("description") or "").lower())
                    parts.append((first.get("companyName") or first.get("company") or "").lower())
                    parts.append((first.get("position") or first.get("title") or "").lower())
                break

    return " ".join(p for p in parts if p).strip()


def _classify_icp_via_llm(
    lead_fields: Dict[str, str],
    icp_config: Dict[str, Any],
    client_context: Dict[str, Any],
    profile: Optional[Dict[str, Any]] = None,
) -> Optional[tuple[str, str]]:
    """LLM ICP classifier — only called as a fallback when the deterministic
    check returns "miss". Goal: rescue false negatives (e.g. a founder at an
    early-stage AI startup, where the LinkedIn data was sparse and no needle token matched
    even though the company is genuinely an AI/SaaS startup that fits).

    Uses gpt-4o-mini via OpenRouter — ~$0.0001 per call (50x cheaper than
    Sonnet). The check is binary (fit/miss) so a small model is plenty.

    Returns (decision, reason) or None on error / no API key. Caller decides
    how to handle None — current policy: keep the deterministic decision.
    """
    from app.config import settings  # local import — matches the pattern used by _apollo_people_match
    api_key = settings.OPENROUTER_API_KEY
    if not api_key:
        logger.debug("OPENROUTER_API_KEY not set — skipping LLM ICP rescue")
        return None

    company_name = lead_fields.get("company_name") or "unknown"
    industry = lead_fields.get("company_industry") or "(not provided by Apollo)"
    title = lead_fields.get("title") or ""
    company_description = (lead_fields.get("company_description") or "")[:500]
    company_founded_year = lead_fields.get("company_founded_year") or ""
    company_size = lead_fields.get("company_size") or ""

    headline = ""
    about = ""
    if isinstance(profile, dict):
        headline = (profile.get("headline") or "")[:200]
        about = (profile.get("about") or "")[:400]

    ctx = client_context or {}
    target_industries = icp_config.get("target_industries") or []
    target_keywords = icp_config.get("target_keywords") or []
    what_they_sell = ctx.get("what_they_sell") or ""
    client_name = ctx.get("company_name") or ctx.get("client_name") or "the client"
    icp_guidance = ctx.get("icp_guidance") or ""

    # Few-shot examples drawn from the org's own client_context. These are
    # the highest-signal teach-by-example data we have — a human wrote
    # explicit "yes" and "no" cases. Pass them verbatim.
    def _fmt_list(value, prefix="  - "):
        if isinstance(value, list):
            return "\n".join(f"{prefix}{item}" for item in value if item)
        if isinstance(value, str) and value.strip():
            return f"{prefix}{value}"
        return f"{prefix}(none specified)"

    yes_examples = _fmt_list(ctx.get("icp_examples_yes"))
    no_examples = _fmt_list(ctx.get("icp_examples_no"))
    disqualify = _fmt_list(ctx.get("disqualify_signals"))
    pain_points = _fmt_list(ctx.get("pain_points_they_solve"))

    prompt = f"""You are an ICP filter for a B2B sales engine. Decide whether this prospect's COMPANY operates in {client_name}'s target market.

## What {client_name} sells
{what_they_sell or "(not specified)"}

## Pain points {client_name} solves
{pain_points}

## ICP guidance
{icp_guidance or "(none specified)"}

## Target industries (the COMPANY's industry must overlap one of these)
{', '.join(target_industries) if target_industries else "(not configured)"}

## Target keywords
{', '.join(target_keywords) if target_keywords else "(not configured)"}

## Disqualify signals (apply only when the COMPANY clearly matches)
{disqualify}

## Reference YES examples (similar COMPANY profiles)
{yes_examples}

## Reference NO examples (similar COMPANY profiles)
{no_examples}

## Prospect to classify

- Company: {company_name}
- Apollo industry: {industry}
- Apollo company description: {company_description or "(empty)"}
- Apollo founded year: {company_founded_year or "(unknown)"}
- Apollo employee count: {company_size or "(unknown)"}
- Title: {title}
- LinkedIn headline: {headline or "(empty)"}
- LinkedIn about: {about or "(empty)"}

## Decision rules — read carefully

1. CRITICAL: All examples and disqualifiers describe the prospect's COMPANY, not their TITLE. Do NOT match on title alone — "Co-Founder" matches both a yes-example and a no-example, what matters is the COMPANY's actual business.

2. The strongest signal is the prospect's company's INDUSTRY relative to the target industries:
   - If the company's industry/business clearly falls within the target industries (or a close variant) → "fit"
   - If the company's industry/business clearly falls OUTSIDE the target industries with no overlap → "miss"

3. Disqualifier matches require explicit evidence about the COMPANY (not the prospect's title). Example: "Company IS a cybersecurity vendor" only applies when the company actually sells cybersecurity products — not when the prospect happens to have "Co-Founder" in their title.

4. When the available data is sparse and you can't confidently determine the company's industry:
   - If the client's target_industries are NARROW (e.g. only "cybersecurity / MSSP / threat intel"), default to "miss" — narrow targets need positive evidence of fit.
   - If the client's target_industries are BROAD (e.g. "SaaS / fintech / healthcare / AI / e-commerce"), default to "fit" — broad targets only miss when there's positive evidence of misfit.
   The list above is your guide to which side {client_name} sits on.

5. Use your knowledge of well-known companies. If you know the company by name, apply that knowledge. Never invent attributes you can't verify from the input data plus your background knowledge.

## Output

Reply with valid JSON only:
{{"decision": "fit"|"miss", "reason": "<one short sentence stating the COMPANY's actual industry/business and why it does or does not match>"}}"""

    try:
        response = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 120,
            },
            timeout=15,
        )
        response.raise_for_status()
        body = response.json()
        raw = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not raw:
            return None

        # Strip markdown fencing if present, then parse JSON
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = _icp_re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
            cleaned = _icp_re.sub(r"\n?```\s*$", "", cleaned)
        match = _icp_re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            cleaned = match.group()

        parsed = json.loads(cleaned)
        decision = (parsed.get("decision") or "").strip().lower()
        reason = (parsed.get("reason") or "")[:200]
        if decision in ("fit", "miss"):
            return decision, reason
        return None
    except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as e:
        logger.warning("LLM ICP rescue failed for %s: %s — keeping deterministic decision", company_name, e)
        return None


def _check_lead_in_icp(
    lead_fields: Dict[str, str],
    icp_config: Dict[str, Any],
    profile: Optional[Dict[str, Any]] = None,
) -> tuple[str, str]:
    """Return (decision, reason).

    Decision is one of "fit" / "miss" / "ambiguous":
      - fit       — proceed with full enrichment + scoring
      - miss      — clearly out of ICP, skip the spend, mark cold/disqualified
      - ambiguous — org has no ICP configured, or we have literally no info
                    about the company (rare); default to enrich

    `profile` is the raw LinkedIn profile dict from scrape_linkedin_profile().
    When provided, its headline / about / experience descriptions are
    appended to the haystack so the gate can classify even when Apollo's
    industry field is empty.
    """
    needles = _icp_needle_set(icp_config)
    if not needles:
        return "ambiguous", "ICP not configured (no target_industries or target_keywords)"

    haystack = _build_icp_haystack(lead_fields, profile)
    if not haystack:
        return "ambiguous", "no company info available to check"

    for n in needles:
        if n and n in haystack:
            return "fit", f"matched '{n}' in haystack"

    # Org has needles configured AND we have a non-empty haystack but nothing
    # matched. Confident miss — even with LinkedIn data in the haystack, the
    # company description doesn't reference anything in the org's ICP.
    industry = (lead_fields.get("company_industry") or "").strip()
    company_name = (lead_fields.get("company_name") or "").strip()
    return "miss", (
        f"industry '{industry or 'unknown'}' / name '{company_name}' / LinkedIn context "
        f"contains no ICP needle for this client"
    )


class FreeReportRequest(BaseModel):
    domain: str = Field(..., min_length=1, max_length=253)
    email: Optional[str] = None
    send: bool = False  # when true + email present, also email the report


def _safe_report_domain(raw: str) -> Optional[str]:
    """Normalise + SSRF-screen the public free-report domain (defence in depth;
    the website route screens too). Rejects URLs/paths, raw IPs, and
    internal/rebind hostnames before any enrichment fetch runs."""
    import re

    d = (raw or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    d = d.split("/")[0].strip()
    if not d or len(d) > 253:
        return None
    if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", d):
        return None
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", d):  # no raw IPs
        return None
    blocked = (".internal", ".local", ".consul", ".cluster.local", ".lan", ".home.arpa")
    rebind = ("nip.io", "sslip.io", "xip.io")
    if d.endswith(blocked) or any(d == r or d.endswith("." + r) for r in rebind):
        return None
    return d


def _build_and_send_free_report(domain: str, email: str) -> None:
    """Assemble the full report and email it. Runs off the request path.

    The deep assembly (Certificate Transparency, posture probes, Apollo
    security-team search, perimeter scan) takes minutes on a large estate,
    which is why the site no longer waits on it.
    """
    db = get_supabase()
    from app.services.company_enricher import enrich_company_deep
    from app.services.email_sender import send_free_report_email

    try:
        report = enrich_company_deep(db, domain)
    except Exception as e:
        logger.warning("free-report deep enrich failed for %s: %s", domain, e)
        return
    try:
        send_free_report_email(to_email=email, report=report)
    except Exception as e:
        logger.warning("free-report email send failed for %s: %s", email, e)


@router.post("/free-report", dependencies=[Depends(hosted_only)])
def free_report(req: FreeReportRequest, background_tasks: BackgroundTasks):
    """Public lead-magnet enrichment (company-only — NO contacts/people/emails).

    Called SERVER-SIDE by the website's /api/free-report route (with the shared
    key) AFTER it has run Turnstile + the per-IP / daily-cap controls. Does the
    cheap lite enrich (cache-first), upserts the global companies row, and optionally emails the report. No org context, no credit charge.

    With `send`, the emailed report is the DEEP one (adds the security
    overview) and is built in the background — the caller gets an immediate
    ack rather than holding a connection open for minutes.
    """
    db = get_supabase()
    from app.services.company_enricher import enrich_company_lite

    domain = _safe_report_domain(req.domain)
    if not domain:
        raise HTTPException(status_code=422, detail="Invalid domain.")

    if req.send and req.email:
        email = req.email.strip()
        # Enrol the lead first: capture survives a failed assembly. One row per
        # email; a repeat searcher updates their company but keeps their place
        # in the sequence.
        try:
            db.table("free_report_leads").upsert(
                {"email": email.lower(), "domain": domain},
                on_conflict="email",
            ).execute()
        except Exception as e:
            logger.warning("free-report lead upsert failed for %s: %s", email, e)
        background_tasks.add_task(_build_and_send_free_report, domain, email)
        return {"domain": domain, "queued": True}

    try:
        report = enrich_company_lite(db, domain)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return report


@router.post("/quick-enrich")
def quick_enrich(request: QuickEnrichRequest, background_tasks: BackgroundTasks):
    """Enrich a single lead from a LinkedIn URL OR a company domain.

    Dispatch rules:
    - `linkedin_url` present → existing person+company flow.
      After lead enrichment, if the resolved company has no canonical
      `companies` row (or one with no enrichment data), kicks the
      company-only helper synchronously so the dashboard shows a
      populated dossier on first visit.
    - `domain` present (and no linkedin_url) → company-only flow.
      Runs PredictLeads bundle, Apollo orgs/enrich, Perplexity deep
      research and tech stack detection; upserts `companies` and
      ensures a watchlist row exists for this org.

    User-provided fields override LinkedIn-scraped data (legacy path).
    """
    db = get_supabase()
    org_id = str(request.org_id)

    # -----------------------------------------------------------------
    # Domain-only path — no LinkedIn URL, the rep pasted a company URL
    # into the research bar. Run comprehensive company enrichment and
    # return; no lead is created.
    # -----------------------------------------------------------------
    if not request.linkedin_url and (request.domain or request.company_domain):
        from app.services.company_enricher import (
            enrich_company_only,
            ensure_watchlist_row,
        )

        target_domain = (request.domain or request.company_domain or "").strip().lower()
        if not target_domain:
            raise HTTPException(
                status_code=422,
                detail="Either linkedin_url or domain must be provided",
            )

        # Ensure the org exists before spending any enrichment budget on
        # a domain we can't attribute.
        org_resp = db.table("organisations").select("id").eq("id", org_id).execute()
        if not org_resp.data:
            raise HTTPException(status_code=404, detail=f"Organisation {org_id} not found")

        # Solo account cap — block a genuinely NEW account at the limit
        # BEFORE we spend any enrichment budget. Re-enriching an account
        # they already have is always allowed; no-op for non-Solo orgs.
        existing_wl = (
            db.table("watchlist").select("id")
            .eq("org_id", org_id).eq("company_domain", target_domain)
            .eq("status", "active").limit(1).execute()
        )
        if not existing_wl.data:
            from app.services.plan_limits import enforce_account_cap
            enforce_account_cap(org_id, db)

        result = enrich_company_only(
            db,
            org_id,
            target_domain,
            company_name_hint=request.company_name,
        )

        # Quick-enrich is a deep enrichment the rep explicitly asked for.
        from app.services import credits as _credits
        _credits.charge(
            db, org_id=org_id, action="deep_enrich",
            related_id=result.get("company_id"),
            metadata={"domain": target_domain, "source": "quick_enrich_domain"},
        )

        company = result.get("company") or {}
        ensure_watchlist_row(db, org_id, target_domain, company_name=company.get("name"))

        # Fire a one-off background monitor pass so social signals, person
        # intel and the WTM paragraph populate within ~60-120s of the paste
        # instead of waiting for the next daily watchlist cron. Gated on
        # accounts_dashboard so lead-first tenants keep their old behaviour.
        icp_for_gate = (
            db.table("organisations")
            .select("icp_config")
            .eq("id", org_id)
            .limit(1)
            .execute()
        )
        accounts_dashboard_on = bool(
            ((icp_for_gate.data or [{}])[0].get("icp_config") or {})
            .get("features", {}).get("accounts_dashboard")
        )
        if accounts_dashboard_on:
            from app.jobs import enqueue_monitor_pass
            enqueue_monitor_pass(org_id, target_domain, background_tasks)

        log_activity(db, org_id, None, "company_quick_enriched", {
            "domain": target_domain,
            "sources_completed": result.get("sources_completed", []),
            "source": "domain_url",
        })

        return {
            "domain": target_domain,
            "company_id": result.get("company_id"),
            "company": company,
            "sources_completed": result.get("sources_completed", []),
            "status": "company_enriched",
        }

    if not request.linkedin_url:
        raise HTTPException(
            status_code=422,
            detail="Either linkedin_url or domain must be provided",
        )

    linkedin_url = request.linkedin_url.strip()

    # Fetch the org's ICP config + client context up front. Both are used
    # for the pre-enrichment ICP gate (below). The scorer fetches them
    # again later — accepting a tiny duplicate read to keep the surface
    # area minimal.
    org_resp = (
        db.table("organisations")
        .select("icp_config, client_context")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    if not org_resp.data:
        raise HTTPException(status_code=404, detail=f"Organisation {org_id} not found")
    icp_config = org_resp.data[0].get("icp_config") or {}
    org_client_context = org_resp.data[0].get("client_context") or {}

    # Step 1: Scrape LinkedIn profile (for verification + missing fields)
    profile = scrape_linkedin_profile(linkedin_url)
    scraped_fields: Dict[str, str] = {}

    if profile:
        scraped_fields = _extract_lead_from_profile(profile, linkedin_url)
        logger.info("LinkedIn scrape succeeded for %s", linkedin_url)
    else:
        logger.warning("LinkedIn scrape failed for %s — using user-provided fields", linkedin_url)

    # Step 2: Merge — user-provided fields override scraped data
    lead_fields = {
        "first_name": request.first_name or scraped_fields.get("first_name", ""),
        "last_name": request.last_name or scraped_fields.get("last_name", ""),
        "title": request.title or scraped_fields.get("title", ""),
        "company_name": request.company_name or scraped_fields.get("company_name", ""),
        "company_domain": request.company_domain or scraped_fields.get("company_domain", ""),
        "company_industry": scraped_fields.get("company_industry", ""),
        "company_size": scraped_fields.get("company_size", ""),
        "company_location": scraped_fields.get("company_location", ""),
        "company_description": "",
        "company_founded_year": "",
        "email": request.email or "",
        "linkedin_url": linkedin_url,
    }

    # If still no domain, resolve via Perplexity Sonar → Claude → naive guess
    if not lead_fields["company_domain"] and lead_fields["company_name"]:
        lead_fields["company_domain"] = resolve_company_domain(lead_fields["company_name"])

    # Must have at least a name (from any source)
    if not lead_fields["first_name"] and not lead_fields["last_name"]:
        raise HTTPException(
            status_code=422,
            detail="Could not determine lead name — LinkedIn scrape returned no name and none was provided",
        )

    # Step 2b: Apollo people match — find email and fill gaps
    if not lead_fields["email"] and lead_fields["first_name"] and lead_fields["last_name"]:
        apollo_match = _apollo_people_match(
            first_name=lead_fields["first_name"],
            last_name=lead_fields["last_name"],
            linkedin_url=linkedin_url,
            company_domain=lead_fields["company_domain"],
            company_name=lead_fields["company_name"],
        )
        if apollo_match:
            if apollo_match.get("email"):
                lead_fields["email"] = apollo_match["email"]
            if not lead_fields["title"] and apollo_match.get("title"):
                lead_fields["title"] = apollo_match["title"]
            if not lead_fields["company_name"] and apollo_match.get("company_name"):
                lead_fields["company_name"] = apollo_match["company_name"]
            if not lead_fields["company_domain"] and apollo_match.get("company_domain"):
                lead_fields["company_domain"] = apollo_match["company_domain"]
            # Pull firmographics — these were never being read from Apollo,
            # which is why compliance_gap couldn't fire on Bank of England
            # (no industry → no regulated-industry signal).
            if not lead_fields["company_industry"] and apollo_match.get("company_industry"):
                lead_fields["company_industry"] = apollo_match["company_industry"]
            if not lead_fields["company_size"] and apollo_match.get("company_size"):
                lead_fields["company_size"] = apollo_match["company_size"]
            if not lead_fields["company_location"] and apollo_match.get("company_location"):
                lead_fields["company_location"] = apollo_match["company_location"]
            # Description + founded year aren't persisted on the leads table
            # but flow through `lead_fields` into the ICP gate haystack and
            # the LLM rescue prompt, which is where they earn their keep.
            if apollo_match.get("company_description"):
                lead_fields["company_description"] = apollo_match["company_description"]
            if apollo_match.get("company_founded_year"):
                lead_fields["company_founded_year"] = apollo_match["company_founded_year"]

    # Step 2c: Pre-enrichment ICP gate — skip the full-enrichment spend on
    # leads that are clearly out of ICP. We've already paid for the LinkedIn
    # scrape + Apollo lookup at this point; what we save is PredictLeads (6
    # endpoints), Tavily, deep_research, Apify posts/jobs, Anthropic, and
    # Findymail — roughly $0.18–0.69 per skipped lead.
    # We pass the raw LinkedIn `profile` so the gate has access to headline /
    # about / experience descriptions — Apollo's industry field is null for
    # ~30% of B2B contacts and the LinkedIn context catches those cases.
    icp_decision, icp_reason = _check_lead_in_icp(lead_fields, icp_config, profile=profile)

    # LLM rescue: when the deterministic check says "miss" — which can
    # happen on sparse LinkedIn data (e.g. headline is just "Co-Founder
    # at <startup>" with no description) — give a small model a chance to
    # see it differently. ~$0.0001 per call via gpt-4o-mini, only fires
    # on misses. If LLM agrees, the miss stands. If LLM says fit, we
    # proceed with enrichment instead of wrongly rejecting a real prospect.
    if icp_decision == "miss":
        llm_result = _classify_icp_via_llm(
            lead_fields, icp_config, org_client_context, profile=profile,
        )
        if llm_result is not None:
            llm_decision, llm_reason = llm_result
            if llm_decision == "fit":
                logger.info(
                    "ICP gate LLM rescue for %s @ %s: deterministic=miss → llm=fit — %s",
                    lead_fields["first_name"], lead_fields["company_name"], llm_reason,
                )
                icp_decision = "fit"
                icp_reason = f"LLM rescue: {llm_reason}"
            else:
                # LLM confirmed miss — strengthen the reason
                icp_reason = f"{icp_reason} | LLM confirmed: {llm_reason}"

    logger.info(
        "ICP gate for %s %s @ %s: %s — %s",
        lead_fields["first_name"], lead_fields["last_name"],
        lead_fields["company_name"], icp_decision, icp_reason,
    )

    # Quick-enrich is the human-paste path — a rep deliberately dropped this
    # LinkedIn URL into the dashboard, so they've already done a sanity-check
    # on whether this lead is worth pursuing. Edge-cases like name collisions
    # (e.g. a startup name → wrong domain via Perplexity, no Apollo match, sparse
    # LinkedIn) cause the gate to flag a real prospect as miss. Trust the rep:
    # downgrade miss → ambiguous so we still enrich + score, but keep the
    # gate's reasoning on the lead so the rep knows we weren't 100% sure.
    # NOTE: Apollo-discovery paths use a different endpoint and DO enforce the
    # gate strictly, because Apollo gives clean firmographics there.
    icp_gate_was_miss = (icp_decision == "miss")
    if icp_gate_was_miss:
        logger.info(
            "Quick-enrich: ICP gate said miss for %s @ %s — overriding to enrich (human pasted URL)",
            lead_fields["first_name"], lead_fields["company_name"],
        )
        icp_decision = "ambiguous"

    # Step 3: Check for duplicate by linkedin_url or email
    existing = (
        db.table("leads")
        .select("id")
        .eq("org_id", org_id)
        .eq("linkedin_url", linkedin_url)
        .execute()
    )
    if not existing.data and lead_fields["email"]:
        existing = (
            db.table("leads")
            .select("id")
            .eq("org_id", org_id)
            .eq("email", lead_fields["email"])
            .execute()
        )

    if existing.data:
        lead_id = existing.data[0]["id"]
        lead_result = db.table("leads").select("*").eq("id", lead_id).execute()
        return {"lead_id": lead_id, "lead": lead_result.data[0], "status": "existing"}

    # Guardrail: check enrichment limit before creating + enriching
    allowed, usage_info = check_enrichment_allowed(db, org_id, 1)
    if not allowed:
        raise HTTPException(status_code=429, detail={
            "error": "enrichment_limit_reached",
            "message": f"Monthly enrichment limit reached ({usage_info['used']}/{usage_info['limit']} used). Resets on {usage_info['resets_on']}.",
            "usage": usage_info,
        })

    # Step 4: Create lead
    # Quick-enrich never blocks on ICP miss (see override above). If the gate
    # flagged miss but we're proceeding anyway, surface that in rating_reason
    # so the rep can see "Vera flagged this but enriched anyway because you
    # pasted the URL". The post-enrichment scorer will overwrite rating_reason
    # with its own assessment once it has full data.
    row = {
        "org_id": org_id,
        "email": lead_fields["email"],
        "first_name": lead_fields["first_name"],
        "last_name": lead_fields["last_name"],
        "title": lead_fields["title"],
        "company_name": lead_fields["company_name"],
        "company_domain": lead_fields["company_domain"],
        "company_industry": lead_fields["company_industry"] or None,
        "company_size": lead_fields["company_size"] or None,
        "company_location": lead_fields["company_location"] or None,
        "linkedin_url": linkedin_url,
        "source": "linkedin_url",
        "status": "new",
        "enrichment_status": None,
        "lead_rating": None,
        "rating_reason": (
            f"ICP gate flagged miss but enriched (manual paste): {icp_reason}"
            if icp_gate_was_miss else None
        ),
    }

    insert_result = db.table("leads").insert(row).execute()
    if not insert_result.data:
        raise HTTPException(status_code=500, detail="Failed to create lead")

    lead_id = insert_result.data[0]["id"]

    log_activity(db, org_id, lead_id, "lead_sourced", {
        "source": "linkedin_url",
        "linkedin_url": linkedin_url,
        "company": lead_fields["company_name"],
        "icp_decision": icp_decision,
        "icp_reason": icp_reason,
    })

    # Step 5: Run enrichment (same sources as /enrichment/full)
    # Pass the LinkedIn profile we scraped at Step 1 so the enricher's
    # Phase 3 verification step doesn't re-scrape (saves one Apify run per call).
    if lead_fields["company_domain"]:
        enrichment_sources = ["website", "tech_stack", "news", "jobs"]
    else:
        enrichment_sources = ["news", "jobs"]

    run_full_enrichment(
        db, org_id, [lead_id], enrichment_sources, "cyber_company_gtm",
        prefetched_profile=profile,
    )

    # Quick-enrich is a deep enrichment the rep explicitly asked for.
    from app.services import credits as _credits
    _credits.charge(
        db, org_id=org_id, action="deep_enrich", related_id=lead_id,
        metadata={"source": "quick_enrich_linkedin"},
    )

    # Step 5b: Company top-up — when the rep pastes a LinkedIn URL, Vera
    # should also enrich the company (2026-05-29). The lead path
    # already side-upserts a thin row via `upsert_company_from_enrichment`,
    # but it skips Perplexity deep_research and the Apollo orgs/enrich
    # business intel. Run the company-only helper to top those up — all
    # underlying sources cache, so the duplicate PL/tech_stack fetches
    # are free. Best-effort; never blocks the lead response.
    #
    # FLAG-GATED: only fires for orgs running the new accounts-first UI
    # (`features.accounts_dashboard`). Lead-first
    # tenants keep their original behaviour — no surprise auto-watch +
    # no extra Perplexity/Apollo spend on their LinkedIn enrichments.
    accounts_dashboard_on = bool(
        (icp_config.get("features") or {}).get("accounts_dashboard")
    )
    if accounts_dashboard_on and lead_fields["company_domain"]:
        try:
            from app.services.company_enricher import (
                enrich_company_only,
                ensure_watchlist_row,
            )
            from app.services.plan_limits import can_add_account
            # Respect the Solo account cap — don't push a brand-new account
            # over the limit from the person path. The person/lead research
            # itself already succeeded and is returned regardless; we just
            # don't auto-add the over-cap account. (no-op for non-Solo)
            if not can_add_account(org_id, lead_fields["company_domain"], db):
                logger.info(
                    "Solo account cap reached — not auto-adding %s as an account from the person path",
                    lead_fields["company_domain"],
                )
            else:
                enrich_company_only(
                    db,
                    org_id,
                    lead_fields["company_domain"],
                    company_name_hint=lead_fields["company_name"],
                )
                # Also surface this account on the Accounts dashboard — without
                # a watchlist row it stays invisible there even though the
                # company is fully enriched.
                ensure_watchlist_row(
                    db, org_id, lead_fields["company_domain"],
                    company_name=lead_fields["company_name"],
                )
                # Same paste-monitor kick as the domain-only path — Phases A-E
                # populate within minutes instead of waiting for tomorrow's cron.
                from app.jobs import enqueue_monitor_pass
                enqueue_monitor_pass(
                    org_id, lead_fields["company_domain"], background_tasks,
                )
        except Exception as e:
            logger.warning(
                "Company top-up after LinkedIn enrichment failed for %s: %s",
                lead_fields["company_domain"], e,
            )

    # Step 6: Score
    score_result = score_leads(db, org_id, [lead_id])

    # Record usage
    record_enrichment(db, org_id, 1)

    # Step 7: Mark as enriched
    db.table("leads").update({"enrichment_status": "enriched"}).eq("id", lead_id).execute()

    # Step 8: Return the fully enriched lead
    final_lead = db.table("leads").select("*").eq("id", lead_id).execute()

    return {
        "lead_id": lead_id,
        "lead": final_lead.data[0] if final_lead.data else {},
        "scoring": score_result.get("results", [{}])[0] if score_result.get("results") else {},
        "status": "created_and_enriched",
    }


# ---------------------------------------------------------------------------
# Suggested leads (unenriched Apollo leads for rep review)
# ---------------------------------------------------------------------------

@router.get("/suggestions")
def get_suggestions(org_id: str):
    """Return suggested leads that haven't been enriched yet."""
    db = get_supabase()

    leads_result = (
        db.table("leads")
        .select(
            "id, first_name, last_name, title, company_name, "
            "company_industry, company_size, company_domain, "
            "linkedin_url, enrichment_status, created_at, data_status"
        )
        .eq("org_id", org_id)
        .eq("enrichment_status", "suggested")
        .order("created_at", desc=True)
        .execute()
    )

    suggestions = leads_result.data or []

    return {
        "suggestions": suggestions,
        "total": len(suggestions),
    }


# ---------------------------------------------------------------------------
# Filter suggestions — pick best prospects from Apollo results via Claude
# ---------------------------------------------------------------------------

# Reuse the industry disqualify list from the scorer
_DISQUALIFY_INDUSTRIES = {
    "events services", "staffing & recruiting", "recruiting",
    "entertainment", "hospitality", "food & beverages",
    "real estate", "education management", "leisure, travel & tourism",
}

# Industries that indicate cybersecurity competitors — used when critical_rules
# mentions that cyber companies are competitors, not prospects.
_CYBER_COMPETITOR_INDUSTRIES = {
    "cybersecurity", "information security", "computer & network security",
    "managed security", "network security", "cyber security",
}


def _check_discovery_icp_filter(lead: LeadInput, client_ctx: Dict) -> Optional[str]:
    """ICP pre-filter for discovery. Returns rejection reason or None to pass.

    Checks:
    1. Standard industry disqualify list
    2. Geography against icp_config.target_locations
    3. Client-specific disqualify_signals from client_context
    4. Critical rules — e.g. filter out cyber companies if they are competitors
    """
    industry = (lead.company_industry or "").lower()

    # Standard industry filter
    if industry in _DISQUALIFY_INDUSTRIES:
        return f"Industry '{lead.company_industry}' is not a target industry."

    # Geography filter — only if the org has target_locations configured AND
    # the lead has reliable location data from Apollo. We compare on country
    # (the most stable level of granularity) using fuzzy matching for the
    # common name variations of UK and US.
    target_locations = client_ctx.get("target_locations") or []
    if target_locations and (lead.company_country or lead.company_location):
        if not _location_matches_targets(lead, target_locations):
            actual = lead.company_country or lead.company_location or "Unknown"
            return f"Location '{actual}' is not in target locations: {', '.join(target_locations)}"

    # Client-specific disqualify signals
    disqualify_signals = client_ctx.get("disqualify_signals", "")
    if disqualify_signals:
        company_lower = (lead.company_name or "").lower()
        lead_text = f"{company_lower} {industry}"
        signals = [s.strip().lower() for s in str(disqualify_signals).split(",") if s.strip()]
        for signal in signals:
            if signal in lead_text:
                return f"Disqualified by client rule: matches disqualify signal '{signal}'."

    # Critical rules — check for cyber competitor industries
    critical_rules = client_ctx.get("critical_rules", "")
    if critical_rules:
        rules_lower = critical_rules.lower()
        # If critical rules mention cybersecurity companies as competitors,
        # filter out leads whose industry is a cyber competitor industry
        is_cyber_competitor_rule = any(
            phrase in rules_lower
            for phrase in ("cybersecurity companies are competitors",
                          "cyber companies are competitors",
                          "security companies are competitors",
                          "do not sell to cybersecurity",
                          "do not sell to cyber companies",
                          "do not prospect cybersecurity",
                          "exclude cybersecurity companies",
                          "exclude cyber companies")
        )
        if is_cyber_competitor_rule and industry in _CYBER_COMPETITOR_INDUSTRIES:
            return f"Filtered by critical rule: '{lead.company_industry}' is a competitor industry."

    return None


# Country name aliases — Apollo, ICP configs, and downstream sources all
# spell countries differently. Group equivalent variants so the filter can
# match them as the same place.
_COUNTRY_ALIASES = {
    "united kingdom": {"united kingdom", "uk", "u.k.", "great britain", "britain", "england", "scotland", "wales", "northern ireland", "gb"},
    "united states": {"united states", "us", "u.s.", "u.s.a.", "usa", "america"},
    "canada": {"canada", "ca"},
    "australia": {"australia", "au"},
    "germany": {"germany", "de", "deutschland"},
    "france": {"france", "fr"},
    "ireland": {"ireland", "ie", "republic of ireland"},
}


def _country_aliases_for(name: str) -> set:
    """Return the alias set that contains *name*, or {name} if no group exists."""
    name_lower = name.strip().lower()
    for canonical, aliases in _COUNTRY_ALIASES.items():
        if name_lower in aliases:
            return aliases
    return {name_lower}


def _location_matches_targets(lead: LeadInput, target_locations: List[str]) -> bool:
    """Return True if the lead's location matches any of the org's target_locations.

    Match is country-level. We accept any alias variant of the canonical
    country name (so "UK" matches "United Kingdom", "USA" matches "United
    States", etc.). Falls back to substring matching on the full
    company_location string when company_country isn't set.
    """
    if not target_locations:
        return True

    # Build the union of all alias sets for the org's targets
    target_aliases: set = set()
    for tl in target_locations:
        if isinstance(tl, str):
            target_aliases |= _country_aliases_for(tl)

    # Prefer the structured country field
    if lead.company_country:
        lead_aliases = _country_aliases_for(lead.company_country)
        if lead_aliases & target_aliases:
            return True

    # Fall back to substring match on the full location string
    if lead.company_location:
        loc_lower = lead.company_location.lower()
        for alias in target_aliases:
            if alias in loc_lower:
                return True

    return False


class FilterSuggestionsRequest(BaseModel):
    org_id: UUID
    lead_ids: List[UUID]
    # Default 5; callers should pass the tier-aware target from
    # SUGGESTION_TARGETS if they want this endpoint to honour the org's tier.
    # The main /discover endpoint uses tier targets automatically.
    top_n: int = DEFAULT_SUGGESTION_TARGET


@router.post("/filter-suggestions")
def filter_suggestions(request: FilterSuggestionsRequest):
    """Pick the best N prospects from ingested suggested leads using ICP pre-filter + Claude."""
    db = get_supabase()
    org_id = str(request.org_id)

    # Fetch org context
    client_context = _get_client_context(db, org_id)

    # Fetch the candidate leads
    lead_ids_str = [str(lid) for lid in request.lead_ids]
    leads_result = (
        db.table("leads")
        .select("id, first_name, last_name, title, company_name, company_industry, company_size, linkedin_url")
        .eq("org_id", org_id)
        .in_("id", lead_ids_str)
        .execute()
    )
    leads = leads_result.data or []

    if not leads:
        return {"selected": [], "filtered_out": 0, "reasoning": "No leads found"}

    # Step 1: ICP pre-filter — remove obvious mismatches
    passed = []
    icp_filtered = 0
    for lead in leads:
        industry = (lead.get("company_industry") or "").lower()
        if industry in _DISQUALIFY_INDUSTRIES:
            # Mark as filtered
            db.table("leads").update({"enrichment_status": "filtered"}).eq("id", lead["id"]).execute()
            icp_filtered += 1
        else:
            passed.append(lead)

    if not passed:
        return {"selected": [], "filtered_out": icp_filtered, "reasoning": "All leads filtered by ICP rules"}

    # If fewer than top_n remain after pre-filter, select them all
    if len(passed) <= request.top_n:
        for lead in passed:
            db.table("leads").update({"enrichment_status": "suggested"}).eq("id", lead["id"]).execute()
        return {
            "selected": [l["id"] for l in passed],
            "filtered_out": icp_filtered,
            "reasoning": f"All {len(passed)} remaining leads selected (fewer than {request.top_n} after ICP filter)",
        }

    # Step 2: Claude picks the best N
    client_name = client_context.get("client_name", "the client")
    what_they_sell = client_context.get("what_they_sell", "") or ", ".join(client_context.get("services_offered", [])) or "N/A"
    product_names = format_product_names(client_context) or "N/A"
    ideal_triggers = client_context.get("ideal_triggers", "") or "N/A"
    disqualify_signals = client_context.get("disqualify_signals", "") or "N/A"
    competitors = format_competitors(client_context) or "N/A"

    lead_list = "\n".join(
        f"- ID: {l['id']} | {l.get('first_name', '')} {l.get('last_name', '')}, "
        f"{l.get('title', '')} at {l.get('company_name', '')} "
        f"(Industry: {l.get('company_industry', 'Unknown')}, Size: {l.get('company_size', 'Unknown')})"
        for l in passed
    )

    prompt = f"""You are Vera, selecting the best prospects for {client_name}.
They sell: {what_they_sell}
Their ideal triggers: {ideal_triggers}
Disqualify if: {disqualify_signals}
Competitors (exclude these): {competitors}

Here are {len(passed)} potential prospects:
{lead_list}

Select the TOP {request.top_n} prospects most likely to need {product_names}. Prioritise:
- Companies in the right size range (10-200 employees)
- Senior decision makers (CEO, Founder, MD, Head of Sales)
- Companies whose industry suggests they'd benefit from {what_they_sell}
- Avoid companies that seem too small (1-2 people) or too large (500+) unless there's a clear reason

Respond ONLY with valid JSON:
{{
    "selected_ids": ["id1", "id2", "id3"],
    "reasoning": "Brief explanation of why these were chosen"
}}"""

    claude_result = call_claude(prompt)

    selected_ids = []
    reasoning = ""

    if "error" not in claude_result:
        selected_ids = claude_result.get("selected_ids", [])
        reasoning = claude_result.get("reasoning", "")
    else:
        # Claude failed — just take the first N
        logger.warning("Claude filtering failed, falling back to first %d leads", request.top_n)
        selected_ids = [l["id"] for l in passed[:request.top_n]]
        reasoning = "Claude filtering failed — selected first available leads"

    # Step 3: Mark selected as "suggested", rest as "filtered"
    selected_set = set(selected_ids)
    for lead in passed:
        if lead["id"] in selected_set:
            db.table("leads").update({"enrichment_status": "suggested"}).eq("id", lead["id"]).execute()
        else:
            db.table("leads").update({"enrichment_status": "filtered"}).eq("id", lead["id"]).execute()

    return {
        "selected": list(selected_ids),
        "filtered_out": icp_filtered + (len(passed) - len(selected_set)),
        "reasoning": reasoning,
    }


# ---------------------------------------------------------------------
# Outcome capture endpoint (used by the dashboard feedback buttons)
# ---------------------------------------------------------------------
class OutcomeRequest(BaseModel):
    """Body of POST /leads/{lead_id}/outcome.

    `outcome` must be one of VALID_OUTCOMES (mirrors the CHECK
    constraint in the schema). Empty / unknown values are rejected
    here instead of failing the insert downstream.
    """
    org_id: str
    outcome: str = Field(..., description="One of VALID_OUTCOMES")
    notes: Optional[str] = None


@router.post("/{lead_id}/outcome")
def record_lead_outcome_endpoint(lead_id: str, request: OutcomeRequest):
    """Record a manual outcome on a lead.

    Hit by the feedback buttons under each briefing card:
      ✓ Booked a meeting → outcome=meeting_booked
      ✗ No reply          → outcome=no_reply
      —  Didn't contact   → outcome=not_contacted

    Snapshots the lead's *current* decision context (scored_signals,
    score_at_time, matched_play) so the future cross-client learning
    layer can correlate which (signal × play) combinations actually
    produced the outcome.
    """
    if request.outcome not in VALID_OUTCOMES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"outcome must be one of {sorted(VALID_OUTCOMES)}; "
                f"got {request.outcome!r}"
            ),
        )

    db = get_supabase()
    lead_result = (
        db.table("leads")
        .select("*")
        .eq("id", lead_id)
        .eq("org_id", request.org_id)
        .limit(1)
        .execute()
    )
    if not lead_result.data:
        raise HTTPException(
            status_code=404,
            detail=f"Lead {lead_id} not found in org {request.org_id}",
        )

    record_from_lead(db, lead_result.data[0], request.outcome)
    return {
        "ok": True,
        "lead_id": lead_id,
        "outcome": request.outcome,
    }
