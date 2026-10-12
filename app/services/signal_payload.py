"""Signal payload contract.

Every signal written to the `signals` table must carry a consistent,
human-legible shape so the dashboard, the recent-digest agent, and the
GTM briefing can all render it without guessing:

    {
      "headline":    "Aviva is hiring a CISO",          # one human line, names the entity
      "detail":      "Open req for Chief Information ...",# 1-2 sentence context
      "entity":      "Aviva",                            # the company the signal is about
      "occurred_at": "2026-05-12T...",                   # when the underlying event happened
      ...structured                                      # type-specific fields kept verbatim
    }

The headline is built deterministically from fields the extractor
already has — NO LLM call. This is the fix for "signal signal detected":
the intelligence was always captured, it just never got turned into a
sentence at write time.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


def build_signal_payload(
    *,
    headline: str,
    entity: Optional[str] = None,
    detail: Optional[str] = None,
    occurred_at: Optional[str] = None,
    **structured: Any,
) -> Dict[str, Any]:
    """Assemble a contract-compliant signal_data dict.

    headline is required and must already name the entity. detail falls
    back to headline when the extractor has nothing richer. Structured
    type-specific fields are merged in verbatim so downstream scoring /
    briefing still gets the raw detail it relies on.
    """
    payload: Dict[str, Any] = {
        "headline": headline.strip(),
        "detail": (detail or headline).strip(),
        "entity": entity,
        "occurred_at": occurred_at,
    }
    payload.update(structured)
    return payload


# ---------------------------------------------------------------------------
# Per-type humanisers — turn the structured detail into a headline line.
# Each takes the company plus whatever that signal type knows and returns
# (headline, detail). Pure string formatting, deterministic, no AI.
# ---------------------------------------------------------------------------

def _co(company: Optional[str]) -> str:
    return company or "This company"


_ROLE_FROM_SUMMARY_RE = re.compile(
    r"\b(?:as|to)\s+(.+?)(?:\s+on\s+\w|$|\.)",
    re.IGNORECASE,
)


def extract_role_from_summary(summary: Optional[str]) -> Optional[str]:
    """Pull the actual title from a PL news summary.

    PL summaries follow two consistent shapes:
        "Co hired X as TITLE on DATE."
        "Co promoted X to TITLE on DATE."
    Returning the TITLE chunk lets callers preserve regional/divisional roles
    (e.g. "Head of Cyber for Iberia") instead of normalising to "CISO".
    """
    if not summary:
        return None
    m = _ROLE_FROM_SUMMARY_RE.search(summary)
    if not m:
        return None
    title = m.group(1).strip().rstrip(".")
    # Strip trailing date fragments the regex can leave behind on edge cases
    title = re.sub(r"\s+on\s+\w.*$", "", title, flags=re.IGNORECASE).strip()
    return title or None


def security_leadership_change_line(
    *, company: Optional[str], title: Optional[str], is_the_prospect: bool,
    change_type: Optional[str], event_summary: Optional[str],
) -> tuple[str, str]:
    """Headline + detail for a security leadership change.

    `change_type` distinguishes the source of the change:
        - "hires"    — external hire
        - "promotes" — internal promotion (regional/divisional roles often
                       sit here, and they are NOT the same as a Group CISO
                       change — preserve the actual title in `title`)
        - None       — fall back to the prospect-is-the-leader phrasing
    """
    co = _co(company)
    t = title or "a security leader"
    if is_the_prospect:
        headline = f"{co} has a new security leader — {t}"
        detail = (
            f"{co}'s {t} is newly in seat. New security leadership = "
            f"vendor-stack review window in their first 90 days."
        )
    elif change_type == "promotes":
        headline = f"{co} promoted a new {t}"
        detail = event_summary or (
            f"{co} elevated {t} internally — a fresh remit on security "
            f"strategy in that scope; budget and vendor decisions are "
            f"likely under review."
        )
    else:  # "hires" or unknown
        headline = f"{co} hired a new {t}"
        detail = event_summary or (
            f"{co} brought on senior security leadership recently — "
            f"budget and vendor decisions are being re-evaluated."
        )
    return headline, detail


# Backwards compat: existing callers may still import this name. New callers
# should prefer `security_leadership_change_line`.
def security_leadership_hire_line(
    *, company: Optional[str], title: Optional[str], is_the_prospect: bool,
    hire_event_summary: Optional[str],
) -> tuple[str, str]:
    return security_leadership_change_line(
        company=company,
        title=title,
        is_the_prospect=is_the_prospect,
        change_type="hires" if hire_event_summary else None,
        event_summary=hire_event_summary,
    )


def security_team_expansion_line(
    *, company: Optional[str], count: int, sample_titles: List[str],
) -> tuple[str, str]:
    co = _co(company)
    sample = ", ".join([t for t in sample_titles[:3] if t])
    headline = f"{co} is scaling its security team — {count} open roles"
    detail = (
        f"{count} security-titled roles open"
        + (f" ({sample})" if sample else "")
        + ". Active build-out — they're investing in security capability now."
    )
    return headline, detail


def _cve_tier(matches: List[Dict[str, Any]]) -> str:
    """Rung on the confidence ladder. KEV (CISA known-exploited) outranks a
    critical-but-not-exploited CVE, which outranks high/other. Drives BOTH
    the wording and the strength so they can never disagree.
    """
    if any(m.get("is_kev") for m in matches):
        return "kev"
    sevs = {(m.get("severity") or "").lower() for m in matches}
    if "critical" in sevs:
        return "critical"
    return "high"


def cve_confidence(matches: List[Dict[str, Any]]) -> float:
    """Confidence ladder for cve_in_stack — replaces the old flat 0.95.

    An actively-exploited (KEV) CVE in the stack is genuinely top urgency;
    a critical CVE that isn't being exploited is strong but not the same
    thing; a high/other match is a softer signal. The threat correlation
    layer already filters out anything below high/non-KEV, so even the
    bottom rung is meaningful.
    """
    return {"kev": 0.95, "critical": 0.8, "high": 0.6}[_cve_tier(matches)]


def cve_in_stack_line(
    *, company: Optional[str], matches: List[Dict[str, Any]],
) -> tuple[str, str]:
    co = _co(company)
    # Bucket DISTINCT CVEs (one CVE can arrive as several tech rows) by
    # what's actually CONFIRMED about each — never let a critical-but-not-
    # exploited CVE be counted under "actively-exploited" just because some
    # OTHER CVE in the set is KEV. The headline count must equal the number
    # of CVEs the wording actually claims.
    kev: List[Dict[str, Any]] = []
    crit: List[Dict[str, Any]] = []
    other: List[Dict[str, Any]] = []
    seen: set = set()
    for m in matches:
        t = m.get("cve_title")
        if not t or t in seen:
            continue
        seen.add(t)
        if m.get("is_kev"):
            kev.append(m)
        elif (m.get("severity") or "").lower() == "critical":
            crit.append(m)
        else:
            other.append(m)

    def _named(ms: List[Dict[str, Any]]) -> tuple:
        m = ms[0]
        return (m.get("cve_title") or "a known CVE"), m.get("tech")

    if not seen:  # no titles to reason about — stay generic
        n = len(matches)
        return (
            f"{n} known CVE{'s' if n != 1 else ''} in {co}'s stack",
            f"Matches {co}'s detected stack. Lead with sector context, "
            f"not their exposure.",
        )

    if kev:
        k = len(kev)
        cve, tech = _named(kev)
        headline = (
            f"{k} actively-exploited CVE{'s' if k != 1 else ''} "
            f"in {co}'s stack"
        )
        extra = len(crit) + len(other)
        extra_clause = (
            f" Plus {extra} other critical/known CVE"
            f"{'s' if extra != 1 else ''} in the detected stack."
            if extra else ""
        )
        detail = (
            f"{cve}"
            + (f" affects {tech}, which {co} runs" if tech
               else f" matches {co}'s detected stack")
            + "." + extra_clause
            + " Lead with sector context, not their exposure."
        )
    elif crit:
        c = len(crit)
        cve, tech = _named(crit)
        headline = (
            f"{c} critical CVE{'s' if c != 1 else ''} affecting {co}'s stack"
        )
        detail = (
            f"{cve}"
            + (f" affects {tech}, which {co} runs" if tech
               else f" matches {co}'s detected stack")
            + ". Not confirmed as actively exploited. Lead with sector "
            "context, not their exposure."
        )
    else:
        n = len(other)
        cve, tech = _named(other)
        headline = f"{n} known CVE{'s' if n != 1 else ''} in {co}'s stack"
        detail = (
            f"{cve}"
            + (f" affects {tech}, which {co} runs" if tech
               else f" matches {co}'s detected stack")
            + ". Lead with sector context, not their exposure."
        )
    return headline, detail


def compliance_gap_line(
    *, company: Optional[str], industry: Optional[str],
) -> tuple[str, str]:
    co = _co(company)
    ind = (industry or "their regulated sector").strip()
    headline = f"{co} — no public ISO 27001 / SOC 2 ({ind})"
    detail = (
        f"{co} operates in {ind} but no current ISO 27001 or SOC 2 "
        f"attestation is publicly visible. Public absence is not proof of "
        f"no certification — they may hold one privately or under NDA. "
        f"Frame as audit-prep support, not gap-shaming."
    )
    return headline, detail


def breach_proximity_line(
    *, company: Optional[str], count: int, matches: List[Dict[str, Any]],
) -> tuple[str, str]:
    co = _co(company)
    top = matches[0] if matches else {}
    title = top.get("title") or "a breach in their sector"
    headline = f"Breach near {co}'s stack — {title}"[:90]
    detail = (
        f"{title}. Affects technology/sector adjacent to {co}. "
        f"Anchor on what peers did, never their own posture."
    )
    return headline, detail


# News-event categories from PredictLeads → human headline.
def predictleads_news_line(
    *, company: Optional[str], category: str, summary: Optional[str],
    product: Optional[str],
) -> tuple[str, str]:
    co = _co(company)
    s = (summary or "").strip()
    templates = {
        "funding": (f"{co} closed a funding round", "Fresh capital + board attention on growth and security maturity."),
        "acquires": (f"{co} — M&A activity", "Combined infrastructure means detection-coverage gaps and a vendor review."),
        "launches": (
            f"{co} launched {product}" if product else f"{co} shipped a new product",
            "New product = new attack surface and a security-readiness conversation.",
        ),
        "partners_with": (f"{co} announced a new partnership", "New integration surface — a natural security-review trigger."),
        "hires": (f"{co} — notable new hire", "Leadership change opens a vendor-evaluation window."),
        "expands_to": (f"{co} is expanding", "New geography/vertical = fresh compliance and security exposure."),
    }
    template_headline, default_detail = templates.get(
        category, (f"{co} — {category.replace('_', ' ')}", "Notable company event.")
    )
    # When PredictLeads gives a specific event summary ("Acme Labs
    # hires Jane Smith"), that IS the headline — it's sharper than
    # the generic category template. Keep the template as the supporting
    # detail. Only fall back to the template headline when the summary is
    # missing or uselessly short.
    if s and len(s) >= 12:
        headline = s if len(s) <= 90 else s[:87].rstrip() + "…"
        detail = template_headline + "."
    else:
        headline = template_headline
        detail = default_detail
    return headline, detail


# ---------------------------------------------------------------------------
# Findings loader — read the contract back as normalized "finding" objects.
#
# The inverse of build_signal_payload(). Scoring and the GTM briefing call
# this to surface the SAME deterministic human sentences the dashboard and
# digest already show — so a lead's score rationale, its briefing, and the
# dashboard never disagree about what was detected.
#
# ADDITIVE / DISPLAY-ONLY by design: returns [] when no (live) rows exist,
# so neither scoring nor play-matching logic ever depends on signal-row
# lifecycle (rows are delete-then-insert on re-enrich and self-expire).
# `_extract_signals` remains the deterministic source of truth for plays.
# ---------------------------------------------------------------------------

# `signals.signal_type` names don't all match the play-library / _extract_signals
# boolean keys. Map a finding onto the play key(s) it can serve as evidence for
# so the briefing can attach it to the matched play.
_SIGNAL_TYPE_PLAY_ALIASES: Dict[str, str] = {
    "recently_funded": "recent_funding",
    "product_launch": "product_launch_or_partnership",
    "new_partnership": "product_launch_or_partnership",
}


def finding_play_keys(signal_type: Optional[str]) -> set:
    """The play required/boost signal names a finding of this type evidences."""
    if not signal_type:
        return set()
    keys = {signal_type}
    alias = _SIGNAL_TYPE_PLAY_ALIASES.get(signal_type)
    if alias:
        keys.add(alias)
    return keys


# ---------------------------------------------------------------------------
# Finding contract v2 — a SUPERSET of v1.
#
# Every v1 key (signal_type, strength, headline, detail, entity, occurred_at,
# detected_at, expires_at, signal_source) is retained verbatim so existing
# consumers are untouched. v2 adds the richer brief schema (type, summary,
# source_name, source_url, observed_at, freshness, confidence, evidence,
# sales_relevance, recommended_use, risk_note, related_persona,
# related_service) plus a canonical `category`. All derived deterministically
# from fields the writer already has — no LLM at finding-build time.
# ---------------------------------------------------------------------------

# The 10 canonical cyber-relevance categories from the brief. Some have a
# producer today; the rest are reserved so future signal types can emit them
# without a taxonomy change.
FINDING_CATEGORIES = (
    "security_leadership_change",
    "security_team_buildout",
    "regulatory_or_compliance_pressure",
    "breach_or_incident_proximity",
    "exposed_technology_relevance",
    "new_attack_surface",
    "customer_vertical_risk",
    "partner_ecosystem_signal",
    "budget_or_growth_signal",
    "competitive_displacement_signal",
)

# "Threat/fear" categories — the ones a no-ambulance-chasing client must
# never LEAD with (CVE exposure, breach proximity). They stay in the
# finding set as supporting context; per-client config only changes their
# ORDERING/prominence, never their strength, confidence, or scoring.
_THREAT_CATEGORIES = frozenset({
    "exposed_technology_relevance",
    "breach_or_incident_proximity",
})


def finding_is_threat(finding: Dict[str, Any]) -> bool:
    """True if this finding is a threat/fear-class signal (CVE / breach)."""
    return (finding or {}).get("category") in _THREAT_CATEGORIES


def wants_threats_deprioritised(
    client_context: Optional[Dict[str, Any]]
) -> bool:
    """Universal hard rule: Vera NEVER sells with fear. Threat/fear
    findings always sort AFTER opportunity signals, threat-alert hooks
    are sector-framed not exposure-led, and rating reasons never lead
    with the prospect's own incident — for EVERY client, by default.

    This is intentionally a code-level default (ON for every org,
    including future ones) rather than per-client config you have to
    remember to set. The only way to turn it off is an explicit
    client_context.deprioritise_threat_findings = false escape hatch
    (deliberate action required; not expected to be used).
    """
    v = (client_context or {}).get("deprioritise_threat_findings")
    if v is None:
        return True  # default: never sell with fear
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(v)


# Lexicon for detecting a fear/threat-led OPENER. The no-fear rule is
# about what the rationale LEADS with — fear as trailing context is fine;
# opening on the prospect's own incident/exposure is not.
_FEAR_OPENER_TERMS = (
    "ransomware", "breach", "ddos", "hacked", "hack ", "cyberattack",
    "cyber attack", "under attack", "attacked", "data leak", "stolen data",
    "exfiltrat", "malware", "intrusion", "zero-day", "zero day",
    "actively-exploited", "actively exploited", "exploit", "vulnerabilit",
    "cve-", "security incident", "security crisis", "security lapse",
    "security breach", "compromis", "leaked", "extortion", "data breach",
)


def _lead_sentence(text: str) -> str:
    """The opening sentence-ish chunk — what the text LEADS with.
    Min length guard avoids splitting on abbreviations like "U.S.".
    """
    t = (text or "").strip()
    for i, ch in enumerate(t):
        if ch in ".!?" and i >= 12:
            return t[: i + 1]
    return t[:160]


def is_fear_led(text: str) -> bool:
    """True if the text OPENS on a threat/fear trigger. One shared
    detector so the scorer rationale and the threat-alert surface agree
    on what 'leads with fear' means for a no-fear client.
    """
    if not text:
        return False
    op = _lead_sentence(text).lower()
    return any(term in op for term in _FEAR_OPENER_TERMS)


def reframe_rating_reason(
    reason: str,
    *,
    client_context: Optional[Dict[str, Any]],
    findings: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """For a no-fear client, never let the rating reason LEAD with the
    prospect's own incident/exposure (a hard rule for these clients). If the
    opening sentence is fear-led, replace just that opener with the
    strongest OPPORTUNITY finding (findings arrive already lever-ordered
    opportunity-first for these clients) and keep the rest of the
    rationale. The fear point is demoted, not deleted — it simply can't
    be the headline.

    No-op for normal clients and for reasons that don't open on fear.
    Deterministic; never raises (returns the original on any error).
    """
    try:
        if not reason or not wants_threats_deprioritised(client_context):
            return reason
        opener = _lead_sentence(reason)
        if not any(term in opener.lower() for term in _FEAR_OPENER_TERMS):
            return reason  # doesn't lead with fear — leave it untouched
        opp = next(
            (f for f in (findings or [])
             if not finding_is_threat(f) and (f.get("headline") or "").strip()),
            None,
        )
        rest = reason[len(opener):].strip()
        if opp:
            new_opener = opp["headline"].strip().rstrip(".")
            return f"{new_opener}. {rest}".strip() if rest else f"{new_opener}."
        # Nothing positive to lead with — at minimum don't lead with fear:
        # drop the fear opener, keep the rest if there's anything to keep.
        return rest or reason
    except Exception:  # pragma: no cover - must never break scoring
        return reason

_SIGNAL_TYPE_TO_CATEGORY: Dict[str, str] = {
    "security_leadership_hire": "security_leadership_change",
    "new_leadership": "security_leadership_change",
    "security_team_expansion": "security_team_buildout",
    "compliance_gap": "regulatory_or_compliance_pressure",
    "breach_proximity": "breach_or_incident_proximity",
    "cve_in_stack": "exposed_technology_relevance",
    "product_launch": "new_attack_surface",
    "new_partnership": "partner_ecosystem_signal",
    "recently_funded": "budget_or_growth_signal",
    "recent_acquisition": "budget_or_growth_signal",
    "expansion": "budget_or_growth_signal",
    "hiring_sales_bd": "budget_or_growth_signal",
    "competitor_tool_detected": "competitive_displacement_signal",
}

# Clean, client-facing source labels — NEVER the raw backend tool name
# (see no-backend-tool-names rule). signal_source is internal only.
_SOURCE_LABELS: Dict[str, str] = {
    "predictleads": "Company intelligence",
    "threat_intel": "Threat intelligence",
    "linkedin": "Verified profile",
    "deep_research": "Web research",
    "jobs": "Hiring activity",
}

# Per-signal-type sales framing. Deterministic, no-ambulance-chasing aware:
# threat-derived types carry an explicit risk_note that keeps the angle on
# sector context, never the prospect's own exposure.
_RELEVANCE: Dict[str, Dict[str, str]] = {
    "security_leadership_hire": {
        "sales_relevance": "New security leadership re-evaluates the vendor stack in their first 90 days.",
        "recommended_use": "Lead with the leadership change and offer a useful POV for their first quarter.",
        "risk_note": "",
    },
    "new_leadership": {
        "sales_relevance": "Leadership change opens a strategy and budget re-evaluation window.",
        "recommended_use": "Reference the move as the reason for timely outreach.",
        "risk_note": "",
    },
    "security_team_expansion": {
        "sales_relevance": "Active security hiring means budget and tooling decisions are live now.",
        "recommended_use": "Anchor on their build-out; position as accelerating the capability they're hiring for.",
        "risk_note": "",
    },
    "compliance_gap": {
        "sales_relevance": "Regulated operators face procurement pressure to evidence security maturity.",
        "recommended_use": "Frame as audit-prep support, not a gap callout.",
        "risk_note": "Public absence of a certification is not proof of no certification — they may hold one privately.",
    },
    "cve_in_stack": {
        "sales_relevance": "Their detected stack overlaps known-exploited vulnerabilities affecting their sector.",
        "recommended_use": "Open on sector threat context and offer help — never their specific exposure.",
        "risk_note": "Do not claim the prospect is vulnerable; frame as sector/peer context only.",
    },
    "breach_proximity": {
        "sales_relevance": "Incidents are hitting technology/peers adjacent to this account.",
        "recommended_use": "Share what peers did in response; position as helpful, not opportunistic.",
        "risk_note": "Never imply the prospect is breached or exposed; this is proximity context.",
    },
    "recently_funded": {
        "sales_relevance": "Fresh capital brings board attention to growth and security maturity.",
        "recommended_use": "Congratulate the raise and connect it to scaling securely.",
        "risk_note": "",
    },
    "recent_acquisition": {
        "sales_relevance": "M&A means infrastructure consolidation and a vendor review.",
        "recommended_use": "Reference the deal as the trigger for an integration-security conversation.",
        "risk_note": "",
    },
    "product_launch": {
        "sales_relevance": "A new product is new attack surface and a security-readiness moment.",
        "recommended_use": "Tie outreach to the launch and the surface it introduces.",
        "risk_note": "",
    },
    "new_partnership": {
        "sales_relevance": "New integrations are a natural security-review trigger.",
        "recommended_use": "Position around securing the new partner/integration surface.",
        "risk_note": "",
    },
    "expansion": {
        "sales_relevance": "New geography/vertical brings fresh compliance and security exposure.",
        "recommended_use": "Anchor on the expansion and the new obligations it creates.",
        "risk_note": "",
    },
    "hiring_sales_bd": {
        "sales_relevance": "Outbound build-out is a buying window for tooling that compresses ramp time.",
        "recommended_use": "Reference the hiring as evidence of a growth motion to support.",
        "risk_note": "",
    },
}


def _confidence_band(strength: float) -> str:
    if strength >= 0.85:
        return "high"
    if strength >= 0.65:
        return "medium"
    return "low"


def _freshness(observed_at: Optional[str], detected_at: Optional[str]) -> str:
    """fresh <30d, recent <90d, aging <180d, else stale/unknown.

    Prefers when the underlying event happened (observed_at); falls back to
    when we detected it.
    """
    ref = observed_at or detected_at
    if not ref:
        return "unknown"
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(str(ref).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - dt).days
    except (ValueError, AttributeError, TypeError):
        return "unknown"
    if age_days < 0:
        return "fresh"
    if age_days < 30:
        return "fresh"
    if age_days < 90:
        return "recent"
    if age_days < 180:
        return "aging"
    return "stale"


def _evidence_from(signal_type: str, data: Dict[str, Any]) -> List[str]:
    """Short, human supporting facts pulled from the structured payload the
    humaniser already stored. Best-effort, never raises, no overclaiming.
    """
    ev: List[str] = []
    try:
        if signal_type == "cve_in_stack":
            n = data.get("count")
            if n:
                ev.append(f"{n} matched CVE record(s) against the detected stack")
            for m in (data.get("matches") or [])[:3]:
                t = m.get("cve_title") or m.get("title")
                tech = m.get("tech")
                if t:
                    ev.append(f"{t}" + (f" — overlaps {tech}" if tech else ""))
        elif signal_type == "security_team_expansion":
            c = data.get("security_role_count")
            if c:
                ev.append(f"{c} open security-titled roles")
            for t in (data.get("sample_titles") or [])[:3]:
                if t:
                    ev.append(t)
        elif signal_type == "breach_proximity":
            for m in (data.get("matches") or [])[:3]:
                t = m.get("title")
                if t:
                    ev.append(t)
        elif signal_type == "compliance_gap":
            ind = data.get("industry")
            if ind:
                ev.append(f"Regulated industry: {ind}")
            ev.append("No public ISO 27001 / SOC 2 attestation found")
        elif signal_type == "security_leadership_hire":
            t = data.get("primary_title")
            if t:
                ev.append(f"Security leadership title: {t}")
            s = data.get("hire_event_summary")
            if s:
                ev.append(s)
        else:
            s = data.get("summary")
            if s:
                ev.append(s)
            p = data.get("product")
            if p:
                ev.append(f"Product: {p}")
    except Exception:  # pragma: no cover - evidence is best-effort
        pass
    return [e for e in ev if e][:5]


def finding_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a signals-table-shaped row dict into a canonical finding.

    A "row" is the dict the enricher's signal writers build (and also what
    the signals table stores): {signal_type, signal_source, signal_strength,
    signal_data, detected_at?, expires_at?}. This is the ONE mapping — the
    enrichment-stored findings, the table fallback, and any future writer
    all go through here so the finding shape can never diverge.

    Returns the v2 SUPERSET: every v1 key plus the brief's richer schema
    (type, summary, source_name, source_url, observed_at, freshness,
    confidence, evidence, sales_relevance, recommended_use, risk_note,
    related_persona, related_service) and a canonical `category`.
    """
    st = row.get("signal_type") or "signal"
    data = row.get("signal_data") or {}
    entity = data.get("entity")
    headline = (data.get("headline") or "").strip() or (
        f"{entity or 'This company'} — {st.replace('_', ' ')}"
    )
    detail = (data.get("detail") or headline).strip()
    strength = float(row.get("signal_strength") or 0.0)
    occurred_at = data.get("occurred_at")
    detected_at = row.get("detected_at")
    source = row.get("signal_source")
    framing = _RELEVANCE.get(st, {})

    return {
        # --- v1 keys (unchanged — existing consumers depend on these) ---
        "signal_type": st,
        "signal_source": source,
        "strength": strength,
        "headline": headline,
        "detail": detail,
        "entity": entity,
        "occurred_at": occurred_at,
        "detected_at": detected_at,
        "expires_at": row.get("expires_at"),
        # --- v2 additive schema ---
        "type": st,
        "category": _SIGNAL_TYPE_TO_CATEGORY.get(st, st),
        "summary": detail,
        "source_name": _SOURCE_LABELS.get(source or "", "Vera intelligence"),
        "source_url": data.get("source_url") or data.get("url"),
        "observed_at": occurred_at,
        "freshness": _freshness(occurred_at, detected_at),
        "confidence": _confidence_band(strength),
        "evidence": _evidence_from(st, data),
        "sales_relevance": framing.get("sales_relevance", ""),
        "recommended_use": framing.get("recommended_use", ""),
        "risk_note": framing.get("risk_note", ""),
        "related_persona": data.get("related_persona"),
        "related_service": data.get("related_service"),
    }


