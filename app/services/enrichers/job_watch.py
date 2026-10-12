"""Weekly job watch — the demand-signal collector.

Runs on the monitor pass. For each watched company: read its live
vacancies from source, remember what we saw, and turn that into signals.

Three things this does that the old once-at-enrichment path could not:

  1. Fresh vs stale. Because we keep a history, a role posted last week
     ("new budget, they're building") is a different signal from one open
     five months ("they can't fill it, your product is the alternative").
     The second is the better opener: it's an admitted, funded, unsolved
     problem.
  2. The actual role. "Hiring a Vulnerability Management Lead" rather than
     a hiring_security boolean.
  3. Honest failure. "We checked and they're not hiring" and "we couldn't
     read their jobs" are recorded separately and never conflated. The
     person-activity scraper 400'd silently for a month; not again.

Every title is gated by the shared Claude cyber taxonomy. No keyword
shortcut, no source-level inference (2026-07-21).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from supabase import Client

from app.config import settings
from app.prompts.enrichment import CYBER_ROLE_CLASSIFY_PROMPT, CYBER_ROLE_TAXONOMY
from app.services.claude import call_claude
from app.services.enrichers.cache import get_cached
from app.services.enrichers.job_boards import (
    SUPPORTED,
    detect_ats,
    fetch_ats_jobs,
)
from app.services.signal_writer import insert_signals

logger = logging.getLogger(__name__)

# A role posted within this window is "new" — fresh budget, act now.
NEW_DAYS = 30
# Open at least this long is "unfilled" — they're struggling, you're the
# alternative to the hire.
STALE_DAYS = 90
# Detection is the expensive bit, so cache it. Re-probe companies where we
# found nothing only occasionally, in case they adopt an ATS later.
DETECT_TTL_DAYS = 30
# Guard against a bad read wiping a company's history.
MIN_JOBS_TO_TRUST_CLOSURE = 1
# Most a single company can contribute in one pass. A big employer can have
# a dozen security roles open; firing all of them reads like recruitment
# spam, which is the opposite of a buying trigger.
MAX_SIGNALS_PER_RUN = 3
# Titles per classifier call. Big boards (one had 302 roles) must be
# chunked, not truncated.
CLASSIFY_BATCH = 60
# Ceiling per pass, so one enormous board can't run up the bill in a single
# run. Anything left over is picked up next week (and logged, never silent).
MAX_TITLES_PER_RUN = 400


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        s = str(ts).replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Waterfall step 2 — the careers page's own structured job data
# ---------------------------------------------------------------------------

def _jsonld_jobs(domain: str) -> Optional[List[Dict[str, Any]]]:
    """Most modern careers pages publish JobPosting JSON-LD for Google.
    Free, dated, and it needs no ATS."""
    out: List[Dict[str, Any]] = []
    for path in ("/careers", "/jobs", "/"):
        try:
            with httpx.Client(timeout=12, follow_redirects=True) as c:
                r = c.get(f"https://{domain}{path}",
                          headers={"User-Agent": "Mozilla/5.0 (compatible; VeraBot/1.0)"})
            if r.status_code != 200:
                continue
            for blob in re.findall(
                r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', r.text, re.S | re.I
            ):
                try:
                    data = json.loads(blob.strip())
                except Exception:
                    continue
                for node in (data if isinstance(data, list) else [data]):
                    if not isinstance(node, dict):
                        continue
                    if node.get("@type") != "JobPosting":
                        continue
                    title = (node.get("title") or "").strip()
                    if not title:
                        continue
                    out.append({
                        "external_id": str(node.get("identifier") or node.get("url") or title)[:200],
                        "title": title,
                        "location": None,
                        "url": node.get("url") or f"https://{domain}{path}",
                        "first_published": node.get("datePosted"),
                        "updated_at": node.get("datePosted"),
                    })
            if out:
                return out
        except Exception:
            continue
    return out or None


# ---------------------------------------------------------------------------
# Waterfall step 3 — LinkedIn, the paid fallback we already own
# ---------------------------------------------------------------------------

def _linkedin_jobs(company_name: str) -> Optional[List[Dict[str, Any]]]:
    try:
        from app.services.apify_client import scrape_linkedin_jobs
        raw = scrape_linkedin_jobs(company_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("job_watch: linkedin fallback failed for %s: %s", company_name, e)
        return None
    if not raw:
        return None
    out = []
    for j in raw:
        title = (j.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "external_id": str(j.get("id") or j.get("url") or title)[:200],
            "title": title,
            "location": j.get("location"),
            "url": j.get("url") or "",
            "first_published": j.get("posted_date"),
            "updated_at": j.get("posted_date"),
        })
    return out or None


# ---------------------------------------------------------------------------
# Waterfall step 1b — Apollo's job-postings endpoint.
#
# The workhorse for large employers. A big brand's careers site 403s our
# scraper and its jobs live on a corporate domain we don't hold (H&M seeded
# as hm.com, jobs on hmgroup.com), so the free routes go blind. Apollo
# aggregates the same boards (LinkedIn, SmartRecruiters, the ATS) server-side,
# dated, and never blocks us. One request (per_page below) pulls the whole
# board for 1 credit.
# ---------------------------------------------------------------------------

# Titles at a big employer are overwhelmingly retail/ops noise. This
# recall-oriented cull keeps only what could be a security or GRC role so the
# Claude classifier — the precision gate that still decides is_security — runs
# on a handful of candidates, not an 800-role board. Deliberately broad (it
# lets "Security Guard" through; Claude drops it) but excludes the bare
# risk/audit terms that would drag in a bank's entire finance function.
_SECURITY_TITLE_RE = re.compile(
    r"\b("
    r"security|cyber(security)?|infosec|information security|"
    r"appsec|application security|product security|cloud security|network security|"
    r"soc|secops|siem|detection|threat|incident response|dfir|forensic|malware|"
    r"vulnerabilit|penetration|pentest|red team|blue team|offensive security|"
    r"ciso|cso|iam|identity and access|privileged access|zero trust|devsecops|"
    r"grc|governance risk|compliance|privacy|data protection|dpo|"
    r"trust and safety|trust & safety"
    r")\b",
    re.I,
)

# Apollo bills 1 credit PER PAGE, and per_page maxes at 10,000 — so one page
# at the max pulls a whole board for a single credit (the endpoint's hard
# ceiling is 10,000 records total anyway). Fetching fewer would just miss
# roles for the same price.
APOLLO_JOBS_PER_PAGE = 10000


def _prefilter_security(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only titles that could plausibly be a security/GRC role."""
    return [j for j in jobs if _SECURITY_TITLE_RE.search(j.get("title") or "")]


