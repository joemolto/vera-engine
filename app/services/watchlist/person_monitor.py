"""Per-person monitoring — the Solo "watch N people" lever.

A lead with ``person_monitoring=true`` is WATCHED: each weekly pass scrapes
that individual's LinkedIn (profile for role-change, their own posts, and
their activity on others' posts), classifies via Haiku, and writes
``person_*`` signals attached to the LEAD (and to their company when one is
known) — the same Phase-D person-intel pipeline, but person-first and
decoupled from whether the person's company is a watched account.

No per-pass credit charge: the Solo ``monitored_people`` standing cap is the
cost ceiling (provider cost is ~$0.01/person/pass), so watching is free-to-the-action
and bounded by the cap instead. Runs on the global ``/monitor-all`` cron via
``batch_monitor.run_monitoring_all_orgs``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from supabase import Client

from app.services.linkedin_person import (
    scrape_profile as li_scrape_profile,
    scrape_person_posts as li_scrape_posts,
    scrape_person_activity as li_scrape_activity,
)
from app.services.person_intent import classify_items_parallel
from app.services.signal_writer import insert_signals

logger = logging.getLogger(__name__)

# Weekly cadence, but scrape a slightly wider window so one missed cron run
# doesn't drop a week of activity. signal_writer dedupes person signals
# (person-name-aware key), so the overlap never double-writes.
_LOOKBACK_DAYS = 14

# Minimum days between person scrapes. People monitoring runs in the per-org
# pass, which for named clients fires DAILY — this gate keeps the actual scrape
# ~weekly (and bounds Apify cost). force=True bypasses it for the immediate
# first pass when a rep hits Watch.
_PERSON_MONITOR_MIN_DAYS = 6


def _full_name(lead: Dict[str, Any]) -> str:
    parts = [
        (lead.get("first_name") or "").strip(),
        (lead.get("last_name") or "").strip(),
    ]
    return " ".join(p for p in parts if p).strip()


def monitor_one_person(
    db: Client, *, org_id: str, lead_id: str, force: bool = False
) -> Dict[str, Any]:
    """Scrape + classify one watched person, writing person_* signals to the
    lead (and their company, when known). Soft-fails per step; never raises.

    Cadence-gated: skips a person scraped within _PERSON_MONITOR_MIN_DAYS
    unless force=True (used for the immediate first pass on toggle-on)."""
    # Imported lazily — _classifier_context lives in the sibling monitor
    # module and pulling it at import time would risk a circular import.
    from app.services.watchlist.monitor import _classifier_context

    try:
        res = (
            db.table("leads")
            .select(
                "id, org_id, first_name, last_name, title, linkedin_url, "
                "company_id, company_domain, person_monitoring, last_person_monitor_at"
            )
            .eq("id", lead_id)
            .eq("org_id", org_id)
            .maybe_single()
            .execute()
        )
        lead = res.data
    except Exception as e:
        logger.warning("person-monitor: lead fetch failed %s: %s", lead_id, e)
        return {"ok": False, "signals": 0}
    if not lead or not lead.get("person_monitoring"):
        return {"ok": False, "signals": 0}

    # Cadence gate — skip if scraped recently (unless forced). Keeps the scrape
    # ~weekly even when the org's monitor cron runs daily.
    if not force:
        last = lead.get("last_person_monitor_at")
        if last:
            try:
                last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - last_dt).days < _PERSON_MONITOR_MIN_DAYS:
                    return {"ok": True, "skipped": True, "signals": 0}
            except (ValueError, TypeError):
                pass

    li_url = lead.get("linkedin_url")
    if not li_url:
        # Nothing to scrape without a LinkedIn handle.
        return {"ok": False, "signals": 0, "reason": "no_linkedin_url"}

    name = _full_name(lead) or "This person"
    company_id = lead.get("company_id")
    title = lead.get("title") or ""
    since_iso = (
        datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)
    ).isoformat()
    vendor_pitch, competitors = _classifier_context(db, org_id)

    signal_rows: List[Dict[str, Any]] = []

    def _row(signal_data: Dict[str, Any], signal_type: str, source: str,
             strength: float) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "org_id": org_id,
            "lead_id": lead_id,  # person-first: signal lands on the watched lead
            "signal_type": signal_type,
            "signal_source": source,
            "signal_strength": strength,
            "signal_subject_type": "person",
            "signal_data": signal_data,
        }
        # Also attach the company when we know it, so the signal surfaces on
        # the account dossier too (the dedupe unions both axes).
        if company_id:
            row["company_id"] = company_id
        return row

    # Profile — role-change detection. Fire only within the first 90 days
    # of a new role (2026-07-07: "new in role" means NEW — supersedes
    # the 90-270 vendor-review window of the 2026-06-08 spec, which had
    # never produced a single signal in production).
    try:
        profile = li_scrape_profile(li_url, db=db)
        if profile and isinstance(profile.get("days_in_current_role"), int):
            days = profile["days_in_current_role"]
            if 0 <= days <= 90:
                signal_rows.append(_row(
                    {
                        "headline": f"{name} is ~{days} days into role: "
                                    f"{profile.get('current_title', '')}",
                        "person_name": name,
                        "current_title": profile.get("current_title"),
                        "current_company": profile.get("current_company"),
                        "days_in_role": days,
                        "url": li_url,
                    },
                    "person_new_in_role", "apify_linkedin_profile", 0.8,
                ))
    except Exception as e:
        logger.warning("person-monitor: profile scrape failed for %s: %s", name, e)

    posts: List[Dict[str, Any]] = []
    try:
        posts = li_scrape_posts(li_url, max_posts=15, since_iso=since_iso, db=db)
    except Exception as e:
        logger.warning("person-monitor: posts scrape failed for %s: %s", name, e)

    activity: List[Dict[str, Any]] = []
    try:
        activity = li_scrape_activity(li_url, max_items=15, since_iso=since_iso, db=db)
    except Exception as e:
        logger.warning("person-monitor: activity scrape failed for %s: %s", name, e)

    all_items = posts + activity
    if not all_items:
        # Silent-zero visibility: distinguishes "person was quiet" from
        # "the scrape returned nothing" when diagnosing dead signal types.
        logger.info("person-monitor: no items for %s (posts=%d, activity=%d)",
                    name, len(posts), len(activity))
    if all_items:
        try:
            kept = classify_items_parallel(
                all_items,
                vendor_pitch=vendor_pitch,
                competitor_list=competitors,
                max_workers=4,
            )
        except Exception as e:
            logger.warning("person-monitor: classify failed for %s: %s", name, e)
            kept = []
        logger.info("person-monitor: %s — %d items scraped, %d kept by classifier",
                    name, len(all_items), len(kept))
        for d in kept:
            signal_rows.append(_row(
                {
                    "headline": (d.get("summary") or d.get("quoted_excerpt") or "")[:280],
                    "quoted_excerpt": d.get("quoted_excerpt") or "",
                    "summary": d.get("summary") or "",
                    "url": d.get("url") or "",
                    "person_name": name,
                    "person_linkedin_url": li_url,
                    "person_title": title,
                    "kind": d.get("kind"),
                    # Full content for the LinkedIn-style post card in the
                    # dashboard (2026-07-07).
                    "post_text": d.get("post_text") or "",
                    "original_post_author": d.get("original_post_author") or "",
                    "comment_text": d.get("comment_text") or "",
                    "posted_at": d.get("posted_at"),
                    "engagement": d.get("engagement") or {},
                },
                d["signal_type"],
                "apify_linkedin_posts" if d.get("kind") == "post"
                else "apify_linkedin_activity",
                d.get("confidence") or 0.7,
            ))

    if signal_rows:
        try:
            insert_signals(db, signal_rows)
        except Exception as e:
            logger.warning("person-monitor: insert_signals failed for %s: %s", name, e)

    # Stamp the high-water-mark (drives the cadence gate) + touch updated_at.
    try:
        _now = datetime.now(timezone.utc).isoformat()
        db.table("leads").update(
            {"last_person_monitor_at": _now, "updated_at": _now}
        ).eq("id", lead_id).eq("org_id", org_id).execute()
    except Exception:
        pass

    return {"ok": True, "signals": len(signal_rows)}


def monitor_watched_people(db: Client, *, org_id: str) -> Dict[str, Any]:
    """Run a person-monitor pass for every watched person in one org."""
    try:
        rows = (
            db.table("leads")
            .select("id")
            .eq("org_id", org_id)
            .eq("person_monitoring", True)
            .execute()
            .data
            or []
        )
    except Exception as e:
        logger.warning("person-monitor: list watched failed for org %s: %s", org_id, e)
        return {"watched": 0, "signals": 0}

    total_signals = 0
    for r in rows:
        out = monitor_one_person(db, org_id=org_id, lead_id=r["id"])
        total_signals += int(out.get("signals") or 0)
    return {"watched": len(rows), "signals": total_signals}
