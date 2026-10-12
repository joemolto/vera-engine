"""Enrichment endpoints — website, linkedin, news, tech stack, full pipeline, re-enrich."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.database import get_supabase
from app.services.usage import check_enrichment_allowed, record_enrichment
from app.models.schemas import (
    FullEnrichRequest,
    JobsEnrichRequest,
    LinkedInContentRequest,
    NewsEnrichRequest,
    TechStackRequest,
    WebsiteEnrichRequest,
)
from app.services.enricher import run_full_enrichment, _build_client_context
from app.services.scorer import score_leads
from app.services.vera_brain import post_enrichment_review
from app.services.enrichers.jobs import enrich_jobs
from app.services.enrichers.linkedin import enrich_linkedin_content
from app.services.enrichers.news import enrich_news
from app.services.enrichers.tech_stack import enrich_tech_stack
from app.services.enrichers.website import enrich_website

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/enrichment", tags=["enrichment"])


@router.post("/website")
def website_enrichment(request: WebsiteEnrichRequest):
    """Scrape company websites and analyse with Claude."""
    db = get_supabase()
    client_context = _build_client_context(db, str(request.org_id))
    results = {}
    for domain in request.domains:
        results[domain] = enrich_website(
            db, domain, request.enrichment_mode,
            client_context=client_context,
        )
    return {"results": results}


@router.post("/linkedin-content")
def linkedin_content_enrichment(request: LinkedInContentRequest):
    """Analyse LinkedIn posts for personalisation hooks."""
    db = get_supabase()

    # Fetch lead context
    lead_result = (
        db.table("leads")
        .select("first_name, last_name, title, company_name")
        .eq("id", str(request.lead_id))
        .eq("org_id", str(request.org_id))
        .execute()
    )
    lead = lead_result.data[0] if lead_result.data else {}

    posts = [p.model_dump() for p in request.recent_posts] if request.recent_posts else None

    # Pass client_context so the analysis is framed around what the org sells.
    client_context = _build_client_context(db, str(request.org_id))

    result = enrich_linkedin_content(
        first_name=lead.get("first_name", ""),
        last_name=lead.get("last_name", ""),
        title=lead.get("title", ""),
        company_name=lead.get("company_name", ""),
        recent_posts=posts,
        client_context=client_context,
    )

    # Store in lead's enrichment_data
    if lead_result.data:
        lead_full = db.table("leads").select("enrichment_data").eq("id", str(request.lead_id)).execute()
        enrichment_data = (lead_full.data[0].get("enrichment_data") or {}) if lead_full.data else {}
        enrichment_data["linkedin_content"] = result
        db.table("leads").update({
            "enrichment_data": enrichment_data,
        }).eq("id", str(request.lead_id)).execute()

    return {"result": result}


@router.post("/news")
def news_enrichment(request: NewsEnrichRequest):
    """Search for recent news about companies/people."""
    db = get_supabase()
    client_context = _build_client_context(db, str(request.org_id))
    result = enrich_news(
        db, search_queries=request.search_queries,
        client_context=client_context,
    )
    return {"result": result}


@router.post("/tech-stack")
def tech_stack_enrichment(request: TechStackRequest):
    """Analyse company tech stacks via Apollo for GTM maturity."""
    db = get_supabase()
    client_context = _build_client_context(db, str(request.org_id))
    results = {}
    for domain in request.domains:
        results[domain] = enrich_tech_stack(db, domain, client_context=client_context)
    return {"results": results}


@router.post("/jobs")
def jobs_enrichment(request: JobsEnrichRequest):
    """Check website, Apollo, and Indeed for open job postings."""
    db = get_supabase()
    client_context = _build_client_context(db, str(request.org_id))
    results = {}
    for domain, company_name in zip(request.domains, request.company_names):
        results[domain] = enrich_jobs(
            db, domain, company_name,
            client_context=client_context,
        )
    return {"results": results}


@router.post("/full")
def full_enrichment(request: FullEnrichRequest):
    """Run full enrichment pipeline + scoring for a batch of leads.

    Enriches all requested sources (including deep research), then
    automatically scores each lead. Returns fully enriched and scored leads.
    """
    db = get_supabase()
    org_id = str(request.org_id)
    lead_ids = [str(lid) for lid in request.lead_ids]

    # Guardrail: check enrichment limit
    allowed, usage_info = check_enrichment_allowed(db, org_id, len(lead_ids))
    if not allowed:
        raise HTTPException(status_code=429, detail={
            "error": "enrichment_limit_reached",
            "message": f"Monthly enrichment limit reached ({usage_info['used']}/{usage_info['limit']} used). Resets on {usage_info['resets_on']}.",
            "usage": usage_info,
        })

    # Step 1: Enrich (all 4 phases including deep research)
    enrichment_results = run_full_enrichment(
        db=db,
        org_id=org_id,
        lead_ids=lead_ids,
        enrichment_sources=request.enrichment_sources,
        enrichment_mode=request.enrichment_mode,
    )

    # Step 2: Score all successfully enriched leads
    enriched_ids = [
        r["lead_id"] for r in enrichment_results
        if "error" not in r
    ]
    failed_results = [r for r in enrichment_results if "error" in r]

    # If ALL leads failed, return an error response so the frontend can detect it
    if not enriched_ids and failed_results:
        errors = [{"lead_id": r.get("lead_id"), "error": r.get("error")} for r in failed_results]
        raise HTTPException(status_code=422, detail={
            "error": "all_leads_failed",
            "message": f"Enrichment failed for all {len(failed_results)} lead(s). Check that the org_id matches the leads.",
            "errors": errors,
        })

    scoring_results = {}
    if enriched_ids:
        score_response = score_leads(db, org_id, enriched_ids)
        for sr in score_response.get("results", []):
            scoring_results[sr["lead_id"]] = sr

    # Record usage
    if enriched_ids:
        record_enrichment(db, org_id, len(enriched_ids))

    # 5 credits per successfully enriched lead (deep_enrich). One ledger
    # row per lead so the credit card in analytics can attribute spend.
    from app.services import credits as _credits
    for lid in enriched_ids:
        _credits.charge(
            db, org_id=org_id, action="deep_enrich", related_id=lid,
        )

    # Merge scoring into enrichment results
    for r in enrichment_results:
        r["scoring"] = scoring_results.get(r.get("lead_id"), {})

    # Step 3: Vera brain reactive review — fast deterministic checks
    brain_actions = []
    for lead_id in enriched_ids:
        try:
            lead_result = db.table("leads").select("*").eq("id", lead_id).execute()
            if lead_result.data:
                actions = post_enrichment_review(db, org_id, lead_id, lead_result.data[0])
                brain_actions.extend(actions)
        except Exception as e:
            logger.warning("Vera brain review failed for lead %s: %s", lead_id, e)

    return {
        "results": enrichment_results,
        "leads_enriched": len(enriched_ids),
        "leads_scored": len(scoring_results),
        "vera_actions": brain_actions,
    }


# ---------------------------------------------------------------------------
# Re-enrich is handled automatically by /enrichment/full.
# When a lead has already been enriched, the pipeline snapshots the old data,
# runs enrichment normally, then compares old vs new and stores changes in
# enrichment_data.changes_since_last. Lovable just calls /enrichment/full
# again — no separate endpoint needed.
# ---------------------------------------------------------------------------


class ReEnrichRequest(BaseModel):
    """Kept for backwards compatibility — Lovable may still call /re-enrich."""
    org_id: UUID
    lead_id: UUID
    enrichment_sources: List[str] = [
        "website", "tech_stack", "news", "jobs"
    ]
    enrichment_mode: str = "cyber_company_gtm"


@router.post("/re-enrich")
def re_enrich(request: ReEnrichRequest):
    """Re-enrich a lead. Delegates to /enrichment/full which now handles
    change detection automatically when a lead has been previously enriched.
    """
    full_request = FullEnrichRequest(
        org_id=request.org_id,
        lead_ids=[request.lead_id],
        enrichment_sources=request.enrichment_sources,
        enrichment_mode=request.enrichment_mode,
    )
    return full_enrichment(full_request)


# Change detection logic is now in app/services/enricher.py (_detect_enrichment_changes)
