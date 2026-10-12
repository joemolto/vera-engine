"""Dashboard data endpoint — read-only aggregation queries."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException

from typing import Any, Dict, List, Optional

from app.database import get_supabase
from app.models.schemas import (
    ActivityItem,
    DashboardResponse,
    DashboardSummary,
    TopLead,
    WeeklyStat,
)
from app.services.intelligence_map import build_intelligence_map
from app.services.recent_digest import get_recent_digest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/intelligence-map/{org_id}")
def intelligence_map(org_id: UUID) -> Dict[str, Any]:
    """Return {nodes, edges, meta} for the Intelligence Map view.

    Demo feature, dashboard-gated by org's icp_config.features
    .intelligence_map flag — the engine doesn't enforce the gate (org
    isolation is via org_id filtering as everywhere else), the
    dashboard does. Cheap aggregation over the org's enriched leads.
    """
    db = get_supabase()
    try:
        return build_intelligence_map(db, str(org_id))
    except Exception as e:
        logger.exception("intelligence_map failed for org %s: %s", org_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/recent-digest/{org_id}")
def recent_digest(org_id: UUID) -> Dict[str, Any]:
    """LLM-cleaned "what's happened recently" feed for the dashboard.

    Returns up to ~8 punchy snippets that cluster duplicate events
    (same funding round seen across RSS/LinkedIn/website = one
    snippet). Cached on the org row with a 15-min TTL — first call
    after expiry hits Haiku (~1.5s), subsequent calls are instant.
    """
    db = get_supabase()
    try:
        return get_recent_digest(db, str(org_id))
    except Exception as e:
        logger.exception("recent_digest failed for org %s: %s", org_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{org_id}", response_model=DashboardResponse)
def get_dashboard(org_id: str):
    """Get dashboard data for an organisation.

    The five independent aggregation queries (leads, replies, activity
    log, top-leads, signals) run in parallel via ThreadPoolExecutor.
    Weekly stats are derived from the already-fetched leads + replies
    rows instead of issuing 16 extra sequential queries (one per
    week-table combination), which used to dominate the endpoint
    latency for orgs with any history.
    """
    db = get_supabase()

    # Verify org exists and get name (cheap, single row by PK)
    org_result = db.table("organisations").select("name").eq("id", org_id).execute()
    if not org_result.data:
        raise HTTPException(status_code=404, detail="Organisation not found")
    org_name = org_result.data[0]["name"]

    # Pull every row we'll need for *any* aggregation in this response
    # in five parallel queries. The Supabase Python client is HTTP under
    # the hood, so threads + concurrent requests are safe.
    def _q_all_leads():
        return (
            db.table("leads")
            .select("id, status, enrichment_status, created_at")
            .eq("org_id", org_id)
            .in_("enrichment_status", ["suggested", "enriched"])
            .execute()
        )

    def _q_all_replies():
        return (
            db.table("replies")
            .select("classification, received_at")
            .eq("org_id", org_id)
            .execute()
        )

    def _q_activity():
        return (
            db.table("activity_log")
            .select("activity_type, activity_data, created_at, lead_id")
            .eq("org_id", org_id)
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )

    def _q_top_leads():
        return (
            db.table("leads")
            .select(
                "first_name, last_name, title, company_name, total_score, "
                "lead_rating, signals, status, enrichment_status"
            )
            .eq("org_id", org_id)
            .in_("enrichment_status", ["suggested", "enriched"])
            .neq("status", "disqualified")
            .order("total_score", desc=True)
            .limit(10)
            .execute()
        )

    def _q_signals():
        return (
            db.table("signals")
            .select("signal_type")
            .eq("org_id", org_id)
            .execute()
        )

    with ThreadPoolExecutor(max_workers=5, thread_name_prefix="dash") as pool:
        f_leads = pool.submit(_q_all_leads)
        f_replies = pool.submit(_q_all_replies)
        f_activity = pool.submit(_q_activity)
        f_top = pool.submit(_q_top_leads)
        f_signals = pool.submit(_q_signals)
        all_leads = f_leads.result()
        all_replies = f_replies.result()
        activity_result = f_activity.result()
        top_leads_result = f_top.result()
        signals_result = f_signals.result()

    # --- Summary counts (suggested + enriched, exclude disqualified) ---
    leads = [l for l in (all_leads.data or []) if l["status"] != "disqualified"]
    total_leads = len(leads)
    qualified = sum(
        1 for l in leads
        if l["status"] in ("qualified", "sequenced", "replied", "meeting", "closed")
    )
    in_sequence = sum(1 for l in leads if l["status"] == "sequenced")

    replies_data = all_replies.data or []
    replies_total = len(replies_data)
    replies_positive = sum(
        1 for r in replies_data
        if r.get("classification") in ("INTERESTED", "CURIOUS")
    )
    meetings_booked = sum(1 for l in leads if l["status"] in ("meeting", "closed"))

    summary = DashboardSummary(
        total_leads=total_leads,
        qualified_leads=qualified,
        in_sequence=in_sequence,
        replies_total=replies_total,
        replies_positive=replies_positive,
        meetings_booked=meetings_booked,
        pipeline_value=0,
    )

    # --- Recent activity (last 20) ---
    recent_activity = []
    for a in (activity_result.data or []):
        data = a.get("activity_data") or {}
        recent_activity.append(ActivityItem(
            type=a["activity_type"],
            lead_name=data.get("email", ""),
            company=data.get("company", ""),
            detail=_format_activity_detail(a["activity_type"], data),
            timestamp=a.get("created_at"),
        ))

    # --- Top leads by score ---
    top_leads = [
        TopLead(
            name=f"{l.get('first_name', '')} {l.get('last_name', '')}".strip(),
            title=l.get("title"),
            company=l.get("company_name"),
            score=float(l.get("total_score", 0)),
            signals=[s.get("type", "") for s in (l.get("signals") or [])],
            status=l.get("status"),
        )
        for l in (top_leads_result.data or [])
    ]

    # --- Signal breakdown ---
    signal_breakdown: Dict[str, int] = {}
    for s in (signals_result.data or []):
        st = s["signal_type"]
        signal_breakdown[st] = signal_breakdown.get(st, 0) + 1

    # --- Weekly stats (last 8 weeks, derived in-process) ---
    weekly_stats = _calculate_weekly_stats_inproc(
        all_leads.data or [], replies_data,
    )

    return DashboardResponse(
        org_id=org_id,
        org_name=org_name,
        summary=summary,
        weekly_stats=weekly_stats,
        recent_activity=recent_activity,
        top_leads=top_leads,
        signal_breakdown=signal_breakdown,
    )


@router.get("/{org_id}/company/{domain}")
def get_company_intelligence(org_id: str, domain: str):
    """Return the cross-client intelligence view of one company.

    The /dashboard/company/{domain} page reads this. The org_id scope
    restricts the answer to clients that have actually researched this
    company (returns 404 otherwise), but the data shown is *global*:
    company-level facts + signal observations + threat surface
    aggregated across every client that's enriched it.

    Anonymisation: only the company's own facts + signal observations
    (no PII) and the *count* of watching orgs are returned — never the
    identities of the other clients.
    """
    db = get_supabase()

    cleaned = (domain or "").strip().lower()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Domain is required")

    # 1) Verify the requesting org actually has a lead at this domain
    #    (otherwise we'd be leaking arbitrary domain intel to anyone
    #    who guesses one). The exists-check uses the leads table only.
    own_lead = (
        db.table("leads")
        .select("id")
        .eq("org_id", org_id)
        .ilike("company_domain", cleaned)
        .limit(1)
        .execute()
    )
    if not own_lead.data:
        raise HTTPException(
            status_code=404,
            detail=f"No lead at {cleaned} for this org",
        )

    # 2) Fetch the global company row
    company_resp = (
        db.table("companies")
        .select("*")
        .ilike("domain", cleaned)
        .limit(1)
        .execute()
    )
    if not company_resp.data:
        # The lead exists but the companies row hasn't been
        # backfilled / upserted yet — return an empty shell rather
        # than a 404 so the page can render "no intelligence yet."
        return {
            "company": None,
            "signals": [],
            "threats": [],
            "anonymized_watcher_count": 0,
        }

    company = company_resp.data[0]
    company_id = company.get("id")

    # 3) Signal observations (global). Each optional section is guarded so a
    #    single sub-query failure (a missing table, an RLS change, a bad row)
    #    degrades that section to empty rather than 500ing the whole page —
    #    the company facts above are the point; signals/threats are extras.
    try:
        signals_data = (
            db.table("company_intel")
            .select(
                "signal_type, signal_data, source, first_observed_at, "
                "latest_observed_at, observed_count"
            )
            .eq("company_id", company_id)
            .order("latest_observed_at", desc=True)
            .execute()
            .data
        ) or []
    except Exception as e:  # noqa: BLE001
        logger.warning("company_intel query failed for %s: %s", cleaned, e)
        signals_data = []

    # 4) Threat surface with the joined threat_intel row
    try:
        threats_data = (
            db.table("company_threat_surface")
            .select(
                "relevance_score, matched_via, first_observed_at, org_alert_count, "
                "threat_intel(id, source, source_id, title, description, severity, "
                "url, published_at)"
            )
            .eq("company_id", company_id)
            .execute()
            .data
        ) or []
    except Exception as e:  # noqa: BLE001
        logger.warning("company_threat_surface query failed for %s: %s", cleaned, e)
        threats_data = []

    # 5) Anonymised "watched by N other Vera clients" — subtract 1 so
    #    the calling org isn't counted as "other." Floor at 0.
    watched = max(0, int(company.get("watched_by_org_count") or 0) - 1)

    return {
        "company": company,
        "signals": signals_data,
        "threats": threats_data,
        "anonymized_watcher_count": watched,
    }


@router.get("/{org_id}/company/{domain}/people")
def get_company_people(
    org_id: str,
    domain: str,
    titles: Optional[str] = None,
    limit: int = 25,
):
    """Apollo people-search scoped to one company domain.

    Used by the accounts-dashboard dossier to surface "who works here"
    inside the account view (replaces the top-level Prospects surface for
    orgs with features.accounts_dashboard on).

    Org scope: the requesting org must already have a lead at this domain
    (same gate as the company-intel endpoint), so we don't leak arbitrary
    company queries to anyone who guesses a domain.

    Apollo people-search is free (no credits charged on search itself —
    enrichment is what costs), so this is safe to hit on every dossier
    open.
    """
    from app.services.discovery import _apollo_search

    db = get_supabase()
    cleaned = (domain or "").strip().lower()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Domain is required")

    # Auth gate: org must have either a lead OR a watchlist row at this
    # domain. (Account-first flow lets users add an account to watchlist
    # before any lead exists, so a lead-only gate would lock them out of
    # their own added accounts.)
    own_lead = (
        db.table("leads")
        .select("id")
        .eq("org_id", org_id)
        .ilike("company_domain", cleaned)
        .limit(1)
        .execute()
    )
    own_watchlist = (
        db.table("watchlist")
        .select("id")
        .eq("org_id", org_id)
        .ilike("company_domain", cleaned)
        .limit(1)
        .execute()
    )
    if not (own_lead.data or own_watchlist.data):
        raise HTTPException(
            status_code=404,
            detail=f"No lead or watchlist entry at {cleaned} for this org",
        )

    title_list = [t.strip() for t in (titles or "").split(",") if t.strip()]
    payload: Dict[str, Any] = {
        "q_organization_domains_list": [cleaned],
        "per_page": min(max(limit, 1), 100),
        "page": 1,
    }
    if title_list:
        payload["person_titles"] = title_list

    people_raw = _apollo_search(payload)

    # Project to the slim shape the dossier UI needs. Strip Apollo-internal
    # fields and anything that looks like backend tool attribution — the
    # dashboard scrubs backend names elsewhere too.
    people = []
    for p in people_raw:
        if not isinstance(p, dict):
            continue
        org = p.get("organization") or {}
        people.append({
            "apollo_id": p.get("id"),
            "first_name": p.get("first_name") or "",
            "last_name": p.get("last_name") or "",
            "title": p.get("title") or "",
            "seniority": p.get("seniority") or "",
            "linkedin_url": p.get("linkedin_url") or "",
            "country": p.get("country") or "",
            "city": p.get("city") or "",
            "state": p.get("state") or "",
            "headline": p.get("headline") or "",
            "departments": p.get("departments") or [],
            "company_name": org.get("name") or "",
        })

    return {
        "domain": cleaned,
        "people": people,
        "total": len(people),
    }


@router.get("/{org_id}/analytics")
def get_analytics(org_id: str):
    """Return aggregated lead data for dashboard charts."""
    db = get_supabase()

    # Fetch all visible leads (suggested + enriched)
    leads_result = (
        db.table("leads")
        .select("company_industry, company_size, company_location, lead_rating, source, enrichment_status, status")
        .eq("org_id", org_id)
        .in_("enrichment_status", ["suggested", "enriched"])
        .execute()
    )
    leads = leads_result.data or []

    # Watchlist count for funnel
    watchlist_result = (
        db.table("watchlist")
        .select("id", count="exact")
        .eq("org_id", org_id)
        .execute()
    )

    # Industry breakdown
    industry_counts: Dict[str, int] = {}
    for l in leads:
        ind = l.get("company_industry") or "Unknown"
        industry_counts[ind] = industry_counts.get(ind, 0) + 1
    industry_breakdown = sorted(
        [{"label": k, "count": v} for k, v in industry_counts.items()],
        key=lambda x: x["count"], reverse=True,
    )

    # Company size breakdown
    size_counts: Dict[str, int] = {}
    for l in leads:
        size = l.get("company_size") or "Unknown"
        size_counts[size] = size_counts.get(size, 0) + 1
    size_breakdown = sorted(
        [{"label": k, "count": v} for k, v in size_counts.items()],
        key=lambda x: x["count"], reverse=True,
    )

    # Rating breakdown (enriched leads only)
    rating_counts: Dict[str, int] = {"hot": 0, "warm": 0, "cold": 0, "unrated": 0}
    for l in leads:
        rating = l.get("lead_rating") or "unrated"
        if rating in rating_counts:
            rating_counts[rating] += 1
        else:
            rating_counts["unrated"] += 1
    rating_breakdown = [{"label": k, "count": v} for k, v in rating_counts.items() if v > 0]

    # Source breakdown
    source_counts: Dict[str, int] = {}
    for l in leads:
        src = l.get("source") or "unknown"
        source_counts[src] = source_counts.get(src, 0) + 1
    source_breakdown = sorted(
        [{"label": k, "count": v} for k, v in source_counts.items()],
        key=lambda x: x["count"], reverse=True,
    )

    # Location breakdown
    location_counts: Dict[str, int] = {}
    for l in leads:
        loc = l.get("company_location") or "Unknown"
        location_counts[loc] = location_counts.get(loc, 0) + 1
    location_breakdown = sorted(
        [{"label": k, "count": v} for k, v in location_counts.items()],
        key=lambda x: x["count"], reverse=True,
    )

    # Pipeline funnel
    suggested = sum(1 for l in leads if l.get("enrichment_status") == "suggested")
    enriched = sum(1 for l in leads if l.get("enrichment_status") == "enriched")
    watchlisted = watchlist_result.count if watchlist_result.count else 0
    qualified = sum(1 for l in leads if l.get("status") in ("qualified", "sequenced", "replied", "meeting", "closed"))
    sequenced = sum(1 for l in leads if l.get("status") == "sequenced")

    return {
        "industry_breakdown": industry_breakdown,
        "company_size_breakdown": size_breakdown,
        "rating_breakdown": rating_breakdown,
        "source_breakdown": source_breakdown,
        "location_breakdown": location_breakdown,
        "pipeline_funnel": {
            "suggested": suggested,
            "enriched": enriched,
            "watchlisted": watchlisted,
            "qualified": qualified,
            "sequenced": sequenced,
        },
        "total_leads": len(leads),
    }


def _format_activity_detail(activity_type: str, data: dict) -> str:
    """Format activity data into a human-readable detail string."""
    if activity_type == "lead_scored":
        return f"Score: {data.get('total_score', 0)} — {data.get('recommended_action', '')}"
    elif activity_type == "reply_classified":
        return f"{data.get('classification', '')} — {data.get('key_quote', '')}"
    elif activity_type == "lead_sourced":
        return f"Sourced from {data.get('source', 'unknown')}"
    elif activity_type == "lead_sequenced":
        return f"Added to campaign (score: {data.get('score', 0)})"
    elif activity_type == "signal_detected":
        return f"Signal: {data.get('signal_type', '')} (strength: {data.get('signal_strength', 0)})"
    elif activity_type == "lead_enriched":
        return f"Enriched: {', '.join(data.get('sources', []))}"
    elif activity_type == "status_updated":
        return f"Status: {data.get('from', '')} → {data.get('to', '')}"
    return str(data)[:100]


def _calculate_weekly_stats_inproc(
    leads: List[Dict[str, Any]], replies: List[Dict[str, Any]],
) -> List[WeeklyStat]:
    """Compute weekly stats from already-fetched rows.

    Used to issue 16 sequential queries (8 weeks * 2 tables); now it's
    pure Python over the rows the dashboard already pulled. For an org
    with even a few thousand leads the round-trip savings dominate.
    """
    now = datetime.now(timezone.utc)
    stats: List[WeeklyStat] = []

    for weeks_ago in range(7, -1, -1):
        week_start = now - timedelta(weeks=weeks_ago + 1)
        week_end = now - timedelta(weeks=weeks_ago)
        start_str = week_start.isoformat()
        end_str = week_end.isoformat()
        iso_week = week_start.strftime("%G-W%V")

        leads_in_week = [
            l for l in leads
            if l.get("created_at") and start_str <= l["created_at"] < end_str
        ]
        replies_in_week = [
            r for r in replies
            if r.get("received_at") and start_str <= r["received_at"] < end_str
        ]
        positive = sum(
            1 for r in replies_in_week
            if r.get("classification") in ("INTERESTED", "CURIOUS")
        )

        stats.append(WeeklyStat(
            week=iso_week,
            leads_sourced=len(leads_in_week),
            replies=len(replies_in_week),
            positive_replies=positive,
        ))

    return stats
