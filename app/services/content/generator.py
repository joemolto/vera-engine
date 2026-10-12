"""Main content suggestion engine — generates weekly LinkedIn post ideas."""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict

from supabase import Client

from app.prompts.content import CONTENT_GENERATION_PROMPT
from app.services.claude import call_claude
from app.services.content.news_feed import fetch_industry_news
from app.services.content.pipeline_insights import get_pipeline_insights

logger = logging.getLogger(__name__)


def generate_weekly_content(db: Client, org_id: str) -> Dict[str, Any]:
    """Generate 3 weekly LinkedIn content suggestions for an organisation.

    Pulls together style guide, voice profile, ICP, industry news,
    and pipeline insights to produce tailored content ideas.
    """
    # 1. Fetch org content_config and icp_config
    org_resp = db.table("organisations").select(
        "name, content_config, icp_config"
    ).eq("id", org_id).limit(1).execute()

    if not org_resp.data:
        return {"error": f"Organisation {org_id} not found"}

    org = org_resp.data[0]
    org_name = org.get("name", "Unknown")
    content_config = org.get("content_config") or {}
    icp_config = org.get("icp_config") or {}

    # Validate we have a style guide
    style_ref = content_config.get("style_reference", {})
    style_guide = style_ref.get("style_guide")
    if not style_guide:
        return {"error": "No style guide found. Run style analysis first."}

    voice_profile = content_config.get("client_voice", {}).get("voice_profile", {})
    content_prefs = content_config.get("content_preferences", {})

    # 2. Fetch industry news
    logger.info("Fetching industry news for %s", org_name)
    news_articles = fetch_industry_news(icp_config)
    industry_news = _format_news(news_articles)

    # 3. Fetch pipeline insights
    logger.info("Fetching pipeline insights for %s", org_name)
    pipeline = get_pipeline_insights(db, org_id)
    pipeline_text = _format_pipeline(pipeline)

    # 3b. Fetch recent threat intel alerts
    threat_summary = _fetch_threat_summary(db, org_id)

    # 4. Build ICP description
    icp_description = _format_icp(icp_config)

    # 5. Extract org details for the prompt
    services_description = icp_config.get("services_offered", icp_config.get("what_we_do", "cybersecurity services"))
    client_role = content_config.get("client_voice", {}).get("role", "Founder")

    # 6. Call Claude
    prompt = CONTENT_GENERATION_PROMPT.format(
        style_guide=json.dumps(style_guide, indent=2),
        voice_profile=json.dumps(voice_profile, indent=2) if voice_profile else "Not yet analysed",
        content_preferences=json.dumps(content_prefs, indent=2) if content_prefs else "No specific preferences set",
        company_name=org_name,
        services_description=services_description,
        client_role=client_role,
        icp_description=icp_description,
        industry_news=industry_news,
        pipeline_insights=pipeline_text,
        threat_intel_summary=threat_summary,
    )

    result = call_claude(prompt)

    if "error" in result:
        logger.error("Content generation failed for %s: %s", org_name, result.get("error"))
        return result

    # 7. Store in content_suggestions table
    now = datetime.now(timezone.utc)
    week_label = now.strftime("%G-W%V")

    try:
        db.table("content_suggestions").insert({
            "org_id": org_id,
            "week_label": week_label,
            "suggestions": result.get("suggestions", []),
            "industry_context": {"articles": news_articles[:5]},
            "pipeline_insights": pipeline,
            "status": "new",
        }).execute()
        logger.info("Stored content suggestions for %s week %s", org_name, week_label)
    except Exception as e:
        logger.error("Failed to store content suggestions: %s", e)

    result["week_label"] = week_label
    result["org_name"] = org_name
    return result


def _format_news(articles):
    """Format news articles for the prompt."""
    if not articles:
        return "No industry news found this week."

    lines = []
    for a in articles[:10]:
        lines.append(
            f"- {a.get('title', 'N/A')} (Source: {a.get('source', 'N/A')}, Date: {a.get('date', 'N/A')})"
        )
    return "\n".join(lines)


def _format_pipeline(pipeline):
    """Format pipeline insights for the prompt."""
    parts = []

    trend = pipeline.get("lead_trend", "stable")
    this_week = pipeline.get("leads_this_week", 0)
    last_week = pipeline.get("leads_last_week", 0)
    parts.append(f"Lead volume: {this_week} this week vs {last_week} last week (trending {trend})")

    signals = pipeline.get("top_signals_this_week", [])
    if signals:
        signal_strs = [f"{s['signal_type']} ({s['count']})" for s in signals]
        parts.append(f"Top signals: {', '.join(signal_strs)}")

    industries = pipeline.get("top_industries", [])
    if industries:
        ind_strs = [f"{i['industry']} ({i['count']})" for i in industries]
        parts.append(f"Top industries among qualified leads: {', '.join(ind_strs)}")

    replies = pipeline.get("recent_positive_replies", [])
    if replies:
        parts.append("Recent positive replies:")
        for r in replies:
            parts.append(f"  - {r}")

    meetings = pipeline.get("meetings_this_week", 0)
    if meetings:
        parts.append(f"Meetings booked this week: {meetings}")

    return "\n".join(parts) if parts else "No pipeline data available yet."


def _format_icp(icp_config):
    """Format ICP config into readable text for the prompt."""
    if not icp_config:
        return "No ICP configured yet."

    parts = []
    for key, value in icp_config.items():
        if isinstance(value, (list, dict)):
            parts.append(f"{key}: {json.dumps(value)}")
        else:
            parts.append(f"{key}: {value}")
    return "\n".join(parts)


def _fetch_threat_summary(db, org_id):
    """Fetch recent threat alerts for the org to include in content generation."""
    try:
        alerts_resp = db.table("threat_alerts").select(
            "relevance_reason, sales_angle, content_hook, urgency, "
            "threat_intel(title, source, severity, ai_summary)"
        ).eq("org_id", org_id).eq("status", "new").order(
            "detected_at", desc=True
        ).limit(5).execute()

        if not alerts_resp.data:
            return "No relevant threat intelligence this week."

        lines = []
        for alert in alerts_resp.data:
            threat = alert.get("threat_intel") or {}
            lines.append(
                f"- [{threat.get('severity', 'unknown').upper()}] "
                f"{threat.get('title', 'Unknown threat')} "
                f"(Source: {threat.get('source', 'unknown')})\n"
                f"  Summary: {threat.get('ai_summary', 'N/A')}\n"
                f"  Why relevant: {alert.get('relevance_reason', 'N/A')}\n"
                f"  Content angle: {alert.get('content_hook', 'N/A')}"
            )
        return "\n".join(lines)
    except Exception as e:
        logger.error("Failed to fetch threat summary for content: %s", e)
        return "No relevant threat intelligence this week."
