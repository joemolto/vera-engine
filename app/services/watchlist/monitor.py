"""Single account monitoring — the core watchlist engine.

v2 design (2026-04): PredictLeads-first, with Jina for website + blog change
detection and Perplexity Sonar for real-time company news. Runs weekly.

v3 design (2026-05): two-phase + heat-based cadence.
  - Phase A: cheap PL bundle (cached) → detect changes
  - Phase B: expensive (Perplexity, Jina, Apify) — only on Phase-A changes
            OR on first run (snapshot population)
  - next_monitor_at: persisted, heat-based:
      hot (changes this run)            → +2d
      warm (changes in last 30d)        → +7d
      cold (quiet)                      → +30d
Lets watchlists scale without per-pass cost ballooning.
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from supabase import Client

from app.services.apify_client import scrape_linkedin_posts
from app.services.credits import charge as charge_credits
from app.services.enrichers.deep_research import enrich_deep_research
from app.services.key_contacts import discover_key_contacts
from app.services.linkedin_person import (
    scrape_profile as li_scrape_profile,
    scrape_person_posts as li_scrape_posts,
    scrape_person_activity as li_scrape_activity,
)
from app.services.person_intent import classify_items_parallel
from app.services.signal_writer import insert_signals
from app.services.why_this_matters import refresh_why_this_matters
from app.services.security_team_profiler import refresh_security_team_profile
from app.services.account_profile_scorer import refresh_profile_score
from app.services.enrichers.cache import set_cache
from app.services.enrichers.predictleads import (
    _fetch_financing,
    _fetch_jobs,
    _fetch_news_events,
    _fetch_technology,
)
from app.services.watchlist.alert_generator import generate_alert
from app.services.watchlist.change_detector import (
    detect_blog_changes,
    detect_funding_changes,

    detect_perplexity_changes,
    detect_predictleads_job_changes,
    detect_predictleads_news_changes,
    detect_tech_stack_changes,
    detect_website_changes,
    hash_content,
)

logger = logging.getLogger(__name__)

# Jina pages to monitor — narrowed from the v1 five pages to just these two.
# Home/careers/team pages change constantly due to dynamic content and trackers,
# so their hashes produced far more noise than signal.
WATCH_PAGES = ["/about", "/blog"]

# Timeout budget for the PredictLeads parallel fetch block.
PL_PARALLEL_TIMEOUT = 25

# How fresh monitoring demands the shared deep-research profile to be.
# Within this window any org's cached research is reused (cost saving);
# older than this we re-fetch and write back for everyone. Tune freely:
# 7d matches the warm monitoring cadence.
RESEARCH_CACHE_MAX_AGE_DAYS = 7


def _maybe_backfill_research(db, org_id: str, domain: str) -> None:
    """Run the one-time deep company enrichment if `domain` has never been
    researched (bare stub: no Apollo / PredictLeads / deep-research data).
    Idempotent — once a profile exists this returns immediately."""
    domain = (domain or "").strip().lower()
    if not domain:
        return
    try:
        res = (
            db.table("companies")
            .select("apollo_data, predictleads_data, deep_research_data")
            .eq("domain", domain)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logger.warning("research backfill lookup failed for %s: %s", domain, e)
        return
    row = (res.data if res else None) or {}  # maybe_single() returns None when no row

    def _empty(v: Any) -> bool:
        return not v or v == {}

    already_researched = not (
        _empty(row.get("apollo_data"))
        and _empty(row.get("predictleads_data"))
        and _empty(row.get("deep_research_data"))
    )
    if already_researched:
        return

    logger.info("Backfilling deep research for un-researched watched account %s", domain)
    from app.services.company_enricher import enrich_company_only
    enrich_company_only(db, org_id, domain)


def monitor_account(
    db: Client,
    watchlist_entry: Dict[str, Any],
    org_config: Dict[str, Any],
    is_first_run: bool = False,
) -> List[Dict[str, Any]]:
    """Run all monitoring checks for a single watchlist account.

    Args:
        db: Supabase client.
        watchlist_entry: Row from the watchlist table.
        org_config: Organisation row with name, icp_config, client_context.
        is_first_run: If True, populate snapshot only — no alerts generated.

    Returns:
        List of new alert dicts that were stored.
    """
    domain = watchlist_entry["company_domain"]
    company_name = watchlist_entry["company_name"]
    org_id = watchlist_entry["org_id"]
    old_snapshot = watchlist_entry.get("latest_snapshot") or {}

    # One-time research backfill. A watched account that was never researched
    # (added via the monitor/bulk path) has a bare company profile — only
    # social signals, no Apollo/PredictLeads/Perplexity. The accept flow
    # assumed "the first monitor pass enriches it", but monitoring never ran
    # the deep enrichment. Do it here, once, when the profile is missing — so
    # no watched account is left with an empty dossier. Idempotent: once a
    # profile exists this no-ops.
    try:
        _maybe_backfill_research(db, watchlist_entry["org_id"], domain)
    except Exception as e:
        logger.warning("research backfill failed for %s: %s", domain, e)
    new_snapshot: Dict[str, Any] = {}
    phase_a_changes: List[Dict[str, Any]] = []
    phase_b_changes: List[Dict[str, Any]] = []
    # Vendor failures during this pass. When a vendor call fails we
    # reuse the previous snapshot's data (better stale than empty), but
    # that staleness must be VISIBLE — recorded in the snapshot and
    # logged loudly at the end of the pass — or a dossier can quietly
    # go a month without refreshing and nobody knows why.
    vendor_failures: List[str] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # PHASE A — cheap PL bundle (cached). Four endpoints in parallel.
    # If nothing changed here, we SKIP the expensive Phase B (Perplexity,
    # Jina, Apify) — they're the real cost driver per pass.
    # ------------------------------------------------------------------
    pl_results = _fetch_predictleads_bundle(domain, db=db)

    pl_news = pl_results.get("news_events")
    if pl_news:
        new_snapshot["predictleads_news"] = pl_news
        if not is_first_run:
            phase_a_changes.extend(detect_predictleads_news_changes(old_snapshot, pl_news))
    else:
        new_snapshot["predictleads_news"] = old_snapshot.get("predictleads_news") or {}

    pl_jobs = pl_results.get("jobs")
    if pl_jobs:
        new_snapshot["predictleads_jobs"] = pl_jobs
        if not is_first_run:
            phase_a_changes.extend(detect_predictleads_job_changes(old_snapshot, pl_jobs))
    else:
        new_snapshot["predictleads_jobs"] = old_snapshot.get("predictleads_jobs") or {}

    pl_tech = pl_results.get("technology")
    if pl_tech:
        new_snapshot["predictleads_tech"] = pl_tech
        if not is_first_run:
            phase_a_changes.extend(detect_tech_stack_changes(old_snapshot, pl_tech))
    else:
        new_snapshot["predictleads_tech"] = old_snapshot.get("predictleads_tech") or {}

    pl_financing = pl_results.get("financing")
    if pl_financing:
        new_snapshot["financing"] = pl_financing
        if not is_first_run:
            phase_a_changes.extend(detect_funding_changes(old_snapshot, pl_financing))
    else:
        new_snapshot["financing"] = old_snapshot.get("financing") or {}

    # ------------------------------------------------------------------
    # PHASE B — expensive checks (Perplexity, Jina, Apify).
    # Only fire when Phase A flagged a change OR this is a first-run
    # snapshot population. Otherwise we leave Phase B fields at their
    # prior snapshot value and skip the cost.
    # ------------------------------------------------------------------
    should_run_phase_b = is_first_run or bool(phase_a_changes)

    if should_run_phase_b:
        # --------------------------------------------------------------
        # Perplexity Sonar — real-time company news (replaces Google News RSS)
        # --------------------------------------------------------------
        try:
            client_context = org_config.get("client_context") or {}
            # Reuse the shared research cache when it's younger than the
            # window; otherwise fetch fresh and write back so every org's
            # enrichments reuse monitoring's freshness instead of re-paying.
            perplexity = enrich_deep_research(
                company_name, domain, client_context=client_context,
                db=db, max_age_seconds=RESEARCH_CACHE_MAX_AGE_DAYS * 86400,
            )
            if perplexity:
                new_snapshot["perplexity"] = perplexity
                if not is_first_run:
                    phase_b_changes.extend(detect_perplexity_changes(old_snapshot, perplexity))
            else:
                new_snapshot["perplexity"] = old_snapshot.get("perplexity") or {}
        except Exception as e:
            logger.error("Perplexity Sonar failed for %s: %s", domain, e)
            vendor_failures.append(f"perplexity: {e}")
            new_snapshot["perplexity"] = old_snapshot.get("perplexity") or {}

        time.sleep(1)

        # --------------------------------------------------------------
        # Jina — website change detection (/about) + blog post detection (/blog)
        # --------------------------------------------------------------
        try:
            page_data = _fetch_page_data(domain)
            new_snapshot["page_hashes"] = page_data.get("hashes", {})
            new_snapshot["blog_titles"] = page_data.get("blog_titles", [])
            if not is_first_run:
                old_hashes = old_snapshot.get("page_hashes") or {}
                phase_b_changes.extend(detect_website_changes(old_hashes, new_snapshot["page_hashes"]))
                phase_b_changes.extend(
                    detect_blog_changes(
                        old_snapshot.get("blog_titles") or [],
                        new_snapshot["blog_titles"],
                    )
                )
        except Exception as e:
            logger.error("Website/blog monitoring failed for %s: %s", domain, e)
            vendor_failures.append(f"website_jina: {e}")
            new_snapshot["page_hashes"] = old_snapshot.get("page_hashes") or {}
            new_snapshot["blog_titles"] = old_snapshot.get("blog_titles") or []

        time.sleep(1)
    else:
        # Phase A saw nothing — preserve prior Phase B state untouched.
        # This is the cost-saving path: 90%+ of weekly passes land here.
        new_snapshot["perplexity"] = old_snapshot.get("perplexity") or {}
        new_snapshot["page_hashes"] = old_snapshot.get("page_hashes") or {}
        new_snapshot["blog_titles"] = old_snapshot.get("blog_titles") or []
        logger.info(
            "Phase B skipped for %s — no Phase-A changes (saved ~$3–5 of Perplexity+Jina+Apify spend)",
            domain,
        )

    # ------------------------------------------------------------------
    # Target contact LinkedIn activity — Phase B (Apify cost).
    # Only runs when Phase A flagged a change or on first run, matching
    # the same cost-saving rule as Perplexity + Jina above.
    # ------------------------------------------------------------------
    contact_linkedin = watchlist_entry.get("target_contact_linkedin")
    if contact_linkedin and should_run_phase_b:
        try:
            # Use a shorter timeout than the enrichment pipeline — the weekly
            # watchlist cron processes many contacts and shouldn't stall 3min
            # per slow Apify actor run. Losing the signal on one account is OK.
            posts = scrape_linkedin_posts(contact_linkedin, max_posts=10, timeout=60, db=db) or []
            new_post_count = len(posts)
            old_post_count = old_snapshot.get("contact_posts_count", 0) or 0
            new_snapshot["contact_posts_count"] = new_post_count

            if not is_first_run and new_post_count > old_post_count:
                from app.prompts.watchlist import CONTACT_ACTIVITY_PROMPT
                from app.services.claude import call_claude

                posts_text = "\n\n".join(
                    f"### Post {i+1}\n{p.get('text', '')}\n[Date: {p.get('date', 'N/A')}, "
                    f"Likes: {p.get('likes', 0)}, Comments: {p.get('comments', 0)}]"
                    for i, p in enumerate(posts[: new_post_count - old_post_count])
                )

                icp_config = org_config.get("icp_config") or {}
                client_services = (
                    icp_config.get("our_services")
                    or (org_config.get("client_context") or {}).get("what_they_sell")
                    or "Not specified"
                )

                prompt = CONTACT_ACTIVITY_PROMPT.format(
                    contact_name=watchlist_entry.get("target_contact_name", "Unknown"),
                    contact_title=watchlist_entry.get("target_contact_title", "Unknown"),
                    company_name=company_name,
                    client_services=client_services,
                    posts_text=posts_text,
                )
                contact_analysis = call_claude(prompt)
                new_snapshot["contact_analysis"] = contact_analysis

                if contact_analysis.get("relevant_signals_found"):
                    phase_b_changes.append({
                        "change_type": "contact_activity",
                        "details": (
                            f"Target contact {watchlist_entry.get('target_contact_name', 'Unknown')} "
                            f"posted {new_post_count - old_post_count} new LinkedIn post(s). "
                            f"Signals: {', '.join(s.get('signal_type', '') for s in contact_analysis.get('signals', []))}"
                        ),
                        "data": {
                            "new_posts_count": new_post_count - old_post_count,
                            "signals": contact_analysis.get("signals", []),
                            # "What to say" copy is outreach — omitted for
                            # intelligence_only orgs (product rule 10).
                            "best_conversation_starter": (
                                ""
                                if (org_config.get("features") or {}).get("intelligence_only")
                                else contact_analysis.get("best_conversation_starter", "")
                            ),
                        },
                    })
        except Exception as e:
            logger.error(
                "LinkedIn contact monitoring failed for %s: %s",
                watchlist_entry.get("target_contact_name", "Unknown"), e,
            )
            new_snapshot["contact_posts_count"] = old_snapshot.get("contact_posts_count", 0)
    else:
        new_snapshot["contact_posts_count"] = old_snapshot.get("contact_posts_count", 0)

    new_snapshot["last_full_check"] = now_iso
    new_snapshot["last_phase_b_check"] = now_iso if should_run_phase_b else (
        old_snapshot.get("last_phase_b_check") or now_iso
    )

    # Surface this pass's vendor failures. One loud summary line per
    # pass (greppable: "VENDOR FAILURES") + a snapshot marker so the
    # staleness is queryable. A clean pass clears any previous marker
    # because new_snapshot starts empty.
    if vendor_failures:
        new_snapshot["vendor_failures"] = {
            "at": now_iso,
            "failures": vendor_failures,
        }
        logger.error(
            "VENDOR FAILURES during monitor pass for %s: %s — affected "
            "fields reuse the previous snapshot (stale until a clean pass)",
            domain,
            "; ".join(vendor_failures),
        )

    # ------------------------------------------------------------------
    # PHASE D — per-account person intelligence (opt-in, paid extra).
    # Gated on companies.person_intel_enabled. When 'off' (default),
    # this is a no-op. 'top1' monitors the highest-ICP contact,
    # 'deep' monitors top 3. Failure never aborts the monitor pass;
    # signals write directly to the signals table.
    # ------------------------------------------------------------------
    try:
        company_id = _resolve_company_id(db, domain, company_name)
        if company_id:
            _run_phase_d_person_intel(
                db,
                company_id=company_id,
                org_id=watchlist_entry["org_id"],
            )
    except Exception as e:
        logger.error("Phase D person intel failed for %s: %s", domain, e)

    # ------------------------------------------------------------------
    # PHASE E — refresh the AI "why this account matters" paragraph.
    # Internally hashes the facts and only calls Haiku if something
    # changed since last generation, so it's cheap on quiet accounts
    # and refreshes automatically when a new buying-trigger signal
    # fires in any of the earlier phases. Failure never blocks.
    # ------------------------------------------------------------------
    try:
        if company_id:
            refresh_why_this_matters(
                db,
                company_id=company_id,
                org_id=watchlist_entry["org_id"],
                trigger="monitor_pass",
            )
    except Exception as e:
        logger.error("Phase E why_this_matters failed for %s: %s", domain, e)

    # ------------------------------------------------------------------
    # PHASE F — refresh the security team headcount profile (Apollo).
    # Internally skips when the cached profile is <30 days old, so this
    # is a no-op on most monitor passes. When stale, runs one free
    # Apollo title search and persists the bucketed counts. Feeds the
    # Maturity axis of Phase G's profile score.
    # ------------------------------------------------------------------
    try:
        if company_id:
            refresh_security_team_profile(db, company_id=company_id)
    except Exception as e:
        logger.error("Phase F security_team_profile failed for %s: %s", domain, e)

    # ------------------------------------------------------------------
    # PHASE F2 — credential exposure (LeakRadar stealer-log / dark-web).
    # Runs every pass (free searches, never unlocks), internally cache-
    # gated (3-day TTL) so we re-hit LeakRadar at most every few days per
    # account regardless of cadence. Refreshes the exposure summary +
    # Risk signals so newly-surfaced credentials feed straight into Phase
    # G's score. NOT gated by the Phase-B cost-saving skip — a new leak
    # must be able to move the score even on an otherwise-quiet account.
    # ------------------------------------------------------------------
    try:
        if company_id:
            from app.services.enrichers.credential_exposure import (
                check_credential_exposure,
                refresh_credential_signals,
            )
            cred = check_credential_exposure(db, domain)
            if cred is not None:
                db.table("companies").update(
                    {"credential_exposure": cred}
                ).eq("id", company_id).execute()
                refresh_credential_signals(
                    db,
                    org_id=watchlist_entry["org_id"],
                    company_id=company_id,
                    summary=cred,
                )
    except Exception as e:
        logger.error("Phase F2 credential_exposure failed for %s: %s", domain, e)

    # ------------------------------------------------------------------
    # PHASE F2b — perimeter scan (what they expose to the internet, and
    # whether the version they run has known CVEs). Internally cache-gated
    # to 7 days: this is the slowest thing in the pass (Certificate
    # Transparency alone is ~40s on a busy domain) and internet-facing
    # infrastructure changes slowly.
    #
    # Only writes when a version was OBSERVED and maps to a verified CPE.
    # Unversioned findings (Citrix, F5 and Cisco all refuse to disclose)
    # produce no CVE claim, by design — see cve_match.
    #
    # Like F2, not gated by the Phase-B skip: a newly-published CVE against
    # a version they were already running must be able to move the score on
    # an otherwise-quiet account.
    # ------------------------------------------------------------------
    try:
        if company_id:
            from app.services.cve_match import kev_ids, refresh_perimeter_signals
            refresh_perimeter_signals(
                db,
                org_id=watchlist_entry["org_id"],
                company_id=company_id,
                domain=domain,
                company_name=company_name,
                kev_ids=kev_ids(),
            )
    except Exception as e:
        logger.error("Phase F2b perimeter failed for %s: %s", domain, e)

    # ------------------------------------------------------------------
    # PHASE F3 — job watch. Read the company's real vacancies from their
    # ATS (free, dated) and turn them into demand signals: a freshly
    # posted security role (new budget) and one open 90+ days (they can't
    # fill it, which is the stronger opener). Self-throttled to weekly
    # inside watch_jobs, so running it every pass is safe.
    #
    # allow_paid=False deliberately: the free routes (ATS + careers-page
    # structured data) cover this, and letting the weekly sweep fall
    # through to the paid LinkedIn scraper across every watched account
    # is how a cheap feature becomes an expensive one by accident.
    # ------------------------------------------------------------------
    try:
        if company_id:
            from app.services.enrichers.job_watch import watch_jobs
            watch_jobs(
                db,
                org_id=watchlist_entry["org_id"],
                company_id=company_id,
                domain=domain,
                company_name=watchlist_entry.get("company_name") or domain,
                allow_paid=False,
            )
    except Exception as e:
        logger.error("Phase F3 job_watch failed for %s: %s", domain, e)

    # ------------------------------------------------------------------
    # PHASE F4 — Apollo news → commercial signals (leadership / funding /
    # M&A). A second, dated source on the Commercial axis that catches exec
    # moves and deals PredictLeads misses (bake-off 2026-07-24). Tightly
    # category-whitelisted so we add signal, not Apollo's web-crawl noise;
    # self-throttled to weekly per company, and Apollo bills only when it
    # returns results, so quiet accounts are free. Runs before Phase G so
    # a fresh funding/leadership signal counts toward the score this pass.
    # ------------------------------------------------------------------
    try:
        if company_id:
            from app.services.enrichers.apollo_news import watch_news
            watch_news(db, org_id=watchlist_entry["org_id"],
                       company_id=company_id, domain=domain,
                       company_name=watchlist_entry.get("company_name") or domain)
    except Exception as e:
        logger.error("Phase F4 apollo_news failed for %s: %s", domain, e)

    # ------------------------------------------------------------------
    # PHASE G — recompute the account Profile score (Risk / Posture /
    # Maturity / Commercial + composite + peer band). Pure aggregation
    # over the signals just written in phases A-E + the team profile
    # from Phase F. No AI calls; cheap to run every pass so the dossier
    # always reflects the freshest evidence.
    # ------------------------------------------------------------------
    try:
        if company_id:
            refresh_profile_score(
                db,
                company_id=company_id,
                org_id=watchlist_entry["org_id"],
            )
    except Exception as e:
        logger.error("Phase G profile_score failed for %s: %s", domain, e)

    # ------------------------------------------------------------------
    # Combine changes from both phases
    # ------------------------------------------------------------------
    all_changes: List[Dict[str, Any]] = phase_a_changes + phase_b_changes

    # ------------------------------------------------------------------
    # Generate alerts for detected changes
    # ------------------------------------------------------------------
    new_alerts: List[Dict[str, Any]] = []
    if not is_first_run and all_changes:
        # Reps ask WHO to contact. When the watch has no explicit target
        # contact, borrow the account's top key contact so the alert prompt
        # can name a person (best-effort — falls back to N/A as before).
        if not (watchlist_entry.get("target_contact_name") or "").strip():
            kc = _top_key_contact(db, watchlist_entry)
            if kc:
                watchlist_entry = {
                    **watchlist_entry,
                    "target_contact_name": kc.get("name"),
                    "target_contact_title": kc.get("title"),
                }
        for change in all_changes:
            try:
                alert_data = generate_alert(
                    change_type=change["change_type"],
                    change_data=change,
                    watchlist_entry=watchlist_entry,
                    org_config=org_config,
                )
                alert_row = {
                    "org_id": watchlist_entry["org_id"],
                    "watchlist_id": watchlist_entry["id"],
                    "alert_type": change["change_type"],
                    "alert_title": alert_data.get("alert_title", "Change detected"),
                    "alert_summary": alert_data.get("alert_summary", change.get("details", "")),
                    "alert_data": change.get("data", {}),
                    "suggested_action": alert_data.get("suggested_action"),
                    "approach_angle": alert_data.get("approach_angle"),
                    "urgency": alert_data.get("urgency", "medium"),
                    "status": "new",
                }
                db.table("watchlist_alerts").insert(alert_row).execute()
                new_alerts.append(alert_row)
                # Fire webhook — best-effort, never raises
                try:
                    from app.services.webhooks import enqueue_event
                    enqueue_event(db, org_id, "watchlist.alert_created", alert_row)
                except Exception as e:
                    logger.error("Webhook enqueue (watchlist.alert_created) failed: %s", e)
            except Exception as e:
                logger.error("Failed to store alert for %s: %s", domain, e)

    # ------------------------------------------------------------------
    # Update snapshot, last_monitored_at, and the heat-based next cadence.
    # ------------------------------------------------------------------
    from app.services.org_features import feature_enabled
    next_monitor_at = _compute_next_monitor_at(
        had_changes=bool(all_changes),
        last_monitored_at=watchlist_entry.get("last_monitored_at"),
        flat_weekly=feature_enabled(org_config.get("icp_config"), "decoupled_monitoring"),
    )
    try:
        db.table("watchlist").update({
            "latest_snapshot": new_snapshot,
            "last_monitored_at": now_iso,
            "next_monitor_at": next_monitor_at.isoformat(),
            "updated_at": now_iso,
        }).eq("id", watchlist_entry["id"]).execute()
    except Exception as e:
        logger.error("Failed to update snapshot for watchlist %s: %s", watchlist_entry["id"], e)

    # ------------------------------------------------------------------
    # account.went_hot webhook — fires when this pass had any material
    # changes (= next_monitor_at jumped to the hot cadence of 2d).
    # Suppressed on the first run (no baseline to be hot vs). Lets
    # customers route "this account just woke up" pings into Slack /
    # CRM the moment something material lands.
    # ------------------------------------------------------------------
    if all_changes and not is_first_run:
        try:
            from app.services.webhooks import enqueue_event
            enqueue_event(db, org_id, "account.went_hot", {
                "watchlist_id": watchlist_entry["id"],
                "company_name": company_name,
                "company_domain": domain,
                "changes_detected": len(all_changes),
                "change_types": list({c.get("change_type") for c in all_changes if c.get("change_type")}),
                "monitored_at": now_iso,
            })
        except Exception as e:
            logger.error("account.went_hot webhook enqueue failed: %s", e)

    # ------------------------------------------------------------------
    # Credit charge: 1 credit per monitor pass per account, regardless of
    # whether Phase B fired. The Phase-A-only optimisation saves provider
    # spend; it does not change what the org is charged.
    # First-run baseline sweeps are NOT charged — they're included in
    # the flat add_to_watchlist price (charged by the /watchlist/add
    # endpoint).
    # ------------------------------------------------------------------
    if not is_first_run:
        charge_credits(
            db,
            org_id=watchlist_entry["org_id"],
            action="monitor_pass",
            related_id=str(watchlist_entry["id"]),
            metadata={
                "domain": domain,
                "company_name": company_name,
                "phase_b_fired": should_run_phase_b,
                "changes_detected": len(all_changes),
            },
        )

    return new_alerts


def run_one_off_monitor_pass(org_id: str, domain: str) -> None:
    """One-off monitor pass for a single account, outside the batch cron.

    Used by the research entry points (URL paste, suggestion accept) so a
    freshly researched account gets its snapshot, signals and AI layers
    (why-this-matters, security team profile, profile score) within a couple
    of minutes instead of never — research accounts have
    monitoring_active=false, so the batch monitor won't touch them.

    First-run passes are free (monitor_account only charges when
    is_first_run=False), so this adds no cost on top of the deep_enrich
    the caller already charged.

    Debounced by `last_monitored_at` — repeat calls within the hour skip, so
    overlapping entry points don't burn a duplicate pass. Best-effort; never
    raises out of a background task.
    """
    try:
        from app.database import get_supabase

        db = get_supabase()
        wl_res = (
            db.table("watchlist")
            .select("*")
            .eq("org_id", org_id)
            .eq("company_domain", domain)
            .limit(1)
            .execute()
        )
        if not wl_res.data:
            logger.info("One-off monitor skipped — no watchlist row for %s/%s", org_id, domain)
            return
        watchlist_entry = wl_res.data[0]

        last = watchlist_entry.get("last_monitored_at")
        if last:
            try:
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - last_dt).total_seconds() < 3600:
                    logger.info(
                        "One-off monitor skipped for %s — last monitored %ss ago",
                        domain, int((datetime.now(timezone.utc) - last_dt).total_seconds()),
                    )
                    return
            except (ValueError, TypeError):
                pass

        org_res = (
            db.table("organisations")
            .select("name, icp_config, client_context")
            .eq("id", org_id)
            .limit(1)
            .execute()
        )
        if not org_res.data:
            logger.warning("One-off monitor skipped — org %s not found", org_id)
            return
        org_config = org_res.data[0]

        is_first_run = not watchlist_entry.get("latest_snapshot")

        monitor_account(db, watchlist_entry, org_config, is_first_run=is_first_run)

        # People discovery on every add (2026-08-24: 32 fresh
        # accounts, zero unlockable contacts). Phase D only discovers when
        # person intel is toggled on, so research-mode adds never populated
        # key_contacts and unlock had nothing to validate against unless
        # the caller re-sent name hints. Free Apollo search, 30-day cached,
        # best-effort — every add path funnels through this pass.
        try:
            co = (db.table("companies").select("id")
                  .ilike("domain", domain).limit(1).execute().data or [])
            if co:
                discover_key_contacts(db, company_id=co[0]["id"],
                                      org_id=org_id, max_contacts=3)
        except Exception as e:  # noqa: BLE001
            logger.warning("post-add people discovery failed for %s/%s: %s",
                           org_id, domain, e)

        logger.info(
            "One-off monitor completed for %s/%s (first_run=%s)",
            org_id, domain, is_first_run,
        )
    except Exception as e:
        logger.error("One-off monitor failed for %s/%s: %s", org_id, domain, e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Heat-based cadence. Stale accounts don't deserve weekly Perplexity passes —
# they deserve monthly check-ins. Accounts that just moved deserve daily.
CADENCE_HOT_DAYS = 2     # something changed this pass → check again in 2 days
CADENCE_WARM_DAYS = 7    # quiet now but had activity in last 30d → weekly
CADENCE_COLD_DAYS = 30   # genuinely stable → monthly


def _compute_next_monitor_at(
    *,
    had_changes: bool,
    last_monitored_at: Optional[str],
    flat_weekly: bool = False,
) -> datetime:
    """How long until this account's next monitor pass.

    flat_weekly (decoupled-monitoring orgs, 2026-06-11): always +7
    days. The user picked this account to watch and is charged a flat
    "~4 credits/month" — a predictable weekly pass beats a variable
    heat cadence they can't budget around.

    Otherwise, the legacy heat-based scheduler:
      - changes detected this pass      → HOT,  +2 days
      - quiet but recently active (<30d) → WARM, +7 days
      - otherwise                        → COLD, +30 days
    """
    now = datetime.now(timezone.utc)
    if flat_weekly:
        return now + timedelta(days=CADENCE_WARM_DAYS)
    if had_changes:
        return now + timedelta(days=CADENCE_HOT_DAYS)

    # No changes this pass — was the last pass recent?
    days_since_last = None
    if last_monitored_at:
        try:
            last_dt = datetime.fromisoformat(last_monitored_at.replace("Z", "+00:00"))
            days_since_last = (now - last_dt).days
        except (ValueError, TypeError):
            pass

    if days_since_last is not None and days_since_last <= 30:
        return now + timedelta(days=CADENCE_WARM_DAYS)
    return now + timedelta(days=CADENCE_COLD_DAYS)


def _top_key_contact(db, watchlist_entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Best key contact for a watched account: highest icp_score entry from
    the company's cached key_contacts. Never raises — alerts must send with
    or without a name."""
    try:
        domain = (watchlist_entry.get("company_domain") or "").lower()
        if not domain:
            return None
        company_id = _resolve_company_id(
            db, domain, watchlist_entry.get("company_name") or ""
        )
        if not company_id:
            return None
        row = (
            db.table("companies")
            .select("key_contacts")
            .eq("id", company_id)
            .maybe_single()
            .execute()
        ).data or {}
        contacts = row.get("key_contacts") or []
        if not isinstance(contacts, list) or not contacts:
            return None
        best = max(
            (c for c in contacts if isinstance(c, dict) and c.get("name")),
            key=lambda c: c.get("icp_score") or 0,
            default=None,
        )
        return best
    except Exception as e:
        logger.warning("top_key_contact lookup failed for %s: %s",
                       watchlist_entry.get("company_domain"), e)
        return None


