"""Apollo News → commercial signals.

Apollo aggregates broad web news per company, each article tagged with
event_categories. The 2026-07-24 bake-off showed it catches leadership moves,
funding and M&A that PredictLeads misses — BT's $4B Verizon JV + a Scottish
Water contract, Target's board appointment, extra H&M exec changes — all
absent from PL on the same accounts.

Apollo's crawl is noisier than PL's curated feed (store openings, community
events, product launches, "self-checkout" gripes), so we take ONLY the
high-precision commercial categories and map them to the SAME signal types PL
already uses. Everything else is dropped — we never surface noise as a signal.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import httpx
from supabase import Client

from app.config import settings

logger = logging.getLogger(__name__)

# Only the high-precision commercial categories. Deliberately NOT the noisy
# ones Apollo over-produces (opens_new_location, attends_event, launches,
# partners_with, has_issues_with) — the bake-off showed those are mostly
# local/marketing noise for a cyber seller.
#
# "leaves" is excluded on purpose: it mostly fires on an EX-employee doing
# something at ANOTHER company ("J. Crew hires former Walmart exec"), which
# isn't a signal about the watched account. hires/promotes are appointments
# AT the company and stay clean.
_NEWS_CATEGORY_TO_TYPE: Dict[str, str] = {
    "hires": "new_leadership",
    "promotes": "new_leadership",
    "acquires": "recent_acquisition",
    "is_acquired_by": "recent_acquisition",
    "acquired_by": "recent_acquisition",
    "merges_with": "recent_acquisition",
    "receives_financing": "recently_funded",
    "raises_funding": "recently_funded",
    "raises": "recently_funded",
}

# Dropped from a company name before we look for it in a headline — they carry
# no identifying weight and would match half the web.
_NAME_STOPWORDS = {"group", "inc", "ltd", "plc", "llc", "corp", "corporation",
                   "company", "co", "holdings", "the", "international", "motor",
                   "limited", "and", "global", "technologies", "solutions"}


def _name_tokens(company_name: str) -> List[str]:
    """Identifying words from a company name (>=2 chars, minus generic
    suffixes). Empty for names too short to match on (H&M, BT) — in which
    case we skip the guard rather than drop everything."""
    toks = re.findall(r"[a-z0-9]{2,}", (company_name or "").lower())
    return [t for t in toks if t not in _NAME_STOPWORDS]


def _mentions(title: str, tokens: List[str]) -> bool:
    """No usable tokens → guard doesn't apply (accept). Otherwise the headline
    must contain at least one identifying word as a whole word — kills Apollo's
    mis-associated articles (a 'Boyd Gaming' headline tagged to Target) and
    junk titles ('Auto draft')."""
    if not tokens:
        return True
    t = title.lower()
    return any(re.search(rf"\b{re.escape(tok)}\b", t) for tok in tokens)

_STRENGTH = {"recently_funded": 0.85, "recent_acquisition": 0.8, "new_leadership": 0.7}

# Only surface news from the last quarter (well inside the 6-month HARD rule).
NEWS_WINDOW_DAYS = 90
# Don't re-scan a company whose news we already captured within this window —
# the monitor can run every 2 days but news doesn't move that fast, and Apollo
# bills per page when it returns results.
RESCAN_DAYS = 7


def _fetch_news(org_id: str) -> List[Dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(days=NEWS_WINDOW_DAYS)).strftime("%Y-%m-%d")
    out: List[Dict[str, Any]] = []
    for page in (1, 2):  # 25/page; two pages is plenty for one company's quarter
        try:
            with httpx.Client(timeout=25) as c:
                r = c.post(
                    "https://api.apollo.io/api/v1/news_articles/search",
                    json={"organization_ids": [org_id], "published_at": {"min": since},
                          "per_page": 25, "page": page},
                    headers={"X-Api-Key": settings.APOLLO_API_KEY,
                             "Content-Type": "application/json"},
                )
            if r.status_code != 200:
                break
            batch = r.json().get("news_articles") or []
        except Exception as e:  # noqa: BLE001
            logger.warning("apollo_news: fetch failed for %s: %s", org_id, e)
            break
        out.extend(batch)
        if len(batch) < 25:
            break
    return out


def _recently_scanned(db: Client, company_id: str) -> bool:
    """True if we already wrote Apollo news for this company inside RESCAN_DAYS.
    Only fires after we've actually found news (a quiet company writes nothing,
    so it re-scans every pass — which is free, Apollo bills only on results)."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=RESCAN_DAYS)).isoformat()
        rows = (db.table("signals").select("detected_at")
                .eq("company_id", company_id).eq("signal_source", "apollo_news")
                .gte("detected_at", cutoff).limit(1).execute().data or [])
        return bool(rows)
    except Exception:
        return False


def watch_news(db: Client, *, org_id: str, company_id: str, domain: str,
               company_name: str = "") -> Dict[str, Any]:
    """Fetch Apollo news, map the commercial categories to signals, write them.
    Never raises into the monitor pass."""
    try:
        if not settings.APOLLO_API_KEY:
            return {"status": "skipped", "signals": 0}
        if _recently_scanned(db, company_id):
            return {"status": "skipped", "reason": "scanned_recently", "signals": 0}

        # Lazy imports break any monitor <-> enricher import cycle.
        from app.services.company_enricher import _summary_event_is_stale
        from app.services.enrichers.job_watch import _apollo_org_id
        from app.services.signal_writer import insert_signals

        apollo_org = _apollo_org_id(db, domain)
        if not apollo_org:
            return {"status": "no_org_id", "signals": 0}

        tokens = _name_tokens(company_name)
        articles = _fetch_news(apollo_org)
        rows: List[Dict[str, Any]] = []
        seen: set = set()
        for a in articles:
            title = (a.get("title") or "").strip()
            if not title:
                continue
            # Map on the first whitelisted category the article carries.
            stype = matched_cat = None
            for cat in (a.get("event_categories") or []):
                stype = _NEWS_CATEGORY_TO_TYPE.get((cat or "").lower())
                if stype:
                    matched_cat = (cat or "").lower()
                    break
            if not stype:
                continue
            # A leadership headline only counts if it's a senior business change
            # — an operational/clinical hire is noise for a cyber seller.
            if stype == "new_leadership":
                from app.services.company_enricher import _is_relevant_leadership
                if not _is_relevant_leadership(title):
                    continue
            # The article must actually be about this company — Apollo
            # mis-tags unrelated headlines to an org, and this drops them.
            if not _mentions(title, tokens):
                continue
            # Apollo returns the same article several times — collapse them.
            key = (stype, title.lower())
            if key in seen:
                continue
            seen.add(key)
            # Belt-and-braces on the 6-month HARD rule: drop anything the
            # headline anchors to a past calendar year.
            if _summary_event_is_stale(title, allow_prior_year=False):
                continue
            rows.append({
                "org_id": org_id,
                "company_id": company_id,
                "signal_type": stype,
                "signal_source": "apollo_news",
                "signal_data": {"headline": title, "category": matched_cat,
                                "url": a.get("url") or None},
                "signal_strength": _STRENGTH.get(stype, 0.7),
                "signal_subject_type": "account",
                "detected_at": a.get("published_at") or datetime.now(timezone.utc).isoformat(),
            })
        written = insert_signals(db, rows) if rows else 0
        return {"status": "ok", "articles": len(articles), "signals": written}
    except Exception as e:  # noqa: BLE001
        logger.error("apollo_news failed for %s: %s", domain, e)
        return {"status": "error", "reason": str(e), "signals": 0}
