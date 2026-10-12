"""Snapshot comparison logic for watchlist change detection.

Designed around PredictLeads-first sources. Each detector takes the previous
`latest_snapshot` plus the freshly-fetched data and returns a list of change
dicts shaped as `{"change_type": str, "details": str, "data": dict}`.
"""

import hashlib
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# Security keywords reused by the monitor to classify hiring_security signals
SECURITY_ROLE_KEYWORDS = {
    "security", "ciso", "infosec", "information security", "cybersecurity",
    "cyber security", "security engineer", "security analyst", "security architect",
    "security operations", "soc ", "devsecops", "appsec", "application security",
    "penetration", "vulnerability", "threat", "compliance officer", "grc ",
    "governance risk", "chief information security",
}


def hash_content(content: str) -> str:
    """Create a hash of page content for change detection.

    Strips Jina AI Reader's metadata headers (Title, URL Source, Published
    Time, Markdown Content) and any lines containing timestamps before
    normalising, so repeated scrapes of the same unchanged page produce the
    same hash. Without this, Jina's `Published Time` header alone would flip
    the hash on every fetch.
    """
    lines = content.splitlines()
    cleaned: List[str] = []
    _SKIP_PREFIXES = (
        "Title:",
        "URL Source:",
        "Published Time:",
        "Markdown Content:",
        "Last updated:",
        "Date:",
        "Updated:",
    )
    for line in lines:
        stripped = line.strip()
        if any(stripped.startswith(p) for p in _SKIP_PREFIXES):
            continue
        cleaned.append(stripped)

    normalized = " ".join(word for line in cleaned for word in line.split())
    return hashlib.md5(normalized.encode()).hexdigest()


# ---------------------------------------------------------------------------
# PredictLeads news events
# ---------------------------------------------------------------------------