def _resolve_company_id(
    db: Client, domain: str, name: str,
) -> Optional[str]:
    """Lookup or create the companies row for this watchlist entry.

    Watchlist rows carry company_domain + company_name but no
    company_id (the schema links by domain). Phases that write signals
    keyed by company_id resolve it here first.

    Returns the company_id, or None if neither lookup nor insert
    works (the caller skips its pass cleanly in that case)."""
    domain = (domain or "").strip().lower()
    if not domain:
        return None
    try:
        existing = (
            db.table("companies")
            .select("id")
            .eq("domain", domain)
            .limit(1)
            .execute()
        )
        if existing.data:
            return existing.data[0]["id"]
    except Exception as e:
        logger.warning("companies lookup for %s failed: %s", domain, e)
        return None

    # Doesn't exist yet — create a minimal row. Enrichment will fill
    # in the rest when it next touches this domain.
    try:
        inserted = (
            db.table("companies")
            .insert({"domain": domain, "name": name or domain})
            .execute()
        )
        if inserted.data:
            return inserted.data[0]["id"]
    except Exception as e:
        # Race-safe: if another monitor pass inserted concurrently the
        # unique constraint on domain will trip — re-lookup and use that.
        logger.debug("companies insert for %s raced: %s", domain, e)
        try:
            existing = (
                db.table("companies")
                .select("id")
                .eq("domain", domain)
                .limit(1)
                .execute()
            )
            if existing.data:
                return existing.data[0]["id"]
        except Exception:
            pass
    return None


