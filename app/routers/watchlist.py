"""Watchlist API endpoints — manage target accounts and alerts."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

# Watchlist News card hides anything older than this. Watchlist signals
# (funding, hiring, leadership changes) stay relevant longer than CVE alerts
# because the user might not have seen them in the moment.
WATCHLIST_ALERT_DISPLAY_DAYS = 14

from app.database import get_supabase
from app.models.schemas import (
    WatchlistAddRequest,
    WatchlistAlertUpdateRequest,
    WatchlistContactPostsRequest,
    WatchlistDeleteRequest,
    WatchlistMonitorRequest,
    WatchlistRefreshRequest,
)
from app.services.activity import log_activity
from app.services.apify_client import scrape_linkedin_posts
from app.services.domain_resolver import canonicalise_domain
from app.services.enrichers.linkedin import enrich_linkedin_content
from app.services.billing import TIER_LIMITS
from app.services.watchlist.batch_monitor import (
    run_daily_monitoring,
    run_monitoring_all_orgs,
)
from app.services.watchlist.monitor import monitor_account
from app.services.org_features import monitoring_default as _monitoring_default

logger = logging.getLogger(__name__)

router = APIRouter(tags=["watchlist"])


# ---------------------------------------------------------------------------
# Endpoint 1: Add a company to the watchlist
# ---------------------------------------------------------------------------
@router.post("/add")
def add_to_watchlist(req: WatchlistAddRequest):
    """Add a company to the target account watchlist."""
    db = get_supabase()

    # Check tier limits — read from the top-level `tier` column.
    org_resp = db.table("organisations").select("*").eq("id", req.org_id).execute()
    if not org_resp.data:
        return {"error": "Organisation not found"}
    org_config = org_resp.data[0]
    tier = org_config.get("tier") or "scout"

    # Caps tiers (solo/starter) use their own structured caps, not the
    # generic per-tier number. enforce_account_cap is a no-op for others.
    from app.services.plan_limits import enforce_account_cap, is_capped
    enforce_account_cap(req.org_id, db)
    if is_capped(tier):
        limit = 10_000  # handled by enforce_account_cap above
    else:
        limit = TIER_LIMITS.get(tier, 5)

    # Count existing entries
    count_resp = (
        db.table("watchlist")
        .select("id", count="exact")
        .eq("org_id", req.org_id)
        .execute()
    )
    current_count = count_resp.count if count_resp.count is not None else len(count_resp.data or [])

    if current_count >= limit:
        raise HTTPException(status_code=429, detail={
            "error": "watchlist_limit_reached",
            "message": f"You've reached your watchlist limit of {limit} accounts on the {tier.title()} plan. Upgrade your plan to monitor more companies.",
            "current_count": current_count,
            "limit": limit,
            "tier": tier,
        })

    # If the caller links this watchlist entry to an existing enriched lead,
    # seed the snapshot from the lead's enrichment_data so the first monitoring
    # run detects changes against real enrichment rather than an empty baseline.
    baseline_snapshot: Dict[str, Any] = {}
    if req.lead_id:
        lead_resp = (
            db.table("leads")
            .select("enrichment_data")
            .eq("id", req.lead_id)
            .eq("org_id", req.org_id)
            .execute()
        )
        if lead_resp.data:
            lead_enrichment = lead_resp.data[0].get("enrichment_data") or {}
            # Project the enrichment_data keys into the watchlist snapshot shape.
            for key in (
                "predictleads_news",
                "predictleads_jobs",
                "predictleads_tech",
                "predictleads_financing",
            ):
                if key in lead_enrichment:
                    # financing lives under `financing` in the watchlist snapshot
                    target_key = "financing" if key == "predictleads_financing" else key
                    baseline_snapshot[target_key] = lead_enrichment[key]

    # Canonicalise the domain before it becomes the account's key: adopt a
    # same-brand redirect target; surface an Apollo-blind sibling domain as
    # a suggestion (octopusenergy.com vs octopus.energy) rather than filing
    # the account somewhere contact discovery will always come up empty.
    canon = canonicalise_domain(req.company_domain or "", req.company_name or "")
    company_domain = canon["domain"] or req.company_domain

    # Insert watchlist entry
    row = {
        "org_id": req.org_id,
        "company_name": req.company_name,
        "company_domain": company_domain,
        "target_contact_name": req.target_contact_name,
        "target_contact_title": req.target_contact_title,
        "target_contact_linkedin": req.target_contact_linkedin,
        "notes": req.notes,
        "priority": req.priority,
        # Research by default for decoupled-monitoring orgs; monitored
        # for everyone else (org_config already loaded above).
        "monitoring_active": _monitoring_default(req.org_id, org_config.get("icp_config")),
        "lead_id": req.lead_id,
        "latest_snapshot": baseline_snapshot,
    }

    try:
        insert_resp = db.table("watchlist").insert(row).execute()
    except Exception as e:
        error_msg = str(e)
        if "duplicate" in error_msg.lower() or "unique" in error_msg.lower():
            return {"error": f"{company_domain} is already on your watchlist"}
        raise

    entry = insert_resp.data[0] if insert_resp.data else row

    # Flat 5-credit charge for the add — includes the baseline monitor
    # sweep below (monitor_account skips its own charge on first run).
    from app.services import credits as _credits
    _credits.charge(
        db,
        org_id=req.org_id,
        action="add_to_watchlist",
        related_id=str(entry.get("id") or ""),
        metadata={"domain": company_domain, "company_name": req.company_name},
    )

    # Run initial monitoring sweep (first run — no alerts, just baseline)
    try:
        monitor_account(db, entry, org_config, is_first_run=True)
        # Reload entry to get updated snapshot
        reload_resp = (
            db.table("watchlist")
            .select("*")
            .eq("id", entry["id"])
            .execute()
        )
        if reload_resp.data:
            entry = reload_resp.data[0]
    except Exception as e:
        logger.error("Initial monitoring sweep failed for %s: %s", req.company_domain, e)

    # Capture initial LinkedIn posts baseline if contact URL provided
    if req.target_contact_linkedin:
        try:
            posts = scrape_linkedin_posts(req.target_contact_linkedin, max_posts=10)
            if posts:
                snapshot = entry.get("latest_snapshot") or {}
                snapshot["contact_posts_count"] = len(posts)
                now_iso = datetime.now(timezone.utc).isoformat()
                db.table("watchlist").update({
                    "latest_snapshot": snapshot,
                    "updated_at": now_iso,
                }).eq("id", entry["id"]).execute()
                # Reload again
                reload_resp = db.table("watchlist").select("*").eq("id", entry["id"]).execute()
                if reload_resp.data:
                    entry = reload_resp.data[0]
                logger.info("Initial LinkedIn posts baseline: %d posts for %s", len(posts), req.target_contact_linkedin)
        except Exception as e:
            logger.error("Initial LinkedIn posts scrape failed for %s: %s", req.target_contact_linkedin, e)

    log_activity(
        db, req.org_id, None, "watchlist_add",
        {"company": req.company_name, "domain": company_domain, "priority": req.priority},
    )

    return {
        "status": "added",
        "watchlist_entry": entry,
        **({"domain_canonicalised_from": canon["canonicalised_from"]} if canon["canonicalised_from"] else {}),
        **({"domain_suggestion": canon["suggestion"]} if canon["suggestion"] else {}),
    }


# ---------------------------------------------------------------------------
# Endpoint 2: List watchlist entries for an org
# ---------------------------------------------------------------------------
@router.get("/{org_id}")
def list_watchlist(org_id: str):
    """List all watchlist entries for an org with alert counts."""
    db = get_supabase()

    wl_resp = db.table("watchlist").select("*").eq("org_id", org_id).execute()
    entries = wl_resp.data or []

    # Get unseen alert counts per watchlist entry
    alerts_resp = (
        db.table("watchlist_alerts")
        .select("watchlist_id, id, alert_title, alert_type, urgency, detected_at, status")
        .eq("org_id", org_id)
        .execute()
    )
    alerts = alerts_resp.data or []

    # Build alert summaries per watchlist_id
    alert_map: Dict[str, Dict[str, Any]] = {}
    for a in alerts:
        wid = a.get("watchlist_id")
        if wid not in alert_map:
            alert_map[wid] = {"unseen_count": 0, "latest_alert": None}
        if a.get("status") == "new":
            alert_map[wid]["unseen_count"] += 1
        # Track latest alert
        current_latest = alert_map[wid]["latest_alert"]
        if current_latest is None or (a.get("detected_at", "") > current_latest.get("detected_at", "")):
            alert_map[wid]["latest_alert"] = a

    # Enrich entries with alert info
    for entry in entries:
        eid = entry.get("id")
        info = alert_map.get(eid, {"unseen_count": 0, "latest_alert": None})
        entry["unseen_alerts"] = info["unseen_count"]
        entry["latest_alert"] = info["latest_alert"]

    return {"org_id": org_id, "watchlist": entries, "total": len(entries)}


# ---------------------------------------------------------------------------
# Endpoint 3: Remove from watchlist
# ---------------------------------------------------------------------------
@router.delete("/{watchlist_id}")
def remove_from_watchlist(watchlist_id: str, req: WatchlistDeleteRequest):
    """Remove a company from the watchlist."""
    db = get_supabase()

    db.table("watchlist").delete().eq("id", watchlist_id).eq("org_id", req.org_id).execute()

    log_activity(db, req.org_id, None, "watchlist_remove", {"watchlist_id": watchlist_id})

    return {"status": "removed", "watchlist_id": watchlist_id}


# ---------------------------------------------------------------------------
# Endpoint 4: Trigger monitoring for an org (fire-and-return)
# ---------------------------------------------------------------------------
def _run_monitoring_org_bg(org_id: str) -> None:
    """Background wrapper so the Supabase client is built inside the task."""
    try:
        run_daily_monitoring(get_supabase(), org_id)
    except Exception as e:
        logger.error("Background monitoring failed for %s: %s", org_id, e)


@router.post("/monitor")
def trigger_monitoring(req: WatchlistMonitorRequest, background_tasks: BackgroundTasks):
    """Trigger monitoring for all active watchlist entries for an org.

    Fires the monitoring run in the background so the HTTP caller gets
    an immediate response. Monitoring a full tier-worth of accounts can take
    10+ minutes due to Apify LinkedIn scrapes.
    """
    background_tasks.add_task(_run_monitoring_org_bg, req.org_id)
    return {
        "status": "started",
        "org_id": req.org_id,
        "message": "Watchlist monitoring is running in the background. "
                   "Alerts will appear in /watchlist/alerts as they are detected.",
    }


@router.post("/refresh-account")
def refresh_account(req: WatchlistRefreshRequest, background_tasks: BackgroundTasks):
    """On-demand monitor pass for ONE account — the dossier "Refresh"
    button (2026-07-16 sim: pre-call reps found week-old dossiers with no
    way to update them). Reuses run_one_off_monitor_pass: free for the
    org, debounced engine-side to one pass per hour per account, runs in
    the background so the button returns instantly."""
    from app.services.watchlist.monitor import run_one_off_monitor_pass
    from app.services.plan_limits import org_lapsed

    domain = (req.domain or "").strip().lower()
    if not domain:
        raise HTTPException(status_code=400, detail="domain is required")
    # "Free for the org" means free of credits, not of provider spend — a lapsed org
    # could refresh every account hourly forever (2026-08-23 audit). Same
    # 429 cap shape as the plan doors so the dashboard's upgrade/reactivate
    # modal opens.
    if org_lapsed(req.org_id):
        raise HTTPException(status_code=429, detail={
            "error": "solo_limit_reached", "cap": "accounts",
            "message": "Reactivate your subscription to refresh accounts.",
        })
    background_tasks.add_task(run_one_off_monitor_pass, req.org_id, domain)
    return {"status": "started", "domain": domain}


def _run_enrich_sweep_bg() -> None:
    """Background wrapper for the monthly enrichment sweep."""
    try:
        from app.services.enrichment_sweep import run_enrichment_sweep
        run_enrichment_sweep(get_supabase())
    except Exception as e:
        logger.error("Background enrich-sweep failed: %s", e)


@router.post("/enrich-sweep")
def trigger_enrich_sweep(background_tasks: BackgroundTasks):
    """Monthly profile re-verification for monitored accounts. Meant for an
    external cron on the 1st of the month. The weekly monitor detects change; this
    re-verifies the facts (revenue, size, stack, email posture, deep
    research) that otherwise drift after the on-add enrichment. Runs in
    the background; idempotent via a 25-day staleness guard."""
    background_tasks.add_task(_run_enrich_sweep_bg)
    return {
        "status": "started",
        "message": "Enrichment sweep running in the background. "
                   "Summary lands in the engine logs.",
    }


# ---------------------------------------------------------------------------
# Endpoint 5: Trigger monitoring for ALL orgs (fire-and-return)
# ---------------------------------------------------------------------------
def _run_monitoring_all_bg() -> None:
    """Background wrapper for the all-orgs monitoring run."""
    try:
        run_monitoring_all_orgs(get_supabase())
    except Exception as e:
        logger.error("Background monitor-all failed: %s", e)


@router.post("/monitor-all")
def trigger_monitoring_all(background_tasks: BackgroundTasks):
    """Trigger monitoring for all orgs. Meant for an external weekly cron.

    Runs in the background; returns immediately so the caller does not time out.
    """
    background_tasks.add_task(_run_monitoring_all_bg)
    return {
        "status": "started",
        "message": "Monitoring all orgs in the background. "
                   "Alerts will appear in /watchlist/alerts as they are detected.",
    }


# ---------------------------------------------------------------------------
# Endpoint 5b: Trigger monitoring for Vera Solo orgs only
# ---------------------------------------------------------------------------
def _run_monitoring_solo_bg() -> None:
    """Background wrapper for the Solo-only monitoring run."""
    try:
        run_monitoring_all_orgs(get_supabase(), tier="solo")
    except Exception as e:
        logger.error("Background monitor-solo failed: %s", e)


@router.post("/monitor-solo")
def trigger_monitoring_solo(background_tasks: BackgroundTasks):
    """Trigger monitoring for Vera Solo orgs only. One global cron covers
    every Solo org (the built-in scheduler calls the same function daily);
    orgs with their own per-org workflow are not Solo, so this never
    overlaps them.
    """
    background_tasks.add_task(_run_monitoring_solo_bg)
    return {
        "status": "started",
        "scope": "solo",
        "message": "Monitoring all Vera Solo orgs in the background.",
    }


@router.post("/solo-digest-test")
def solo_digest_test(org_id: str, since_days: int = 30):
    """Preview/send the Solo daily digest on demand (test trigger). Ignores the
    feature flag + dedupe and looks back `since_days` so a populated email goes
    out, and returns the rendered subject + text for review."""
    from app.services.solo_digest import build_and_send
    return build_and_send(get_supabase(), org_id, since_days=since_days, force=True)


# ---------------------------------------------------------------------------
# Endpoint 6: Get alerts for an org
# ---------------------------------------------------------------------------
@router.get("/alerts/{org_id}")
def get_alerts(
    org_id: str,
    status: Optional[str] = Query(None),
    alert_type: Optional[str] = Query(None),
    watchlist_id: Optional[str] = Query(None),
    limit: int = Query(20),
    days: int = Query(WATCHLIST_ALERT_DISPLAY_DAYS),
):
    """Get all alerts for an org with optional filters.

    Only returns alerts detected in the last `days` days (default 14) so the
    Watchlist News card stays fresh. Older alerts stay in the DB for
    historical record. Pass `days=0` to bypass the filter and see everything.
    """
    db = get_supabase()

    query = (
        db.table("watchlist_alerts")
        .select("*")
        .eq("org_id", org_id)
        .order("detected_at", desc=True)
        .limit(limit)
    )

    if status:
        query = query.eq("status", status)
    if alert_type:
        query = query.eq("alert_type", alert_type)
    if watchlist_id:
        query = query.eq("watchlist_id", watchlist_id)
    if days and days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        query = query.gte("detected_at", cutoff)

    resp = query.execute()
    return {
        "org_id": org_id,
        "alerts": resp.data or [],
        "count": len(resp.data or []),
        "filtered_to_last_days": days if days > 0 else None,
    }


# ---------------------------------------------------------------------------
# Endpoint 7: Update alert status
# ---------------------------------------------------------------------------
@router.patch("/alerts/{alert_id}")
def update_alert(alert_id: str, req: WatchlistAlertUpdateRequest):
    """Update alert status (mark as seen, actioned, dismissed)."""
    db = get_supabase()

    update_data: Dict[str, Any] = {"status": req.status}
    now_iso = datetime.now(timezone.utc).isoformat()

    if req.status == "seen":
        update_data["seen_at"] = now_iso
    elif req.status == "actioned":
        update_data["actioned_at"] = now_iso

    db.table("watchlist_alerts").update(update_data).eq("id", alert_id).eq("org_id", req.org_id).execute()

    return {"status": "updated", "alert_id": alert_id, "new_status": req.status}


# ---------------------------------------------------------------------------
# Endpoint 8: Submit LinkedIn posts for a watched contact
# ---------------------------------------------------------------------------
@router.post("/{watchlist_id}/contact-posts")
def submit_contact_posts(watchlist_id: str, req: WatchlistContactPostsRequest):
    """Submit LinkedIn posts for a watched contact (from Apify or manual input)."""
    db = get_supabase()

    # Fetch watchlist entry
    wl_resp = (
        db.table("watchlist")
        .select("*")
        .eq("id", watchlist_id)
        .eq("org_id", req.org_id)
        .execute()
    )
    if not wl_resp.data:
        return {"error": "Watchlist entry not found"}
    entry = wl_resp.data[0]

    # Analyse posts using LinkedIn content analyser, framed around what the
    # org sells so signals are scored against their actual buying triggers.
    from app.services.client_context import get_client_context
    client_context = get_client_context(db, req.org_id)
    posts_dicts = [p.dict() for p in req.posts]
    analysis = enrich_linkedin_content(
        first_name=entry.get("target_contact_name", "").split(" ")[0] if entry.get("target_contact_name") else "",
        last_name=entry.get("target_contact_name", "").split(" ")[-1] if entry.get("target_contact_name") else "",
        title=entry.get("target_contact_title", ""),
        company_name=entry.get("company_name", ""),
        recent_posts=posts_dicts,
        client_context=client_context,
    )

    # Update snapshot with contact post data
    snapshot = entry.get("latest_snapshot") or {}
    old_post_count = snapshot.get("contact_posts_count", 0)
    new_post_count = len(req.posts)

    snapshot["contact_posts_count"] = old_post_count + new_post_count
    snapshot["contact_analysis"] = analysis

    now_iso = datetime.now(timezone.utc).isoformat()
    db.table("watchlist").update({
        "latest_snapshot": snapshot,
        "updated_at": now_iso,
    }).eq("id", watchlist_id).execute()

    # Generate alert if there are relevant signals in the posts
    new_alerts = []
    if analysis and analysis.get("status") != "linkedin_content_not_checked":
        relevant_signals = []
        if analysis.get("mentions_growth"):
            relevant_signals.append("mentions growth")
        if analysis.get("mentions_hiring"):
            relevant_signals.append("mentions hiring")
        if analysis.get("mentions_funding"):
            relevant_signals.append("mentions funding")
        if analysis.get("mentions_challenges"):
            relevant_signals.append("mentions challenges")
        if analysis.get("mentions_sales_or_pipeline"):
            relevant_signals.append("mentions sales/pipeline")

        if relevant_signals:
            # Fetch org config for alert generation
            org_resp = db.table("organisations").select("*").eq("id", req.org_id).execute()
            org_config = org_resp.data[0] if org_resp.data else {}

            from app.services.watchlist.alert_generator import generate_alert

            change_data = {
                "details": (
                    f"Target contact {entry.get('target_contact_name', 'Unknown')} "
                    f"posted on LinkedIn. Signals: {', '.join(relevant_signals)}"
                ),
                "data": {
                    "signals": relevant_signals,
                    "post_count": new_post_count,
                    "personalisation_hooks": analysis.get("personalisation_hooks", []),
                },
            }

            alert_content = generate_alert(
                change_type="contact_activity",
                change_data=change_data,
                watchlist_entry=entry,
                org_config=org_config,
            )

            alert_row = {
                "org_id": req.org_id,
                "watchlist_id": watchlist_id,
                "alert_type": "contact_activity",
                "alert_title": alert_content.get("alert_title", "Contact activity detected"),
                "alert_summary": alert_content.get("alert_summary", change_data["details"]),
                "alert_data": change_data.get("data", {}),
                "suggested_action": alert_content.get("suggested_action"),
                "approach_angle": alert_content.get("approach_angle"),
                "urgency": alert_content.get("urgency", "medium"),
                "status": "new",
            }
            db.table("watchlist_alerts").insert(alert_row).execute()
            new_alerts.append(alert_row)

    log_activity(
        db, req.org_id, None, "watchlist_contact_posts",
        {
            "watchlist_id": watchlist_id,
            "posts_submitted": new_post_count,
            "alerts_generated": len(new_alerts),
        },
    )

    return {
        "status": "processed",
        "posts_analysed": new_post_count,
        "analysis": analysis,
        "alerts_generated": len(new_alerts),
    }
