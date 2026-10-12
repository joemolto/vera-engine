"""Heartbeat of the dashboard — surfaces the named moments worth the
rep's attention in the last 48h. Backed by Claude Haiku.

Design intent: the home dashboard should feel like Vera tapping the
rep on the shoulder with "here's the 5-7 things you should know right
now". NOT a summary of what Vera did ("we enriched 22 leads, scanned
7 threats"). NAMED moments — specific people, specific companies,
specific CVEs, specific news.

Examples of good output:
  - "Jane Smith (CEO, Acme Security) just hit Hot — 90 score, hiring 5
    security engineers."
  - "Push Security raised $30M Series C — watchlist hit, board pressure
    on outbound now."
  - "CVE-2026-1234 actively exploited — affects Fortinet on 3 of your
    watchlist accounts."

Examples of bad output:
  - "7 threat alerts triggered — review them"  (count, not moment)
  - "Bulk lead sourcing run completed — 22 vendors found"  (operation log)

Cached on the organisations row with a 10-minute TTL. First dashboard
load after expiry hits Haiku (~1.5s), every load inside the window is
instant.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from supabase import Client

from app.config import settings
from app.services.claude import call_claude

logger = logging.getLogger(__name__)

CACHE_TTL_MINUTES = 10
LOOKBACK_HOURS = 48
# Soft upper bound. The agent is told to PREFER FEWER snippets — one
# great moment beats seven mediocre ones. Most days, 2-4 is the right
# answer.
MAX_SNIPPETS = 6


DIGEST_SYSTEM = """You are Vera's home-dashboard editor. The dashboard is the rep's HEARTBEAT — the moment they open the app, they should see only what's actually new and worth their attention.

QUALITY OVER QUANTITY. One great moment is better than five mediocre ones. Most days you should return 2-4 snippets. Six is the absolute maximum. Empty array is a valid answer when nothing real has happened.

WHAT BELONGS (a moment must clearly fit one of these to make the cut):
1. A genuinely new hot lead — the lead BECAME hot in the input window; not a stale lead that's been hot for weeks
2. A specific named watchlist event — funding round closed, acquisition announced, new exec appointed at a watched company, breach disclosed
3. A specific named CVE / threat newly correlated to the rep's tech stack or watchlist
4. A specific buying-signal moment naming a real lead or company

WHAT NEVER BELONGS:
- Operation summaries — "Enriched 22 leads", "Scanned 7 threats", "Vera ran a discovery batch", "Bulk lead sourcing completed". These are admin logs, not heartbeat moments. Skip them entirely.
- Counts and clusters — never say "Multiple X detected"; either pick the single most important X and name it, or skip the bucket.
- Stale leads — if a hot_lead row's first_hot_at is more than 48h old (you'll see it in the data), the rep already knows. DO NOT surface them.
- Anything generic, anything mechanical, anything the rep has seen before.

RULES:
1. NAME THE ENTITY in every snippet — person, company, CVE. Never describe as "a lead" or "an event".
2. EACH SNIPPET = ONE NAMED MOMENT. No clustering.
3. PUNCHY: title ≤ 9 words, summary ≤ 22 words, lead with the WHY.
4. NO CORPORATE FLUFF. No "we are excited", "thrilled to share", "important update".
5. BE RUTHLESS. If the input is mostly noise, return [] or 1 snippet. Don't pad.

OUTPUT JSON only:
{
  "snippets": [
    {
      "title": "Push Security closed $30M Series C",
      "summary": "Funding round announced this week — board pressure on outbound, vendor-review window now open.",
      "kind": "watchlist_news",
      "entity": "Push Security",
      "ts": "2026-05-14T08:00:00Z"
    }
  ]
}