def detect_predictleads_news_changes(
    old_snapshot: Dict[str, Any],
    new_news: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Detect transitions on PredictLeads news event flags and new events.

    Fires on: has_recent_funding, has_recent_acquisition, has_recent_product_launch,
    has_recent_partnership (when they flip from False to True), plus any event
    dated after the most recent event we already stored.
    """
    changes: List[Dict[str, Any]] = []
    if not isinstance(new_news, dict):
        return changes

    old_news = old_snapshot.get("predictleads_news") or {}

    flag_alerts = {
        "has_recent_funding": ("funding_round", "Funding round detected"),
        "has_recent_acquisition": ("acquisition", "Acquisition activity detected"),
        "has_recent_product_launch": ("product_launch", "New product launch detected"),
        "has_recent_partnership": ("partnership", "New partnership announced"),
    }

    for flag, (change_type, label) in flag_alerts.items():
        if new_news.get(flag) and not old_news.get(flag):
            # Find the event(s) of that category to give better context
            matching_events = [
                e for e in (new_news.get("events") or [])
                if isinstance(e, dict) and _news_flag_matches(flag, e.get("category", ""))
            ]
            event_summary = ""
            if matching_events:
                top = matching_events[0]
                event_summary = top.get("summary") or top.get("article_title") or ""

            details = f"{label}: {event_summary}" if event_summary else label
            changes.append({
                "change_type": change_type,
                "details": details,
                "data": {
                    "events": matching_events[:3],
                },
            })

    # Also surface brand-new events that weren't in the previous snapshot
    old_event_keys = {
        _event_key(e)
        for e in (old_news.get("events") or [])
        if isinstance(e, dict)
    }
    for event in (new_news.get("events") or []):
        if not isinstance(event, dict):
            continue
        key = _event_key(event)
        if key in old_event_keys:
            continue
        category = event.get("category") or "news"
        if category in ("receives_financing", "acquires", "launches", "partners_with"):
            # Already covered by the flag-transition block above
            continue
        summary = event.get("summary") or event.get("article_title") or "New PredictLeads event"
        changes.append({
            "change_type": "predictleads_news",
            "details": f"{category}: {summary}",
            "data": {
                "category": category,
                "summary": summary,
                "article_url": event.get("article_url"),
                "date": event.get("date"),
            },
        })

    return changes


def _news_flag_matches(flag: str, category: str) -> bool:
    mapping = {
        "has_recent_funding": "receives_financing",
        "has_recent_acquisition": "acquires",
        "has_recent_product_launch": "launches",
        "has_recent_partnership": "partners_with",
    }
    return mapping.get(flag) == category


def _event_key(event: Dict[str, Any]) -> str:
    return f"{event.get('category', '')}::{event.get('article_url') or event.get('summary') or event.get('date') or ''}"


# ---------------------------------------------------------------------------
# PredictLeads financing
# ---------------------------------------------------------------------------

def detect_funding_changes(
    old_snapshot: Dict[str, Any],
    new_financing: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Detect new funding rounds from PredictLeads /financing_events."""
    changes: List[Dict[str, Any]] = []
    if not isinstance(new_financing, dict):
        return changes

    old_rounds = (old_snapshot.get("financing") or {}).get("rounds") or []
    new_rounds = new_financing.get("rounds") or []

    old_keys = {
        f"{r.get('type', '')}::{r.get('date', '')}::{r.get('amount', '')}"
        for r in old_rounds if isinstance(r, dict)
    }

    for r in new_rounds:
        if not isinstance(r, dict):
            continue
        key = f"{r.get('type', '')}::{r.get('date', '')}::{r.get('amount', '')}"
        if key in old_keys:
            continue
        investors = r.get("investors") or []
        details = (
            f"New {r.get('type') or 'funding'} round"
            + (f" of {r['amount']}" if r.get("amount") else "")
            + (f" from {', '.join(investors[:3])}" if investors else "")
            + (f" ({r['date']})" if r.get("date") else "")
        )
        changes.append({
            "change_type": "funding_round",
            "details": details,
            "data": r,
        })

    return changes


# ---------------------------------------------------------------------------
# PredictLeads jobs
# ---------------------------------------------------------------------------

def detect_predictleads_job_changes(
    old_snapshot: Dict[str, Any],
    new_jobs: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Detect new job titles and hiring-category transitions.

    New signals: hiring_security (keyword match on title), transitions on
    is_hiring_sales / is_hiring_leadership, and brand-new job titles.
    """
    changes: List[Dict[str, Any]] = []
    if not isinstance(new_jobs, dict):
        return changes

    old_jobs = old_snapshot.get("predictleads_jobs") or {}

    old_titles = {
        (j.get("title") or "").lower()
        for j in (old_jobs.get("jobs") or [])
        if isinstance(j, dict)
    }
    new_job_rows = [j for j in (new_jobs.get("jobs") or []) if isinstance(j, dict)]

    new_titles_added: List[Dict[str, Any]] = []
    security_roles: List[Dict[str, Any]] = []

    for job in new_job_rows:
        title = job.get("title") or ""
        title_lower = title.lower()
        if not title_lower:
            continue
        if title_lower not in old_titles:
            new_titles_added.append(job)
        if any(kw in title_lower for kw in SECURITY_ROLE_KEYWORDS):
            security_roles.append(job)

    # Transition: hiring_sales
    if new_jobs.get("is_hiring_sales") and not old_jobs.get("is_hiring_sales"):
        changes.append({
            "change_type": "hiring_sales",
            "details": "Now hiring sales/BD roles — go-to-market expansion signal",
            "data": {
                "jobs": [j for j in new_job_rows if _is_sales_title(j.get("title", ""))][:5],
            },
        })

    # Transition: hiring_leadership
    if new_jobs.get("is_hiring_leadership") and not old_jobs.get("is_hiring_leadership"):
        changes.append({
            "change_type": "hiring_leadership",
            "details": "Now hiring leadership roles — org restructure or scale-up",
            "data": {
                "jobs": [j for j in new_job_rows if _is_leadership_title(j.get("title", ""))][:5],
            },
        })

    # New signal: hiring_security (transition from none → at least one)
    old_had_security = any(
        any(kw in (j.get("title") or "").lower() for kw in SECURITY_ROLE_KEYWORDS)
        for j in (old_jobs.get("jobs") or [])
        if isinstance(j, dict)
    )
    if security_roles and not old_had_security:
        changes.append({
            "change_type": "hiring_security",
            "details": (
                f"Now hiring security roles: "
                f"{', '.join((r.get('title') or '') for r in security_roles[:3])}"
            ),
            "data": {"security_jobs": security_roles[:5]},
        })

    # Brand-new job titles (only if we didn't already alert via transitions)
    if new_titles_added:
        # Cap the alert at the 5 most recent new titles so the alert stays readable
        sample = new_titles_added[:5]
        changes.append({
            "change_type": "new_job_posting",
            "details": (
                f"{len(new_titles_added)} new role(s) posted: "
                f"{', '.join((j.get('title') or '') for j in sample)}"
            ),
            "data": {"new_jobs": sample},
        })

    return changes


def _is_sales_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in ("sales", "bdr", "sdr", "account executive", "revenue", "business development"))


def _is_leadership_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in ("cmo", "cro", "vp ", "vice president", "head of", "director", "chief"))


# ---------------------------------------------------------------------------
# PredictLeads technology detections
# ---------------------------------------------------------------------------

def detect_tech_stack_changes(
    old_snapshot: Dict[str, Any],
    new_tech: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Compare PredictLeads tech stack — new tools added or removed."""
    changes: List[Dict[str, Any]] = []
    if not isinstance(new_tech, dict):
        return changes

    old_names = {
        (t.get("name") or "").lower()
        for t in ((old_snapshot.get("predictleads_tech") or {}).get("technologies") or [])
        if isinstance(t, dict)
    }
    new_names = {
        (t.get("name") or "").lower()
        for t in (new_tech.get("technologies") or [])
        if isinstance(t, dict)
    }

    added = new_names - old_names - {""}
    removed = old_names - new_names - {""}

    if added:
        changes.append({
            "change_type": "tech_stack_added",
            "details": f"New technologies detected: {', '.join(sorted(added))}",
            "data": {"added": sorted(added)},
        })

    if removed:
        changes.append({
            "change_type": "tech_stack_removed",
            "details": f"Technologies no longer detected: {', '.join(sorted(removed))}",
            "data": {"removed": sorted(removed)},
        })

    return changes


# ---------------------------------------------------------------------------
# Perplexity Sonar deep research
# ---------------------------------------------------------------------------

def detect_perplexity_changes(
    old_snapshot: Dict[str, Any],
    new_research: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Compare Perplexity Sonar growth_signals between runs.

    We only surface *new* items that weren't in the previous research result.
    Compliance and security_posture are ignored here — they change rarely and
    should be part of enrichment, not weekly watchlist alerts.

    Perplexity is an LLM so it rewords the same item on each run (e.g.
    'Acme Reports Partnership' vs 'Acme Partnership Status Change
    Detected'). We cannot rely on string equality. Instead we extract a
    keyword fingerprint from each item and only flag it as new if no old
    item shares a significant keyword overlap.

    We also cap new items per category at 2 (not 3) so one category with 5
    re-worded items can't flood the alert feed.
    """
    changes: List[Dict[str, Any]] = []
    if not isinstance(new_research, dict):
        return changes

    old = (old_snapshot.get("perplexity") or {}).get("growth_signals") or {}
    new = new_research.get("growth_signals") or {}

    diffs = {
        "recent_partnerships": ("partnership", "Partnership reported"),
        "product_launches": ("product_launch", "Product launch reported"),
        "upcoming_events": ("event", "Upcoming event"),
        "office_expansions": ("office_expansion", "Office expansion"),
    }

    for key, (change_type, label) in diffs.items():
        old_items = _normalise_perplexity_list(old.get(key) or [])
        new_items = _normalise_perplexity_list(new.get(key) or [])

        old_fingerprints = [_perplexity_fingerprint(item) for item in old_items]
        seen_in_this_run: List[set] = []

        for item in new_items:
            fp = _perplexity_fingerprint(item)
            if not fp:
                continue
            # Skip if this item is a fuzzy match for anything in the old run
            if any(_fingerprints_match(fp, old_fp) for old_fp in old_fingerprints):
                continue
            # Also skip if it fuzzy-matches something we already emitted in
            # THIS run (deduplicates multiple re-wordings of the same signal)
            if any(_fingerprints_match(fp, prev_fp) for prev_fp in seen_in_this_run):
                continue
            seen_in_this_run.append(fp)

            changes.append({
                "change_type": f"perplexity_{change_type}",
                "details": f"{label}: {item}",
                "data": {"item": item, "source": "perplexity_sonar"},
            })
            if len(seen_in_this_run) >= 2:
                break  # cap per category

    return changes


def _normalise_perplexity_list(items: List[Any]) -> List[str]:
    """Perplexity sometimes returns strings, sometimes dicts — flatten to strings."""
    out: List[str] = []
    for item in items:
        if isinstance(item, str):
            out.append(item.strip())
        elif isinstance(item, dict):
            # Best-effort extraction
            label = item.get("name") or item.get("title") or item.get("description") or item.get("summary") or ""
            if label:
                out.append(label.strip())
    return [s for s in out if s]


# Words that Perplexity loves to reuse but carry no signal — strip them before
# fingerprinting so 'Partnership Activity Detected' and 'Reports Partnership'
# fingerprint to the same thing.
_PERPLEXITY_STOP = {
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "for", "with",
    "to", "is", "are", "has", "have", "had", "new", "recent", "latest",
    "upcoming", "shows", "reports", "reported", "report", "detected",
    "detect", "activity", "status", "change", "changes", "update",
    "updated", "updates", "possible", "opportunity", "announcement",
    "announced", "announces", "announce", "signal", "signals",
    "information", "data", "found", "not",
}


def _perplexity_fingerprint(text: str) -> set:
    """Reduce a Perplexity item to a set of meaningful lowercase tokens."""
    if not text:
        return set()
    # Keep only alphanumerics, split on whitespace
    lowered = "".join(c if c.isalnum() or c.isspace() else " " for c in text.lower())
    return {
        word for word in lowered.split()
        if len(word) >= 3 and word not in _PERPLEXITY_STOP
    }


def _fingerprints_match(a: set, b: set) -> bool:
    """Return True if two fingerprints share enough keywords to be 'the same'.

    If either side has <=2 meaningful tokens, any overlap counts as a match.
    Otherwise require at least 50% overlap of the smaller set.
    """
    if not a or not b:
        return False
    overlap = a & b
    if not overlap:
        return False
    smaller = min(len(a), len(b))
    if smaller <= 2:
        return True
    return len(overlap) / smaller >= 0.5


# ---------------------------------------------------------------------------
# Jina website + blog
# ---------------------------------------------------------------------------

def detect_website_changes(
    old_hashes: Dict[str, str],
    new_hashes: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Compare Jina page hashes for /about and /blog only."""
    changes: List[Dict[str, Any]] = []
    page_labels = {
        "/about": "About page",
        "/blog": "Blog",
    }
    for path, new_hash in new_hashes.items():
        old_hash = old_hashes.get(path)
        if old_hash and old_hash != new_hash:
            label = page_labels.get(path, path)
            changes.append({
                "change_type": "website_change",
                "details": f"{label} content has changed",
                "data": {"page": path},
            })
    return changes


def detect_blog_changes(
    old_titles: List[str],
    new_titles: List[str],
) -> List[Dict[str, Any]]:
    """Detect new blog post titles (simple string diff)."""
    changes: List[Dict[str, Any]] = []
    old_set = {t.strip().lower() for t in (old_titles or []) if t}
    added = []
    for title in (new_titles or []):
        if not title:
            continue
        if title.strip().lower() not in old_set:
            added.append(title.strip())

    if added:
        changes.append({
            "change_type": "new_blog_post",
            "details": f"New blog post(s): {', '.join(added[:3])}",
            "data": {"titles": added[:5]},
        })
    return changes