def _apollo_org_id(db: Client, domain: str) -> Optional[str]:
    """Apollo's organization id for a domain, from the shared enrich cache
    (warm for any monitored account — Phase B already enriched it). Falls back
    to a live enrich only when the cache is cold."""
    data = get_cached(db, domain, "apollo_org")
    if not data:
        try:
            from app.services.enrichers.tech_stack import fetch_apollo_business_intel
            fetch_apollo_business_intel(domain)  # populates the apollo_org cache
            data = get_cached(db, domain, "apollo_org")
        except Exception as e:  # noqa: BLE001
            logger.warning("job_watch: apollo enrich failed for %s: %s", domain, e)
            data = None
    org = (data or {}).get("organization") or {}
    return org.get("id")


def _apollo_jobs(db: Client, domain: str) -> Optional[List[Dict[str, Any]]]:
    """The company's live board via Apollo, pre-filtered to security/GRC
    candidates. Returns:
      - a list (possibly empty) when Apollo answered — the board was readable
      - None when we couldn't read Apollo at all (no org id / error), so the
        caller falls through to the next route.
    'Readable but no security roles' ([]) is NOT 'unreadable' (None): a big
    employer with 800 retail roles and no security opening is a real answer.
    """
    org_id = _apollo_org_id(db, domain)
    if not org_id or not settings.APOLLO_API_KEY:
        return None
    try:
        with httpx.Client(timeout=45) as c:
            r = c.get(
                f"https://api.apollo.io/api/v1/organizations/{org_id}/job_postings",
                params={"per_page": APOLLO_JOBS_PER_PAGE, "page": 1},
                headers={"X-Api-Key": settings.APOLLO_API_KEY},
            )
        if r.status_code != 200:
            logger.warning("job_watch: apollo job_postings %s for %s", r.status_code, domain)
            return None
        payload = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("job_watch: apollo job_postings failed for %s: %s", domain, e)
        return None

    raw = payload.get("organization_job_postings") or []
    total = (payload.get("pagination") or {}).get("total_entries")
    if total and total > len(raw):
        # Loud, never silent: we capped the read, so downstream knows the
        # board is bigger than what we classified this pass.
        logger.warning(
            "job_watch: %s apollo board has %d postings, read first %d",
            domain, total, len(raw),
        )

    out: List[Dict[str, Any]] = []
    for p in raw:
        title = (p.get("title") or "").strip()
        if not title:
            continue
        loc = ", ".join(x for x in (p.get("city"), p.get("state"), p.get("country")) if x) or None
        out.append({
            "external_id": str(p.get("id") or p.get("url") or title)[:200],
            "title": title,
            "location": loc,
            "url": p.get("url") or "",
            "first_published": p.get("posted_at"),
            "updated_at": p.get("last_seen_at") or p.get("posted_at"),
        })
    return _prefilter_security(out)