# Cache types must match enrich_predictleads's endpoint_specs so the
# enricher's read path gets hits on rows the monitor wrote.
_PL_CACHE_TYPES = {
    "news_events": "predictleads_news_events",
    "jobs": "predictleads_jobs",
    "technology": "predictleads_technology",
    "financing": "predictleads_financing",
}


def _fetch_predictleads_bundle(
    domain: str,
    db: Optional[Client] = None,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """Fetch four PredictLeads endpoints in parallel.

    Returns a dict with keys: news_events, jobs, technology, financing.
    Any endpoint that fails or times out contributes None.

    With `db`, each successful response is written back to
    enrichment_cache (same keys/TTLs as enrich_predictleads) so
    monitoring passes keep the shared cache fresh for later
    enrichments. Failures are never cached — the monitor always
    fetches fresh regardless (change detection needs it), so there is
    no cache READ here, only the write-back.
    """
    results: Dict[str, Optional[Dict[str, Any]]] = {
        "news_events": None,
        "jobs": None,
        "technology": None,
        "financing": None,
    }

    endpoints = {
        "news_events": _fetch_news_events,
        "jobs": _fetch_jobs,
        "technology": _fetch_technology,
        "financing": _fetch_financing,
    }

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {name: executor.submit(fn, domain) for name, fn in endpoints.items()}
        for name, future in futures.items():
            try:
                result = future.result(timeout=PL_PARALLEL_TIMEOUT)
            except FuturesTimeoutError:
                logger.warning("PredictLeads %s timed out for %s", name, domain)
                continue
            except Exception as e:
                logger.warning("PredictLeads %s failed for %s: %s", name, domain, e)
                continue

            results[name] = result
            # Mirror enrich_predictleads: cache the payload, or the _none
            # sentinel when the API legitimately had nothing for this domain.
            if db is not None:
                payload = result if isinstance(result, dict) else {"_none": True}
                set_cache(db, domain, _PL_CACHE_TYPES[name], None, payload)

    return results


def _fetch_page_data(domain: str) -> Dict[str, Any]:
    """Fetch /about and /blog via Jina. Returns hashes + blog post titles."""
    hashes: Dict[str, str] = {}
    blog_titles: List[str] = []

    for path in WATCH_PAGES:
        url = f"https://r.jina.ai/https://{domain}{path}"
        try:
            with httpx.Client(timeout=15, follow_redirects=True) as client:
                response = client.get(
                    url,
                    headers={"Accept": "text/plain", "X-Timeout": "10"},
                )
                response.raise_for_status()
                text = response.text.strip()
                if text and len(text) > 50:
                    hashes[path] = hash_content(text)
                    if path == "/blog":
                        blog_titles = _extract_blog_titles(text)
        except Exception as e:
            logger.debug("Could not fetch %s%s: %s", domain, path, e)

    return {"hashes": hashes, "blog_titles": blog_titles}


_MARKDOWN_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.MULTILINE)
_MARKDOWN_LINK_RE = re.compile(r"^\s*[-*]\s+\[([^\]]{6,160})\]\(", re.MULTILINE)


