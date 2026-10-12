"""
Solo daily digest — the rule-based "brain".

After the monitor pass, look at a Solo org's fresh buying-trigger signals on its
watched accounts, pick the 1-3 most valuable moves (cap-aware), and email the
owner. No AI: deterministic priority ranking + templated, one-line copy.
Quiet days send the short all-quiet email instead of nothing (2026-07-15:
every user gets the daily heartbeat; orgs watching nothing are
the only skip).

Gated on the ``solo_digest`` feature flag — in the SOLO_FEATURES preset since
2026-07-09 (was one test org only during the test period), so every Solo org
gets it; per-org opt-out via icp_config.solo_digest.opted_out.
Runs once/day off the back of /watchlist/monitor-solo. Marketed as autonomous;
under the hood it's pure rules, so it costs nothing per run.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from html import escape as _esc
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from supabase import Client

from app.config import settings
from app.services.email_sender import _send_via_resend
from app.services.org_features import feature_enabled
from app.services.plan_limits import current_usage, effective_limits

logger = logging.getLogger(__name__)

_DASHBOARD = f"{settings.DASHBOARD_URL}/dashboard"
_ACCOUNTS = f"{settings.DASHBOARD_URL}/dashboard/accounts"
_RESEARCH = f"{settings.DASHBOARD_URL}/dashboard/research"
_MAX_ITEMS = 3
_MAX_WAITING = 4                # locked contacts surfaced per email
_CONTACT_COOLDOWN_DAYS = 14     # don't re-surface the same person within this
_MAX_SENDS_PER_WEEK = 4         # rolling-7-day ceiling (2026-07-30)
_GENERIC_LOCALPARTS = {"hello", "hi", "info", "contact", "team", "admin", "sales", "support"}

# signal_type -> (rank, one-line template). Lower rank = more valuable. One
# sentence, no fluff (2026-06-29). {name} = the account name.
_DIGEST: Dict[str, Tuple[int, str]] = {
    "credential_leak":                (1, "{name} just had credentials leaked."),
    "credential_leak_supply_chain":   (1, "{name} just had credentials leaked."),
    "social_breach_mention":          (2, "a breach hit {name}'s sector."),
    "breach_proximity":               (2, "a breach hit {name}'s sector."),
    "breach_mention":                 (2, "a breach hit {name}'s sector."),
    "cve_in_stack":                   (3, "spotted a CVE in {name}'s stack."),
    "recently_funded":                (4, "{name} just raised a round."),
    "social_funding_news":            (4, "{name} just raised a round."),
    "recent_acquisition":             (5, "there's M&A movement at {name}."),
    "security_leadership_hire":       (6, "{name} brought in a new security lead."),
    "social_leadership_change":       (7, "leadership changed at {name}."),
    "new_leadership":                 (7, "leadership changed at {name}."),
    "security_team_expansion":        (8, "{name} is hiring around security tooling."),
    "hiring_security_leadership":     (6, "{name} is recruiting a security leader."),
    "hiring_security":                (8, "{name} is hiring around security tooling."),
    "hiring_grc":                     (8, "{name} is hiring for GRC/compliance."),
    "person_engaged_with_competitor": (9, "{who} at {name} engaged with a competitor."),
    "social_vendor_relationship":     (9, "{who} at {name} talked publicly about their security vendors."),
    "person_new_in_role":             (10, "{who} at {name} just moved into a new role."),
    "person_posted_relevant_topic":   (11, "{who} at {name} posted something relevant on LinkedIn."),
    "person_engaged_with_topic":      (11, "{who} at {name} posted something relevant on LinkedIn."),
}


def _who(sd: dict) -> str:
    """The person behind a person-driven signal, e.g. "Jane Doe (CISO)".
    person_monitor writes person_name (+ current_title) on every person
    signal; the reps' morning question is WHO to contact, so surface the
    name whenever we have it. Falls back to "someone" — never guess."""
    name = str(sd.get("person_name") or "").strip()
    if not name or "@" in name or len(name) > 60:
        return "someone"
    title = str(sd.get("current_title") or sd.get("person_title") or "").strip()
    if title and len(title) <= 60:
        return f"{name} ({title})"
    return name


# ---------------------------------------------------------------------------
# Recipient + name
# ---------------------------------------------------------------------------
def _name_from_email(email: Optional[str]) -> Optional[str]:
    """Greeting name from the email local-part; dropped for generic mailboxes
    (hello@, info@) so we never write 'Hey Hello'."""
    if not email or "@" not in email:
        return None
    token = email.split("@")[0].lower().replace("_", ".").replace("-", ".").split(".")[0]
    if not token or token in _GENERIC_LOCALPARTS:
        return None
    return token[:1].upper() + token[1:]


def _owner(db: Client, org_id: str) -> Tuple[Optional[str], Optional[str]]:
    """(email, first_name) for the org owner. Name from auth metadata
    (first_name, then full_name's first token) — the same source the dashboard
    greeting uses — falling back to a non-generic email prefix."""
    try:
        rows = (
            db.table("org_users").select("user_id")
            .eq("org_id", org_id).eq("role", "owner").limit(1).execute().data or []
        )
        if not rows:
            # Pre-Solo orgs predate the owner role — their first
            # admin is the de facto owner. Oldest row wins.
            rows = (
                db.table("org_users").select("user_id")
                .eq("org_id", org_id).eq("role", "admin")
                .order("created_at", desc=False).limit(1).execute().data or []
            )
        if not rows:
            return None, None
        user = getattr(db.auth.admin.get_user_by_id(rows[0]["user_id"]), "user", None)
        if user is None:
            return None, None
        email = getattr(user, "email", None)
        meta = getattr(user, "user_metadata", None) or {}
        name = (meta.get("first_name") or "").strip()
        if not name:
            full = (meta.get("full_name") or "").strip()
            name = full.split()[0] if full else ""
        return email, (name or _name_from_email(email))
    except Exception as e:  # noqa: BLE001
        logger.warning("solo_digest: owner lookup failed for %s: %s", org_id, e)
        return None, None


# ---------------------------------------------------------------------------
# Resolve fresh signals to watched accounts
# ---------------------------------------------------------------------------
def _watched(db: Client, org_id: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    rows = (
        db.table("watchlist").select("company_name, company_domain")
        .eq("org_id", org_id).eq("status", "active").eq("monitoring_active", True)
        .execute().data or []
    )
    by_domain: Dict[str, str] = {}
    by_name: Dict[str, str] = {}
    for r in rows:
        dom = (r.get("company_domain") or "").strip().lower()
        nm = (r.get("company_name") or dom or "").strip()
        if not nm:
            continue
        if dom:
            by_domain[dom] = nm
        by_name[nm.lower()] = nm
    return by_domain, by_name


def _domain_maps(db: Client, org_id: str, signals: List[dict]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """company_id -> domain and lead_id -> domain for the signals in hand."""
    cids = list({s["company_id"] for s in signals if s.get("company_id")})
    lids = list({s["lead_id"] for s in signals if s.get("lead_id")})
    cid_dom: Dict[str, str] = {}
    lid_dom: Dict[str, str] = {}
    if cids:
        co = db.table("companies").select("id, domain").in_("id", cids).execute().data or []
        for c in co:
            if c.get("id") and c.get("domain"):
                cid_dom[c["id"]] = c["domain"].lower()
    if lids:
        ld = db.table("leads").select("id, company_domain").in_("id", lids).execute().data or []
        for l in ld:
            if l.get("id") and l.get("company_domain"):
                lid_dom[l["id"]] = l["company_domain"].lower()
    return cid_dom, lid_dom


def _fresh_moves(db: Client, org_id: str, since_iso: str) -> List[dict]:
    by_domain, by_name = _watched(db, org_id)
    if not by_domain and not by_name:
        return []
    sigs = (
        db.table("signals")
        .select("signal_type, signal_data, company_id, lead_id, detected_at")
        .eq("org_id", org_id)
        .in_("signal_type", list(_DIGEST.keys()))
        .gte("detected_at", since_iso)
        .order("detected_at", desc=True)
        .limit(400)
        .execute().data or []
    )
    if not sigs:
        return []
    from app.services.signal_visibility import fetch_hidden_signal_types
    hidden = fetch_hidden_signal_types(db, org_id)
    sigs = [s for s in sigs if s.get("signal_type") not in hidden]
    if not sigs:
        return []
    cid_dom, lid_dom = _domain_maps(db, org_id, sigs)

    best: Dict[str, dict] = {}  # account key -> strongest move
    for s in sigs:
        rank_tpl = _DIGEST.get(s.get("signal_type"))
        if not rank_tpl:
            continue
        rank, template = rank_tpl
        sd = s.get("signal_data") or {}
        cid, lid = s.get("company_id"), s.get("lead_id")
        cand_dom = (
            (cid_dom.get(cid) if cid else None)
            or (lid_dom.get(lid) if lid else None)
            or (str(sd.get("company_domain") or sd.get("domain") or "").lower() or None)
        )
        name, domain = None, None
        if cand_dom and cand_dom in by_domain:
            name, domain = by_domain[cand_dom], cand_dom
        else:
            cand_name = str(sd.get("company_name") or sd.get("name") or "").lower()
            if cand_name and cand_name in by_name:
                name = by_name[cand_name]
        if not name:
            continue  # can't tie it to a watched account — skip, never guess
        key = domain or name.lower()
        at = s.get("detected_at") or ""
        prev = best.get(key)
        if prev is None or rank < prev["rank"] or (rank == prev["rank"] and at > prev["at"]):
            best[key] = {"rank": rank, "name": name, "domain": domain,
                         "line": template.format(name=name, who=_who(sd)), "at": at}

    moves = sorted(best.values(), key=lambda m: m["at"], reverse=True)  # recency first
    moves = sorted(moves, key=lambda m: m["rank"])  # then by value (stable)
    return _diversify(moves)


def _diversify(moves: List[dict]) -> List[dict]:
    """Pick the top moves with at most 2 per signal family (rank = the
    template family), so one signal type never fills the whole email —
    self-serve clients weight signals differently (2026-07-15). Only
    binds when variety exists: if the week is all credential leaks, a
    third leak still beats sending two lines.
    """
    picked: List[dict] = []
    per_rank: Dict[int, int] = {}
    for m in moves:
        if len(picked) >= _MAX_ITEMS:
            break
        if per_rank.get(m["rank"], 0) >= 2:
            continue
        picked.append(m)
        per_rank[m["rank"]] = per_rank.get(m["rank"], 0) + 1
    if len(picked) < _MAX_ITEMS:  # variety ran out — backfill in value order
        chosen = {id(m) for m in picked}
        for m in moves:
            if len(picked) >= _MAX_ITEMS:
                break
            if id(m) not in chosen:
                picked.append(m)
        picked.sort(key=lambda m: m["rank"])
    return picked


# ---------------------------------------------------------------------------
# Cap line + render
# ---------------------------------------------------------------------------
def _cap_line(db: Client, org_id: str) -> Optional[str]:
    if settings.SELF_HOSTED:
        return None  # no plan caps to report
    try:
        usage = current_usage(org_id, db)
        limits = effective_limits(org_id, db)
        researched = usage.get("accounts", 0)
        acc_cap = limits.get("accounts", 0)
        unlocks_left = max(0, limits.get("unlocks_per_month", 0) - usage.get("unlocks_this_month", 0))
        return f"{researched} of {acc_cap} researched this month, {unlocks_left} unlocks left."
    except Exception as e:  # noqa: BLE001
        logger.warning("solo_digest: cap line failed for %s: %s", org_id, e)
        return None


def _render_quiet(first_name: Optional[str], watched_count: int, cap_line: Optional[str]) -> Tuple[str, str, str]:
    """The quiet-day email — short proof that Vera checked and nothing
    needs them. 2026-07-15: every user gets a daily email; the daily
    heartbeat IS the value, not just the news days."""
    greet = f"Hey {first_name}," if first_name else "Hey there,"
    plural = "account" if watched_count == 1 else "accounts"
    subject = "All quiet across your accounts today"
    intro = (
        f"{greet} I checked your {watched_count} watched {plural} today. "
        "Nothing worth your time moved."
    )

    text = intro
    if cap_line:
        text += "\n\n" + cap_line
    text += (
        f"\n\nSee your dashboard: {_DASHBOARD}"
        f"\nResearch a new account: {_RESEARCH}"
        f"\n\nI'll keep watching.\nVera"
    )

    btn = (
        f'<a href="{_DASHBOARD}" style="display:inline-block;background:#7c3aed;color:#ffffff;'
        'text-decoration:none;padding:10px 18px;border-radius:8px;font-weight:600;margin:0 8px 8px 0">'
        'See your dashboard</a>'
        f'<a href="{_RESEARCH}" style="display:inline-block;background:#ffffff;color:#7c3aed;'
        'text-decoration:none;padding:10px 18px;border-radius:8px;font-weight:600;border:1px solid #7c3aed;'
        'margin:0 8px 8px 0">Research a new account</a>'
    )
    html = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.55;color:#1a1a1a">'
        f'<p style="margin:0 0 14px">{_esc(intro)}</p>'
        + (f'<p style="margin:14px 0 16px;color:#777">{cap_line}</p>' if cap_line else '<div style="height:8px"></div>')
        + f'<p style="margin:0 0 16px">{btn}</p>'
        + '<p style="margin:8px 0 0">I\'ll keep watching.</p>'
        + '<p style="margin:2px 0 0">Vera</p>'
        + "</div>"
    )
    return subject, text, html


def _render(
    first_name: Optional[str],
    moves: List[dict],
    cap_line: Optional[str],
    waiting: Optional[List[dict]] = None,
) -> Tuple[str, str, str]:
    greet = f"Hey {first_name}," if first_name else "Hey there,"
    intro = f"{greet} here's what's happened recently:"
    subject = moves[0]["line"].rstrip(".")
    subject = subject[:1].upper() + subject[1:]

    # HTML rows interpolate account names + domains that trace back to
    # user-entered / vendor data, so escape them (plaintext needs none).
    text_items, html_items = [], []
    for m in moves:
        url = f"{_ACCOUNTS}/{m['domain']}" if m.get("domain") else _ACCOUNTS
        text_items.append(f"• {m['line']}  {url}")
        html_items.append(
            f'<p style="margin:0 0 10px">• {_esc(m["line"])} '
            f'<a href="{_esc(url)}" style="color:#7c3aed;text-decoration:none">Open &rarr;</a></p>'
        )

    # Locked contacts ride along as a section under the moves — same email,
    # signals lead (perishable), contacts as the evergreen "Vera found these
    # people" nudge below.
    wtext = whtml = ""
    if waiting:
        wtext, whtml = _waiting_fragment(waiting)

    text = intro + "\n\n" + "\n".join(text_items)
    if wtext:
        text += "\n\n" + wtext
    if cap_line:
        text += "\n\n" + cap_line
    text += (
        f"\n\nSee your dashboard: {_DASHBOARD}"
        f"\nResearch a new account: {_RESEARCH}"
        f"\n\nI'll keep watching.\nVera"
    )

    btn = (
        f'<a href="{_DASHBOARD}" style="display:inline-block;background:#7c3aed;color:#ffffff;'
        'text-decoration:none;padding:10px 18px;border-radius:8px;font-weight:600;margin:0 8px 8px 0">'
        'See your dashboard</a>'
        f'<a href="{_RESEARCH}" style="display:inline-block;background:#ffffff;color:#7c3aed;'
        'text-decoration:none;padding:10px 18px;border-radius:8px;font-weight:600;border:1px solid #7c3aed;'
        'margin:0 8px 8px 0">Research a new account</a>'
    )
    intro_html = _esc(intro)
    html = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.55;color:#1a1a1a">'
        f'<p style="margin:0 0 14px">{intro_html}</p>'
        + "".join(html_items)
        + whtml
        + (f'<p style="margin:14px 0 16px;color:#777">{cap_line}</p>' if cap_line else '<div style="height:8px"></div>')
        + f'<p style="margin:0 0 16px">{btn}</p>'
        + '<p style="margin:8px 0 0">I\'ll keep watching.</p>'
        + '<p style="margin:2px 0 0">Vera</p>'
        + "</div>"
    )
    return subject, text, html


# ---------------------------------------------------------------------------
# Locked contacts — the "Vera found these people, you haven't unlocked them"
# retention nudge. We already hold key_contacts; this makes that data valuable.
# ---------------------------------------------------------------------------
def _waiting_contacts(db: Client, org_id: str, *, exclude_keys: set) -> List[dict]:
    """Locked (not-yet-unlocked) key contacts at watched accounts, ranked by
    ICP relevance score (the same score the relevance service now writes onto
    key_contacts). exclude_keys carries recently-surfaced people (cooldown) so
    the same faces never repeat. Caps total to _MAX_WAITING, max 2 per company
    so one account can't fill the block."""
    by_domain, _ = _watched(db, org_id)
    if not by_domain:
        return []
    try:
        rows = (
            db.table("companies").select("domain, name, key_contacts")
            .in_("domain", list(by_domain.keys())).execute().data or []
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("solo_digest: waiting-contacts query failed for %s: %s", org_id, e)
        return []

    cands: List[dict] = []
    for c in rows:
        dom = (c.get("domain") or "").lower()
        kcs = c.get("key_contacts")
        if not isinstance(kcs, list):
            continue
        company = by_domain.get(dom) or (c.get("name") or dom)
        for p in kcs:
            if not isinstance(p, dict):
                continue
            if p.get("unlocked") is True or p.get("lead_id"):
                continue  # already unlocked — nothing to nudge
            name = str(p.get("name") or "").strip()
            if not name:
                continue
            key = str(p.get("apollo_id") or f"{dom}:{name.lower()}")
            if key in exclude_keys:
                continue
            cands.append({
                "key": key, "name": name, "domain": dom, "company": company,
                "title": str(p.get("title") or "").strip(),
                "reason": str(p.get("relevance_reason") or "").strip(),
                "score": float(p.get("icp_score") or 0),
            })

    cands.sort(key=lambda x: x["score"], reverse=True)
    picked: List[dict] = []
    per_company: Dict[str, int] = {}
    for x in cands:
        if len(picked) >= _MAX_WAITING:
            break
        if per_company.get(x["domain"], 0) >= 2:
            continue
        picked.append(x)
        per_company[x["domain"]] = per_company.get(x["domain"], 0) + 1
    return picked


def _waiting_fragment(waiting: List[dict]) -> Tuple[str, str]:
    """(text, html) for the 'people waiting' block. Reused as a section inside
    the signal digest and as the body of the contacts-only email."""
    heading = "People waiting at your accounts"
    text_lines = [heading]
    html_parts = [f'<p style="margin:18px 0 8px;font-weight:700;color:#1a1a1a">{heading}</p>']
    for p in waiting:
        url = f"{_ACCOUNTS}/{p['domain']}" if p.get("domain") else _ACCOUNTS
        role = f"{p['title']} at {p['company']}" if p.get("title") else f"at {p['company']}"
        text_lines.append(f"• {p['name']}, {role}")
        if p.get("reason"):
            text_lines.append(f"  {p['reason']}")
        text_lines.append(f"  Unlock: {url}")
        reason_html = (
            f'<br><span style="color:#777">{_esc(p["reason"])}</span>' if p.get("reason") else ""
        )
        html_parts.append(
            f'<p style="margin:0 0 10px">• <strong>{_esc(p["name"])}</strong>, '
            f'{_esc(role)}{reason_html} '
            f'<a href="{_esc(url)}" style="color:#7c3aed;text-decoration:none">Unlock &rarr;</a></p>'
        )
    return "\n".join(text_lines), "".join(html_parts)


def _cta_html() -> str:
    return (
        f'<a href="{_DASHBOARD}" style="display:inline-block;background:#7c3aed;color:#ffffff;'
        'text-decoration:none;padding:10px 18px;border-radius:8px;font-weight:600;margin:0 8px 8px 0">'
        'See your dashboard</a>'
        f'<a href="{_RESEARCH}" style="display:inline-block;background:#ffffff;color:#7c3aed;'
        'text-decoration:none;padding:10px 18px;border-radius:8px;font-weight:600;border:1px solid #7c3aed;'
        'margin:0 8px 8px 0">Research a new account</a>'
    )


def _render_waiting(
    first_name: Optional[str], waiting: List[dict], cap_line: Optional[str]
) -> Tuple[str, str, str]:
    """The contacts-only email — sent on days nothing moved but people are
    waiting. Turns a quiet day into a retention touch: Vera did the research,
    the rep just clicks unlock."""
    greet = f"Hey {first_name}," if first_name else "Hey there,"
    n = len(waiting)
    people = "person" if n == 1 else "people"
    if n == 1:
        subject = f"{waiting[0]['name']} at {waiting[0]['company']} is worth a look"
    else:
        subject = f"{n} people worth reaching at your accounts"
    intro = (
        f"{greet} I found {n} {people} at accounts you're watching who you "
        "haven't unlocked yet."
    )
    wtext, whtml = _waiting_fragment(waiting)

    text = intro + "\n\n" + wtext
    if cap_line:
        text += "\n\n" + cap_line
    text += (
        f"\n\nSee your dashboard: {_DASHBOARD}"
        f"\nResearch a new account: {_RESEARCH}"
        f"\n\nI'll keep watching.\nVera"
    )
    html = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.55;color:#1a1a1a">'
        f'<p style="margin:0 0 14px">{_esc(intro)}</p>'
        + whtml
        + (f'<p style="margin:14px 0 16px;color:#777">{cap_line}</p>' if cap_line else '<div style="height:8px"></div>')
        + f'<p style="margin:0 0 16px">{_cta_html()}</p>'
        + '<p style="margin:8px 0 0">I\'ll keep watching.</p>'
        + '<p style="margin:2px 0 0">Vera</p>'
        + "</div>"
    )
    return subject, text, html


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def build_and_send(db: Client, org_id: str, *, since_days: Optional[int] = None, force: bool = False) -> Dict[str, Any]:
    """Build the digest for one org and email it. Gated, deduped, quiet-safe.

    force/since_days are for the test trigger: ignore the flag + dedupe and look
    back `since_days` so a populated email can be previewed.
    """
    org = (
        db.table("organisations").select("icp_config").eq("id", org_id).maybe_single().execute().data or {}
    )
    icp = org.get("icp_config") or {}
    if not force and not feature_enabled(icp, "solo_digest"):
        return {"status": "skipped", "reason": "flag_off"}
    state = icp.get("solo_digest") or {}
    if state.get("opted_out"):
        return {"status": "skipped", "reason": "opted_out"}

    now = datetime.now(timezone.utc)
    if since_days is not None:
        since_iso = (now - timedelta(days=since_days)).isoformat()
    else:
        floor = (now - timedelta(hours=26)).isoformat()
        last = state.get("last_sent_at")
        since_iso = max(last, floor) if last else floor

    moves = _fresh_moves(db, org_id, since_iso)

    # Smart-group changes (notify-enabled account groups) ride along as extra
    # move lines — "who entered which segment" is the ABM campaign trigger
    # (2026-07-16 sim). A quiet-on-signals day with a group change still sends.
    try:
        from app.services.group_notify import evaluate_org_group_changes
        for ch in evaluate_org_group_changes(db, org_id)[:2]:
            names = ch["entered"]
            shown = ", ".join(names[:3]) + (
                f" and {len(names) - 3} more" if len(names) > 3 else ""
            )
            moves.append({
                "rank": 98, "name": shown, "domain": None, "at": "",
                "line": f"{shown} entered your '{ch['group']}' group.",
            })
    except Exception as e:
        logger.warning("digest group lines failed for %s: %s", org_id, e)

    # Locked contacts Vera already found — the retention nudge. Cooldown so the
    # same person never repeats within _CONTACT_COOLDOWN_DAYS.
    notified = {
        k: v for k, v in (state.get("notified_contacts") or {}).items()
        if v >= (now - timedelta(days=_CONTACT_COOLDOWN_DAYS)).isoformat()
    }
    waiting = _waiting_contacts(db, org_id, exclude_keys=set(notified.keys()))

    # Decider: signals lead (perishable), contacts fill quiet days (evergreen).
    # Nothing on either side → send nothing (2026-07-20: no all-quiet spam).
    if not moves and not waiting:
        watched_count = (
            db.table("watchlist").select("id", count="exact")
            .eq("org_id", org_id).eq("status", "active")
            .eq("monitoring_active", True).execute().count or 0
        )
        if watched_count == 0:
            return {"status": "quiet", "reason": "nothing_watched"}
        if not force:
            return {"status": "quiet", "reason": "nothing_to_report"}
        watched_ct = watched_count  # force preview falls through to all-quiet

    # Cadence cap (2026-07-30): at most _MAX_SENDS_PER_WEEK sends in any
    # rolling 7 days, and never twice in a day. Enforced in code so the daily
    # monitor cron needs no change. force (preview) bypasses both.
    recent_sends = [
        t for t in (state.get("recent_sends") or [])
        if t >= (now - timedelta(days=7)).isoformat()
    ]
    if not force:
        if str(state.get("last_sent_at", ""))[:10] == now.date().isoformat():
            return {"status": "skipped", "reason": "already_sent_today"}
        if len(recent_sends) >= _MAX_SENDS_PER_WEEK:
            return {"status": "skipped", "reason": "weekly_cap"}

    to_email, first_name = _owner(db, org_id)
    if not to_email:
        return {"status": "error", "reason": "no_owner_email"}

    cap_line = _cap_line(db, org_id)
    if moves:
        subject, text, html = _render(first_name, moves, cap_line, waiting=waiting)
        shown_contacts = waiting
        fingerprint = hashlib.sha1(
            "|".join(f"{m['domain'] or m['name']}:{m['rank']}" for m in moves).encode()
        ).hexdigest()[:16]
    elif waiting:
        subject, text, html = _render_waiting(first_name, waiting, cap_line)
        shown_contacts = waiting
        fingerprint = "waiting:" + hashlib.sha1(
            "|".join(p["key"] for p in waiting).encode()
        ).hexdigest()[:12]
    else:
        # force preview with nothing to show — the all-quiet template.
        subject, text, html = _render_quiet(first_name, watched_ct, cap_line)
        shown_contacts = []
        fingerprint = f"quiet:{now.date().isoformat()}"
    # Per-org unsubscribe token (mirrors the free-report unsub_token pattern):
    # the link carries an unguessable token, NOT the raw org_id, so there is
    # no IDOR — the /api/unsubscribe handler matches this token to flip
    # icp_config.solo_digest.opted_out. Generated once, then stable.
    unsub_token = state.get("unsub_token") or str(uuid4())
    unsub = f"{settings.DASHBOARD_URL}/api/unsubscribe?t={unsub_token}&kind=solo_digest"
    # Visible unsubscribe in the body, alongside the List-Unsubscribe header
    # (the header drives the mail client's native button; some people still
    # look for a footer link).
    text += f"\n\nDon't want these emails? Unsubscribe: {unsub}"
    html += (
        '<p style="margin:20px 0 0;font-family:Arial,Helvetica,sans-serif;'
        'font-size:12px;color:#999">'
        f'Don\'t want these emails? <a href="{unsub}" style="color:#999;'
        'text-decoration:underline">Unsubscribe</a>.</p>'
    )
    headers = {
        "List-Unsubscribe": f"<{unsub}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    if not settings.RESEND_API_KEY or not settings.RESEND_FROM_EMAIL:
        # Email is optional for a self-hosted install: skip quietly.
        logger.info("solo_digest: email not configured (RESEND_*); not sending for %s", org_id)
        return {"status": "skipped", "reason": "email_not_configured", "subject": subject, "text": text}
    try:
        _send_via_resend(to_email=to_email, subject=subject, html=html, text=text, headers=headers)
    except Exception as e:  # noqa: BLE001
        logger.error("solo_digest send failed for %s: %s", org_id, e)
        return {"status": "error", "reason": f"send_failed: {e}", "subject": subject, "text": text}

    # Slack daily brief (2026-08-03): the same editorial decision on a
    # second surface — posts whenever the email sends, to the org's
    # connected channel. Best-effort: Slack trouble never breaks email.
    slack_posted = False
    try:
        from app.services.integrations.slack_brief import send_brief
        slack_posted = send_brief(
            db,
            org_id,
            first_name=first_name,
            moves=moves,
            waiting=shown_contacts,
            cap_line=cap_line,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("slack brief failed for %s: %s", org_id, e)

    # A forced preview must not disturb real cadence (last_sent_at, the weekly
    # window, or contact cooldowns) — return the render, write nothing.
    if not force:
        try:
            icp["solo_digest"] = {
                **state,
                "last_sent_at": now.isoformat(),
                "last_fingerprint": fingerprint,
                "unsub_token": unsub_token,
                "recent_sends": (recent_sends + [now.isoformat()])[-10:],
                "notified_contacts": {
                    **notified,
                    **{p["key"]: now.isoformat() for p in shown_contacts},
                },
            }
            db.table("organisations").update({"icp_config": icp}).eq("id", org_id).execute()
        except Exception as e:  # noqa: BLE001
            logger.warning("solo_digest: state write failed for %s: %s", org_id, e)

    return {"status": "sent", "to": to_email, "subject": subject, "text": text,
            "moves": len(moves), "waiting": len(shown_contacts),
            "slack_brief": slack_posted}


def run_for_org(db: Client, org_id: str) -> Dict[str, Any]:
    """Called after a caps org's monitor pass. Flag-gated. Sends when there are
    fresh signals or locked contacts waiting, capped to _MAX_SENDS_PER_WEEK in
    any rolling 7 days (2026-07-30); silent otherwise. Never raises (must
    not break the monitor run)."""
    try:
        return build_and_send(db, org_id)
    except Exception as e:  # noqa: BLE001
        logger.error("solo_digest run failed for %s: %s", org_id, e)
        return {"status": "error", "reason": str(e)}
