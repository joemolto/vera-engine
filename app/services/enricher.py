"""Enrichment orchestrator — parallel source fetching with per-source timeouts."""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone
from functools import partial

from supabase import Client

from app.services.activity import log_activity
from app.services.apollo_payload import post_search_filter
from app.services.client_context import get_client_context
from app.services.companies import (
    record_intel_observations,
    upsert_company_from_enrichment,
)
from app.services.personas import active_personas
from app.services import signal_payload as sp
from app.services.signal_writer import insert_signal, insert_signals

from app.services.enrichers.deep_research import enrich_deep_research
from app.services.apify_client import scrape_linkedin_posts
from app.services.enrichers.jobs import (
    enrich_jobs,
    SECURITY_LEADERSHIP_TITLES as _SECURITY_TITLE_KEYWORDS,
)
from app.services.job_keyword_extraction import (
    KEYWORD_PATTERNS as _JOB_KEYWORD_PATTERNS,
    PRESSURE_CATEGORIES as _JOB_PRESSURE_CATEGORIES,
    extract_job_signals,
)
from app.services.website_posture import (
    COMPLIANCE_FLAGS as _WEBSITE_COMPLIANCE_FLAGS,
    POSTURE_BOOL_SIGNALS as _WEBSITE_POSTURE_BOOLS,
    create_website_posture_signals,
)
from app.services.person_signals import create_person_signals
from app.services.compound_signals import (
    COMPOUND_RULES as _COMPOUND_RULES,
    derive_compound_signals,
)
from app.services.ai_quality_pass import run_quality_pass
from app.services.enrichers.linkedin import enrich_linkedin_content
from app.services.enrichers.news import enrich_news
from app.services.enrichers.predictleads import enrich_predictleads, filter_technologies_for_sales, filter_technologies_with_ai
from app.services.enrichers.recency import is_recent, filter_recent
from app.services.enrichers.tech_stack import enrich_tech_stack
from app.services.stack_detection import merge_dns_into_tech_stack
from app.services.enrichers.website import enrich_website
from app.services.threat_intel.analyser import correlate_threats_for_lead
from app.services.email_finder import find_email
from app.services.linkedin_verifier import scrape_linkedin_profile, verify_lead_employment
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Per-source timeouts (seconds)
TIMEOUT_WEBSITE = 45  # bumped from 30s — Push Security and similar sites
                     # were timing out cleanly (Jina + Claude analysis on
                     # large landing pages can take 35-40s)
TIMEOUT_NEWS = 25     # bumped from 15s — Tavily + multi-query Claude
                     # analysis is heavier than the old Google News RSS path
TIMEOUT_JOBS = 45

TIMEOUT_TECH_STACK = 60
TIMEOUT_LINKEDIN = 60
TIMEOUT_LINKEDIN_POSTS = 75  # Apify post scrapes are heavier than profile scrapes
TIMEOUT_DEEP_RESEARCH = 35
TIMEOUT_PREDICTLEADS = 30

# How many recent LinkedIn posts to scrape per lead
LINKEDIN_POSTS_TO_SCRAPE = 20

# Total enrichment timeout — return whatever we have after this
TOTAL_TIMEOUT = 180


def _build_client_context(db: Client, org_id: str) -> Optional[Dict]:
    """Fetch org record and build client context dict.

    Uses the shared get_client_context helper which reads client_context + icp_config.
    Returns None if no meaningful context is available.
    """
    ctx = get_client_context(db, org_id)
    if not ctx or (not ctx.get("services_offered") and not ctx.get("what_they_sell")):
        return None
    return ctx


def run_full_enrichment(
    db: Client,
    org_id: str,
    lead_ids: List[str],
    enrichment_sources: List[str],
    enrichment_mode: str = "cyber_company_gtm",
    prefetched_profile: Optional[Dict[str, Any]] = None,
) -> List[dict]:
    """Run enrichment pipeline for a batch of leads.

    For each lead, runs enrichment sources in parallel where possible,
    merges results into the lead's enrichment_data, and updates the DB.

    `prefetched_profile`: when the caller has already scraped the LinkedIn
    profile (e.g. quick-enrich extracts fields from it before creating the
    lead), pass it here to skip the second Apify call inside Phase 3+4.
    Only safe for single-lead batches — multi-lead batches each have their
    own LinkedIn URL so a shared prefetched profile would mismatch.
    """
    # Fetch client context once for all leads in this batch
    client_context = _build_client_context(db, org_id)

    # Bulk-fetch every lead row in one query (kills the per-lead SELECT
    # inside _enrich_single_lead — was an N+1 at batch scale).
    leads_by_id: Dict[str, Dict[str, Any]] = {}
    if lead_ids:
        try:
            leads_result = (
                db.table("leads")
                .select("*")
                .eq("org_id", org_id)
                .in_("id", lead_ids)
                .execute()
            )
            leads_by_id = {row["id"]: row for row in (leads_result.data or [])}
        except Exception as e:
            logger.warning(
                "Bulk lead pre-fetch failed for batch (org=%s, n=%d): %s",
                org_id, len(lead_ids), e,
            )

    def _process_one(lead_id: str) -> dict:
        lead_row = leads_by_id.get(lead_id)
        try:
            result = _enrich_single_lead(
                db, org_id, lead_id, enrichment_sources, enrichment_mode,
                lead=lead_row,
                client_context=client_context,
                prefetched_profile=(
                    prefetched_profile if len(lead_ids) == 1 else None
                ),
            )
            # Fire lead.enriched webhook — best-effort, never raises.
            # Lightweight payload (id + sources) since the full enrichment
            # blob can be huge and webhook receivers usually want the
            # cue to fetch via API rather than the data inline.
            try:
                from app.services.webhooks import enqueue_event
                enqueue_event(db, org_id, "lead.enriched", {
                    "lead_id": lead_id,
                    "sources_completed": result.get("sources_completed", []),
                    "company_name": result.get("company_name"),
                })
            except Exception as e:
                logger.warning(
                    "Webhook lead.enriched enqueue failed for %s: %s",
                    lead_id, e,
                )
            return result
        except Exception as e:
            logger.error("Enrichment failed for lead %s: %s", lead_id, e)
            try:
                db.table("leads").update({
                    "last_enrichment_error": str(e),
                }).eq("id", lead_id).execute()
            except Exception:
                pass
            return {"lead_id": lead_id, "error": str(e)}

    # Single-lead batches skip the pool to avoid thread overhead and to
    # keep the prefetched_profile pass-through intact. Multi-lead batches
    # fan out — each lead's own per-source ThreadPoolExecutor still runs
    # inside, so we cap concurrency at 5 to bound the total worker count.
    if len(lead_ids) <= 1:
        return [_process_one(lead_id) for lead_id in lead_ids]

    with ThreadPoolExecutor(max_workers=5, thread_name_prefix="lead-enrich") as pool:
        # pool.map preserves input order, so callers (router responses,
        # webhook fan-out indexed by position) see deterministic results.
        return list(pool.map(_process_one, lead_ids))