`kind` is one of: hot_lead, watchlist_news, threat, leadership_change, signal_event, other.
Return an empty snippets array when nothing genuinely new has happened. That is the correct answer most days."""


def _fetch_raw_activity(db: Client, org_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """Pull a rich, ENTITY-NAMED activity stream for the digester.

    Returned dict has separate buckets so the LLM can see at-a-glance
    what kind of moment each item is — preserves names, scores, and
    company context rather than flattening to event types.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).isoformat()
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "hot_leads": [],
        "watchlist_news": [],
        "threats": [],
        "signals": [],
    }

    # 1. Hot/warm leads that FIRST became hot in the last 48h.
    # We filter on first_hot_at (set once by the scorer, never overwritten)
    # rather than scored_at — otherwise the dashboard surfaces leads the
    # rep has known about for weeks just because the engine re-scored
    # them today. The rep only cares about NEW hot leads here.
    try:
        leads_resp = (
            db.table("leads")
            .select("id, first_name, last_name, title, company_name, "
                    "company_industry, total_score, lead_rating, "
                    "best_approach_angle, signals, buying_signals, "
                    "scored_at, enriched_at, first_hot_at")
            .eq("org_id", org_id)
            .gte("first_hot_at", since)
            .in_("lead_rating", ["hot", "warm"])
            .order("total_score", desc=True)
            .limit(10)
            .execute()
        )
        for r in (leads_resp.data or []):
            full_name = f"{r.get('first_name', '')} {r.get('last_name', '')}".strip()
            buckets["hot_leads"].append({
                "name": full_name or "(unknown)",
                "title": r.get("title"),
                "company": r.get("company_name"),
                "industry": r.get("company_industry"),
                "score": r.get("total_score"),
                "rating": r.get("lead_rating"),
                "angle": r.get("best_approach_angle"),
                "signals": (r.get("signals") or [])[:4],
                "buying_signals": (r.get("buying_signals") or [])[:4],
                "first_hot_at": r.get("first_hot_at"),
            })
    except Exception as e:
        logger.debug("hot_leads fetch failed: %s", e)

    # 2. Watchlist alerts — named news/events on watched companies
    try:
        wl_resp = (
            db.table("watchlist_alerts")
            .select("title, alert_type, summary, source, company_name, created_at")
            .eq("org_id", org_id)
            .gte("created_at", since)
            .order("created_at", desc=True)
            .limit(40)
            .execute()
        )
        for r in (wl_resp.data or []):
            buckets["watchlist_news"].append({
                "title": r.get("title"),
                "type": r.get("alert_type"),
                "summary": r.get("summary"),
                "source": r.get("source"),
                "company": r.get("company_name"),
                "ts": r.get("created_at"),
            })
    except Exception as e:
        logger.debug("watchlist_alerts fetch failed: %s", e)

    # 3. Threat alerts — CVE/breach/regulatory hits relevant to the org
    try:
        threat_resp = (
            db.table("threat_alerts")
            .select("created_at, alert_data")
            .eq("org_id", org_id)
            .gte("created_at", since)
            .order("created_at", desc=True)
            .limit(30)
            .execute()
        )
        for r in (threat_resp.data or []):
            d = r.get("alert_data") or {}
            buckets["threats"].append({
                "title": d.get("threat_title") or d.get("title"),
                "summary": d.get("summary") or d.get("why_relevant"),
                "matched_tech": d.get("matched_tech") or d.get("matched_technologies"),
                "matched_companies": d.get("matched_companies"),
                "matched_industries": d.get("matched_industries"),
                "urgency": d.get("urgency"),
                "source": d.get("threat_source"),
                "ts": r.get("created_at"),
            })
    except Exception:
        pass

    # 4. Signals (new_leadership, recently_funded, hiring_*) joined to leads
    #    where available — gives us named "X joined as CISO at Y" moments.
    try:
        sig_resp = (
            db.table("signals")
            .select("signal_type, signal_data, detected_at, lead_id")
            .eq("org_id", org_id)
            .gte("detected_at", since)
            .order("detected_at", desc=True)
            .limit(40)
            .execute()
        )
        from app.services.signal_visibility import fetch_hidden_signal_types
        hidden = fetch_hidden_signal_types(db, org_id)
        sig_rows = [s for s in (sig_resp.data or []) if s.get("signal_type") not in hidden]
        # Pull lead names for any referenced lead_ids in one batch
        lead_ids = list({s.get("lead_id") for s in sig_rows if s.get("lead_id")})
        leads_by_id: Dict[str, Dict[str, Any]] = {}
        if lead_ids:
            lookup_resp = (
                db.table("leads")
                .select("id, first_name, last_name, title, company_name")
                .in_("id", lead_ids)
                .execute()
            )
            for r in (lookup_resp.data or []):
                leads_by_id[r["id"]] = r
        for s in sig_rows:
            lead = leads_by_id.get(s.get("lead_id"))
            entry: Dict[str, Any] = {
                "type": s.get("signal_type"),
                "ts": s.get("detected_at"),
            }
            data = s.get("signal_data") or {}
            for k in ("headline", "title", "summary", "amount", "stage",
                      "role", "company_name", "matched_play"):
                if k in data:
                    entry[k] = data[k]
            if lead:
                entry["lead_name"] = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip()
                entry["lead_title"] = lead.get("title")
                entry["lead_company"] = lead.get("company_name")
            buckets["signals"].append(entry)
    except Exception as e:
        logger.debug("signals fetch failed: %s", e)

    return buckets


