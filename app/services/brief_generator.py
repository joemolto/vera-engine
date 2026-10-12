"""Sales brief generation — produces concise, actionable briefs for sales reps."""

import logging
from datetime import datetime, timezone

from supabase import Client

from app.prompts.enrichment import _format_services_list
from app.services.claude import call_claude
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SALES_BRIEF_PROMPT = """You are writing a sales brief for a sales rep at {client_name}.

CLIENT CONTEXT:
{client_name} sells: {services_offered}
Client positioning: {client_positioning}

LEAD DATA:
Name: {first_name} {last_name}, {title} at {company_name}
Score: {total_score}
LinkedIn verified: {data_status}

ENRICHMENT INTELLIGENCE:
Website: {website_summary}
Tech stack: {tech_stack_summary} — Technologies: {tech_list}
News: {news_stories}
Hiring: {jobs_summary}
Threat matches: {threat_alerts}
Personalisation hooks: {all_hooks}

Write a concise sales brief with these sections:

1. WHY THIS LEAD MATTERS (2-3 sentences explaining why this company is relevant to what {client_name} sells. Connect the tech stack, hiring, or news to a specific gap that {client_name}'s services fill.)

2. SUGGESTED APPROACH ANGLE (A specific opening message angle — not generic. Reference a real signal from the data. Write it as a one-liner the rep could actually say on a call or in an email.)

3. TIMING ASSESSMENT (Is now a good time to reach out? Why or why not? Reference signal freshness — a funding round from 9 months ago is not urgent. A CVE matching their tech stack from this week IS urgent.)

4. WATCH OUT FOR (Any red flags — stale data, side role, company recently acquired, events company not a real prospect, etc.)

Keep it under 200 words total. Write like a senior sales strategist briefing a rep before a call — direct, specific, no fluff.

Respond ONLY with valid JSON:
{{
    "why_this_lead_matters": "<2-3 sentences>",
    "suggested_approach_angle": "<one-liner the rep could say>",
    "timing_assessment": "<is now a good time? why?>",
    "watch_out_for": "<red flags or caveats>",
    "brief_summary": "<one sentence overall summary>"
}}"""


def _extract_enrichment_fields(lead: Dict[str, Any]) -> Dict[str, str]:
    """Pull summary fields from enrichment_data for the brief prompt."""
    enrichment = lead.get("enrichment_data") or {}

    # Website
    website = enrichment.get("website") or {}
    website_summary = website.get("notes") or website.get("services_relevance") or "No website data"

    # Tech stack
    tech_stack = enrichment.get("tech_stack") or {}
    tech_stack_summary = tech_stack.get("tech_stack_summary") or tech_stack.get("opportunity_notes") or "No tech stack data"
    tech_list_items = tech_stack.get("technologies_detected") or []
    if isinstance(tech_list_items, list):
        # Handle both string lists and dict lists
        names = []
        for t in tech_list_items:
            if isinstance(t, dict):
                names.append(t.get("name", ""))
            elif isinstance(t, str):
                names.append(t)
        tech_list = ", ".join(n for n in names if n) or "None detected"
    else:
        tech_list = "None detected"

    # News
    news = enrichment.get("news") or {}
    key_stories = news.get("key_stories") or []
    if key_stories and isinstance(key_stories, list):
        news_lines = []
        for story in key_stories[:3]:
            if isinstance(story, dict):
                news_lines.append(f"- {story.get('headline', '')} ({story.get('date', '')})")
        news_stories = "\n".join(news_lines) if news_lines else "No recent news"
    else:
        news_stories = "No recent news"

    # Jobs
    jobs = enrichment.get("jobs") or {}
    jobs_summary = jobs.get("notes") or jobs.get("hiring_velocity_notes") or "No hiring data"

    # Threat alerts
    threats = enrichment.get("threat_alerts") or enrichment.get("threats") or []
    if isinstance(threats, list) and threats:
        threat_lines = [f"- {t.get('title', '') if isinstance(t, dict) else str(t)}" for t in threats[:3]]
        threat_alerts = "\n".join(threat_lines)
    else:
        threat_alerts = "No threat matches"

    # Personalisation hooks — collect from all sources
    all_hooks = []
    for section_key in ("website", "tech_stack", "news", "jobs"):
        section = enrichment.get(section_key) or {}
        if isinstance(section, dict):
            hooks = section.get("personalisation_hooks") or []
            if isinstance(hooks, list):
                all_hooks.extend(str(h) for h in hooks)
    hooks_text = "\n".join(f"- {h}" for h in all_hooks) if all_hooks else "None found"

    return {
        "website_summary": website_summary,
        "tech_stack_summary": tech_stack_summary,
        "tech_list": tech_list,
        "news_stories": news_stories,
        "jobs_summary": jobs_summary,
        "threat_alerts": threat_alerts,
        "all_hooks": hooks_text,
    }


def generate_brief(
    db: Client,
    org_id: str,
    lead: Dict[str, Any],
    client_context: Dict[str, str],
) -> Dict[str, Any]:
    """Generate a sales brief for a single lead.

    Returns the brief dict from Claude, or an error dict.
    """
    fields = _extract_enrichment_fields(lead)

    # Score
    scoring = lead.get("enrichment_data", {}).get("scoring") or {}
    total_score = scoring.get("total_score") or lead.get("total_score") or "Not scored"

    # Data status from LinkedIn verification
    data_status = lead.get("data_status") or "unknown"

    prompt = SALES_BRIEF_PROMPT.format(
        client_name=client_context.get("client_name", "the client"),
        services_offered=_format_services_list(client_context.get("services_offered", [])),
        client_positioning=client_context.get("client_positioning", "N/A"),
        first_name=lead.get("first_name", ""),
        last_name=lead.get("last_name", ""),
        title=lead.get("title", ""),
        company_name=lead.get("company_name", ""),
        total_score=total_score,
        data_status=data_status,
        **fields,
    )

    result = call_claude(prompt)

    if "error" not in result:
        result["generated_at"] = datetime.now(timezone.utc).isoformat()

        # Store in enrichment_data
        enrichment_data = lead.get("enrichment_data") or {}
        enrichment_data["sales_brief"] = result
        db.table("leads").update({
            "enrichment_data": enrichment_data,
        }).eq("id", lead["id"]).execute()

    return result


def generate_briefs_for_org(
    db: Client,
    org_id: str,
    client_context: Dict[str, str],
) -> Dict[str, Any]:
    """Generate briefs for all enriched leads in an org that don't have one yet.

    Returns summary with counts and any errors.
    """
    # Fetch enriched leads without a sales brief
    leads_result = (
        db.table("leads")
        .select("*")
        .eq("org_id", org_id)
        .not_.is_("enriched_at", "null")
        .execute()
    )

    leads = leads_result.data or []

    # Filter to those without a brief
    leads_needing_brief = []
    for lead in leads:
        enrichment = lead.get("enrichment_data") or {}
        if not enrichment.get("sales_brief"):
            leads_needing_brief.append(lead)

    generated = 0
    errors: List[str] = []

    for lead in leads_needing_brief:
        try:
            result = generate_brief(db, org_id, lead, client_context)
            if "error" in result:
                errors.append(f"Lead {lead['id']}: {result['error']}")
            else:
                generated += 1
        except Exception as e:
            logger.error("Brief generation failed for lead %s: %s", lead["id"], e)
            errors.append(f"Lead {lead['id']}: {e}")

    return {
        "total_enriched": len(leads),
        "already_had_brief": len(leads) - len(leads_needing_brief),
        "briefs_generated": generated,
        "errors": errors,
    }