def _select_live_findings(
    findings: List[Dict[str, Any]],
    deprioritise_threats: bool = False,
) -> List[Dict[str, Any]]:
    """Drop expired findings, dedupe to strongest-then-newest per type,
    return sorted. Default order is strength then recency.

    When ``deprioritise_threats`` is True, opportunity findings sort ahead
    of ALL threat/fear findings (then strength, then recency within each
    class) — so a client whose rule is "never lead with tech/fear" gets
    funding/leadership/hiring first and CVE/breach as supporting context.
    Strength/confidence/scoring are untouched; only presentation order is.
    """
    now = _utcnow_iso()
    best_by_type: Dict[str, Dict[str, Any]] = {}
    for f in findings:
        exp = f.get("expires_at")
        if exp and str(exp) < now:
            continue
        st = f.get("signal_type") or "signal"
        prev = best_by_type.get(st)
        if prev is None or (
            f.get("strength") or 0.0,
            f.get("detected_at") or "",
        ) > (prev.get("strength") or 0.0, prev.get("detected_at") or ""):
            best_by_type[st] = f

    def _key(f: Dict[str, Any]):
        base = (f.get("strength") or 0.0, f.get("detected_at") or "")
        if deprioritise_threats:
            # 1 = opportunity (sorts first), 0 = threat (sorts last).
            return (0 if finding_is_threat(f) else 1, *base)
        return base

    return sorted(best_by_type.values(), key=_key, reverse=True)


