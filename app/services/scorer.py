"""Lead scoring service — signal-based traffic light rating with ICP pre-filter."""

import json
import logging
from datetime import datetime, timezone

from supabase import Client

from app.prompts.scoring import SCORING_PROMPT
from app.services.activity import log_activity
from app.services.claude import call_claude
from app.services.client_context import get_client_context, format_competitors, format_product_names
from app.services.gtm_briefing import generate_gtm_briefing
from app.services.llm_responses import ScoringResponse, parse_llm_response
from app.services.persona_matcher import pick_best_persona
from app.services.personas import read_personas
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Backwards-compatible total_score mapping
RATING_SCORE_MAP = {"hot": 90.0, "warm": 60.0, "cold": 20.0}


def score_leads(db: Client, org_id: str, lead_ids: Optional[List[str]] = None) -> dict:
    """Score leads using Claude API with signal-based traffic light system.

    If lead_ids is None, scores all unscored leads for this org.
    Returns dict with results, total_scored, and errors.
    """
    # Fetch client context (includes org name, icp_config fields, and client_context)
    client_ctx = get_client_context(db, org_id)
    if not client_ctx:
        return {"results": [], "total_scored": 0, "errors": ["Organisation not found"]}

    # Backwards-compat: also fetch icp_config directly for pre-filter rules
    org_result = (
        db.table("organisations")
        .select("name, icp_config")
        .eq("id", org_id)
        .execute()
    )
    if not org_result.data:
        return {"results": [], "total_scored": 0, "errors": ["Organisation not found"]}

    org = org_result.data[0]
    icp_config = org.get("icp_config") or {}

    # Fetch leads to score — only enriched leads
    query = db.table("leads").select("*").eq("org_id", org_id)
    if lead_ids:
        query = query.in_("id", lead_ids)
    else:
        # Auto-score: only pick up enriched but unscored leads
        query = query.is_("scored_at", "null").not_.is_("enriched_at", "null")

    leads_result = query.execute()
    leads = leads_result.data

    if not leads:
        return {"results": [], "total_scored": 0, "errors": []}

    results = []
    errors = []

    for lead in leads:
        # Only score leads that have been fully enriched
        if not lead.get("enriched_at"):
            errors.append(f"Lead {lead['id']}: has not been enriched yet — skipping")
            continue

        try:
            score_result = _score_single_lead(db, org_id, lead, icp_config, client_ctx)
            results.append(score_result)
        except Exception as e:
            error_msg = f"Error scoring lead {lead['id']}: {e}"
            logger.error(error_msg)
            errors.append(error_msg)

    return {
        "results": results,
        "total_scored": len(results),
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# ICP pre-filter — disqualify obvious mismatches without calling Claude
# ---------------------------------------------------------------------------

# Apollo industry keywords that always pass through (cyber/security companies)
_AUTO_QUALIFY_INDUSTRY_KEYWORDS = {"security", "cyber", "defense & space", "military"}

# Apollo industries that are never relevant — skip Claude entirely
_AUTO_DISQUALIFY_INDUSTRIES = {
    "events services",
    "staffing & recruiting",
    "recruiting",
    "entertainment",
    "hospitality",
    "food & beverages",
    "real estate",
    "education management",
    "leisure, travel & tourism",
}

# Company name keywords that indicate a cyber company regardless of Apollo's industry label
_CYBER_NAME_KEYWORDS = {
    "cyber", "security", "pen test", "pentest", "threat", "infosec",
    "soc", "mssp", "grc",
}


def _check_icp_prefilter(lead: dict, icp_config: dict, client_ctx: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Check if lead should be auto-disqualified before calling Claude.

    Returns a cold rating_reason string if disqualified, or None if the lead
    should proceed to Claude scoring.
    Uses disqualify_signals from client_context plus existing ICP rules.
    """
    # Stale data — person has left the company
    data_status = lead.get("data_status", "")
    if data_status == "stale":
        return "LinkedIn verification shows this person has left the company. Data is stale."

    # Data quality gate — if we don't have enough reliable data on this person,
    # don't present them as a good prospect. Catches cases like hidden last names,
    # no verified email, or sparse enrichment from LinkedIn URL research.
    data_quality_issues = _check_data_quality(lead)
    if data_quality_issues:
        return f"Insufficient data quality: {data_quality_issues}"


    # Client-specific disqualify signals from client_context
    if client_ctx:
        disqualify_signals = client_ctx.get("disqualify_signals", "")
        if disqualify_signals:
            company_name_lower = (lead.get("company_name") or "").lower()
            industry_lower = (lead.get("company_industry") or "").lower()
            lead_text = f"{company_name_lower} {industry_lower}"
            signals = [s.strip().lower() for s in str(disqualify_signals).split(",") if s.strip()]
            for signal in signals:
                if signal in lead_text:
                    return f"Disqualified by client rule: matches disqualify signal '{signal}'."

        # Critical rules — hard filter for cyber competitor industries
        critical_rules = client_ctx.get("critical_rules", "")
        if critical_rules:
            rules_lower = critical_rules.lower()
            _cyber_competitor_industries = {
                "cybersecurity", "information security", "computer & network security",
                "managed security", "network security", "cyber security",
            }
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
            lead_industry = (lead.get("company_industry") or "").lower()
            if is_cyber_competitor_rule and lead_industry in _cyber_competitor_industries:
                return f"Filtered by critical rule: '{lead.get('company_industry')}' is a competitor industry."

    # Company name check — cyber keywords in the name override any industry label
    company_name_lower = (lead.get("company_name") or "").lower()
    if any(kw in company_name_lower for kw in _CYBER_NAME_KEYWORDS):
        # Auto-qualify: company name signals cyber, let Claude score it
        return None

    # Industry check — two-tier filter
    lead_industry = (lead.get("company_industry") or "").lower()
    if lead_industry:
        # Tier 1: auto-qualify known-good industries
        if any(kw in lead_industry for kw in _AUTO_QUALIFY_INDUSTRY_KEYWORDS):
            return None

        # "information technology & services" is too broad to reject
        if "information technology" in lead_industry:
            return None

        # Tier 2: auto-disqualify known-bad industries
        if lead_industry in _AUTO_DISQUALIFY_INDUSTRIES:
            return f"Industry '{lead.get('company_industry')}' is not a target industry."

    # Tier 3: everything else passes through to Claude

    # No enrichment data at all
    enrichment = lead.get("enrichment_data") or {}
    if not enrichment:
        return "No enrichment data available — cannot assess this lead."

    return None


def _check_data_quality(lead: dict) -> Optional[str]:
    """Check if lead has enough reliable data to be worth presenting.

    Returns a human-readable issue string if data quality is too low,
    or None if the lead is fine.

    This catches LinkedIn URL researches where the profile is private,
    the last name is hidden, or we couldn't find basic information.
    """
    issues = []

    # Incomplete name — single character or initial-only last name
    last_name = (lead.get("last_name") or "").strip().rstrip(".")
    if len(last_name) <= 1:
        issues.append("last name is hidden or incomplete")

    # No email or very low confidence email
    email = lead.get("email") or ""
    email_confidence = lead.get("email_confidence") or 0
    if not email:
        issues.append("no email address found")
    elif email_confidence < 30:
        issues.append(f"email confidence is only {email_confidence}%")

    # Title is a LinkedIn headline blob, not a real title
    title = lead.get("title") or ""
    if len(title) > 80:
        issues.append("title appears to be a LinkedIn headline, not a job title")

    # If 2+ issues, the data is too poor to present as a real prospect
    if len(issues) >= 2:
        return ". ".join(issues).capitalize()

    return None


def _disqualify_lead(db: Client, org_id: str, lead: dict, reason: str) -> dict:
    """Mark a lead as cold/disqualified and return the result dict."""
    now = datetime.now(timezone.utc).isoformat()

    db.table("leads").update({
        "total_score": RATING_SCORE_MAP["cold"],
        "lead_rating": "cold",
        "rating_reason": reason,
        "score_reasoning": reason,
        "scored_at": now,
        "updated_at": now,
        "enrichment_status": "enriched",
        "status": "disqualified",
    }).eq("id", lead["id"]).execute()

    log_activity(db, org_id, lead["id"], "lead_scored", {
        "lead_rating": "cold",
        "total_score": RATING_SCORE_MAP["cold"],
        "buying_signals": [],
        "recommended_action": "skip",
    })

    return {
        "lead_id": lead["id"],
        "lead_rating": "cold",
        "rating_reason": reason,
        "buying_signals": [],
        "icp_fit": {},
        "recommended_action": "skip",
        "best_approach_angle": "",
        "icp_score": RATING_SCORE_MAP["cold"],
        "timing_score": RATING_SCORE_MAP["cold"],
        "total_score": RATING_SCORE_MAP["cold"],
        "reasoning": reason,
    }


# ---------------------------------------------------------------------------
# Enrichment field extraction for the scoring prompt
# ---------------------------------------------------------------------------

def _extract_scoring_fields(lead: dict) -> Dict[str, str]:
    """Extract individual enrichment fields for the scoring prompt."""
    enrichment = lead.get("enrichment_data") or {}

    # Website
    website = enrichment.get("website") or {}
    website_notes = ""
    if isinstance(website, dict) and not website.get("error"):
        website_notes = website.get("notes") or website.get("services_relevance") or "No website insights"

    # Tech stack
    tech = enrichment.get("tech_stack") or {}
    tech_stack_summary = ""
    tech_list = "None detected"
    missing_tools = "Unknown"
    if isinstance(tech, dict) and not tech.get("error"):
        tech_stack_summary = tech.get("tech_stack_summary") or tech.get("opportunity_notes") or ""
        # Build tech list
        tech_items = tech.get("technologies_detected") or []
        if isinstance(tech_items, list) and tech_items:
            names = []
            for t in tech_items:
                if isinstance(t, dict):
                    names.append(t.get("name", ""))
                elif isinstance(t, str):
                    names.append(t)
            tech_list = ", ".join(n for n in names if n) or "None detected"
        # Missing tools
        gaps = tech.get("missing_tools_for_client") or tech.get("gtm_gaps") or ""
        if gaps:
            missing_tools = gaps if isinstance(gaps, str) else ", ".join(gaps)

    # News
    news = enrichment.get("news") or {}
    news_summary = "No recent news"
    if isinstance(news, dict):
        stories = news.get("key_stories") or []
        if isinstance(stories, list) and stories:
            lines = []
            for s in stories[:3]:
                if isinstance(s, dict):
                    lines.append(f"{s.get('headline', '')} ({s.get('date', '')})")
            if lines:
                news_summary = "; ".join(lines)

    # Jobs
    jobs = enrichment.get("jobs") or {}
    jobs_summary = "No hiring data"
    is_hiring_sales = "Unknown"
    if isinstance(jobs, dict) and not jobs.get("error"):
        jobs_summary = jobs.get("hiring_velocity_notes") or jobs.get("notes") or "No hiring data"
        is_hiring_sales = "Yes" if jobs.get("is_hiring_sales_bd") else "No"

    # Threats — quick-enrich does not currently write threat data into
    # enrichment_data (threat intel runs from a separate router). Kept here
    # so that a future wire-in (or a manual write) lands cleanly.
    threats = enrichment.get("threat_alerts") or []
    threat_alerts = "None"
    if isinstance(threats, list) and threats:
        lines = [t.get("title", str(t)) if isinstance(t, dict) else str(t) for t in threats[:3]]
        threat_alerts = "; ".join(lines)

    # Deep research
    deep = enrichment.get("deep_research") or {}
    deep_research_summary = "No deep research data"
    if isinstance(deep, dict) and deep.get("compliance"):
        parts = []
        # Compliance
        compliance = deep.get("compliance", {})
        certs = [k for k, v in compliance.items()
                 if v is True or (isinstance(v, str) and v not in ("Not found", "false"))
                 and k != "other_certifications"]
        other = compliance.get("other_certifications") or []
        if isinstance(other, list):
            certs.extend(other)
        if certs:
            parts.append(f"Certifications: {', '.join(certs)}")

        # Business intel
        biz = deep.get("business_intel", {})
        if biz.get("founded_year") and biz["founded_year"] != "Not found":
            parts.append(f"Founded: {biz['founded_year']}")
        if biz.get("estimated_revenue") and biz["estimated_revenue"] != "Not found":
            parts.append(f"Revenue: {biz['estimated_revenue']}")
        customers = biz.get("key_customers") or []
        if isinstance(customers, list) and customers and customers != ["Not found"]:
            parts.append(f"Key customers: {', '.join(str(c) for c in customers[:5])}")

        # Growth signals
        growth = deep.get("growth_signals", {})
        partnerships = growth.get("recent_partnerships") or []
        if isinstance(partnerships, list) and partnerships and partnerships != ["Not found"]:
            parts.append(f"Recent partnerships: {', '.join(str(p) for p in partnerships[:3])}")
        events = growth.get("upcoming_events") or []
        if isinstance(events, list) and events and events != ["Not found"]:
            parts.append(f"Upcoming events: {', '.join(str(e) for e in events[:3])}")

        if parts:
            deep_research_summary = "; ".join(parts)

    # PredictLeads: behind-firewall technologies
    pl_tech = enrichment.get("predictleads_tech") or {}
    behind_firewall_techs = "None"
    if isinstance(pl_tech, dict) and pl_tech.get("behind_firewall_techs"):
        bft = pl_tech["behind_firewall_techs"]
        if isinstance(bft, list) and bft:
            behind_firewall_techs = ", ".join(bft)

    # PredictLeads: financing events
    pl_financing = enrichment.get("predictleads_financing") or {}
    financing_summary = "None"
    if isinstance(pl_financing, dict):
        rounds = pl_financing.get("rounds") or []
        if isinstance(rounds, list) and rounds:
            parts = []
            for r in rounds[:3]:
                if isinstance(r, dict):
                    investors = ", ".join(r.get("investors", [])[:3])
                    parts.append(f"{r.get('type', '')} {r.get('amount', '')} ({r.get('date', '')}) — {investors}".strip(" —"))
            if parts:
                financing_summary = "; ".join(parts)

    # PredictLeads: similar companies
    pl_similar = enrichment.get("predictleads_similar") or {}
    similar_companies = "None"
    if isinstance(pl_similar, dict):
        sims = pl_similar.get("similar_companies") or []
        if isinstance(sims, list) and sims:
            similar_companies = ", ".join(
                f"{s.get('name', '')} ({s.get('domain', '')})" for s in sims[:5] if isinstance(s, dict)
            )

    # PredictLeads news event signals — derive from the filtered key_stories
    # in enrichment_data["news"] (already 6-month-filtered). Do NOT trust the
    # top-level pl_news.has_recent_* booleans which fire on year-old events.
    news_event_signals = "None"
    filtered_news = enrichment.get("news") or {}
    filtered_stories = filtered_news.get("key_stories") or []
    if filtered_stories:
        funding_cats = {"receives_financing", "raises_funding", "funding"}
        acquisition_cats = {"acquires", "acquired_by", "acquisition"}
        launch_cats = {"launches", "product_launch"}
        partnership_cats = {"partners_with", "partnership"}
        signals = []
        if any(s.get("category") in funding_cats for s in filtered_stories):
            signals.append("Recent funding")
        if any(s.get("category") in acquisition_cats for s in filtered_stories):
            signals.append("Recent acquisition")
        if any(s.get("category") in launch_cats for s in filtered_stories):
            signals.append("Recent product launch")
        if any(s.get("category") in partnership_cats for s in filtered_stories):
            signals.append("Recent partnership")
        if signals:
            news_event_signals = ", ".join(signals)

    return {
        "website_notes": website_notes or "No website data",
        "tech_stack_summary": tech_stack_summary or "No tech stack data",
        "tech_list": tech_list,
        "missing_tools": missing_tools,
        "news_summary": news_summary,
        "jobs_summary": jobs_summary,
        "is_hiring_sales": is_hiring_sales,
        "threat_alerts": threat_alerts,
        "deep_research_summary": deep_research_summary,
        "behind_firewall_techs": behind_firewall_techs,
        "financing_summary": financing_summary,
        "similar_companies": similar_companies,
        "news_event_signals": news_event_signals,
    }


# ---------------------------------------------------------------------------
# Company revenue snapshot (firmographic — the prospect's own revenue,
# NOT revenue_potential which is the deal value to the client). Multi-
# provider deterministic reconciliation lives in company_revenue.reconcile.
# ---------------------------------------------------------------------------

def _company_revenue_snapshot(enrichment: dict) -> Optional[Dict[str, Any]]:
    """Reconcile the prospect company's own annual revenue across Apollo,
    Perplexity/Sonar and (UK) Companies House, written to the denormalised
    leads.company_revenue column so the contact list reads it cheaply.

    Deterministic, no LLM — see app.services.company_revenue. None when no
    provider reported a real number (UI renders nothing; org-agnostic,
    same contract as revenue_potential).
    """
    from app.services.company_revenue import reconcile

    bi = enrichment.get("business_intel") or {}
    deep_bi = (enrichment.get("deep_research") or {}).get("business_intel") or {}
    return reconcile(
        apollo_amount=bi.get("annual_revenue"),
        # Sonar's free-text estimate: prefer the merged business_intel
        # value, fall back to the raw deep_research block.
        perplexity_text=bi.get("estimated_revenue") or deep_bi.get("estimated_revenue"),
        companies_house_bracket=bi.get("companies_house_revenue_bracket"),
    )


# ---------------------------------------------------------------------------
# Core scoring
# ---------------------------------------------------------------------------

def _score_single_lead(
    db: Client, org_id: str, lead: dict, icp_config: dict, client_ctx: Dict[str, Any],
) -> dict:
    """Score a single lead and update the database."""
    client_name = client_ctx.get("client_name", "")

    # --- ICP pre-filter: disqualify obvious mismatches without calling Claude ---
    cold_reason = _check_icp_prefilter(lead, icp_config, client_ctx)
    if cold_reason:
        logger.info("Lead %s auto-disqualified: %s", lead["id"], cold_reason)
        return _disqualify_lead(db, org_id, lead, cold_reason)

    # --- Extract enrichment fields for prompt ---
    fields = _extract_scoring_fields(lead)

    # --- Pull recent action_outcomes as exemplars for this org ---
    # Below the cold-start threshold get_outcome_block returns a "(none yet)"
    # marker so the prompt template still renders cleanly.
    try:
        from app.services.outcomes import get_outcome_block
        fields["past_outcomes"] = get_outcome_block(db, org_id)
    except Exception as e:
        logger.warning("Failed to fetch outcome exemplars for org %s: %s", org_id, e)
        fields["past_outcomes"] = "(none yet)"

    # Build critical rules section for scoring prompt
    critical_rules = client_ctx.get("critical_rules", "")
    if critical_rules:
        critical_rules_section = (
            f"CRITICAL RULES FOR THIS CLIENT:\n{critical_rules}\n\n"
            "These rules override all other scoring criteria. Any lead that "
            "violates these rules should be scored WARM at best with a clear warning."
        )
    else:
        critical_rules_section = ""

    # Build prompt with full client context
    prompt = SCORING_PROMPT.format(
        client_name=client_name,
        what_they_sell=client_ctx.get("what_they_sell", "") or ", ".join(icp_config.get("services_offered", [])) or "N/A",
        key_differentiators=client_ctx.get("key_differentiators", "N/A") or "N/A",
        pain_points_they_solve=client_ctx.get("pain_points_they_solve", "N/A") or "N/A",
        ideal_triggers=client_ctx.get("ideal_triggers", "N/A") or "N/A",
        competitors=format_competitors(client_ctx) or "N/A",
        client_positioning=client_ctx.get("client_positioning", "N/A") or icp_config.get("client_positioning", "N/A"),
        critical_rules_section=critical_rules_section,
        approach_style=client_ctx.get("approach_style", "direct and signal-based") or "direct and signal-based",
        tone_of_voice=client_ctx.get("tone_of_voice", "professional and concise") or "professional and concise",
        product_names=format_product_names(client_ctx) or "N/A",
        first_name=lead.get("first_name", "Unknown"),
        last_name=lead.get("last_name", "Unknown"),
        title=lead.get("title", "Unknown"),
        company_name=lead.get("company_name", "Unknown"),
        company_industry=lead.get("company_industry", "Unknown"),
        company_size=lead.get("company_size", "Unknown"),
        company_location=lead.get("company_location", "Unknown"),
        data_status=lead.get("data_status", "unknown"),
        current_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        **fields,
    )

    score_data = call_claude(prompt)

    if isinstance(score_data, dict) and "error" in score_data:
        raise ValueError(f"Claude scoring failed: {score_data.get('error')}")

    # Validate the response shape. A malformed payload (missing
    # lead_rating, wrong values, not a dict) is logged with the failing
    # fields and treated as 'warm' — the same fallback the existing
    # code applied implicitly, now visible in logs.
    parsed = parse_llm_response(ScoringResponse, score_data, context="scoring")
    if parsed is None:
        score_data = {"lead_rating": "warm", "rating_reason": ""}
        lead_rating = "warm"
    else:
        lead_rating = parsed.lead_rating
        # Cold is handled by the pre-filter; if Claude returned cold,
        # coerce up to warm to preserve the existing contract.
        if lead_rating == "cold":
            lead_rating = "warm"

    rating_reason = score_data.get("rating_reason", "")
    # No-fear clients (client_context deprioritises threats): the rating
    # rationale must not LEAD with the prospect's own incident/exposure —
    # same hard rule as findings ordering, but this surface was previously
    # ungoverned (it rated a prospect hot "because of a DDoS crisis").
    # Deterministic, gated to no-fear clients, never breaks scoring.
    try:
        from app.services import signal_payload as sp
        if sp.wants_threats_deprioritised(client_ctx):
            _nf_findings = sp.load_findings(
                db, org_id=org_id, lead_id=lead["id"],
                enrichment_data=lead.get("enrichment_data") or {},
                deprioritise_threats=True,
            )
            rating_reason = sp.reframe_rating_reason(
                rating_reason, client_context=client_ctx,
                findings=_nf_findings,
            )
    except Exception as e:
        logger.warning(
            "rating_reason reframe failed for lead %s: %s",
            lead.get("id"), e,
        )
    buying_signals = score_data.get("buying_signals", [])
    recommended_action = score_data.get("recommended_action", "nurture_sequence")
    best_approach_angle = score_data.get("best_approach_angle", "")

    # Backwards-compatible total_score
    total_score = RATING_SCORE_MAP.get(lead_rating, 60.0)

    # Update lead in database
    now = datetime.now(timezone.utc).isoformat()
    update_data = {
        "total_score": total_score,
        "lead_rating": lead_rating,
        "rating_reason": rating_reason,
        "score_reasoning": rating_reason,
        "best_approach_angle": best_approach_angle,
        # Persist recommended_action to its column (base-schema column
        # that was previously only emitted to webhooks/activity, leaving
        # the dashboard "Action" column empty).
        "recommended_action": recommended_action,
        "scored_at": now,
        "updated_at": now,
        "enrichment_status": "enriched",
    }

    # Auto-qualify hot/warm leads
    if lead.get("status") == "new":
        update_data["status"] = "qualified"

    # first_hot_at — written ONCE, the moment a lead first becomes hot
    # or warm. Never overwritten on re-scores, so the dashboard digest
    # can distinguish "genuinely new hot today" from "old hot lead just
    # got re-scored". Existing leads were backfilled when this shipped.
    if lead_rating in ("hot", "warm") and not lead.get("first_hot_at"):
        update_data["first_hot_at"] = now

    # Persona attribution — pick the best-fit persona deterministically
    # (cheap field-overlap match, no LLM cost). The LLM scoring above
    # already ran against the union of all personas via dual-write; this
    # step only answers "which persona does this lead represent?".
    try:
        personas_for_org = read_personas(db, org_id)
        match = pick_best_persona(lead, lead.get("enrichment_data") or {}, personas_for_org)
        if match and match.get("persona_id"):
            update_data["matched_persona_id"] = match["persona_id"]
            logger.info(
                "Lead %s matched persona %r (score %d)",
                lead["id"], match.get("persona_name"), match.get("match_score"),
            )
    except Exception as e:
        # Never fail scoring because of persona attribution
        logger.warning("Persona match failed for lead %s: %s", lead["id"], e)

    db.table("leads").update(update_data).eq("id", lead["id"]).execute()

    # Generate GTM Intelligence Briefing
    enrichment = lead.get("enrichment_data") or {}
    scoring_output = {
        "lead_rating": lead_rating,
        "buying_signals": buying_signals,
        "recommended_action": recommended_action,
        "best_approach_angle": best_approach_angle,
    }
    try:
        briefing = generate_gtm_briefing(
            lead, enrichment, scoring_output, client_ctx, icp_config=icp_config, db=db,
        )
        enrichment["gtm_briefing"] = briefing
        # Revenue potential is computed inside the briefing (full post-scoring
        # context there). Lift it to a top-level enrichment key so consumers
        # don't have to dig into the briefing — additive, single DB write,
        # no recompute. Non-required: absent if the briefing failed.
        _rev = briefing.get("revenue_potential")
        _lead_update = {"enrichment_data": enrichment}
        if _rev is not None:
            enrichment["revenue_potential"] = _rev
            # Denormalised top-level column: lets the
            # dashboard contact list read it without the enrichment_data
            # blob and makes per-org pipeline value a simple aggregate.
            _lead_update["revenue_potential"] = _rev
        # Denormalised signal snapshot: the strongest
        # finding headlines, already lever-ordered by the briefing
        # (opportunity-first for no-fear clients). Drives the dashboard
        # "Signals" column without the enrichment_data blob.
        _sig = (briefing.get("commercial") or {}).get("strongest_findings")
        if not _sig:
            _sig = [
                f.get("headline")
                for f in (briefing.get("findings") or [])[:3]
                if f.get("headline")
            ]
        _sig = [s[:90] for s in (_sig or []) if s][:3]
        if _sig:
            _lead_update["signal_summary"] = _sig
        # Denormalised prospect-company revenue snapshot:
        # the firmographic "how big is the company this lead works at",
        # distinct from revenue_potential (deal value to the client).
        # Lets the prospects list show it without the enrichment_data blob.
        _cr = _company_revenue_snapshot(enrichment)
        if _cr is not None:
            _lead_update["company_revenue"] = _cr
        db.table("leads").update(_lead_update).eq("id", lead["id"]).execute()
        logger.info(
            "GTM briefing generated for lead %s — readiness: %s, play: %s, "
            "revenue: %s %s (%s)",
            lead["id"], briefing.get("buying_readiness"),
            (briefing.get("recommended_play") or {}).get("play_name", "none"),
            (briefing.get("revenue_potential") or {}).get("currency", ""),
            (briefing.get("revenue_potential") or {}).get(
                "estimated_revenue_potential", "n/a"),
            (briefing.get("revenue_potential") or {}).get("confidence", "—"),
        )
    except Exception as e:
        logger.warning("GTM briefing generation failed for lead %s: %s", lead["id"], e)

    # Surface the deterministic contract findings into buying_signals so the
    # activity log, webhooks, and API response carry the SAME human lines the
    # dashboard/digest show. Additive: list-of-strings shape preserved,
    # Claude's signals kept first, finding headlines appended de-duplicated.
    # Independently guarded so a missing/expired row never affects scoring.
    try:
        from app.services import signal_payload as sp
        finding_lines = [
            f["headline"]
            for f in sp.load_findings(
                db,
                org_id=org_id,
                lead_id=lead["id"],
                enrichment_data=enrichment,
                deprioritise_threats=sp.wants_threats_deprioritised(client_ctx),
            )
        ]
        buying_signals = list(buying_signals) + [
            h for h in finding_lines if h not in buying_signals
        ]
    except Exception as e:
        logger.warning(
            "Finding merge into buying_signals failed for lead %s: %s",
            lead["id"], e,
        )

    # Log activity
    log_activity(db, org_id, lead["id"], "lead_scored", {
        "lead_rating": lead_rating,
        "total_score": total_score,
        "buying_signals": buying_signals,
        "recommended_action": recommended_action,
    })

    # Fire webhooks. Best-effort — never raise from here. Two events:
    # one general lead.scored, plus a more specific lead.rated_hot if
    # this lead just turned hot (high-signal trigger customers may want
    # to react to immediately).
    try:
        from app.services.webhooks import enqueue_event
        scored_payload = {
            "lead_id": lead["id"],
            "lead_rating": lead_rating,
            "total_score": total_score,
            "buying_signals": buying_signals,
            "recommended_action": recommended_action,
            "best_approach_angle": best_approach_angle,
            "company_name": lead.get("company_name"),
            "revenue_potential": enrichment.get("revenue_potential"),
        }
        enqueue_event(db, org_id, "lead.scored", scored_payload)
        if lead_rating == "hot":
            enqueue_event(db, org_id, "lead.rated_hot", scored_payload)
        # Brief was generated above; emit brief.created if it landed
        if enrichment.get("gtm_briefing"):
            enqueue_event(db, org_id, "brief.created", {
                "lead_id": lead["id"],
                "company_name": lead.get("company_name"),
                "briefing": enrichment["gtm_briefing"],
            })
    except Exception as e:
        logger.warning("Webhook enqueue failed for lead %s: %s", lead["id"], e)

    return {
        "lead_id": lead["id"],
        "lead_rating": lead_rating,
        "rating_reason": rating_reason,
        "buying_signals": buying_signals,
        "icp_fit": {},
        "recommended_action": recommended_action,
        "best_approach_angle": best_approach_angle,
        "revenue_potential": enrichment.get("revenue_potential"),
        # Backwards compatibility
        "icp_score": total_score,
        "timing_score": total_score,
        "total_score": total_score,
        "reasoning": rating_reason,
    }