def _extract_blog_titles(text: str) -> List[str]:
    """Pull candidate blog post titles from a Jina markdown dump.

    Jina returns pages as markdown. Blog listing pages typically render post
    titles either as H2/H3 headings or as bullet-list links. We extract both
    and deduplicate. Cap at 20 titles so the snapshot stays small.
    """
    titles: List[str] = []
    seen: set = set()

    for match in _MARKDOWN_HEADING_RE.finditer(text):
        title = match.group(2).strip()
        key = title.lower()
        # Filter out obvious non-post headings
        if key in seen or len(title) < 10 or len(title) > 200:
            continue
        if title.lower() in ("blog", "recent posts", "latest posts", "all posts", "categories", "tags"):
            continue
        seen.add(key)
        titles.append(title)
        if len(titles) >= 20:
            return titles

    for match in _MARKDOWN_LINK_RE.finditer(text):
        title = match.group(1).strip()
        key = title.lower()
        if key in seen or len(title) < 10 or len(title) > 200:
            continue
        seen.add(key)
        titles.append(title)
        if len(titles) >= 20:
            break

    return titles


# ---------------------------------------------------------------------------
# Phase D: per-account person intelligence
# ---------------------------------------------------------------------------
def _run_phase_d_person_intel(
    db: Client, *, company_id: str, org_id: str
) -> None:
    """Apify-driven person-level signal pass. Gated on
    companies.person_intel_enabled.

    Flow:
      1. Read companies.person_intel_enabled. If 'off' → no-op.
      2. Pick contact count: 1 (top1) or 3 (deep).
      3. discover_key_contacts (cached → no extra Apollo cost unless
         stale or first run).
      4. For each contact:
         a. Maybe scrape profile (only if it hasn't been done in 30d
            OR last_person_intel_at is null). Detect job changes
            vs cached baseline → write person_new_in_role signal.
         b. Scrape posts incrementally since last_person_intel_at.
         c. Scrape activity (comments + reactions on others' posts)
            incrementally.
      5. Classify all post/activity items via Haiku → drop noise.
      6. Write surviving signals via signal_writer.
      7. Update companies.last_person_intel_at.
      8. Charge 1 (top1) or 2 (deep) credits via the ledger.

    Failure inside any step is logged and continues — never raises
    out of the monitor pass.
    """
    from datetime import datetime, timezone

    # 1. Read the enable flag + company state
    try:
        res = (
            db.table("companies")
            .select(
                "id, domain, person_intel_enabled, "
                "key_contacts, last_person_intel_at"
            )
            .eq("id", company_id)
            .maybe_single()
            .execute()
        )
        company = res.data
    except Exception as e:
        logger.warning("Phase D: company fetch failed: %s", e)
        return
    if not company:
        return
    mode = (company.get("person_intel_enabled") or "off").lower()
    if mode == "off":
        return  # opt-in: do nothing unless explicitly enabled
    if mode not in ("top1", "deep"):
        logger.warning("Phase D: unknown person_intel_enabled=%s", mode)
        return

    contact_count = 1 if mode == "top1" else 3

    # 2-3. Discover/load key contacts.
    contacts = discover_key_contacts(
        db,
        company_id=company_id,
        org_id=org_id,
        max_contacts=3,  # discover up to 3 regardless of mode, take subset below
    )
    if not contacts:
        # Persist that we tried so we don't loop trying every pass
        _stamp_person_intel_run(db, company_id)
        return
    contacts = contacts[:contact_count]

    # Fetch vendor pitch + competitor list for the classifier
    vendor_pitch, competitors = _classifier_context(db, org_id)

    since_iso = company.get("last_person_intel_at")
    now_iso = datetime.now(timezone.utc).isoformat()
    signal_rows: List[Dict[str, Any]] = []

    # 4. Walk each contact
    for contact in contacts:
        li_url = contact.get("linkedin_url")
        if not li_url:
            continue
        name = contact.get("name") or ""

        # 4a. Profile scrape — for job-change detection. Skip if we
        # have a recent scrape (within 30d via key_contacts_refreshed_at
        # in the discovery cache). Even when we DO scrape, the cost
        # is tiny ($0.003) so the gate is light.
        if not since_iso:
            try:
                profile = li_scrape_profile(li_url)
                if profile and isinstance(profile.get("days_in_current_role"), int):
                    days = profile["days_in_current_role"]
                    # New-in-role window: 90-270 days (3-9 months in
                    # role). Spec 2026-06-08: this is the evaluation
                    # window where a new buyer reviews vendors.
                    if 90 <= days <= 270:
                        signal_rows.append(
                            {
                                "org_id": org_id,
                                "company_id": company_id,
                                "signal_type": "person_new_in_role",
                                "signal_source": "apify_linkedin_profile",
                                "signal_data": {
                                    "headline": f"{name} is ~{days} days into role: {profile.get('current_title', '')}",
                                    "person_name": name,
                                    "current_title": profile.get("current_title"),
                                    "current_company": profile.get("current_company"),
                                    "days_in_role": days,
                                    "url": li_url,
                                },
                                "signal_strength": 0.8,
                                "signal_subject_type": "person",
                            }
                        )
            except Exception as e:
                logger.warning("Profile scrape failed for %s: %s", name, e)

        # 4b. Person's own posts (incremental since last_person_intel_at)
        posts: List[Dict[str, Any]] = []
        try:
            posts = li_scrape_posts(li_url, max_posts=15, since_iso=since_iso)
        except Exception as e:
            logger.warning("Posts scrape failed for %s: %s", name, e)

        # 4c. Activity (comments + reactions on others' posts)
        activity: List[Dict[str, Any]] = []
        try:
            activity = li_scrape_activity(li_url, max_items=15, since_iso=since_iso)
        except Exception as e:
            logger.warning("Activity scrape failed for %s: %s", name, e)

        all_items = posts + activity
        if not all_items:
            continue

        # 5. Classify
        try:
            kept = classify_items_parallel(
                all_items,
                vendor_pitch=vendor_pitch,
                competitor_list=competitors,
                max_workers=4,
            )
        except Exception as e:
            logger.warning("Classifier failed for %s: %s", name, e)
            kept = []

        # 6. Project into signal rows
        for d in kept:
            signal_rows.append(
                {
                    "org_id": org_id,
                    "company_id": company_id,
                    "signal_type": d["signal_type"],
                    "signal_source": (
                        "apify_linkedin_posts"
                        if d.get("kind") == "post"
                        else "apify_linkedin_activity"
                    ),
                    "signal_data": {
                        "headline": (d.get("summary") or d.get("quoted_excerpt") or "")[:280],
                        "quoted_excerpt": d.get("quoted_excerpt") or "",
                        "summary": d.get("summary") or "",
                        "url": d.get("url") or "",
                        "person_name": name,
                        "person_linkedin_url": li_url,
                        "person_title": contact.get("title") or "",
                        "kind": d.get("kind"),
                        # Full content for the dashboard's LinkedIn-style
                        # post card (2026-07-07) — same fields the
                        # watched-people path persists.
                        "post_text": d.get("post_text") or "",
                        "original_post_author": d.get("original_post_author") or "",
                        "comment_text": d.get("comment_text") or "",
                        "posted_at": d.get("posted_at"),
                        "engagement": d.get("engagement") or {},
                    },
                    "signal_strength": d.get("confidence") or 0.7,
                    "signal_subject_type": "person",
                }
            )

    # Write all collected signals
    if signal_rows:
        try:
            insert_signals(db, signal_rows)
        except Exception as e:
            logger.warning("Phase D insert_signals failed: %s", e)

    # 7. Stamp the run timestamp so next pass is incremental
    _stamp_person_intel_run(db, company_id)

    # 8. Charge credits — 1 for top1, 2 for deep
    try:
        charge_credits(
            db,
            org_id=org_id,
            action="person_intel_pass",
            related_id=str(company_id),
            override_units=1 if mode == "top1" else 2,
            metadata={
                "mode": mode,
                "contacts_monitored": len(contacts),
                "signals_written": len(signal_rows),
            },
        )
    except Exception as e:
        logger.warning("Phase D credit charge failed: %s", e)


