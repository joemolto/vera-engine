"""Per-account key-contact discovery.

For each watched account, find the top 1-3 people we want to monitor
for person-level signals (job changes, posts, comments on others'
posts). Uses Apollo `mixed_people_search` filtered by the org's
target_titles + the company's primary domain.

Cached on `companies.key_contacts` (jsonb) so the monitor pass doesn't
re-discover every cycle. Refresh cadence: 30 days (turnover is rare
and re-discovery costs Apollo credits we'd rather not spend repeatedly).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from supabase import Client

from app.config import settings
from app.services.relevance import build_relevance_context, judge_titles

logger = logging.getLogger(__name__)

_APOLLO_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
_TIMEOUT = 30
_REFRESH_AFTER_DAYS = 30


def discover_key_contacts(
    db: Client,
    *,
    company_id: str,
    org_id: str,
    max_contacts: int = 3,
    force_refresh: bool = False,
) -> List[Dict[str, Any]]:
    """Find and cache the top N people at a company matching the org's
    target_titles. Returns the cached list — never an exception (cache
    miss returns []).

    Behaviour:
      - If `companies.key_contacts` is fresh (< 30 days), return it.
      - Otherwise, Apollo search → score → store top N → return.
      - Apollo failure → return what's currently cached (could be empty);
        never blocks the monitor pass.
    """
    company = _fetch_company(db, company_id)
    if not company:
        logger.warning("discover_key_contacts: company %s not found", company_id)
        return []

    cached = company.get("key_contacts") or []
    refreshed_at = company.get("key_contacts_refreshed_at")
    if not force_refresh and _is_fresh(refreshed_at):
        return cached if isinstance(cached, list) else []

    domain = (company.get("domain") or "").strip().lower()
    if not domain:
        return cached if isinstance(cached, list) else []

    titles = _get_target_titles(db, org_id)
    if not titles:
        logger.info(
            "discover_key_contacts org=%s has no target_titles configured; skipping",
            org_id,
        )
        return cached if isinstance(cached, list) else []

    # Cast a wide canonical security net alongside the rep's own
    # target_titles, so we find the security buyer even when the rep's
    # selections are narrow or phrased differently (e.g. they picked
    # "Head of Security" but the account has a "Head of Security
    # Operations"). Rep titles first so their exact ICP ranks highest.
    # Graduated cascade — surface the most security-relevant person we can,
    # and only widen when a tier is genuinely empty (spec 2026-06-17):
    #   1. SECURITY — rep's target_titles ∪ the canonical security net.
    #      Finds the security buyer regardless of exact phrasing, and the
    #      rep's own picks (even a CFO, if that's their ICP) rank top.
    #   2. TECH — CTO / CIO / Head of IT etc. who own security when there's
    #      no dedicated security hire. A tech leader beats a CFO for a
    #      cyber vendor, so this comes BEFORE general leadership.
    #   3. GENERAL — CEO / CFO / Founder, last resort, so the rep at least
    #      knows who runs the company.
    # Apollo data is factual at every tier (people genuinely work there).
    search_titles = list(dict.fromkeys([*titles, *_SECURITY_TITLES]))
    contacts = _apollo_search(domain=domain, titles=search_titles)
    tier = "security"

    if not contacts:
        logger.info("No security contact for %s — trying tech leadership", domain)
        contacts = _apollo_search(domain=domain, titles=_TECH_LEADERSHIP_TITLES)
        if contacts:
            tier = "tech"

    if not contacts:
        logger.info("No tech contact for %s — trying general leadership", domain)
        contacts = _apollo_search(domain=domain, titles=_GENERAL_LEADERSHIP_TITLES)
        if contacts:
            tier = "general"

    if not contacts:
        # TIER 4 — Vera's own web research (2026-08-24: Apollo can't be
        # trusted alone). CISOs were found on companies' own leadership
        # pages while Apollo's record was empty. Tavily over public pages + one strict Claude extraction —
        # named person with a security/tech leadership title and a source
        # URL, or nothing. Entries carry no apollo_id, so the dossier shows
        # them without an unlock button (facts, not reachability claims).
        researched = _web_research_leader(
            domain=domain, company_name=(company.get("name") or ""),
        )
        if researched:
            ranked = _merge_signal_leaders(
                db, company_id=company_id, domain=domain, contacts=researched
            )
            _store(db, company_id=company_id, contacts=ranked)
            return ranked
        # Truly empty — keep cached. Future: manual add via dossier.
        return cached if isinstance(cached, list) else []

    # Score against the list we actually searched in this tier. Only the
    # security tier requires a title match (it drops fuzzy Apollo noise);
    # the fallback tiers keep whoever we found, since we searched for those
    # titles deliberately. We never require a LinkedIn URL — the dossier
    # builds a Google fallback link and Phase D skips URL-less people, so
    # surfacing the right person beats dropping them.
    if tier == "security":
        scoring_titles = search_titles
    elif tier == "tech":
        scoring_titles = _TECH_LEADERSHIP_TITLES
    else:
        scoring_titles = _GENERAL_LEADERSHIP_TITLES
    is_fallback = tier != "security"
    ranked = _rank_and_truncate(
        contacts,
        target_titles=scoring_titles,
        max_contacts=max_contacts,
        require_linkedin=False,
        require_title_match=not is_fallback,
    )
    # Security tier: same LLM relevance judge the drawer uses — archetype-aware
    # ranking + a "why they fit" reason, so the dossier's people agree with the
    # drawer instead of running their own string match. Fallback tiers stay as
    # deterministic (they're deliberate CTO/CEO searches, framed as such).
    if not is_fallback:
        ranked = _apply_relevance(db, org_id, ranked)
    # Mark fallback rows so the UI frames them honestly ("no security leader
    # found — showing the CTO / senior leadership") rather than as an ICP
    # match. leadership_tier carries which tier we landed in for the copy.
    if is_fallback:
        for c in ranked:
            c["is_general_leadership"] = True
            c["leadership_tier"] = tier
    # Merge security leaders Vera's OWN research has named (public-web
    # derived person_security_leader signals). Apollo is the source of
    # REACHABLE contacts; this fills the who-actually-runs-security gap
    # when Apollo simply doesn't index the person (Monzo's CISO,
    # 2026-08-04). Signals-only by design — never a fresh web guess
    # (that path was removed 2026-06-10 after the Lemonade/UPS false
    # positive). Entries carry no apollo_id, so the dossier shows them
    # without an unlock button and links the name to a LinkedIn search.
    ranked = _merge_signal_leaders(
        db, company_id=company_id, domain=domain, contacts=ranked
    )
    _store(db, company_id=company_id, contacts=ranked)
    return ranked


_LEADER_SIGNAL_MAX_AGE_DAYS = 365


def _signal_leaders(db: Client, *, company_id: str, domain: str) -> List[Dict[str, Any]]:
    """person_security_leader signals for this company, both axes
    (company_id + leads at the domain), newest first, deduped by name.
    Content is public-research derived, so merging into the shared
    companies cache is consistent with the global-cache model."""
    from datetime import datetime, timedelta, timezone

    since = (
        datetime.now(timezone.utc) - timedelta(days=_LEADER_SIGNAL_MAX_AGE_DAYS)
    ).isoformat()
    rows: List[Dict[str, Any]] = []
    try:
        rows += (
            db.table("signals")
            .select("signal_data, detected_at")
            .eq("signal_type", "person_security_leader")
            .eq("company_id", company_id)
            .gte("detected_at", since)
            .order("detected_at", desc=True)
            .limit(10)
            .execute()
            .data
            or []
        )
        lead_rows = (
            db.table("leads")
            .select("id")
            .eq("company_domain", domain)
            .limit(25)
            .execute()
            .data
            or []
        )
        lead_ids = [r["id"] for r in lead_rows]
        if lead_ids:
            rows += (
                db.table("signals")
                .select("signal_data, detected_at")
                .eq("signal_type", "person_security_leader")
                .in_("lead_id", lead_ids)
                .gte("detected_at", since)
                .order("detected_at", desc=True)
                .limit(10)
                .execute()
                .data
                or []
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("signal-leader lookup failed for %s: %s", domain, e)
        return []

    rows.sort(key=lambda r: r.get("detected_at") or "", reverse=True)
    leaders: List[Dict[str, Any]] = []
    seen: set = set()
    for r in rows:
        d = r.get("signal_data") or {}
        name = str(d.get("person_name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        leaders.append(
            {
                "name": name,
                "title": str(d.get("primary_title") or "Security leader").strip(),
                "detected_at": r.get("detected_at"),
            }
        )
    return leaders


def _name_key(name: str) -> tuple:
    """(first name, surname initial) — matches 'Jane Smith' to the
    obfuscated 'Jane S.' Apollo rows use, without collapsing different
    people who merely share a surname."""
    parts = [p for p in (name or "").lower().replace(".", "").split() if p]
    if not parts:
        return ("", "")
    return (parts[0], parts[-1][:1] if len(parts) > 1 else "")


def _merge_signal_leaders(
    db: Client,
    *,
    company_id: str,
    domain: str,
    contacts: List[Dict[str, Any]],
    max_extra: int = 2,
) -> List[Dict[str, Any]]:
    leaders = _signal_leaders(db, company_id=company_id, domain=domain)
    if not leaders:
        return contacts
    existing = {_name_key(str(c.get("name") or "")) for c in contacts}
    out = list(contacts)
    added = 0
    for leader in leaders:
        if _name_key(leader["name"]) in existing:
            continue
        entry = {
            "name": leader["name"],
            "title": leader["title"],
            "apollo_id": None,
            "linkedin_url": "",
            "icp_score": None,
            "source": "vera_research",
            "found_via": "public sources",
            "discovered_at": leader.get("detected_at"),
        }
        title_l = leader["title"].lower()
        # A named CISO/CSO outranks Apollo's manager-level rows.
        if "chief" in title_l or "ciso" in title_l or title_l.startswith("cso"):
            out.insert(0, entry)
        else:
            out.append(entry)
        existing.add(_name_key(leader["name"]))
        added += 1
        if added >= max_extra:
            break
    return out


# Canonical security-leadership titles. We ALWAYS search these alongside
# the org's own target_titles, so we find the actual security buyer at an
# account (Head of Security Operations, VP Security, etc.) even when the
# rep picked only a couple of titles or phrased them differently. This is
# the buying committee for a cyber vendor — discovery shouldn't depend on
# the rep typing the exact title. Leadership/management level only (not
# ICs like SOC Analyst) — we want the decision-maker.
_SECURITY_TITLES: List[str] = [
    "CISO", "Chief Information Security Officer",
    "CSO", "Chief Security Officer",
    "VP Security", "VP of Security", "VP Information Security",
    "VP Cyber Security",
    "Head of Security", "Head of Information Security",
    "Head of Cyber Security", "Head of Cyber", "Head of InfoSec",
    "Head of Security Operations", "Head of SOC",
    "Director of Security", "Director of Information Security",
    "Director of Cyber Security",
    "Security Operations Manager", "SOC Manager",
    "Security Manager", "Information Security Manager",
    "Cyber Security Manager", "Head of IT Security",
    "Head of GRC", "Head of Risk and Compliance",
    "Data Protection Officer", "DPO",
    "Security Architect", "Head of Security Engineering",
]


# TIER 2 fallback — tech leadership. When an account has NO security
# person, the CTO / CIO / Head of IT is who typically owns security, so
# we surface them BEFORE general leadership (a CTO is a far better contact
# for a cyber vendor than a CFO). Spec 2026-06-17.
_TECH_LEADERSHIP_TITLES: List[str] = [
    "CTO", "Chief Technology Officer",
    "CIO", "Chief Information Officer",
    "VP Engineering", "VP of Engineering", "Head of Engineering",
    "IT Director", "Director of IT", "Head of IT",
    "Head of Technology", "Head of Infrastructure",
    "VP Technology", "VP IT",
]


# TIER 3 fallback — general leadership. Last resort when there's neither a
# security nor a tech leader: surface whoever runs the company so the rep
# at least knows who's there. Factual (Apollo confirms they work there).
_GENERAL_LEADERSHIP_TITLES: List[str] = [
    # Founder titles first — tiny companies often have no C-suite split;
    # the Founder is the decision-maker for everything, security included.
    "Founder", "Co-founder", "Co-Founder",
    "Founder & CEO", "CEO & Founder",
    "CEO", "Chief Executive Officer",
    "President",
    "COO", "Chief Operating Officer",
    "CFO", "Chief Financial Officer",
    "Chief Risk Officer", "Chief Compliance Officer",
    "Chief Legal Officer", "General Counsel",
    "CPO", "Chief Product Officer",
]


# ---------------------------------------------------------------------------
# Apollo search
# ---------------------------------------------------------------------------
def _apollo_search(*, domain: str, titles: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """One-page mixed_people_search against a specific domain. We don't
    paginate — the goal is the TOP people only, not exhaustive. No titles
    → unfiltered page of up to 100 (the unlock resolver's wide net)."""
    api_key = settings.APOLLO_API_KEY
    if not api_key:
        logger.warning("Apollo search skipped — APOLLO_API_KEY not set")
        return []

    body: Dict[str, Any] = {
        "q_organization_domains_list": [domain],
        "per_page": 25 if titles else 100,
        "page": 1,
    }
    if titles:
        body["person_titles"] = titles
    try:
        res = httpx.post(
            _APOLLO_URL,
            json=body,
            headers={"X-Api-Key": api_key},
            timeout=_TIMEOUT,
        )
        res.raise_for_status()
        payload = res.json()
    except Exception as e:
        logger.warning("Apollo people-search failed for domain=%s: %s", domain, e)
        return []

    people = payload.get("people") or payload.get("contacts") or []
    if not isinstance(people, list):
        return []
    return people


def _web_research_leader(*, domain: str, company_name: str) -> List[Dict[str, Any]]:
    """Find the security/tech leader from PUBLIC web pages when Apollo has
    nobody at the domain. One Tavily search + one Claude extraction under
    strict no-invention rules: only people a source explicitly names as
    currently holding a security or technology leadership role at THIS
    company, each with the source URL — anything uncertain returns empty
    (product rule: honest empty beats invented data). Best-effort; any
    failure returns []."""
    if not settings.TAVILY_API_KEY or not (company_name or domain):
        return []
    label = company_name or domain
    try:
        payload = {
            "query": (
                f'"{label}" CISO OR "chief information security officer" OR '
                f'"head of security" OR "head of information security" OR '
                f"CTO leadership"
            ),
            "search_depth": "advanced",
            "max_results": 6,
            "include_answer": True,
        }
        res = httpx.post(
            "https://api.tavily.com/search",
            json=payload,
            headers={"Authorization": f"Bearer {settings.TAVILY_API_KEY}"},
            timeout=30,
        )
        res.raise_for_status()
        data = res.json()
    except Exception as e:  # noqa: BLE001
        logger.info("web leader research search failed for %s: %s", domain, e)
        return []

    results = data.get("results") or []
    if not results and not data.get("answer"):
        return []
    sources = "\n\n".join(
        f"URL: {r.get('url')}\nTITLE: {r.get('title')}\nCONTENT: {str(r.get('content') or '')[:1200]}"
        for r in results[:6]
    )
    known_urls = [str(r.get("url") or "") for r in results]

    try:
        from app.services.claude import call_claude

        system = (
            "You extract security/technology leadership facts from provided "
            "web sources. Reply with ONLY a JSON object. NEVER invent or "
            "infer a person: only include someone a source EXPLICITLY names "
            "as CURRENTLY holding a security or technology leadership role "
            "at the target company. The target is identified by its DOMAIN: "
            "a similarly named company on a different domain is NOT a match "
            "(live miss 2026-08-24: barricadesystems.com offered for "
            "barricade.ai). Wrong company, past roles, or ambiguity = "
            "exclude. An empty list is the correct answer when unsure."
        )
        prompt = (
            f"TARGET COMPANY: {label} ({domain})\n\n"
            f"SOURCES:\n{sources}\n\n"
            f'Tavily summary: {str(data.get("answer") or "")[:600]}\n\n'
            "Return JSON: {\"people\": [{\"name\": full name, \"title\": their "
            "exact current title at the target company, \"source_url\": the "
            "URL above that states it}]} — at most 2 people, security roles "
            "(CISO/Head of Security) before technology roles (CTO/CIO)."
        )
        parsed = call_claude(prompt, system=system, model=settings.CLAUDE_MODEL)
    except Exception as e:  # noqa: BLE001
        logger.info("web leader research extraction failed for %s: %s", domain, e)
        return []
    if not isinstance(parsed, dict):
        return []

    out: List[Dict[str, Any]] = []
    for p in (parsed.get("people") or [])[:2]:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        title = str(p.get("title") or "").strip()
        source_url = str(p.get("source_url") or "").strip()
        # Guards: real-looking name, real title, and the source must be one
        # of the URLs we actually handed over — no invented citations.
        if len(name.split()) < 2 or not title:
            continue
        if source_url and not any(source_url.startswith(u[:40]) for u in known_urls if u):
            source_url = ""
        out.append({
            "name": name,
            "title": title,
            "linkedin_url": "",
            "apollo_id": None,
            "icp_score": 0.9,
            "source": "web_research",
            "source_url": source_url or None,
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        })
    if out:
        logger.info("web leader research found %d for %s", len(out), domain)
    return out


def _display_name(p: Dict[str, Any]) -> str:
    """Displayable name from an Apollo person row. Apollo returns one of:
      - first_name + last_name      (full plan / verified)
      - first_name + last_name_obfuscated ("Ru***")
      - just first_name
    Fall through in order so we always show as much as Apollo gives us —
    "Ronald T." reads better than just "Ronald"; the rep can still find
    them on LinkedIn from first name + last initial + company + title."""
    first = (p.get("first_name") or "").strip()
    last_full = (p.get("last_name") or "").strip()
    last_obfuscated = (p.get("last_name_obfuscated") or "").strip()
    if last_full:
        return f"{first} {last_full}".strip()
    if last_obfuscated:
        initial = last_obfuscated[0].upper()
        return f"{first} {initial}.".strip() if initial else first
    return (p.get("name") or first or "").strip()


def resolve_person_by_id(
    db: Client, *, org_id: str, domain: str, apollo_id: str
) -> Optional[Dict[str, Any]]:
    """Live Apollo lookup of ONE person at a domain by apollo id — the
    unlock path's safety net when key_contacts hasn't caught up and the
    caller sent no name hints (2026-08-24: agents don't
    reliably re-send name/title from find_people results, so unlocks
    409'd on ids find_people itself had just returned). Searches the
    title cascade first (cheap page), then an unfiltered page of 100.
    Free, non-reveal. Returns {name, title, linkedin_url} or None."""
    if not domain or not apollo_id:
        return None
    titles = _get_target_titles(db, org_id) or []
    search_titles = list(dict.fromkeys([*titles, *_SECURITY_TITLES]))
    for tier in (search_titles, None):
        try:
            people = _apollo_search(domain=domain, titles=tier)
        except Exception:  # noqa: BLE001
            people = []
        for p in people:
            if isinstance(p, dict) and str(p.get("id")) == str(apollo_id):
                return {
                    "name": _display_name(p),
                    "title": (p.get("title") or "").strip(),
                    "linkedin_url": (p.get("linkedin_url") or "").strip(),
                }
    return None


def _rank_and_truncate(
    people: List[Dict[str, Any]],
    *,
    target_titles: List[str],
    max_contacts: int,
    require_linkedin: bool = True,
    require_title_match: bool = True,
) -> List[Dict[str, Any]]:
    """Score each person by title-match strength + seniority indicator,
    drop empties, keep top N. Score is 0..1 — used downstream as the
    person's ICP fit signal for scoring + as the tie-breaker in top-1
    mode.

    require_linkedin: when True (default — for cyber-targeted matches
    where Phase D will scrape posts/activity), drops rows without a
    LinkedIn URL. When False (broader-leadership fallback, spec
    2026-06-10), surfaces the contact even without a URL — they're
    factually at the company per Apollo and useful for the rep to
    know about, even if we can't scrape their feed yet.
    """
    target_lower = [t.lower() for t in target_titles]
    scored: List[Dict[str, Any]] = []
    for p in people:
        if not isinstance(p, dict):
            continue
        linkedin_url = (p.get("linkedin_url") or "").strip()
        if require_linkedin and not linkedin_url:
            # No LinkedIn URL → can't scrape activity → drop in the
            # strict path. In the lenient path, surface anyway.
            continue
        name = _display_name(p)
        if not name:
            continue
        title = (p.get("title") or "").strip()
        score = _score_title(title, target_lower)
        if score <= 0:
            if require_title_match:
                # Cyber path: title must match a security/ICP title —
                # drops fuzzy Apollo non-matches (e.g. "Head of Social").
                continue
            # Broader-leadership path: keep with a neutral score so rank
            # order stays deterministic but the row isn't lost.
            score = 0.5
        scored.append(
            {
                "name": name,
                "title": title,
                "linkedin_url": linkedin_url,
                "apollo_id": p.get("id"),
                "headline": (p.get("headline") or "").strip() or None,
                "icp_score": score,
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    scored.sort(key=lambda c: c["icp_score"], reverse=True)
    return scored[:max_contacts]


def _apply_relevance(
    db: Client, org_id: str, contacts: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Re-rank + annotate the security-tier contacts with the LLM relevance
    service (the same judge the account drawer uses). Attaches
    `relevance_score` + `relevance_reason`, drops LLM-judged non-buyers, and
    sorts best-fit first. Never raises — any failure keeps the deterministic
    list untouched so discovery never breaks on a relevance hiccup."""
    if not contacts:
        return contacts
    try:
        ctx = build_relevance_context(db, org_id)
        verdicts = judge_titles(
            db,
            archetype=ctx["archetype"],
            icp_titles=ctx["icp_titles"],
            sells_what=ctx["sells_what"],
            titles=[c.get("title") or "" for c in contacts],
        )
    except Exception as e:
        logger.warning("key_contacts relevance judge failed for org=%s: %s", org_id, e)
        return contacts

    kept: List[Dict[str, Any]] = []
    for c in contacts:
        v = verdicts.get(c.get("title") or "") or {}
        if v.get("relevant") is False:
            continue
        score = v.get("score")
        if isinstance(score, (int, float)):
            c["icp_score"] = float(score)
        reason = v.get("reason")
        if isinstance(reason, str) and reason:
            c["relevance_reason"] = reason
        kept.append(c)

    # Guard: if the judge dropped everyone (misfire), keep the deterministic
    # security-title matches rather than surfacing an empty people list.
    if not kept:
        return contacts
    kept.sort(key=lambda c: c.get("icp_score", 0), reverse=True)
    return kept


def _score_title(title: str, target_titles_lower: List[str]) -> float:
    """0..1 match score for the contact's title vs the org's target_titles.
    Exact lower-case match = 1.0; substring match (either direction)
    = 0.7; no match = 0."""
    if not title:
        return 0.0
    t = title.lower()
    if t in target_titles_lower:
        return 1.0
    for target in target_titles_lower:
        if not target:
            continue
        if target in t or t in target:
            return 0.7
    return 0.0


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def _fetch_company(db: Client, company_id: str) -> Optional[Dict[str, Any]]:
    try:
        res = (
            db.table("companies")
            .select("id, domain, name, key_contacts, key_contacts_refreshed_at")
            .eq("id", company_id)
            .maybe_single()
            .execute()
        )
        return res.data
    except Exception as e:
        logger.warning("Fetch company %s failed: %s", company_id, e)
        return None


def _get_target_titles(db: Client, org_id: str) -> List[str]:
    """Read target_titles from icp_config. Prefer persona[0].target_titles,
    fall back to top-level icp_config.target_titles."""
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
        return []

    personas = cfg.get("personas")
    if isinstance(personas, list) and personas:
        first = personas[0]
        if isinstance(first, dict):
            titles = first.get("target_titles")
            if isinstance(titles, list) and titles:
                return [str(t) for t in titles if t]

    titles = cfg.get("target_titles")
    if isinstance(titles, list):
        return [str(t) for t in titles if t]
    return []


def _store(db: Client, *, company_id: str, contacts: List[Dict[str, Any]]) -> None:
    try:
        db.table("companies").update(
            {
                "key_contacts": contacts,
                "key_contacts_refreshed_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", company_id).execute()
    except Exception as e:
        logger.warning("Failed to store key_contacts for %s: %s", company_id, e)


def _is_fresh(refreshed_at: Optional[str]) -> bool:
    if not refreshed_at:
        return False
    try:
        dt = datetime.fromisoformat(refreshed_at.replace("Z", "+00:00"))
    except Exception:
        return False
    return dt > datetime.now(timezone.utc) - timedelta(days=_REFRESH_AFTER_DAYS)