# ---------------------------------------------------------------------------
# The waterfall
# ---------------------------------------------------------------------------

def fetch_jobs(db: Client, company_id: str, domain: str, company_name: str,
               allow_paid: bool = True) -> Dict[str, Any]:
    """Try every route in order. Returns jobs plus an honest status.

    status: ok | no_jobs_found | unreadable
      ok           - we read a board and it has vacancies
      no_jobs_found- we read a board and it genuinely has none
      unreadable   - every route failed; this is NOT "they aren't hiring"
    """
    cached = _cached_board(db, company_id)
    board = cached
    if not board or _stale_detection(board):
        detected = detect_ats(domain, company_name)
        board = detected or {"platform": None, "token": None, "via": "none"}
        board["detected_at"] = _now().isoformat()

    # 1. ATS
    platform, token = board.get("platform"), board.get("token")
    if platform in SUPPORTED and token:
        jobs = fetch_ats_jobs(platform, token)
        if jobs is not None:
            board["status"] = "ok" if jobs else "no_jobs"
            _save_board(db, company_id, board)
            return {"jobs": jobs, "source": platform,
                    "status": "ok" if jobs else "no_jobs_found", "board": board}
        board["status"] = "fetch_failed"
    elif platform:
        board["status"] = "unsupported_platform"
    else:
        board["status"] = "no_ats_found"
    _save_board(db, company_id, board)

    # 2. Apollo's aggregated board — comprehensive, dated, never blocked. This
    #    is what rescues the big brands the free careers-page routes can't read.
    #    Answering here (even with zero security roles) stops the waterfall, so
    #    we don't waste time 403-probing a careers page we already have covered.
    apollo = _apollo_jobs(db, domain)
    if apollo is not None:
        return {"jobs": apollo, "source": "apollo",
                "status": "ok" if apollo else "no_jobs_found", "board": board}

    # 3. Careers-page structured data
    jobs = _jsonld_jobs(domain)
    if jobs:
        return {"jobs": jobs, "source": "jsonld", "status": "ok", "board": board}

    # 4. LinkedIn (paid — skipped when the caller is cost-conscious)
    if allow_paid:
        jobs = _linkedin_jobs(company_name)
        if jobs:
            return {"jobs": jobs, "source": "linkedin", "status": "ok", "board": board}

    # Deliberately NOT falling through to web search: aggregator pages carry
    # no reliable posting date, and without a date the fresh-vs-stale split
    # (the whole point) collapses. Better spend is a Workday adapter.
    return {"jobs": [], "source": None, "status": "unreadable", "board": board}


def _cached_board(db: Client, company_id: str) -> Optional[Dict[str, Any]]:
    try:
        row = (db.table("companies").select("job_board")
               .eq("id", company_id).maybe_single().execute().data or {})
        return row.get("job_board") or None
    except Exception:
        return None


