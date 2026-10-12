"""Extract content-relevant patterns from pipeline data."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from supabase import Client

logger = logging.getLogger(__name__)


def get_pipeline_insights(db: Client, org_id: str) -> Dict[str, Any]:
    """Extract content-relevant patterns from the client's pipeline data.

    Queries Supabase for lead trends, signals, industries, replies,
    and meetings to inform content generation.
    """
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    week_ago_str = week_ago.isoformat()
    two_weeks_ago_str = two_weeks_ago.isoformat()

    insights: Dict[str, Any] = {
        "leads_this_week": 0,
        "leads_last_week": 0,
        "lead_trend": "stable",
        "top_signals_this_week": [],
        "top_industries": [],
        "recent_positive_replies": [],
        "meetings_this_week": 0,
    }

    try:
        # Leads this week vs last week
        this_week = db.table("leads").select("id", count="exact").eq(
            "org_id", org_id
        ).gte("created_at", week_ago_str).execute()

        last_week = db.table("leads").select("id", count="exact").eq(
            "org_id", org_id
        ).gte("created_at", two_weeks_ago_str).lt("created_at", week_ago_str).execute()

        insights["leads_this_week"] = this_week.count or 0
        insights["leads_last_week"] = last_week.count or 0

        if insights["leads_this_week"] > insights["leads_last_week"]:
            insights["lead_trend"] = "up"
        elif insights["leads_this_week"] < insights["leads_last_week"]:
            insights["lead_trend"] = "down"

    except Exception as e:
        logger.warning("Failed to fetch lead counts: %s", e)

    try:
        # Most common signal types this week
        signals_resp = db.table("signals").select("signal_type").eq(
            "org_id", org_id
        ).gte("detected_at", week_ago_str).execute()

        if signals_resp.data:
            type_counts: Dict[str, int] = {}
            for sig in signals_resp.data:
                st = sig.get("signal_type", "unknown")
                type_counts[st] = type_counts.get(st, 0) + 1
            sorted_types = sorted(type_counts.items(), key=lambda x: x[1], reverse=True)
            insights["top_signals_this_week"] = [
                {"signal_type": t, "count": c} for t, c in sorted_types[:5]
            ]

    except Exception as e:
        logger.warning("Failed to fetch signal types: %s", e)

    try:
        # Most common industries among qualified leads
        qualified = db.table("leads").select("company_industry").eq(
            "org_id", org_id
        ).in_("status", ["qualified", "sequenced", "replied", "meeting"]).execute()

        if qualified.data:
            industry_counts: Dict[str, int] = {}
            for lead in qualified.data:
                ind = lead.get("company_industry")
                if ind:
                    industry_counts[ind] = industry_counts.get(ind, 0) + 1
            sorted_industries = sorted(industry_counts.items(), key=lambda x: x[1], reverse=True)
            insights["top_industries"] = [
                {"industry": ind, "count": c} for ind, c in sorted_industries[:5]
            ]

    except Exception as e:
        logger.warning("Failed to fetch industry breakdown: %s", e)

    try:
        # Recent positive replies (anonymised)
        positive_replies = db.table("replies").select(
            "classification, extracted_intent, lead_email"
        ).eq("org_id", org_id).in_(
            "classification", ["INTERESTED", "CURIOUS"]
        ).gte("received_at", week_ago_str).limit(5).execute()

        if positive_replies.data:
            for reply in positive_replies.data:
                # Look up the lead for anonymised context
                lead_resp = db.table("leads").select(
                    "title, company_industry"
                ).eq("org_id", org_id).eq(
                    "email", reply.get("lead_email", "")
                ).limit(1).execute()

                if lead_resp.data:
                    lead = lead_resp.data[0]
                    title = lead.get("title", "a decision-maker")
                    industry = lead.get("company_industry", "a company")
                    insights["recent_positive_replies"].append(
                        f"a {title} at a {industry} company replied interested"
                    )

    except Exception as e:
        logger.warning("Failed to fetch positive replies: %s", e)

    try:
        # Meetings booked this week
        meetings = db.table("activity_log").select("id", count="exact").eq(
            "org_id", org_id
        ).eq("activity_type", "status_updated").gte(
            "created_at", week_ago_str
        ).execute()

        # Filter for meeting status updates from activity_data
        if meetings.data:
            meeting_count = 0
            for activity in meetings.data:
                data = activity.get("activity_data", {})
                if isinstance(data, dict) and data.get("new_status") == "meeting":
                    meeting_count += 1
            insights["meetings_this_week"] = meeting_count

    except Exception as e:
        logger.warning("Failed to fetch meetings count: %s", e)

    return insights
