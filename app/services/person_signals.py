"""Phase 3 — person-level signal layer.

The whole point of this module is to draw a clean line between
*account claims* and *person claims*. Account-level signals (funding,
hiring, CVEs) should never appear as person claims — that's the
structural bug that produced a false new-in-role claim
(2026-05-27).

Each signal here has subject=person and either:
  - explicit evidence carried in signal_data (started_at, months_in_role,
    previous_company), OR
  - is a function-only role label that makes no claim about tenure
    or activity ("security leader on team", not "new in seat").

Inputs:
  - lead dict (Apollo-derived: name, title, seniority, company_name)
  - linkedin_verification dict (post-verifier output with primary_role
    enriched by _extract_role_tenure)

Output: list of signal rows ready for signal_writer.insert_signals.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.services import signal_payload as sp

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Title regex sets — kept compact and well-bordered so partial matches
# don't fire ("Head of Sales Operations" must not become a security
# leader).
# ---------------------------------------------------------------------------

SECURITY_TITLE_PATTERNS = [
    re.compile(r"\bciso\b", re.I),
    re.compile(r"\bcso\b", re.I),
    re.compile(r"chief information security", re.I),
    re.compile(r"chief security", re.I),
    re.compile(r"head of (information )?security", re.I),
    re.compile(r"head of cyber", re.I),
    re.compile(r"head of infosec", re.I),
    re.compile(r"vp (of )?security", re.I),
    re.compile(r"vice president security", re.I),
    re.compile(r"director,? security", re.I),
    re.compile(r"director of security", re.I),
    re.compile(r"security director", re.I),
    re.compile(r"security officer", re.I),
    re.compile(r"deputy ciso", re.I),
    re.compile(r"(regional|divisional) ciso", re.I),
]

GRC_TITLE_PATTERNS = [
    re.compile(r"\bgrc\b", re.I),
    re.compile(r"head of (compliance|risk|governance|audit|privacy)", re.I),
    re.compile(r"director,? (compliance|risk|governance|audit|privacy)", re.I),
    re.compile(r"director of (compliance|risk|governance|audit|privacy)", re.I),
    re.compile(r"chief (compliance|risk|privacy) officer", re.I),
    re.compile(r"\b(compliance|risk|privacy|audit) (manager|officer|lead)\b", re.I),
    re.compile(r"data protection officer", re.I),
    re.compile(r"\bdpo\b", re.I),
]

TECHNICAL_TITLE_PATTERNS = [
    re.compile(r"\bcto\b", re.I),
    re.compile(r"chief technology", re.I),
    re.compile(r"vp (of )?engineering", re.I),
    re.compile(r"vice president engineering", re.I),
    re.compile(r"head of (engineering|platform|infrastructure|architecture)", re.I),
    re.compile(r"director of (engineering|platform|infrastructure)", re.I),
    re.compile(r"chief architect", re.I),
    re.compile(r"principal architect", re.I),
]

COMMERCIAL_TITLE_PATTERNS = [
    re.compile(r"\bcro\b", re.I),
    re.compile(r"chief revenue", re.I),
    re.compile(r"vp (of )?sales", re.I),
    re.compile(r"vice president sales", re.I),
    re.compile(r"head of (sales|revenue|enterprise sales|enterprise)", re.I),
    re.compile(r"director of (sales|revenue|enterprise)", re.I),
    re.compile(r"chief commercial", re.I),
]


def _matches_any(title: str, patterns: List[re.Pattern]) -> bool:
    return any(p.search(title) for p in patterns)


# ---------------------------------------------------------------------------
# Function-classification → signal_type
# ---------------------------------------------------------------------------

def classify_person_function(title: str) -> Optional[str]:
    """Return the *function* signal_type for this title, or None.

    Returns the most-specific match. A "CISO" matches security_leader
    not technical_leader. A "Head of GRC" matches grc_leader, not
    security_leader (even though they overlap).
    """
    if not title:
        return None
    # Order matters — most-specific first.
    if _matches_any(title, GRC_TITLE_PATTERNS):
        return "person_grc_leader"
    if _matches_any(title, SECURITY_TITLE_PATTERNS):
        return "person_security_leader"
    if _matches_any(title, COMMERCIAL_TITLE_PATTERNS):
        return "person_commercial_leader"
    if _matches_any(title, TECHNICAL_TITLE_PATTERNS):
        return "person_technical_leader"
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def create_person_signals(
    org_id: str,
    lead_id: str,
    lead: Dict[str, Any],
    enrichment_data: Dict[str, Any],
    expires_days: int = 180,
) -> List[Dict[str, Any]]:
    """Build person-level signal rows for this lead.

    Every signal carries enough evidence in signal_data that the
    dashboard can render honestly. No tenure without evidence:
    we never claim newness without explicit dated proof.

    Note: `person_security_leader` is also fired by _create_cyber_signals
    as the "no recency evidence" fallback path. The wipe-and-rewrite in
    that function clears person_security_leader before this runs, so
    whichever path produces it last wins — they always produce the same
    payload shape, so it doesn't matter which.
    """
    rows: List[Dict[str, Any]] = []
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=expires_days)
    ).isoformat()

    # Pull verifier output (post-tenure enrichment) — primary_role may
    # now carry started_at, ended_at, months_in_role, started_at_raw.
    verification = enrichment_data.get("linkedin_verification") or {}
    primary_role = verification.get("primary_role") or {}
    verification_status = verification.get("verification_status") or ""

    title = (primary_role.get("title") or lead.get("title") or "").strip()
    company = (
        primary_role.get("company")
        or lead.get("company_name")
        or ""
    ).strip()
    full_name = " ".join(
        x for x in [lead.get("first_name"), lead.get("last_name")] if x
    ).strip() or "Lead"

    started_at = primary_role.get("started_at")
    months_in_role = primary_role.get("months_in_role")

    # ---- 1. Function-only role labels (no tenure claim) -----------------

    role_signal_type = classify_person_function(title)
    if role_signal_type:
        rows.append({
            "org_id": org_id,
            "lead_id": lead_id,
            "signal_type": role_signal_type,
            "signal_source": "lead_title",
            "signal_data": sp.build_signal_payload(
                headline=_role_headline(role_signal_type, full_name, company),
                entity=company,
                detail=_role_detail(title, months_in_role),
                primary_title=title,
                person_name=full_name,
                company=company,
                started_at=started_at,
                months_in_role=months_in_role,
            ),
            "signal_strength": 0.7,
            "expires_at": expires_at,
        })

    # ---- 2. person_new_in_role — explicit recency evidence required ----

    new_in_role = _is_within_days(started_at, days=90)
    if new_in_role:
        rows.append({
            "org_id": org_id,
            "lead_id": lead_id,
            "signal_type": "person_new_in_role",
            "signal_source": "linkedin",
            "signal_data": sp.build_signal_payload(
                headline=f"{full_name} is new in their current role",
                entity=company,
                detail=(
                    f"{title} at {company} — started {primary_role.get('started_at_raw') or started_at}"
                    + (f", ≈{months_in_role} months ago" if months_in_role else "")
                ),
                primary_title=title,
                person_name=full_name,
                company=company,
                started_at=started_at,
                months_in_role=months_in_role,
                evidence_kind="linkedin_started_at",
            ),
            "signal_strength": 0.9,
            "expires_at": expires_at,
        })

    # ---- 3. person_promoted / person_changed_company — from PL events --
    #
    # PL "promotes" events at the lead's company → person_promoted.
    # PL "hires" events at the lead's company → person_changed_company.
    # We use the verifier status as additional evidence.
    pl_news = enrichment_data.get("predictleads_news") or {}
    pl_events = pl_news.get("events") or []
    name_lc = full_name.lower()
    cutoff_90 = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()

    matching_event = None
    matching_category = None
    for ev in pl_events:
        if not isinstance(ev, dict):
            continue
        if ev.get("category") not in ("hires", "promotes"):
            continue
        ev_date = ev.get("date") or ""
        if ev_date and ev_date < cutoff_90:
            continue
        summary_lc = (ev.get("summary") or "").lower()
        if name_lc and name_lc in summary_lc:
            matching_event = ev
            matching_category = ev.get("category")
            break

    if matching_event and matching_category == "promotes":
        rows.append({
            "org_id": org_id,
            "lead_id": lead_id,
            "signal_type": "person_promoted",
            "signal_source": "predictleads",
            "signal_data": sp.build_signal_payload(
                headline=f"{full_name} was promoted",
                entity=company,
                detail=matching_event.get("summary") or "Promotion announced",
                source_url=matching_event.get("article_url"),
                event_date=matching_event.get("date"),
                evidence_kind="predictleads_event",
            ),
            "signal_strength": 0.9,
            "expires_at": expires_at,
        })
    elif matching_event and matching_category == "hires":
        rows.append({
            "org_id": org_id,
            "lead_id": lead_id,
            "signal_type": "person_changed_company",
            "signal_source": "predictleads",
            "signal_data": sp.build_signal_payload(
                headline=f"{full_name} joined {company}",
                entity=company,
                detail=matching_event.get("summary") or "Hire announced",
                source_url=matching_event.get("article_url"),
                event_date=matching_event.get("date"),
                evidence_kind="predictleads_event",
            ),
            "signal_strength": 0.9,
            "expires_at": expires_at,
        })
    elif verification_status == "corrected" and new_in_role:
        # Apollo had a different company; LinkedIn confirms a recent
        # company change. No PL event but the corrected verification +
        # recent start date together is enough evidence.
        rows.append({
            "org_id": org_id,
            "lead_id": lead_id,
            "signal_type": "person_changed_company",
            "signal_source": "linkedin",
            "signal_data": sp.build_signal_payload(
                headline=f"{full_name} recently changed companies",
                entity=company,
                detail=(
                    f"LinkedIn shows {title} at {company}, started "
                    f"{primary_role.get('started_at_raw') or started_at}. "
                    "Apollo record was on a previous employer."
                ),
                person_name=full_name,
                company=company,
                started_at=started_at,
                evidence_kind="linkedin_corrected_verification",
            ),
            "signal_strength": 0.8,
            "expires_at": expires_at,
        })

    # ---- 4. LinkedIn activity signals (from scraped posts) -------------

    raw_posts = enrichment_data.get("linkedin_posts") or []
    if raw_posts:
        rows.extend(_derive_linkedin_activity_signals(
            org_id=org_id,
            lead_id=lead_id,
            full_name=full_name,
            company=company,
            raw_posts=raw_posts,
            expires_at=expires_at,
        ))

    return rows


# ---------------------------------------------------------------------------
# LinkedIn activity signals
# ---------------------------------------------------------------------------

# Topics that matter for cyber/GTM sales conversations. A post whose
# text matches any of these tells the rep "this person is publicly
# talking about something relevant" — strong personalisation hook.
RELEVANT_TOPIC_PATTERNS = [
    ("security", re.compile(r"\b(security|cyber(security)?|infosec|cisa|cve)\b", re.I)),
    ("compliance", re.compile(r"\b(compliance|soc[\s\-]?2|iso[\s/\-]?27001|hipaa|gdpr|nis[\s\-]?2|dora|fedramp|audit)\b", re.I)),
    ("ai", re.compile(r"\b(ai|artificial intelligence|llm|genai|gen[\s\-]?ai|ml|machine learning)\b", re.I)),
    ("data", re.compile(r"\b(data (governance|protection|breach)|privacy)\b", re.I)),
    ("trust", re.compile(r"\b(trust centre|trust center|zero trust)\b", re.I)),
    ("risk", re.compile(r"\b(risk management|vendor risk|third[\s\-]?party risk|enterprise risk)\b", re.I)),
    ("enterprise", re.compile(r"\b(enterprise (sales|ready|customer)|going upmarket|moving upmarket)\b", re.I)),
]

# Activity window for "they're posting" — kept loose because senior cyber
# leaders often go 6-8 weeks between posts. 90 days catches normal cadence
# without grading the truly inactive.
_ACTIVE_WINDOW_DAYS = 90

# Relevance window is wider — a post from 5 months ago about TPRM is
# still a strong personalisation hook for a cyber-vendor sales rep.
# Bounded by the 180-day signal expiry so we never reference content
# older than the signal itself.
_RELEVANT_WINDOW_DAYS = 180


def _derive_linkedin_activity_signals(
    org_id: str,
    lead_id: str,
    full_name: str,
    company: str,
    raw_posts: List[Dict[str, Any]],
    expires_at: str,
) -> List[Dict[str, Any]]:
    """Fire person_active_linkedin + person_posted_relevant_topic from posts.

    Two independent windows:
      - Activity (90d): proves they post on a normal-for-senior cadence.
      - Relevance (180d): captures topical posts as personalisation hooks,
        even when the activity signal wouldn't fire on its own.
    """
    rows: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    active_cutoff = now - timedelta(days=_ACTIVE_WINDOW_DAYS)
    relevant_cutoff = now - timedelta(days=_RELEVANT_WINDOW_DAYS)

    dated_posts: List[tuple] = []  # (datetime, post_dict)
    for p in raw_posts:
        if not isinstance(p, dict):
            continue
        # Normalised posts carry posted_at / postedAt / date (apify_client
        # normalises to flat ISO under the "date" key).
        posted_at_str = (
            p.get("posted_at")
            or p.get("postedAt")
            or p.get("date")
            or ""
        )
        if isinstance(posted_at_str, dict):
            posted_at_str = posted_at_str.get("date") or ""
        if not posted_at_str:
            continue
        try:
            posted_dt = datetime.fromisoformat(
                str(posted_at_str).replace("Z", "+00:00")
            )
            if posted_dt.tzinfo is None:
                posted_dt = posted_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if posted_dt >= relevant_cutoff:
            dated_posts.append((posted_dt, p))

    # Activity signal — posts within the last _ACTIVE_WINDOW_DAYS
    fresh_posts = [p for dt, p in dated_posts if dt >= active_cutoff]

    if fresh_posts:
        # Activity signal — just "they post". Subject=person, observed.
        latest = max(
            fresh_posts,
            key=lambda p: str(p.get("posted_at") or p.get("postedAt") or ""),
        )
        latest_url = (
            latest.get("post_url")
            or latest.get("linkedinUrl")
            or latest.get("url")
        )
        rows.append({
            "org_id": org_id,
            "lead_id": lead_id,
            "signal_type": "person_active_linkedin",
            "signal_source": "linkedin_posts",
            "signal_data": sp.build_signal_payload(
                headline=(
                    f"{full_name} is active on LinkedIn — "
                    f"{len(fresh_posts)} post{'' if len(fresh_posts) == 1 else 's'} in last {_ACTIVE_WINDOW_DAYS} days"
                ),
                entity=company,
                detail=None,
                source_url=latest_url,
                post_count=len(fresh_posts),
                window_days=_ACTIVE_WINDOW_DAYS,
                evidence_kind="linkedin_posts",
            ),
            "signal_strength": 0.7,
            "expires_at": expires_at,
        })

    # Relevant-topic signal — scan all posts in the wider 180d window,
    # not just the 90d activity window. A 5-month-old post about TPRM is
    # still a strong personalisation hook.
    matched_topics: Dict[str, Dict[str, Any]] = {}
    for _dt, p in dated_posts:
        text = (p.get("text") or p.get("content") or "").strip()
        if not text:
            continue
        for topic_id, pattern in RELEVANT_TOPIC_PATTERNS:
            if topic_id in matched_topics:
                continue
            m = pattern.search(text)
            if m:
                matched_topics[topic_id] = {
                    "topic": topic_id,
                    "snippet": _snippet_around_post(text, m.start(), m.end()),
                    "post_url": (
                        p.get("post_url")
                        or p.get("linkedinUrl")
                        or p.get("url")
                    ),
                }
    if matched_topics:
        # Emit ONE compound signal per person summarising matched topics.
        # Don't fire one per topic — that floods the dashboard.
        topic_list = sorted(matched_topics.keys())
        first_topic = matched_topics[topic_list[0]]
        rows.append({
            "org_id": org_id,
            "lead_id": lead_id,
            "signal_type": "person_posted_relevant_topic",
            "signal_source": "linkedin_posts",
            "signal_data": sp.build_signal_payload(
                headline=(
                    f"{full_name} posted about {', '.join(topic_list)}"
                ),
                entity=company,
                detail=f"Personalisation hook — {len(topic_list)} relevant topic(s) across {len(fresh_posts)} recent posts.",
                source_url=first_topic.get("post_url"),
                topics=topic_list,
                evidence_kind="linkedin_posts",
            ),
            "signal_strength": 0.8,
            "expires_at": expires_at,
            "snippet": first_topic.get("snippet"),
        })

    return rows


def _snippet_around_post(text: str, start: int, end: int, padding: int = 90) -> str:
    s = max(0, start - padding)
    e = min(len(text), end + padding)
    out = text[s:e].replace("\n", " ").strip()
    if s > 0:
        out = "…" + out
    if e < len(text):
        out = out + "…"
    return out[:220]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _is_within_days(iso_date: Optional[str], days: int = 90) -> bool:
    if not iso_date:
        return False
    try:
        dt = datetime.fromisoformat(iso_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days <= days
    except (ValueError, TypeError):
        return False


def _role_headline(signal_type: str, full_name: str, company: str) -> str:
    label = {
        "person_security_leader": "Security leader",
        "person_grc_leader": "GRC / compliance leader",
        "person_technical_leader": "Technical leader",
        "person_commercial_leader": "Commercial leader",
    }.get(signal_type, "Function leader")
    if company:
        return f"{label} at {company}: {full_name}"
    return f"{label}: {full_name}"


def _role_detail(title: str, months_in_role: Optional[int]) -> str:
    parts = [f"{title}"] if title else []
    if months_in_role:
        parts.append(f"≈{months_in_role} months in role")
    else:
        parts.append("tenure unknown")
    return " · ".join(parts)