def _enrich_single_lead(
    db: Client,
    org_id: str,
    lead_id: str,
    sources: List[str],
    enrichment_mode: str,
    *,
    lead: Optional[Dict[str, Any]] = None,
    client_context: Optional[Dict] = None,
    prefetched_profile: Optional[Dict[str, Any]] = None,
) -> dict:
    """Run enrichment for a single lead with parallel source fetching.

    `lead` may be passed in by `run_full_enrichment` after a bulk fetch
    so we don't issue one SELECT per lead in a batch. Single-lead
    callers (Vera Brain tool calls, ad-hoc API hits) can omit it and
    we'll fetch it ourselves.
    """
    pipeline_start = time.monotonic()

    if lead is None:
        lead_result = (
            db.table("leads")
            .select("*")
            .eq("id", lead_id)
            .eq("org_id", org_id)
            .execute()
        )
        if not lead_result.data:
            raise ValueError(f"Lead {lead_id} not found for org {org_id} — check the org_id matches the lead's org")
        lead = lead_result.data[0]

    # ---------------------------------------------------------------------
    # Repair-on-read: skeletal leads (no last_name / linkedin_url / email)
    # came in from an older Vera Brain Apollo path that round-tripped data
    # through a text summary and lost most of the contact fields. Without
    # those, LinkedIn verification, the email-finder waterfall, and the
    # scorer's data-quality gate all bail out and the lead lands cold.
    #
    # Hit Apollo people/match once to backfill, persist immediately so
    # subsequent re-enrichments skip the lookup, and update the in-memory
    # row so the rest of this enrichment run benefits.
    # ---------------------------------------------------------------------
    if lead.get("first_name") and lead.get("company_domain") and not all((
        (lead.get("last_name") or "").strip(),
        (lead.get("linkedin_url") or "").strip(),
        (lead.get("email") or "").strip(),
    )):
        try:
            from app.services.vera_brain import _hydrate_from_apollo_match
            hydrated = _hydrate_from_apollo_match(lead)
            patch: Dict[str, Any] = {}
            for col in ("last_name", "linkedin_url", "email", "title", "apollo_id"):
                old = (lead.get(col) or "")
                new = hydrated.get(col) or ""
                if not old and new:
                    patch[col] = new
            if patch:
                logger.info(
                    "Hydrated skeletal lead %s via Apollo people/match: %s",
                    lead_id, list(patch.keys()),
                )
                try:
                    db.table("leads").update(patch).eq("id", lead_id).execute()
                except Exception as e:
                    logger.warning(
                        "Lead %s in-memory hydrated but DB update failed: %s",
                        lead_id, e,
                    )
                lead.update(patch)
        except Exception as e:
            # Hydration is best-effort. Falling through with the original
            # skeletal row is no worse than the pre-fix behaviour.
            logger.warning("Hydration attempt failed for %s: %s", lead_id, e)

    domain = lead.get("company_domain", "")
    existing_enrichment = lead.get("enrichment_data") or {}
    enrichment_data: Dict = dict(existing_enrichment)

    # Snapshot previous state for change detection on re-enrichment
    previous_data: Optional[Dict] = None
    if lead.get("enriched_at"):
        previous_data = {
            "enrichment_data": dict(existing_enrichment),
            "enriched_at": lead.get("enriched_at", ""),
            "lead_rating": lead.get("lead_rating", "unrated"),
            "total_score": float(lead.get("total_score", 0)),
        }
    sources_completed: List[str] = list(lead.get("enrichment_sources_completed") or [])

    # Canonical finding store accumulator. Every signal writer in this run
    # appends the row dict(s) it produced; before save we normalize these
    # into enrichment_data["findings"] (the durable source of truth that
    # survives on cached enrichment) and the signals table stays a
    # projection. See project-intelligence-engine-roadmap.
    finding_rows: List[dict] = []

    # Seed enrichment with pre-enrichment data from discovery phase.
    # Full enrichment will overwrite with complete data, but this ensures
    # we have PredictLeads company/news/tech even if the full call fails.
    pre_enrichment = lead.get("pre_enrichment_data") or {}
    if pre_enrichment:
        for key in ("predictleads_company", "predictleads_news", "predictleads_tech"):
            if key in pre_enrichment and key not in enrichment_data:
                enrichment_data[key] = pre_enrichment[key]

    # Build lead context for cross-referencing
    lead_context: Optional[Dict[str, str]] = None
    if lead.get("first_name") or lead.get("title"):
        lead_name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip()
        lead_context = {
            "lead_name": lead_name or "N/A",
            "lead_title": lead.get("title", "N/A"),
        }

    # =====================================================================
    # Phase 1: Parallel data fetching (independent sources)
    # =====================================================================
    website_result = None
    news_result = None
    jobs_result = None
    raw_website_content: Optional[str] = None

    # Manual executor lifecycle: a `with` block calls shutdown(wait=True)
    # on exit, which blocks until every submitted thread completes —
    # even ones we've already given up on via future.result(timeout=…).
    # For heavy sites (Trustpilot, etc.) the website scrape can hang past
    # the per-source timeout while the underlying httpx call drags on;
    # that drain previously ate the whole TOTAL_TIMEOUT budget and made
    # Phase 2/3/4/5 silently skip. Manage the executor by hand and
    # shutdown(wait=False, cancel_futures=True) so Phase 2 starts on
    # time. Orphan threads continue in the background as daemons.
    executor = ThreadPoolExecutor(
        max_workers=5,
        thread_name_prefix=f"p1-{lead_id[:8]}",
    )
    try:
        futures: Dict[str, Any] = {}

        # PredictLeads — all 6 endpoints internally parallelised + cached
        if domain:
            futures["predictleads"] = executor.submit(
                enrich_predictleads, domain, client_context, db,
            )

        # Website
        if "website" in sources and domain:
            futures["website"] = executor.submit(
                enrich_website, db, domain, enrichment_mode,
                client_context, lead_context,
            )

        # News
        if "news" in sources and lead.get("company_name"):
            queries = [lead["company_name"]]
            if lead.get("first_name") and lead.get("last_name"):
                queries.append(f"{lead['first_name']} {lead['last_name']} {lead['company_name']}")

            futures["news"] = executor.submit(
                enrich_news, db,
                search_queries=queries,
                company_name=lead.get("company_name", ""),
                first_name=lead.get("first_name", ""),
                last_name=lead.get("last_name", ""),
                title=lead.get("title", ""),
                client_context=client_context,
            )

        # Jobs
        if "jobs" in sources and domain and lead.get("company_name"):
            futures["jobs"] = executor.submit(
                enrich_jobs,
                db=db,
                domain=domain,
                company_name=lead["company_name"],
                website_careers_content=None,  # Not available yet in parallel
                client_context=client_context,
            )

        # Collect results with per-source timeouts
        timeouts = {
            "predictleads": TIMEOUT_PREDICTLEADS,
            "website": TIMEOUT_WEBSITE,
            "news": TIMEOUT_NEWS,
            "jobs": TIMEOUT_JOBS,

        }

        for source_name, future in futures.items():
            timeout = timeouts.get(source_name, 30)

            # Check total timeout
            elapsed = time.monotonic() - pipeline_start
            remaining = TOTAL_TIMEOUT - elapsed
            if remaining <= 0:
                logger.warning("Total enrichment timeout reached for lead %s — returning partial data", lead_id)
                future.cancel()
                continue

            effective_timeout = min(timeout, remaining)

            try:
                result = future.result(timeout=effective_timeout)
            except FuturesTimeoutError:
                logger.warning("Source %s timed out after %ds for %s — continuing with partial data",
                               source_name, effective_timeout, domain)
                enrichment_data[source_name] = {"error": f"Timeout after {effective_timeout}s"}
                continue
            except Exception as e:
                logger.warning("Source %s failed for %s: %s — continuing with partial data",
                               source_name, domain, e)
                enrichment_data[source_name] = {"error": str(e)}
                continue

            # Process results
            if source_name == "predictleads" and result:
                # Store raw PredictLeads data under separate keys
                if result.get("technology"):
                    enrichment_data["predictleads_tech"] = result["technology"]
                if result.get("jobs"):
                    enrichment_data["predictleads_jobs"] = result["jobs"]
                    # PredictLeads jobs ALWAYS take priority (domain-specific, not name-search)
                    pl_jobs = result["jobs"]
                    enrichment_data["jobs"] = {
                        "total_roles_found": pl_jobs.get("total_jobs", 0),
                        "is_hiring_sales_bd": pl_jobs.get("is_hiring_sales", False),
                        "is_hiring_technical": pl_jobs.get("is_hiring_technical", False),
                        "is_hiring_leadership": pl_jobs.get("is_hiring_leadership", False),
                        "hiring_velocity": "high" if pl_jobs.get("total_jobs", 0) >= 10 else (
                            "moderate" if pl_jobs.get("total_jobs", 0) >= 3 else "low"
                        ),
                        "hiring_velocity_notes": f"{pl_jobs.get('total_jobs', 0)} open roles detected",
                        "source": "predictleads",
                    }
                if result.get("news_events"):
                    enrichment_data["predictleads_news"] = result["news_events"]
                    # PredictLeads news ALWAYS takes priority (domain-specific, not name-search)
                    # Only include events from the last 6 months — uses the same
                    # is_recent helper that Tavily and the dashboard recency
                    # checks share, so the cutoff is one source of truth.
                    pl_news = result["news_events"]
                    key_stories = []
                    for ev in (pl_news.get("events") or []):
                        if not is_recent(ev.get("date")):
                            continue
                        key_stories.append({
                            "headline": ev.get("summary") or ev.get("article_title") or "",
                            "date": ev.get("date") or "",
                            "category": ev.get("category") or "",
                            "product": ev.get("product") or "",
                            "source": "predictleads",
                        })
                        if len(key_stories) >= 10:
                            break
                    # Derive booleans from FILTERED key_stories, NOT from unfiltered
                    # pl_news top-level booleans (which flag "ever happened", not recent)
                    funding_cats = {"receives_financing", "raises_funding"}
                    acquisition_cats = {"acquires", "acquired_by"}
                    launch_cats = {"launches"}
                    partnership_cats = {"partners_with"}
                    has_recent_funding = any(s.get("category") in funding_cats for s in key_stories)
                    has_recent_acquisition = any(s.get("category") in acquisition_cats for s in key_stories)
                    has_recent_product_launch = any(s.get("category") in launch_cats for s in key_stories)
                    has_recent_partnership = any(s.get("category") in partnership_cats for s in key_stories)
                    enrichment_data["news"] = {
                        "news_found": bool(key_stories),
                        "key_stories": key_stories,
                        "source": "predictleads",
                        "has_recent_funding": has_recent_funding,
                        "has_recent_acquisition": has_recent_acquisition,
                        "has_recent_product_launch": has_recent_product_launch,
                        "has_recent_partnership": has_recent_partnership,
                    }
                if result.get("financing"):
                    enrichment_data["predictleads_financing"] = result["financing"]
                if result.get("similar_companies"):
                    enrichment_data["predictleads_similar"] = result["similar_companies"]
                if result.get("company_info"):
                    enrichment_data["predictleads_company"] = result["company_info"]
                sources_completed.append("predictleads")

                # Auto-create signals from PredictLeads news events
                finding_rows += _create_predictleads_signals(
                    db, org_id, lead_id, lead, enrichment_data
                ) or []

            elif source_name == "website" and result:
                raw_website_content = result.pop("_raw_website_content", None)
                enrichment_data["website"] = result
                sources_completed.append("website")

            elif source_name == "news" and result:
                # Store Tavily news as supplementary — PredictLeads takes priority
                # since it's domain-keyed (Tavily is query-keyed and noisier).
                if enrichment_data.get("news", {}).get("source") == "predictleads":
                    enrichment_data["news_tavily"] = result
                else:
                    enrichment_data["news"] = result
                sources_completed.append("news")

            elif source_name == "jobs" and result:
                # Store scraped jobs as supplementary — PredictLeads takes priority
                if enrichment_data.get("jobs", {}).get("source") == "predictleads":
                    enrichment_data["jobs_scraped"] = result
                else:
                    enrichment_data["jobs"] = result
                # Auto-create signals for EVERY hiring category fired by
                # Claude (security / GRC / sales_bd). The producer side
                # (enrichers/jobs.py) returns `_create_signals` as a list;
                # legacy single-entry `_create_signal` is still honoured
                # so older cached payloads don't blow up.
                signals_to_create = result.pop("_create_signals", None)
                if not signals_to_create:
                    legacy = result.pop("_create_signal", None)
                    signals_to_create = [legacy] if legacy else []
                for signal_data in signals_to_create:
                    if not signal_data:
                        continue
                    s_type = signal_data["signal_type"]
                    s_source = signal_data["signal_source"]
                    # Idempotent on re-enrich: clear this lead's prior
                    # rows for the same (type, source) first. Without this
                    # every enrichment run piled another near-identical
                    # hiring row onto the lead — the duplicate clutter.
                    try:
                        db.table("signals").delete().eq(
                            "lead_id", lead_id
                        ).eq("signal_type", s_type).eq(
                            "signal_source", s_source
                        ).execute()
                    except Exception as e:
                        logger.warning(
                            "Failed clearing stale %s signals for lead %s: %s",
                            s_type, lead_id, e,
                        )

                    raw = signal_data.get("signal_data") or {}
                    roles = raw.get("roles") or []
                    company = lead.get("company_name")
                    role_str = ", ".join([r for r in roles[:3] if r])
                    headline, detail = _jobs_signal_copy(
                        s_type, company, role_str,
                    )
                    jobs_row = {
                        "org_id": org_id,
                        "lead_id": lead_id,
                        "signal_type": s_type,
                        "signal_source": s_source,
                        "signal_data": sp.build_signal_payload(
                            headline=headline,
                            entity=company,
                            detail=detail,
                            **raw,
                        ),
                        "signal_strength": signal_data["signal_strength"],
                    }
                    if insert_signal(db, jobs_row):
                        finding_rows.append(jobs_row)
                sources_completed.append("jobs")
    finally:
        # Non-blocking shutdown — pending tasks are cancelled, in-flight
        # threads continue as daemons in the background. Phase 2 can
        # now start immediately even when one Phase 1 source (a slow
        # website scrape, a sluggish PredictLeads call) is still
        # holding its socket. Previously this was an implicit
        # shutdown(wait=True) that ate the rest of TOTAL_TIMEOUT.
        executor.shutdown(wait=False, cancel_futures=True)

    # Check total timeout before Phase 2
    elapsed = time.monotonic() - pipeline_start
    if elapsed >= TOTAL_TIMEOUT:
        logger.warning("Total enrichment timeout after Phase 1 for lead %s", lead_id)
        return _save_and_return(db, org_id, lead_id, lead, enrichment_data, sources_completed, sources, {}, previous_data)

    # =====================================================================
    # Phase 2: Tech stack analysis (depends on website content)
    # =====================================================================
    if "tech_stack" in sources and domain:
        remaining = TOTAL_TIMEOUT - (time.monotonic() - pipeline_start)
        effective_timeout = min(TIMEOUT_TECH_STACK, remaining)

        # Pass PredictLeads tech data if available (Tier 0 in fallback chain)
        pl_tech = enrichment_data.get("predictleads_tech")

        tech_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"p2-tech-{lead_id[:8]}",
        )
        try:
            future = tech_executor.submit(
                enrich_tech_stack, db, domain,
                client_context=client_context,
                website_content=raw_website_content,
                predictleads_tech=pl_tech,
            )
            try:
                tech_result = future.result(timeout=effective_timeout)
                if tech_result:
                    enrichment_data["tech_stack"] = tech_result
                    sources_completed.append("tech_stack")
            except FuturesTimeoutError:
                logger.warning("Source tech_stack timed out after %ds for %s — continuing with partial data",
                               effective_timeout, domain)
                enrichment_data["tech_stack"] = {"error": f"Timeout after {effective_timeout}s"}
            except Exception as e:
                logger.warning("Source tech_stack failed for %s: %s — continuing with partial data", domain, e)
                enrichment_data["tech_stack"] = {"error": str(e)}
        finally:
            # Non-blocking shutdown so a slow tech_stack call doesn't
            # block Phase 3+. (Same drain bug as Phase 1.)
            tech_executor.shutdown(wait=False, cancel_futures=True)

        # ----- Merge DNS detections (Tier A confidence) into tech_stack -----
        # DNS is the highest-confidence source we have (live observable
        # infra, no inference). Run AFTER the existing tech_stack pipeline
        # so it enriches the same `technologies_detected` array — every
        # tech DNS confirms gets "dns" appended to detection_methods, and
        # the dashboard tiers it as Confirmed.
        try:
            enrichment_data["tech_stack"] = merge_dns_into_tech_stack(
                domain, enrichment_data.get("tech_stack")
            )
        except Exception as e:
            logger.warning("DNS merge failed for %s: %s — continuing", domain, e)

    # Filter technologies for sales relevance (AI-powered when client context available)
    tech_data = enrichment_data.get("tech_stack") or {}
    if isinstance(tech_data, dict):
        techs = tech_data.get("technologies_detected") or tech_data.get("technologies") or []
        if isinstance(techs, list) and techs:
            tech_data["technologies_filtered"] = filter_technologies_with_ai(techs, client_context)
            enrichment_data["tech_stack"] = tech_data

    # Tech-exclude check — drop the lead if its detected tech matches ANY
    # active persona's exclude list. Phase 1.5 behaviour: we use the union
    # of all active persona excludes (read from dual-written flat field).
    # Phase 2 fan-out will narrow this to the matched_persona_id only.
    detected_techs = []
    if isinstance(tech_data, dict):
        detected_techs = (
            tech_data.get("technologies_detected")
            or tech_data.get("technologies")
            or []
        )
    if detected_techs:
        try:
            personas_now = active_personas(db, org_id)
        except Exception as e:
            logger.debug("Tech-exclude skipped — persona load failed for org %s: %s", org_id, e)
            personas_now = []
        for persona in personas_now:
            if not post_search_filter(persona, detected_techs):
                excludes = persona.get("target_tech_excludes") or []
                matched = [t for t in detected_techs
                           if any(exc.lower() in str(t).lower() for exc in excludes)]
                logger.info(
                    "Lead %s excluded by persona %r — detected tech %s matched excludes %s",
                    lead_id, persona.get("name"), matched, excludes,
                )
                enrichment_data["excluded_by_tech"] = {
                    "persona_id": persona.get("id"),
                    "persona_name": persona.get("name"),
                    "matched_technologies": matched,
                }
                # Persist the exclusion + flip status; downstream phases skipped.
                db.table("leads").update({
                    "enrichment_status": "excluded",
                    "enrichment_data": enrichment_data,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", lead_id).execute()
                log_activity(db, org_id, lead_id, "lead_excluded_by_tech", {
                    "persona_id": persona.get("id"),
                    "matched_technologies": matched,
                })
                return enrichment_data

    # (LinkedIn content is now populated by Phase 3+4 below — no placeholder needed.)

    # Check total timeout before Phase 3+4
    elapsed = time.monotonic() - pipeline_start
    if elapsed >= TOTAL_TIMEOUT:
        logger.warning("Total enrichment timeout before LinkedIn+Sonar for lead %s", lead_id)
        return _save_and_return(db, org_id, lead_id, lead, enrichment_data, sources_completed, sources, {}, previous_data)

    # =====================================================================
    # Phase 3+4: LinkedIn verification + LinkedIn post scrape + Perplexity
    # ---------------------------------------------------------------------
    # Three independent network calls run concurrently in one 3-worker pool:
    #   1. scrape_linkedin_profile (Apify)  — for employment verification
    #   2. scrape_linkedin_posts   (Apify)  — for personalisation hooks
    #   3. enrich_deep_research    (Sonar)  — for compliance + business intel
    # All three have no real dependency on each other.
    # The 3-second Apify rate-limit pause stays on the main thread between
    # the two Apify calls, but it overlaps with deep_research execution.
    # =====================================================================
    update_fields: Dict[str, Any] = {}
    linkedin_url = lead.get("linkedin_url")
    pipeline_remaining = TOTAL_TIMEOUT - (time.monotonic() - pipeline_start)
    linkedin_timeout = min(TIMEOUT_LINKEDIN, pipeline_remaining)
    posts_timeout = min(TIMEOUT_LINKEDIN_POSTS, pipeline_remaining)
    deep_timeout = min(TIMEOUT_DEEP_RESEARCH, pipeline_remaining)

    linkedin_future = None
    posts_future = None
    deep_future = None
    prefetched_profile_used = False

    # Manual lifecycle (same drain bug as Phase 1 — see comment there).
    p3_executor = ThreadPoolExecutor(
        max_workers=3,
        thread_name_prefix=f"p3-{lead_id[:8]}",
    )
    try:
        executor = p3_executor  # keep the inner block readable
        # Submit deep_research first — it's an OpenRouter call, no dependency
        # on either Apify call. Starting it now means it overlaps with the
        # 3s Apify rate-limit pause and the Apify scrapes themselves.
        if domain and lead.get("company_name"):
            deep_future = executor.submit(
                enrich_deep_research,
                company_name=lead["company_name"],
                company_domain=domain,
                client_context=client_context,
                db=db,
            )

        if linkedin_url:
            # If the caller already scraped this profile (quick-enrich path),
            # reuse it instead of paying Apify again. We still kick off the
            # posts scrape in parallel — that's a separate actor and the
            # caller didn't pre-fetch posts.
            if prefetched_profile is not None:
                prefetched_profile_used = True
                logger.info("Reusing prefetched LinkedIn profile for lead %s — skipping duplicate scrape", lead_id)
                posts_future = executor.submit(
                    scrape_linkedin_posts,
                    linkedin_url,
                    max_posts=LINKEDIN_POSTS_TO_SCRAPE,
                )
            else:
                time.sleep(3)  # Apify rate limit (kept; Win 2 parked)
                linkedin_future = executor.submit(scrape_linkedin_profile, linkedin_url)
                posts_future = executor.submit(
                    scrape_linkedin_posts,
                    linkedin_url,
                    max_posts=LINKEDIN_POSTS_TO_SCRAPE,
                )
        else:
            update_fields["data_status"] = "no_linkedin"

        # ----- Collect LinkedIn result (and run verification) -----
        if prefetched_profile_used or linkedin_future is not None:
            try:
                if prefetched_profile_used:
                    profile = prefetched_profile
                else:
                    profile = linkedin_future.result(timeout=linkedin_timeout)

                if profile:
                    # Pass PredictLeads news events into the verifier so it
                    # can spot acquisition-driven Apollo↔LinkedIn discrepancies
                    # (parent company in Apollo, acquired subsidiary in LinkedIn).
                    # The verifier uses this to choose "post_acquisition" over
                    # "stale" when there's an M&A explanation.
                    pl_news_events = (
                        (enrichment_data.get("predictleads_news") or {}).get("events") or []
                    )
                    verification = verify_lead_employment(
                        lead_data={
                            "first_name": lead.get("first_name", ""),
                            "last_name": lead.get("last_name", ""),
                            "title": lead.get("title", ""),
                            "company_name": lead.get("company_name", ""),
                        },
                        linkedin_profile=profile,
                        recent_news_events=pl_news_events,
                    )
                    enrichment_data["linkedin_verification"] = verification

                    status = verification.get("verification_status", "ambiguous")
                    # The verification statuses fall into two buckets:
                    # GOOD-TO-CONTACT: verified, corrected, post_acquisition, side_role
                    # NEEDS-CARE:      stale, ambiguous, unverified
                    # Statuses that "correct" the lead data (corrected, post_acquisition,
                    # stale) all overwrite company_name/title with the recommended values
                    # so downstream enrichment uses the right company. The difference is
                    # how they get LABELLED in the dashboard.
                    if status in ("stale", "corrected", "post_acquisition"):
                        update_fields["original_company_name"] = lead.get("company_name", "")
                        update_fields["original_title"] = lead.get("title", "")
                        update_fields["company_name"] = verification.get("recommended_company", lead.get("company_name", ""))
                        update_fields["title"] = verification.get("recommended_title", lead.get("title", ""))
                        update_fields["data_status"] = status
                        update_fields["data_status_note"] = verification.get("explanation", "")
                    elif status == "side_role":
                        update_fields["data_status"] = "side_role"
                        update_fields["data_status_note"] = verification.get("outreach_note", "")
                    elif status == "verified":
                        update_fields["data_status"] = "verified"
                    else:
                        update_fields["data_status"] = "ambiguous"
                        update_fields["data_status_note"] = verification.get("explanation", "")

                    # Always overwrite title with the verifier's recommendation
                    # when it provides one — catches cases like LinkedIn headline
                    # being stored as title instead of the actual job title.
                    rec_title = verification.get("recommended_title", "")
                    if rec_title and rec_title != lead.get("title", ""):
                        update_fields["title"] = rec_title

                    update_fields["linkedin_verified_at"] = datetime.now(timezone.utc).isoformat()
                    sources_completed.append("linkedin_verification")
                else:
                    update_fields["data_status"] = "unverified"
            except FuturesTimeoutError:
                logger.warning("LinkedIn verification timed out after %ds for lead %s — continuing",
                               linkedin_timeout, lead_id)
                update_fields["data_status"] = "unverified"
            except Exception as e:
                logger.error("LinkedIn verification failed for lead %s: %s", lead_id, e)
                update_fields["data_status"] = "unverified"

        # ----- Collect deep_research result (independent of LinkedIn) -----
        if deep_future is not None:
            try:
                deep_result = deep_future.result(timeout=deep_timeout)
                if deep_result:
                    enrichment_data["deep_research"] = deep_result
                    sources_completed.append("deep_research")
            except FuturesTimeoutError:
                logger.warning("Deep research timed out after %ds for %s — continuing", deep_timeout, domain)
            except Exception as e:
                logger.warning("Deep research failed for %s: %s — continuing", domain, e)

        # ----- Apollo business intel (revenue / funding / founded / headcount growth) -----
        # Apollo's organizations/enrich response already has these fields, but
        # the tech_stack enricher was only extracting technologies + jobs.
        # We re-use the cached Apollo call here (tech_stack already paid the
        # 1 credit during this run, so this is free) to capture revenue +
        # funding into enrichment_data.business_intel — fills the gaps where
        # Perplexity Sonar returned "Not found" for private/non-VC companies.
        if domain:
            try:
                from app.services.enrichers.tech_stack import fetch_apollo_business_intel
                apollo_bi = fetch_apollo_business_intel(domain)
                if apollo_bi:
                    existing_bi = (
                        (enrichment_data.get("deep_research") or {}).get("business_intel") or {}
                    )
                    # Merge: Sonar (existing_bi) wins on overlap; Apollo fills gaps.
                    merged: Dict[str, Any] = dict(apollo_bi)
                    for k, v in existing_bi.items():
                        # Treat "Not found" / empty strings as missing
                        if v in (None, "", "Not found", "not found"):
                            continue
                        merged[k] = v
                    enrichment_data["business_intel"] = merged
            except Exception as e:
                logger.warning("Apollo business intel fetch failed for %s: %s", domain, e)

        # ----- Companies House revenue bracket (UK only, guardrail) -----
        # A coarse accounts bracket used purely to corroborate / flag a
        # conflict against the Apollo+Perplexity revenue consensus (see
        # company_revenue.reconcile). UK-gated — a CH name search for a
        # non-UK firm returns a wrong match. Best-effort + cached: never
        # blocks or breaks enrichment, and non-UK leads skip it entirely.
        from app.services.company_revenue import is_uk_company
        if lead.get("company_name") and is_uk_company(
            lead.get("company_country", ""),
            lead.get("company_location", ""),
            domain,
        ):
            try:
                from app.services.enrichers.companies_house import (
                    enrich_companies_house,
                )
                ch_results = enrich_companies_house(
                    db, company_names=[lead["company_name"]]
                )
                if ch_results:
                    ch = ch_results[0]
                    enrichment_data["companies_house"] = ch
                    bracket = ch.get("estimated_revenue_bracket")
                    if bracket and bracket != "Unknown":
                        bi = enrichment_data.setdefault("business_intel", {})
                        bi["companies_house_revenue_bracket"] = bracket
            except Exception as e:
                logger.warning(
                    "Companies House lookup failed for %s: %s",
                    lead.get("company_name"), e,
                )

        # ----- Collect LinkedIn posts result (raw scrape only — analysis runs below) -----
        scraped_posts: List[dict] = []
        if posts_future is not None:
            try:
                raw_posts = posts_future.result(timeout=posts_timeout) or []
                if raw_posts:
                    scraped_posts = raw_posts
                    logger.info("Scraped %d LinkedIn posts for lead %s", len(scraped_posts), lead_id)
            except FuturesTimeoutError:
                logger.warning("LinkedIn posts scrape timed out after %ds for lead %s — continuing",
                               posts_timeout, lead_id)
            except Exception as e:
                logger.warning("LinkedIn posts scrape failed for lead %s: %s — continuing", lead_id, e)
    finally:
        # Non-blocking shutdown so Phase 4 / 5 (deep research, email
        # resolution, threat correlation, scoring) all run even when
        # an Apify scrape hangs past its timeout.
        p3_executor.shutdown(wait=False, cancel_futures=True)

    # =====================================================================
    # LinkedIn content analysis (Claude — runs after the parallel pool)
    # ---------------------------------------------------------------------
    # Takes the raw posts scraped above and runs them through Claude with
    # client_context-aware framing to extract personalisation hooks +
    # buying signals tailored to what the client actually sells.
    # =====================================================================
    if scraped_posts:
        # Persist the raw posts so person_signals can fire activity-based
        # signals (person_active_linkedin / person_posted_relevant_topic).
        # Stored under a separate key from linkedin_content (the Claude
        # narrative) so we keep the structured + freeform layers distinct.
        enrichment_data["linkedin_posts"] = scraped_posts
        try:
            content_analysis = enrich_linkedin_content(
                first_name=lead.get("first_name", ""),
                last_name=lead.get("last_name", ""),
                title=lead.get("title", ""),
                company_name=lead.get("company_name", ""),
                recent_posts=scraped_posts,
                client_context=client_context,
            )
            if content_analysis and "error" not in content_analysis:
                enrichment_data["linkedin_content"] = content_analysis
                # Mark linkedin_content as completed (overrides the placeholder
                # set earlier when sources includes "linkedin_content")
                if "linkedin_content" not in sources_completed:
                    sources_completed.append("linkedin_content")
        except Exception as e:
            logger.warning("LinkedIn content analysis failed for lead %s: %s", lead_id, e)
    elif "linkedin_content" in sources:
        # No posts scraped — keep the existing not_checked placeholder
        enrichment_data["linkedin_content"] = {"status": "linkedin_content_not_checked"}

    # =====================================================================
    # Location verification: cross-reference LinkedIn location vs ICP target
    # (Runs after the parallel pool because it depends on linkedin_verification.)
    # =====================================================================
    linkedin_data = enrichment_data.get("linkedin_verification") or {}
    if linkedin_data and client_context:
        target_locations = []
        # Get target locations from client context / icp config
        org_result = db.table("organisations").select("icp_config").eq("id", org_id).execute()
        if org_result.data:
            icp = org_result.data[0].get("icp_config") or {}
            target_locations = [loc.lower() for loc in (icp.get("target_locations") or [])]

        if target_locations:
            # Extract location hints from LinkedIn profile data
            contact_location = ""
            primary_role = linkedin_data.get("primary_role") or {}
            # LinkedIn verification doesn't always include location, but explanation often mentions it
            explanation = linkedin_data.get("explanation") or ""
            outreach_note = linkedin_data.get("outreach_note") or ""

            # Also check deep research and PredictLeads for company HQ
            pl_company = enrichment_data.get("predictleads_company") or {}
            pl_location = (pl_company.get("location") or "").lower()

            # Check for US vs UK mismatch
            uk_terms = {"united kingdom", "uk", "england", "scotland", "wales", "london", "manchester", "birmingham", "edinburgh", "bristol", "leeds", "glasgow"}
            us_terms = {"united states", "usa", "us", "america", "new york", "california", "texas", "florida", "connecticut", "hartford", "boston", "chicago", "san francisco"}

            location_is_us = any(t in pl_location for t in us_terms)
            location_is_uk = any(t in pl_location for t in uk_terms)
            target_is_uk = any("united kingdom" in t or "uk" in t for t in target_locations)
            target_is_us = any("united states" in t or "us" in t for t in target_locations)

            if location_is_us and target_is_uk and not target_is_us:
                enrichment_data["location_mismatch"] = {
                    "detected_location": pl_location or "United States (inferred)",
                    "target_locations": [loc.title() for loc in target_locations],
                    "note": "Company appears to be US-based but ICP targets UK. Contact may not be in target geography.",
                }
                logger.info("Location mismatch detected for lead %s: %s vs target %s",
                            lead_id, pl_location, target_locations)
            elif location_is_uk and target_is_us and not target_is_uk:
                enrichment_data["location_mismatch"] = {
                    "detected_location": pl_location or "United Kingdom (inferred)",
                    "target_locations": [loc.title() for loc in target_locations],
                    "note": "Company appears to be UK-based but ICP targets US. Contact may not be in target geography.",
                }
                logger.info("Location mismatch detected for lead %s: %s vs target %s",
                            lead_id, pl_location, target_locations)

    # =====================================================================
    # Phase 5: Email resolution waterfall
    # ---------------------------------------------------------------------
    # Runs AFTER LinkedIn verification so we use the corrected company
    # domain (not Apollo's stale one) when asking Findymail.
    # Waterfall: Apollo → Findymail LinkedIn → Findymail name → permutation → AI guess.
    # Always returns an email + confidence score. No lead is ever hidden.
    # =====================================================================
    apollo_email = lead.get("email") or ""
    # Use corrected domain if LinkedIn verification overwrote the company
    resolved_domain = update_fields.get("company_domain") or lead.get("company_domain") or domain or ""
    resolved_company = update_fields.get("company_name") or lead.get("company_name") or ""
    resolved_first = lead.get("first_name", "")
    resolved_last = lead.get("last_name", "")

    email_result = find_email(
        apollo_email=apollo_email,
        linkedin_url=linkedin_url or "",
        first_name=resolved_first,
        last_name=resolved_last,
        company_domain=resolved_domain,
        company_name=resolved_company,
    )
    enrichment_data["email_resolution"] = email_result

    if email_result.get("email"):
        update_fields["email"] = email_result["email"]
        update_fields["email_confidence"] = email_result.get("confidence", 0)
        # First-class verdict columns — the list surfaces read these, not
        # enrichment_data (2026-08-28).
        update_fields["email_verified"] = email_result.get("email_verified")
        update_fields["email_domain_mismatch"] = bool(
            (email_result.get("checks") or {}).get("domain_mismatch")
        )
        update_fields["email_source"] = email_result.get("source")
        if email_result.get("source") and email_result["source"] != "apollo":
            sources_completed.append("email_finder")
        logger.info("Email resolved via %s for lead %s: %s (%d%% confidence)",
                    email_result.get("source"), lead_id,
                    email_result["email"], email_result.get("confidence", 0))

    # =====================================================================
    # Phase 5: Cyber-specific correlation (Phase A — was orphaned before)
    # ---------------------------------------------------------------------
    # Match this lead's tech_stack + industry against the threat_intel table
    # (populated by scheduled NVD / CISA KEV / NCSC / Tavily pollers). Embed
    # the matches into enrichment_data.threat_alerts so the scorer prompt
    # picks them up, and emit signal rows so vera_brain + the dashboard
    # surface them as triggers.
    # =====================================================================
    try:
        tech_payload = enrichment_data.get("tech_stack") or {}
        techs_for_corr = tech_payload.get("technologies_detected") or []
        industry_for_corr = (
            update_fields.get("company_industry")
            or lead.get("company_industry")
        )
        correlation = correlate_threats_for_lead(
            db,
            tech_stack=techs_for_corr,
            industry=industry_for_corr,
            company_domain=domain,
            # Breach/threat correlation window = 12 months (2026-06-19):
            # breach_proximity (and CVE-in-stack / threat-alert) matches now
            # look back a full year, not 90 days. Scorer still hard-caps any
            # signal at 365d by detected_at, so nothing scores beyond a year.
            days=365,
            limit=5,
        )
        if correlation.get("threat_alerts"):
            enrichment_data["threat_alerts"] = correlation["threat_alerts"]
            sources_completed.append("threat_intel")

        finding_rows += _create_cyber_signals(
            db, org_id, lead_id, lead, enrichment_data, correlation,
        ) or []

        # Phase 4 (2026-05-28): job-description keyword extraction.
        # Scans raw_postings for compliance/security tooling keywords,
        # fires atomic + compound pressure signals. No new data source —
        # we already have the JD text from jobs enrichment.
        finding_rows += _create_job_keyword_signals(
            db, org_id, lead_id, lead, enrichment_data,
        ) or []

        # Phase 5 (2026-05-28): website posture signal lift.
        # Promotes deep_research.security_posture booleans + compliance
        # claims into proper signals (subject=website), plus runs cheap
        # security.txt + status.{domain} probes, plus fires derived
        # certification_gap / security_proof_gap when regulated.
        finding_rows += _create_website_posture_signals(
            db, org_id, lead_id, lead, enrichment_data,
        ) or []

        # Phase 3 (2026-05-28): person-level signal layer.
        # Fires person_security_leader / grc / technical / commercial
        # function labels (no tenure claim) plus person_new_in_role /
        # person_promoted / person_changed_company when explicit recency
        # evidence is present (PL event OR LinkedIn started_at). The
        # whole point is to draw a clean line: account claims stay
        # account-level, person claims require person-evidence.
        finding_rows += _create_person_level_signals(
            db, org_id, lead_id, lead, enrichment_data,
        ) or []

        # Phase 6 (2026-05-28): compound cyber-pressure signals.
        # Compose atomic signals just emitted into higher-value compound
        # types (enterprise_readiness_pressure, post_funding_security
        # _maturity_push, etc.). Runs LAST so it sees every atomic.
        finding_rows += _create_compound_signals(
            db, org_id, lead_id, lead, finding_rows,
        ) or []

        # Phase 2 AI quality pass (2026-05-28). Runs ONCE after every
        # atomic + compound signal is in place. Output is structured
        # JSON: dedup groups, entity resolution, quality scores, and
        # market-context hedges on inferred signals. Fail-quiet — empty
        # pass on any error so the deterministic baseline always wins.
        logger.info(
            "AI quality pass: about to run for lead %s (%d atomic+compound signals)",
            lead_id, len(finding_rows),
        )
        try:
            tech_for_pass = (
                (enrichment_data.get("tech_stack") or {})
                .get("technologies_detected") or []
            )
            apollo_data = enrichment_data.get("apollo") or {}
            quality_pass = run_quality_pass(
                company_name=lead.get("company_name"),
                company_domain=domain or "",
                company_size_band=lead.get("company_size"),
                company_industry=lead.get("company_industry"),
                company_revenue=(
                    apollo_data.get("organization_revenue_printed")
                    or apollo_data.get("annual_revenue_printed")
                ),
                is_public_company=bool(apollo_data.get("publicly_traded_symbol")),
                signals=finding_rows,
                technologies=tech_for_pass,
            )
            enrichment_data["ai_quality_pass"] = quality_pass
        except Exception as e:
            logger.warning("AI quality pass failed for lead %s: %s — continuing", lead_id, e)
    except Exception as e:
        logger.warning("Cyber correlation failed for lead %s: %s — continuing", lead_id, e)

    # ---------------------------------------------------------------------
    # Canonical finding store. Normalize every signal row produced this run
    # into enrichment_data["findings"] — the durable source of truth that
    # travels with the lead, so scoring/briefing work on cached enrichment
    # with zero live signal rows. The signals table stays a projection.
    # Also annotate threat_alerts[*].finding so the cyber context the scorer
    # prompt already reads carries its normalized finding inline.
    # ---------------------------------------------------------------------
    try:
        findings = sp.collect_findings(finding_rows)
        if findings:
            enrichment_data["findings"] = findings
            by_type = {f["signal_type"]: f for f in findings}
            cve_f = by_type.get("cve_in_stack")
            breach_f = by_type.get("breach_proximity")
            for ta in enrichment_data.get("threat_alerts") or []:
                if not isinstance(ta, dict):
                    continue
                ttype = (ta.get("threat_type") or "").lower()
                if ttype == "vulnerability" and cve_f:
                    base = cve_f
                elif ttype in ("breach", "ransomware") and breach_f:
                    base = breach_f
                else:
                    continue
                # `findings` is deduped to one-per-signal_type, so `base` is the
                # account's STRONGEST cve/breach finding. Attaching it verbatim
                # to every alert made all of an account's CVE alerts show the
                # SAME CVE detail (e.g. 5 distinct CVE alerts all reading
                # "CVE-2026-45657 ..."). Copy it and override the human-readable
                # text with THIS alert's own so per-alert consumers (get_account,
                # dossier) read the right CVE. Structural fields the scorer reads
                # (category, strength, signal_type, risk_note, recommended_use)
                # stay from `base`.
                per_alert = dict(base)
                alert_text = ta.get("ai_summary") or ta.get("why_relevant")
                if alert_text:
                    per_alert["detail"] = alert_text
                    per_alert["summary"] = alert_text
                if ta.get("title"):
                    per_alert["headline"] = ta["title"]
                ta["finding"] = per_alert
    except Exception as e:
        logger.warning("Finding consolidation failed for lead %s: %s — continuing", lead_id, e)

    elapsed = time.monotonic() - pipeline_start
    logger.info("Enrichment for lead %s completed in %.1fs (%d sources)", lead_id, elapsed, len(sources_completed))

    return _save_and_return(db, org_id, lead_id, lead, enrichment_data, sources_completed, sources, update_fields, previous_data)


def _save_and_return(
    db: Client,
    org_id: str,
    lead_id: str,
    lead: dict,
    enrichment_data: Dict,
    sources_completed: List[str],
    sources: List[str],
    update_fields: Dict[str, Any],
    previous_data: Optional[Dict] = None,
) -> dict:
    """Save enrichment data to DB and return result dict.

    If previous_data is provided (re-enrichment), runs change detection
    and stores changes_since_last in the enrichment data.
    """
    now = datetime.now(timezone.utc).isoformat()
    enrichment_data["enriched_at"] = now
    enrichment_data["enrichment_sources_run"] = sources

    # Change detection for re-enrichment (millisecond in-memory comparison)
    if previous_data:
        old = previous_data.get("enrichment_data") or {}
        changes = _detect_enrichment_changes(old, enrichment_data)
        enrichment_data["changes_since_last"] = {
            "re_enriched_at": now,
            "previous_enriched_at": previous_data.get("enriched_at", ""),
            "changes": changes,
            "total_changes": len(changes),
        }

    version = (lead.get("enrichment_version") or 0) + 1

    lead_update = {
        "enrichment_data": enrichment_data,
        "enriched_at": now,
        "enrichment_status": "enriched",
        "enrichment_sources_completed": list(set(sources_completed)),
        "enrichment_version": version,
        "last_enrichment_error": None,
        "updated_at": now,
    }
    # Merge LinkedIn verification fields
    lead_update.update(update_fields)

    # Mirror company-level facts into the canonical companies table.
    # Done before the lead update so the FK is set on the same write.
    domain_for_company = (
        update_fields.get("company_domain")
        or lead.get("company_domain")
    )
    if domain_for_company:
        try:
            merged_for_company = {**lead, **update_fields, "company_domain": domain_for_company}
            company_id = upsert_company_from_enrichment(
                db, domain_for_company, enrichment_data, lead=merged_for_company,
            )
            if company_id:
                lead_update["company_id"] = company_id
                # Roll this enrichment's signal observations up to the
                # global intelligence layer (company_intel + companies
                # rollup booleans / counters). Best-effort — caught
                # below; never aborts the enrichment.
                try:
                    # finding_rows lives on the outer enrichment scope and
                    # isn't passed into _save_and_return; the canonical
                    # store is enrichment_data["findings"], so use that.
                    record_intel_observations(
                        db,
                        company_id,
                        org_id,
                        enrichment_data.get("findings") or [],
                    )
                except Exception as e:
                    logger.warning(
                        "Intel observation rollup failed for %s: %s",
                        domain_for_company, e,
                    )
        except Exception as e:
            logger.warning("Company upsert failed for %s: %s", domain_for_company, e)

    db.table("leads").update(lead_update).eq("id", lead_id).execute()

    log_activity(db, org_id, lead_id, "lead_enriched", {
        "sources": sources,
        "sources_completed": list(set(sources_completed)),
        "version": version,
        "is_re_enrichment": previous_data is not None,
    })

    return {
        "lead_id": lead_id,
        "enrichment_data": enrichment_data,
        "sources_completed": list(set(sources_completed)),
    }


def _detect_enrichment_changes(old: Dict, new: Dict) -> List[Dict[str, str]]:
    """Compare old and new enrichment data. Runs in milliseconds (in-memory only)."""
    changes: List[Dict[str, str]] = []

    # News changes — new stories not in old
    old_stories = set()
    for s in (old.get("news", {}).get("key_stories") or []):
        old_stories.add(s.get("headline", "").lower())
    for s in (new.get("news", {}).get("key_stories") or []):
        headline = s.get("headline", "")
        if headline.lower() not in old_stories and headline:
            changes.append({
                "type": "new_news",
                "detail": headline,
                "significance": "high" if s.get("category") in ("funding", "acquires") else "medium",
            })

    # Tech stack changes
    old_techs = set()
    for t in (old.get("tech_stack", {}).get("technologies_detected") or []):
        name = t.get("name", "") if isinstance(t, dict) else str(t)
        if name:
            old_techs.add(name.lower())
    new_techs = set()
    for t in (new.get("tech_stack", {}).get("technologies_detected") or []):
        name = t.get("name", "") if isinstance(t, dict) else str(t)
        if name:
            new_techs.add(name.lower())

    added_techs = new_techs - old_techs
    removed_techs = old_techs - new_techs
    if added_techs:
        changes.append({
            "type": "tech_added",
            "detail": f"New tools detected: {', '.join(sorted(added_techs)[:5])}",
            "significance": "medium",
        })
    if removed_techs:
        changes.append({
            "type": "tech_removed",
            "detail": f"Tools no longer detected: {', '.join(sorted(removed_techs)[:5])}",
            "significance": "low",
        })

    # Jobs changes
    old_job_count = old.get("jobs", {}).get("total_roles_found", 0) or 0
    new_job_count = new.get("jobs", {}).get("total_roles_found", 0) or 0
    if new_job_count > old_job_count + 2:
        changes.append({
            "type": "new_jobs",
            "detail": f"Job postings increased from {old_job_count} to {new_job_count}",
            "significance": "high" if new_job_count - old_job_count >= 5 else "medium",
        })
    elif new_job_count < old_job_count - 2:
        changes.append({
            "type": "jobs_decreased",
            "detail": f"Job postings decreased from {old_job_count} to {new_job_count}",
            "significance": "medium",
        })

    # Hiring sales/BD changed
    old_hiring_bd = old.get("jobs", {}).get("is_hiring_sales_bd", False)
    new_hiring_bd = new.get("jobs", {}).get("is_hiring_sales_bd", False)
    if new_hiring_bd and not old_hiring_bd:
        changes.append({
            "type": "now_hiring_sales",
            "detail": "Started hiring for sales/BD roles — GTM expansion signal",
            "significance": "high",
        })

    # LinkedIn verification status changed
    old_status = (old.get("linkedin_verification") or {}).get("verification_status", "")
    new_status = (new.get("linkedin_verification") or {}).get("verification_status", "")
    if old_status and new_status and old_status != new_status:
        changes.append({
            "type": "contact_status_changed",
            "detail": f"Contact verification changed from {old_status} to {new_status}",
            "significance": "high" if new_status == "stale" else "medium",
        })

    if not changes:
        changes.append({
            "type": "no_changes",
            "detail": "No significant changes detected since last enrichment",
            "significance": "low",
        })

    return changes


# ---------------------------------------------------------------------------
# Per-type copy for jobs_enrichment auto-created signals. Keeps the
# headline/detail honest to the actual category instead of pasting the
# sales-BD framing onto every row.
# ---------------------------------------------------------------------------
def _jobs_signal_copy(
    signal_type: str,
    company: Optional[str],
    role_str: str,
) -> Tuple[str, str]:
    name = company or "This company"
    they = company or "They"
    if signal_type == "hiring_security_leadership":
        headline = (
            f"{name} is hiring security leadership"
            + (f" — {role_str}" if role_str else "")
        )
        detail = (
            f"{they} has an open {role_str or 'security leadership'} role. "
            "A leadership opening usually means nobody senior owns security "
            "yet, or the seat is about to change hands — budget and vendor "
            "choice are in play."
        )
    elif signal_type == "hiring_security":
        headline = (
            f"{name} is hiring security"
            + (f" — {role_str}" if role_str else " roles")
        )
        detail = (
            f"{they} has open {role_str or 'security'} roles — "
            "active capability investment. Strong Maturity signal."
        )
    elif signal_type == "hiring_grc":
        headline = (
            f"{name} is hiring GRC / compliance"
            + (f" — {role_str}" if role_str else " roles")
        )
        detail = (
            f"{they} has open {role_str or 'GRC / compliance'} roles — "
            "compliance and audit pressure is becoming workflow. Strong "
            "Maturity signal."
        )
    else:  # hiring_sales_bd or any legacy fallback
        headline = (
            f"{name} is hiring"
            + (f" — {role_str}" if role_str else " (sales/BD roles open)")
        )
        detail = (
            f"{they} has open {role_str or 'sales/BD'} roles — "
            "building outbound capacity, a buying-window for tooling "
            "that compresses ramp time."
        )
    return headline, detail


def _create_predictleads_signals(
    db: Client, org_id: str, lead_id: str, lead: dict, enrichment_data: dict
) -> List[dict]:
    """Create signals from PredictLeads news events (last 6 months only).

    Returns the row dicts it built so the caller folds them into the
    canonical enrichment_data["findings"]; the table write is the projection.

    Deletes the lead's existing PredictLeads signals first so re-enrichment
    drops stale signals (e.g. a funding event that's now >6 months old).
    Sets expires_at = event_date + 6 months so signals self-expire.

    Every signal written here carries the signal-payload contract
    (headline / detail / entity / occurred_at) so the dashboard + digest
    render a human line, not "signal signal detected". A "hires" event
    whose summary names a security title is SKIPPED here — the cyber
    extractor emits the sharper `security_leadership_hire` for it, and we
    don't want a duplicate generic `new_leadership`.
    """
    company = lead.get("company_name")
    pl_news = enrichment_data.get("predictleads_news") or {}
    events = pl_news.get("events") or []

    # Always wipe this lead's existing PredictLeads signals so stale ones
    # (events that have aged out of the 6-month window) don't linger.
    try:
        db.table("signals").delete().eq("lead_id", lead_id).eq("signal_source", "predictleads").execute()
    except Exception as e:
        logger.warning("Failed to clear stale signals for lead %s: %s", lead_id, e)

    if not events:
        return []

    built_rows: List[dict] = []
    six_months_ago = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()

    signal_map = {
        "acquires": ("recent_acquisition", 0.9),
        "funding": ("recently_funded", 0.9),
        "launches": ("product_launch", 0.8),
        "partners_with": ("new_partnership", 0.7),
        "hires": ("new_leadership", 0.7),
        "promotes": ("new_leadership", 0.7),
        "expands_to": ("expansion", 0.7),
    }

    created = set()  # Avoid duplicate signal types
    for event in events:
        category = event.get("category", "")
        if category not in signal_map or category in created:
            continue

        # Only create signals from events in the last 6 months
        ev_date = event.get("date") or ""
        if ev_date and ev_date < six_months_ago:
            continue

        # Sub-classify "hires" and "promotes": if the event names a security
        # title, the cyber extractor will emit the sharper
        # security_leadership_hire. Skip the generic new_leadership here so
        # we don't double-fire.
        if category in ("hires", "promotes"):
            summary_lc = (event.get("summary") or "").lower()
            if any(kw in summary_lc for kw in _SECURITY_TITLE_KEYWORDS):
                continue

        signal_type, strength = signal_map[category]

        # expires_at = event date + 180 days
        expires_at = None
        try:
            event_dt = datetime.fromisoformat(ev_date.replace("Z", "+00:00"))
            expires_at = (event_dt + timedelta(days=180)).isoformat()
        except (ValueError, TypeError):
            expires_at = (datetime.now(timezone.utc) + timedelta(days=180)).isoformat()

        headline, detail = sp.predictleads_news_line(
            company=company,
            category=category,
            summary=event.get("summary"),
            product=event.get("product"),
        )

        pl_row = {
            "org_id": org_id,
            "lead_id": lead_id,
            "signal_type": signal_type,
            "signal_source": "predictleads",
            "signal_data": sp.build_signal_payload(
                headline=headline,
                entity=company,
                detail=detail,
                occurred_at=ev_date or None,
                summary=event.get("summary", ""),
                product=event.get("product", ""),
                category=category,
            ),
            "signal_strength": strength,
            "expires_at": expires_at,
        }
        # Fold into the canonical store regardless of projection-write
        # success — enrichment_data is the source of truth.
        built_rows.append(pl_row)
        # Validated projection-write. If the payload is malformed it's
        # logged + skipped; enrichment_data remains the source of truth.
        if insert_signal(db, pl_row):
            created.add(category)

    return built_rows


# ---------------------------------------------------------------------------
# Cyber-specific signal extractors (Phase A)
# ---------------------------------------------------------------------------
# Five cyber-specific signal types beyond the generic B2B set above. All write
# to the `signals` table with signal_source set to the data origin so we can
# trace where each row came from. expires_at honours the 180-day window —
# signals self-expire and the dashboard filters by expires_at > now().
#
# - security_leadership_hire — prospect (or org) hired CISO/CSO/Head of Security
#                              in the last 90 days
# - security_team_expansion            — ≥3 security-titled open roles on careers page
# - cve_in_stack             — actively-exploited CVE matches a tech in stack
# - compliance_gap           — REMOVED 2026-05-28 (over-confident framing,
#                              absence of evidence is not evidence of absence)
# - breach_proximity         — recent breach/ransomware affects their stack
#                              or industry

# _SECURITY_TITLE_KEYWORDS now lives in enrichers/jobs.py as
# SECURITY_LEADERSHIP_TITLES and is imported at the top of this module —
# the job-hiring split and the person-in-post checks must agree on what
# counts as security leadership, so there is one definition.

_SECURITY_ROLE_KEYWORDS = {
    "security engineer", "security analyst", "security architect",
    "security operations", "soc analyst", "soc engineer",
    "devsecops", "appsec", "application security",
    "incident response", "threat hunter", "vulnerability management",
    "penetration tester", "pentester",
}

_REGULATED_INDUSTRY_KEYWORDS = {
    "financial services", "banking", "insurance", "fintech",
    "investment management", "venture capital",
    "healthcare", "pharmaceuticals",
    "energy", "oil & gas", "utilities",
    "government", "public sector", "defense", "defence", "military",
    "telecommunications", "legal services", "law practice",
    "gambling", "casinos",
}


def _create_cyber_signals(
    db: Client,
    org_id: str,
    lead_id: str,
    lead: dict,
    enrichment_data: dict,
    correlation: dict,
) -> List[dict]:
    """Emit cyber-specific signal rows from the assembled enrichment payload.

    Returns the row dicts it built so the caller can fold them into the
    canonical enrichment_data["findings"]; the signals-table write here is
    the projection for digest/dashboard.

    Idempotent for re-enrichment: clears the lead's existing rows for these
    signal types first, then inserts whatever the current enrichment supports.
    """
    cyber_types = (
        "security_leadership_hire",
        "security_team_expansion",
        "cve_in_stack",
        # compliance_gap intentionally NOT in the wipe list now that it's
        # no longer fired — keeping it in the delete would silently scrub
        # any historical rows that might want auditing. Use the one-off
        # cleanup SQL instead.
        "breach_proximity",
        # Phase 3 person-level (see signal_writer.KNOWN_SIGNAL_TYPES).
        # Clear on re-enrichment so a stale "leader on team" doesn't
        # persist after the person has actually moved on.
        "person_security_leader",
        "person_new_in_role",
        "person_promoted",
        "person_changed_company",
    )
    try:
        db.table("signals").delete().eq("lead_id", lead_id).in_("signal_type", list(cyber_types)).execute()
    except Exception as e:
        logger.warning("Failed to clear cyber signals for lead %s: %s", lead_id, e)

    now_iso = datetime.now(timezone.utc).isoformat()
    expires_180 = (datetime.now(timezone.utc) + timedelta(days=180)).isoformat()

    rows: List[dict] = []

    # 1. security_leadership_hire — fires ONLY with explicit recency evidence.
    #
    # Two evidence paths:
    #   (a) PL "hires"/"promotes" news event in the last 90 days that names
    #       a security title (recent_security_event), OR
    #   (b) LinkedIn primary_role.started_at within the last 90 days AND
    #       the lead has a security title.
    #
    # Title alone is NOT evidence of newness (2026-05-28 bug: a long-tenured
    # security leader was reported as newly hired).
    # A security-titled person with no recency evidence gets the honest
    # signal `person_security_leader` instead — useful context, no false
    # claim of being new in seat.
    linkedin = enrichment_data.get("linkedin_verification") or {}
    primary_role = linkedin.get("primary_role") or {}
    primary_title = (primary_role.get("title") or lead.get("title") or "").lower()
    is_security_titled = any(kw in primary_title for kw in _SECURITY_TITLE_KEYWORDS)

    pl_news = enrichment_data.get("predictleads_news") or {}
    pl_events = pl_news.get("events") or []
    cutoff_90 = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()

    recent_security_event: Optional[Dict] = None
    for ev in pl_events:
        if ev.get("category") not in ("hires", "promotes"):
            continue
        ev_date = ev.get("date") or ""
        if ev_date and ev_date < cutoff_90:
            continue
        summary = (ev.get("summary") or "").lower()
        if any(kw in summary for kw in _SECURITY_TITLE_KEYWORDS):
            recent_security_event = ev
            break

    # LinkedIn-derived recency: primary_role.started_at within last 90 days?
    started_at = primary_role.get("started_at")
    linkedin_recent = False
    months_in_role = primary_role.get("months_in_role")
    if started_at:
        try:
            start_dt = datetime.fromisoformat(started_at).replace(tzinfo=timezone.utc)
            linkedin_recent = (datetime.now(timezone.utc) - start_dt).days <= 90
        except (ValueError, TypeError):
            linkedin_recent = False

    has_recency_evidence = bool(recent_security_event) or (
        is_security_titled and linkedin_recent
    )

    if has_recency_evidence:
        _company = primary_role.get("company") or lead.get("company_name")
        _change_type = (recent_security_event or {}).get("category")
        _event_summary = (recent_security_event or {}).get("summary")
        # Title selection:
        #   - PL event present: extract title from summary (avoids collapsing
        #     a regional Head of Cyber into "CISO")
        #   - Otherwise: use lead's primary_role title (LinkedIn evidence)
        if recent_security_event:
            _title = sp.extract_role_from_summary(_event_summary) or "a security leader"
        else:
            _title = primary_role.get("title") or lead.get("title")
        _hl, _dt = sp.security_leadership_change_line(
            company=_company,
            title=_title,
            is_the_prospect=bool(is_security_titled),
            change_type=_change_type,  # "hires" | "promotes" | None
            event_summary=_event_summary,
        )
        # signal_data carries the recency evidence so downstream UI can
        # verify before claiming "new in seat"
        evidence_kind = (
            "predictleads_event" if recent_security_event else "linkedin_started_at"
        )
        rows.append({
            "org_id": org_id,
            "lead_id": lead_id,
            "signal_type": "security_leadership_hire",
            "signal_source": "linkedin" if linkedin_recent else "predictleads",
            "signal_data": sp.build_signal_payload(
                headline=_hl,
                entity=_company,
                detail=_dt,
                primary_title=_title,
                company=_company,
                hire_event_summary=_event_summary,
                change_type=_change_type,
                # Recency evidence — required for any "new in seat" UI claim.
                # If neither field is present, do NOT label as new.
                started_at=started_at if linkedin_recent else None,
                months_in_role=months_in_role if linkedin_recent else None,
                evidence_kind=evidence_kind,
            ),
            "signal_strength": 0.9 if recent_security_event else 0.8,
            "expires_at": expires_180,
        })
    elif is_security_titled:
        # Honest fallback: security leader on team, no tenure claim.
        _company = primary_role.get("company") or lead.get("company_name")
        _title = primary_role.get("title") or lead.get("title") or "a security role"
        rows.append({
            "org_id": org_id,
            "lead_id": lead_id,
            "signal_type": "person_security_leader",
            "signal_source": "linkedin",
            "signal_data": sp.build_signal_payload(
                headline=f"Security leader on team at {_company}" if _company else "Security leader on team",
                entity=_company,
                detail=f"{_title} — tenure unknown" + (
                    f" (≈{months_in_role} months in role)" if months_in_role else ""
                ),
                primary_title=_title,
                company=_company,
                started_at=started_at,
                months_in_role=months_in_role,
            ),
            "signal_strength": 0.6,
            "expires_at": expires_180,
        })

    # 2. security_team_expansion — ≥3 security-titled roles on the careers page
    pl_jobs = enrichment_data.get("predictleads_jobs") or {}
    jobs_list = pl_jobs.get("jobs") or []
    sec_roles = [
        j for j in jobs_list
        if any(kw in (j.get("title") or "").lower() for kw in _SECURITY_ROLE_KEYWORDS)
    ]
    if len(sec_roles) >= 3:
        _company = lead.get("company_name")
        _titles = [r.get("title") for r in sec_roles[:5] if r.get("title")]
        _hl, _dt = sp.security_team_expansion_line(
            company=_company, count=len(sec_roles), sample_titles=_titles,
        )
        rows.append({
            "org_id": org_id,
            "lead_id": lead_id,
            "signal_type": "security_team_expansion",
            "signal_source": "predictleads",
            "signal_data": sp.build_signal_payload(
                headline=_hl,
                entity=_company,
                detail=_dt,
                security_role_count=len(sec_roles),
                sample_titles=_titles,
            ),
            "signal_strength": min(0.95, 0.6 + 0.05 * len(sec_roles)),
            "expires_at": expires_180,
        })

    # 3. cve_in_stack — REMOVED 2026-08-04.
    # The signal claimed "this CVE is in their stack". It could not support
    # that claim on three counts, measured over the 420 rows in production:
    #   (a) 330 of 420 (79%) had NO technology match at all. match_threat_to_orgs
    #       fires on industry OR watchlist OR tech, but every hit was written as
    #       cve_in_stack regardless. "A Check Point CVE exists and you are in
    #       financial services" was being sold as "you run a vulnerable gateway".
    #   (b) The 90 that did match, matched on languages and vendor names —
    #       php, microsoft, node.js, oracle, adobe, python, git. A CVE in a PHP
    #       application is not a CVE in "PHP", and Microsoft ships hundreds of
    #       products. PredictLeads tech data is vendor-level, not product-level,
    #       so it cannot carry a CVE match.
    #   (c) Half the tech corpus is JobPost-sourced (21k of 43k entries) —
    #       inference from job adverts, treated identically to DNS observation.
    #       "We are hiring someone who knows Splunk" became "they run Splunk".
    # Age was broken too: fetch_cisa_kev_recent filters on KEV dateAdded, then
    # sets published_at = dateAdded, so a 2008 CVE back-added to the catalogue
    # arrived looking 48 hours old and was stamped detected_at = now().
    # Same class of bug as the compliance_gap removal below: a claim the
    # evidence does not support. Filtering cannot fix it — the underlying tech
    # data lacks the granularity.
    # To rebuild: allowlist internet-facing products where a match means
    # exposure (Citrix NetScaler, Fortinet, Ivanti, Exchange, MOVEit,
    # Confluence, PAN-OS), require an OBSERVED source (DNS/HtmlSourceCode,
    # never JobPost), and gate on the CVE's own publication date within 90
    # days, ignoring KEV dateAdded.

    # 4. compliance_gap — REMOVED 2026-05-28.
    # The signal was based on absence-of-public-evidence: regulated industry
    # + no SOC 2 / ISO 27001 visible on the public site = "compliance gap".
    # That framing is wrong — large regulated companies almost always hold
    # these certs, just not on the marketing site. Saying "they have no
    # compliance" when we can only see "they don't display compliance" is
    # the same class of bug as the tenure hallucination above, inverted.
    # If you need the underlying fact, look for `certification_claim_detected`
    # (positive evidence, fires when public) — the absence of those tells
    # the same story without making a false claim.

    # 5. breach_proximity — from threat correlation
    breach_matches = correlation.get("breach_proximity_matches") or []
    if breach_matches:
        _company = lead.get("company_name")
        _trimmed = [
            {
                "title": m.get("title"),
                "matched_technologies": m.get("matched_technologies", []),
                "published_at": m.get("published_at"),
            }
            for m in breach_matches[:5]
        ]
        _hl, _dt = sp.breach_proximity_line(
            company=_company, count=len(breach_matches), matches=_trimmed,
        )
        rows.append({
            "org_id": org_id,
            "lead_id": lead_id,
            "signal_type": "breach_proximity",
            "signal_source": "threat_intel",
            "signal_data": sp.build_signal_payload(
                headline=_hl,
                entity=_company,
                detail=_dt,
                matches=_trimmed,
                count=len(breach_matches),
            ),
            "signal_strength": 0.85,
            "expires_at": expires_180,
        })

    if not rows:
        return []

    written = insert_signals(db, rows)
    logger.info(
        "Cyber signals projected for lead %s (%d/%d valid): %s",
        lead_id, written, len(rows), [r["signal_type"] for r in rows],
    )

    # Return regardless of projection-write success: the canonical store is
    # enrichment_data, so findings must survive even if the table insert fails.
    return rows


# ---------------------------------------------------------------------------
# Phase 4 — Job description keyword extraction
# ---------------------------------------------------------------------------

# All atomic + compound signal_types fired by job_keyword_extraction.
# Used for the wipe-and-rewrite below so stale signals don't pile up
# when JDs change between enrichment runs.
_JOB_KEYWORD_TYPES = tuple(
    [t for t, _ in _JOB_KEYWORD_PATTERNS]
    + [c for c, _ in _JOB_PRESSURE_CATEGORIES]
)


def _create_job_keyword_signals(
    db: Client,
    org_id: str,
    lead_id: str,
    lead: dict,
    enrichment_data: dict,
) -> List[dict]:
    """Emit job-keyword signals from raw_postings on this lead.

    Idempotent — wipes this lead's prior job_keyword signals first so
    a re-enrichment with a refreshed JD set doesn't double-fire.
    """
    raw_postings = (enrichment_data.get("jobs") or {}).get("raw_postings") or []
    if not raw_postings:
        return []

    try:
        db.table("signals").delete().eq("lead_id", lead_id).in_(
            "signal_type", list(_JOB_KEYWORD_TYPES)
        ).execute()
    except Exception as e:
        logger.warning(
            "Failed to clear job-keyword signals for lead %s: %s", lead_id, e
        )

    rows = extract_job_signals(
        org_id=org_id,
        lead_id=lead_id,
        company_name=lead.get("company_name"),
        raw_postings=raw_postings,
    )
    if not rows:
        return []

    written = insert_signals(db, rows)
    logger.info(
        "Job-keyword signals projected for lead %s (%d/%d valid): %s",
        lead_id,
        written,
        len(rows),
        [r["signal_type"] for r in rows],
    )
    return rows


# ---------------------------------------------------------------------------
# Phase 5 — Website posture signal lift
# ---------------------------------------------------------------------------

_WEBSITE_POSTURE_TYPES = tuple(
    [t for _, t, _ in _WEBSITE_POSTURE_BOOLS]
    + ["certification_claim_detected", "security_contact_present",
       "status_page_present", "public_proof_not_visible"]
)


def _create_website_posture_signals(
    db: Client,
    org_id: str,
    lead_id: str,
    lead: dict,
    enrichment_data: dict,
) -> List[dict]:
    """Lift website posture booleans into proper signals + run cheap probes.

    Idempotent — wipes prior posture signals first so the security.txt
    / status-subdomain probes don't accumulate stale rows if the company
    later turns the page off.
    """
    domain = (
        lead.get("company_domain")
        or lead.get("domain")
        or ""
    ).lower()
    if not domain:
        return []

    try:
        db.table("signals").delete().eq("lead_id", lead_id).in_(
            "signal_type", list(_WEBSITE_POSTURE_TYPES)
        ).execute()
    except Exception as e:
        logger.warning(
            "Failed to clear website posture signals for lead %s: %s", lead_id, e
        )

    rows = create_website_posture_signals(
        org_id=org_id,
        lead_id=lead_id,
        domain=domain,
        company_name=lead.get("company_name"),
        enrichment_data=enrichment_data,
        industry=lead.get("company_industry"),
    )
    if not rows:
        return []

    written = insert_signals(db, rows)
    logger.info(
        "Website posture signals projected for lead %s (%d/%d valid): %s",
        lead_id,
        written,
        len(rows),
        [r["signal_type"] for r in rows],
    )
    return rows


# ---------------------------------------------------------------------------
# Phase 3 — Person-level signal layer
# ---------------------------------------------------------------------------

_PERSON_SIGNAL_TYPES = (
    "person_security_leader",
    "person_grc_leader",
    "person_technical_leader",
    "person_commercial_leader",
    "person_new_in_role",
    "person_promoted",
    "person_changed_company",
    "person_prior_vendor_experience",
    "person_active_linkedin",
    "person_posted_relevant_topic",
)


def _create_person_level_signals(
    db: Client,
    org_id: str,
    lead_id: str,
    lead: dict,
    enrichment_data: dict,
) -> List[dict]:
    """Project person-level signals onto this lead. Idempotent.

    NB: person_security_leader is ALSO fired by _create_cyber_signals
    as the no-recency-evidence fallback. The wipe-and-rewrite below
    runs after that path, so this is the final word — both paths
    produce the same row shape, so consistency is preserved.
    """
    try:
        db.table("signals").delete().eq("lead_id", lead_id).in_(
            "signal_type", list(_PERSON_SIGNAL_TYPES)
        ).execute()
    except Exception as e:
        logger.warning(
            "Failed to clear person-level signals for lead %s: %s", lead_id, e
        )

    rows = create_person_signals(
        org_id=org_id,
        lead_id=lead_id,
        lead=lead,
        enrichment_data=enrichment_data,
    )
    if not rows:
        return []

    written = insert_signals(db, rows)
    logger.info(
        "Person-level signals projected for lead %s (%d/%d valid): %s",
        lead_id,
        written,
        len(rows),
        [r["signal_type"] for r in rows],
    )
    return rows


# ---------------------------------------------------------------------------
# Phase 6 — Compound cyber-pressure signals
# ---------------------------------------------------------------------------

_COMPOUND_TYPES = tuple(r["signal_type"] for r in _COMPOUND_RULES)


def _create_compound_signals(
    db: Client,
    org_id: str,
    lead_id: str,
    lead: dict,
    atomic_rows: List[Dict[str, Any]],
) -> List[dict]:
    """Compose compound signals from the atomic rows just emitted.

    Idempotent — wipes prior compound rows so a re-enrichment with
    different atomic signals doesn't leave stale compounds.
    """
    if not atomic_rows:
        return []

    try:
        db.table("signals").delete().eq("lead_id", lead_id).in_(
            "signal_type", list(_COMPOUND_TYPES)
        ).execute()
    except Exception as e:
        logger.warning(
            "Failed to clear compound signals for lead %s: %s", lead_id, e
        )

    rows = derive_compound_signals(
        org_id=org_id,
        lead_id=lead_id,
        company_name=lead.get("company_name"),
        atomic_rows=atomic_rows,
    )
    if not rows:
        return []

    written = insert_signals(db, rows)
    logger.info(
        "Compound signals projected for lead %s (%d/%d valid): %s",
        lead_id,
        written,
        len(rows),
        [r["signal_type"] for r in rows],
    )
    return rows