def _format_for_llm(buckets: Dict[str, List[Dict[str, Any]]]) -> str:
    """Compact JSON the LLM can scan quickly while keeping NAMED entities.

    We send each bucket separately so the model knows which kind of
    moment each row is — easier to reason about priority that way.
    """
    payload = {
        "hot_leads_scored_last_48h": buckets.get("hot_leads", [])[:12],
        "watchlist_news_last_48h": buckets.get("watchlist_news", [])[:25],
        "threats_last_48h": buckets.get("threats", [])[:15],
        "buying_signals_last_48h": buckets.get("signals", [])[:25],
    }
    return json.dumps(payload, default=str, separators=(",", ":"))


def generate_recent_digest(db: Client, org_id: str) -> List[Dict[str, Any]]:
    """Generate fresh — pull entities, prompt Haiku, cache, return."""
    buckets = _fetch_raw_activity(db, org_id)
    total_raw = sum(len(v) for v in buckets.values())

    if total_raw == 0:
        snippets: List[Dict[str, Any]] = []
    else:
        prompt = (
            f"Here is the rep's pipeline activity over the last {LOOKBACK_HOURS}h.\n"
            f"\n"
            f"Return AT MOST {MAX_SNIPPETS} snippets. Prefer fewer — empty array is "
            f"valid if nothing meaningful happened. Do NOT output operation summaries, "
            f"counts, or stale leads.\n\n"
            f"{_format_for_llm(buckets)}"
        )
        result = call_claude(
            prompt=prompt,
            system=DIGEST_SYSTEM,
            model=settings.CLAUDE_FAST_MODEL,
            cache_system=True,
        )
        if isinstance(result, dict) and result.get("error"):
            logger.warning("Digest LLM call failed for org %s: %s", org_id, result.get("error"))
            snippets = []
        else:
            snippets = (result or {}).get("snippets", []) if isinstance(result, dict) else []
            if not isinstance(snippets, list):
                snippets = []

    now_iso = datetime.now(timezone.utc).isoformat()
    db.table("organisations").update({
        "recent_digest": snippets,
        "recent_digest_at": now_iso,
    }).eq("id", org_id).execute()

    logger.info(
        "Generated recent digest for org %s — %d snippets from %d hot_leads, %d watchlist, %d threats, %d signals",
        org_id, len(snippets),
        len(buckets.get("hot_leads", [])),
        len(buckets.get("watchlist_news", [])),
        len(buckets.get("threats", [])),
        len(buckets.get("signals", [])),
    )
    return snippets


def get_recent_digest(db: Client, org_id: str) -> Dict[str, Any]:
    """Cache-aware fetch. Regenerates if older than CACHE_TTL_MINUTES."""
    resp = (
        db.table("organisations")
        .select("recent_digest, recent_digest_at")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    if not resp.data:
        return {"snippets": [], "cached": False, "generated_at": None}

    row = resp.data[0]
    cached_at = row.get("recent_digest_at")
    cached_snippets = row.get("recent_digest") or []

    cache_fresh = False
    if cached_at:
        try:
            cached_dt = datetime.fromisoformat(cached_at.replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - cached_dt
            cache_fresh = age < timedelta(minutes=CACHE_TTL_MINUTES)
        except (ValueError, TypeError):
            cache_fresh = False

    if cache_fresh:
        return {
            "snippets": cached_snippets,
            "cached": True,
            "generated_at": cached_at,
        }

    snippets = generate_recent_digest(db, org_id)
    return {
        "snippets": snippets,
        "cached": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