def _stale_detection(board: Dict[str, Any]) -> bool:
    """Only re-probe boards we never resolved, and only occasionally."""
    if board.get("platform"):
        return False
    seen = _parse(board.get("detected_at"))
    return not seen or (_now() - seen).days >= DETECT_TTL_DAYS


def _save_board(db: Client, company_id: str, board: Dict[str, Any]) -> None:
    try:
        db.table("companies").update({"job_board": board}).eq("id", company_id).execute()
    except Exception as e:  # noqa: BLE001
        logger.warning("job_watch: board cache write failed for %s: %s", company_id, e)


# ---------------------------------------------------------------------------
# History reconciliation
# ---------------------------------------------------------------------------

def _reconcile(db: Client, company_id: str, source: str,
               jobs: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Upsert what we saw, close what vanished. Only ever touches rows from
    the source we actually read, so a failed LinkedIn call can't close the
    Greenhouse history."""
    now_iso = _now().isoformat()
    try:
        existing = (db.table("company_job_postings")
                    .select("id, external_id, title, first_published, first_seen, closed_at, is_security, role_category")
                    .eq("company_id", company_id).eq("source", source).execute().data or [])
    except Exception as e:  # noqa: BLE001
        logger.warning("job_watch: history read failed for %s: %s", company_id, e)
        return {"new": [], "open": [], "closed": [], "seeded": False}

    # First time we've ever looked at this company: everything open is
    # pre-existing state, not news. Record it, announce nothing (same rule
    # the smart-group snapshot uses). Otherwise adding an account dumps its
    # whole backlog into the digest at once.
    first_run = not existing

    by_ext = {r["external_id"]: r for r in existing}
    seen_ids, new_rows, upd = set(), [], []

    for j in jobs:
        ext = j["external_id"]
        seen_ids.add(ext)
        if ext in by_ext:
            upd.append(by_ext[ext]["id"])
        else:
            new_rows.append({
                "company_id": company_id,
                "source": source,
                "external_id": ext,
                "title": _clean_title(j["title"])[:300],
                "location": (j.get("location") or None),
                "url": (j.get("url") or None),
                "first_published": _parse(j.get("first_published")).isoformat()
                if _parse(j.get("first_published")) else None,
                "first_seen": now_iso,
                "last_seen": now_iso,
            })

    if new_rows:
        try:
            db.table("company_job_postings").insert(new_rows).execute()
        except Exception as e:  # noqa: BLE001
            logger.warning("job_watch: insert failed for %s: %s", company_id, e)
    if upd:
        try:
            db.table("company_job_postings").update({"last_seen": now_iso}).in_("id", upd).execute()
        except Exception as e:  # noqa: BLE001
            logger.warning("job_watch: last_seen update failed: %s", e)

    # Anything we've stored for this source but didn't see this run is gone.
    closed = []
    if len(jobs) >= MIN_JOBS_TO_TRUST_CLOSURE:
        gone = [r for r in existing if r["external_id"] not in seen_ids and not r.get("closed_at")]
        if gone:
            try:
                db.table("company_job_postings").update({"closed_at": now_iso}) \
                  .in_("id", [r["id"] for r in gone]).execute()
                closed = gone
            except Exception as e:  # noqa: BLE001
                logger.warning("job_watch: close update failed: %s", e)

    try:
        rows = (db.table("company_job_postings")
                .select("id, external_id, title, url, first_published, first_seen, is_security, role_category")
                .eq("company_id", company_id).eq("source", source)
                .is_("closed_at", "null").execute().data or [])
    except Exception as e:  # noqa: BLE001
        # Loud, not silent: an empty list here looks identical to "no jobs".
        logger.error("job_watch: open-rows read failed for %s: %s", company_id, e)
        rows = []
    return {"new": new_rows, "open": rows, "closed": closed, "seeded": first_run}


# ---------------------------------------------------------------------------
# Classification — the single gate. Every title goes through here.
# ---------------------------------------------------------------------------

def _classify(db: Client, company_name: str, rows: List[Dict[str, Any]]) -> None:
    """Fill is_security / is_grc / role_category for postings not yet judged.

    Collapses identical titles before calling Claude: the same role posted in
    120 cities is ONE classification, and the verdict fans out to every row
    that shares the title. A noisy retail board (Target: 120 physical-security
    rows, 17 unique titles) costs one classification pass, not twenty.
    """
    todo = [r for r in rows if r.get("is_security") is None]
    if not todo:
        return

    # Group unjudged rows by normalised title. One representative per group is
    # classified; the rest inherit its verdict.
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in todo:
        groups.setdefault(_title_key(r["title"]), []).append(r)
    reps = [g[0] for g in groups.values()]

    # Cap on UNIQUE titles now, not total rows — dedup already did the heavy
    # lifting. Loud: a silent truncation here reads downstream as "not hiring".
    if len(reps) > MAX_TITLES_PER_RUN:
        logger.warning(
            "job_watch: %s has %d unique unjudged titles, classifying first %d this pass",
            company_name, len(reps), MAX_TITLES_PER_RUN,
        )
        reps = reps[:MAX_TITLES_PER_RUN]

    # Chunked, because a 300-title prompt both truncates the response and
    # silently loses titles. Ebury's Senior Security Engineer went unclassified
    # this way.
    for start in range(0, len(reps), CLASSIFY_BATCH):
        batch = reps[start:start + CLASSIFY_BATCH]
        verdicts = _classify_titles(company_name, [r["title"] for r in batch])
        for i, rep in enumerate(batch):
            v = verdicts.get(i)
            if not v:
                continue
            is_sec = bool(v.get("is_security"))
            is_grc = bool(v.get("is_grc"))
            category = (v.get("category") or "other")[:40]
            if is_grc and not is_sec:
                # Kept apart deliberately: "Number of security jobs" must not
                # count a sanctions or AML vacancy just because it's
                # governance-adjacent.
                category = "grc"
            patch = {"is_security": is_sec, "is_grc": is_grc, "role_category": category}
            # Fan the one verdict out to every row that shares this title.
            group = groups[_title_key(rep["title"])]
            for r in group:
                r.update(patch)
            try:
                db.table("company_job_postings").update(patch) \
                  .in_("id", [r["id"] for r in group]).execute()
            except Exception as e:  # noqa: BLE001
                logger.warning("job_watch: verdict write failed: %s", e)


def _classify_titles(company_name: str, titles: List[str]) -> Dict[int, Dict[str, Any]]:
    """Classify one batch of unique titles → {index: verdict}. No DB writes."""
    listing = "\n".join(f"{i}. {t}" for i, t in enumerate(titles))
    try:
        result = call_claude(
            CYBER_ROLE_CLASSIFY_PROMPT.format(
                taxonomy=CYBER_ROLE_TAXONOMY, company_name=company_name, titles=listing
            )
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("job_watch: classify failed for %s: %s", company_name, e)
        return {}

    verdicts = {v.get("i"): v for v in (result or {}).get("roles", []) if isinstance(v, dict)}
    missing = sum(1 for i in range(len(titles)) if i not in verdicts)
    if missing:
        logger.warning(
            "job_watch: %s — %d/%d titles came back unjudged, will retry next pass",
            company_name, missing, len(titles),
        )
    return verdicts


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def _clean_title(title: str) -> str:
    """Strip the junk boards emit: zero-width marks, tabs, repeated spaces.
    (Tide's board served a title with trailing U+200E and combining marks.)"""
    t = re.sub(r"[\u0000-\u001f\u007f\u200b-\u200f\u2028\u2029\u034f\ufeff]", " ", title or "")
    return re.sub(r"\s+", " ", t).strip()


def _title_key(title: str) -> str:
    """Same role in five cities collapses to one key."""
    t = _clean_title(title).lower()
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    # Trailing requisition numbers ("Engineer, Threat Detection - 5").
    t = re.sub(r"\b\d+\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _month(dt: Optional[datetime]) -> str:
    return dt.strftime("%B %Y") if dt else "an earlier date"


def _build_signals(org_id: str, company_id: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Two triggers, both deliberately stable in wording so the weekly rerun
    dedupes instead of re-announcing the same vacancy."""
    now = _now()
    out = []
    seen_titles: set[str] = set()
    for r in rows:
        if not (r.get("is_security") or r.get("is_grc")):
            continue
        # One role advertised in five cities is one opening, not five.
        key = _title_key(r["title"])
        if key in seen_titles:
            continue
        seen_titles.add(key)
        posted = _parse(r.get("first_published")) or _parse(r.get("first_seen"))
        if not posted:
            continue
        age = (now - posted).days
        stype = "hiring_grc" if r.get("is_grc") and not r.get("is_security") else "hiring_security"
        base = {
            "org_id": org_id,
            "company_id": company_id,
            "signal_type": stype,
            "signal_source": "job_board",
            "signal_subject_type": "account",
        }
        data = {
            "role": r["title"],
            "role_category": r.get("role_category"),
            "url": r.get("url"),
            "posted_at": posted.date().isoformat(),
            "days_open": age,
        }
        if age <= NEW_DAYS:
            out.append({**base, "signal_strength": 0.8, "_rank": (1, -age), "signal_data": {
                **data, "state": "new",
                "headline": f"Posted a {r['title']} role",
            }})
        elif age >= STALE_DAYS:
            # No day count in the headline: it would change every week and
            # defeat dedupe. The posting month is stable, so this fires once
            # and then stays quiet however long the vacancy lingers.
            out.append({**base, "signal_strength": 0.9, "_rank": (0, -age), "signal_data": {
                **data, "state": "unfilled",
                "headline": f"Still hiring a {r['title']}, open since {_month(posted)}",
            }})

    # Unfilled first (the stronger opener: an admitted, funded, unsolved
    # problem), then freshest. Then cap, so one big employer can't fill a
    # digest on its own.
    out.sort(key=lambda s: s.pop("_rank"))
    return out[:MAX_SIGNALS_PER_RUN]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def watch_jobs(db: Client, org_id: str, company_id: str, domain: str,
               company_name: str, allow_paid: bool = True,
               min_interval_days: int = 7, force: bool = False) -> Dict[str, Any]:
    """Collect, remember, classify, signal. Never raises into the monitor.

    Self-throttling: the monitor runs as often as every 2 days on hot
    accounts, but job boards don't move that fast and adverts sit live for
    weeks. Weekly is plenty and keeps the classifier spend down.
    """
    try:
        if not force:
            board = _cached_board(db, company_id) or {}
            last = _parse(board.get("last_checked_at"))
            if last and (_now() - last).days < min_interval_days:
                return {"status": "skipped", "reason": "checked_recently", "signals": 0, "roles": 0}

        fetched = fetch_jobs(db, company_id, domain, company_name, allow_paid=allow_paid)
        board = fetched.get("board") or {}
        board["last_checked_at"] = _now().isoformat()
        board["last_status"] = fetched["status"]
        _save_board(db, company_id, board)
        if fetched["status"] == "unreadable":
            return {"status": "unreadable", "signals": 0, "roles": 0}
        if not fetched["jobs"]:
            return {"status": "no_jobs_found", "signals": 0, "roles": 0}

        hist = _reconcile(db, company_id, fetched["source"], fetched["jobs"])
        # Classify either way: on the seeding run this fills the account's
        # open-roles picture (context for the dossier and the score) even
        # though we stay silent about it.
        _classify(db, company_name, hist["open"])
        sec = sum(1 for r in hist["open"] if r.get("is_security"))

        if hist.get("seeded"):
            return {"status": "seeded", "source": fetched["source"],
                    "roles": len(fetched["jobs"]), "security_roles": sec, "signals": 0}

        rows = _build_signals(org_id, company_id, hist["open"])
        written = insert_signals(db, rows) if rows else 0
        return {
            "status": "ok",
            "source": fetched["source"],
            "roles": len(fetched["jobs"]),
            "security_roles": sec,
            "signals": written,
        }
    except Exception as e:  # noqa: BLE001
        logger.error("job_watch failed for %s: %s", domain, e)
        return {"status": "error", "reason": str(e), "signals": 0, "roles": 0}