def collect_findings(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build the canonical enrichment_data['findings'] list from the signal
    rows the enricher's writers produced. Deduped to strongest-per-type;
    expiry is applied at READ time by load_findings (a finding can be stored
    now and age out later), so it is NOT filtered here.
    """
    best_by_type: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if not r:
            continue
        f = finding_from_row(r)
        st = f["signal_type"]
        prev = best_by_type.get(st)
        if prev is None or (f["strength"] >= (prev.get("strength") or 0.0)):
            best_by_type[st] = f
    return sorted(
        best_by_type.values(),
        key=lambda f: f.get("strength") or 0.0,
        reverse=True,
    )


def load_findings(
    db: Any,
    *,
    org_id: Optional[str],
    lead_id: Optional[str],
    enrichment_data: Optional[Dict[str, Any]] = None,
    deprioritise_threats: bool = False,
    limit: int = 25,
) -> List[Dict[str, Any]]:
    """Resolve a lead's normalized findings, canonical store first.

    Resolution order (architecture decided 2026-05-18):
      1. ``enrichment_data["findings"]`` — the CANONICAL durable store. It
         travels with the lead, so scoring/briefing work on cached
         enrichment with ZERO live signal rows.
      2. The ``signals`` table — a projection for digest/dashboard, used
         only as a fallback for leads enriched before the canonical store
         existed.

    Expired findings are dropped and the set is deduped to the
    strongest-then-newest per signal_type. Never raises — any failure
    yields [] so callers stay decoupled from store availability.
    """
    # 1. Canonical: enrichment_data is the source of truth.
    ed_findings = (enrichment_data or {}).get("findings")
    if isinstance(ed_findings, list) and ed_findings:
        return _select_live_findings(ed_findings, deprioritise_threats)

    # 2. Fallback: project from the signals table (pre-canonical leads).
    if db is None or not org_id or not lead_id:
        return []
    try:
        resp = (
            db.table("signals")
            .select(
                "signal_type, signal_source, signal_strength, signal_data, "
                "detected_at, expires_at"
            )
            .eq("org_id", org_id)
            .eq("lead_id", lead_id)
            .order("detected_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = resp.data or []
    except Exception:  # pragma: no cover - display-only, never fatal
        return []

    return _select_live_findings(
        [finding_from_row(r) for r in rows], deprioritise_threats
    )


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