def _stamp_person_intel_run(db: Client, company_id: str) -> None:
    from datetime import datetime, timezone
    try:
        db.table("companies").update(
            {"last_person_intel_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", company_id).execute()
    except Exception as e:
        logger.warning("Stamp last_person_intel_at failed: %s", e)


def _classifier_context(db: Client, org_id: str) -> tuple[str, List[str]]:
    """Pull vendor_pitch + competitor list for the classifier from
    icp_config.client_context. Falls back to a generic vendor profile
    if the org hasn't populated it."""
    try:
        res = (
            db.table("organisations")
            .select("icp_config")
            .eq("id", org_id)
            .maybe_single()
            .execute()
        )
        cfg = (res.data or {}).get("icp_config") or {}
    except Exception:
        return ("a B2B vendor", [])

    cc = cfg.get("client_context")
    pitch = ""
    competitors: List[str] = []
    if isinstance(cc, dict):
        for key in ("product_pitch", "sells_what", "what_they_sell", "description"):
            v = cc.get(key)
            if isinstance(v, str) and v.strip():
                pitch = v.strip()
                break
        c = cc.get("competitors")
        if isinstance(c, list):
            competitors = [str(x) for x in c if x]

    if not pitch:
        pitch = "a B2B vendor"
    return (pitch, competitors)
