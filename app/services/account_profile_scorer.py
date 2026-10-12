"""Evidence-driven account Profile scorer.

For each company, compute four axis scores (Risk / Posture / Maturity /
Commercial, each 0-100) from the company's signals, security team
profile, and a few state flags. Roll those into one composite Profile
score (0-100), and compute a peer-band percentile so the dossier can
say "top 25% of financial services accounts Vera tracks" without us
guessing.

Why evidence-driven over signal-count: an account with 30 noisy
social_complaint rows shouldn't outrank an account with 1 fresh
breach. Each axis has a recipe of SPECIFIC contributors with caps —
the resulting score is fully auditable on the dossier, and tuning
weights is a one-place edit (the recipe dicts below).

No AI calls — pure aggregation over the signals table + the cached
security_team_profile. Cheap enough to run on every monitor pass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from supabase import Client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Recency model.
#
# Signals decay over a 180-day half-life: a fresh breach contributes
# its full points; a 6-month-old breach contributes half. Anything
# older than 12 months contributes <25%. This is the only "time" knob
# in scoring — everything else is a per-signal cap.
# ---------------------------------------------------------------------------
_HALF_LIFE_DAYS = 180
_HARD_CUTOFF_DAYS = 365


def _recency_weight(detected_at: Optional[str], now: datetime) -> float:
    if not detected_at:
        return 0.5  # unknown date — treat as middle-aged
    try:
        ts = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 0.5
    age = (now - ts).days
    if age >= _HARD_CUTOFF_DAYS:
        return 0.0
    if age <= 0:
        return 1.0
    # Half-life decay
    return 0.5 ** (age / _HALF_LIFE_DAYS)


# ---------------------------------------------------------------------------
# Per-axis evidence recipes.
#
# Each axis is a dict mapping signal_type -> {points_per: int, cap: int}.
#   points_per = points earned by each instance (multiplied by recency_weight)
#   cap        = the most this signal_type can contribute to the axis
#
# Composite axis score = sum(contributions) capped at 100.
#
# Tuning notes:
#   - Risk weights are tilted toward dated, attributable exposure
#   - Posture is largely a sum of website/page presence checks (Phase 5 detector)
#   - Maturity is dominated by the security_team_profile.score (0-50) +
#     leadership presence + hiring signals (each capped)
#   - Commercial leans on funding/M&A/non-security exec changes
# ---------------------------------------------------------------------------

RISK_RECIPE: Dict[str, Dict[str, int]] = {
    # Stealer-log credential exposure (LeakRadar). credential_leak fires only
    # when employee creds surfaced in the last 90 days (recency-gated in
    # company_enricher), so it represents LIVE exposure — made the single
    # biggest Risk contributor (2026-06-18): a fresh stealer-log hit
    # pushes Risk above any breach/CVE evidence. credential_leak_supply_chain
    # (third parties) is a meaningful but secondary contributor.
    "credential_leak":              {"points_per": 50, "cap": 55},
    "credential_leak_supply_chain": {"points_per": 14, "cap": 18},
    "breach_proximity":      {"points_per": 35, "cap": 45},
    "social_breach_mention": {"points_per": 12, "cap": 35},
    "cve_in_stack":          {"points_per": 6,  "cap": 30},
}

POSTURE_RECIPE: Dict[str, Dict[str, int]] = {
    # Website / first-party stance — each presence check adds a chunk
    "bug_bounty_present":              {"points_per": 18, "cap": 18},
    "responsible_disclosure_present":  {"points_per": 12, "cap": 12},
    "trust_center_present":            {"points_per": 14, "cap": 14},
    "security_page_present":           {"points_per": 10, "cap": 10},
    "compliance_page_present":         {"points_per": 8,  "cap": 8},
    "status_page_present":             {"points_per": 6,  "cap": 6},
    "security_contact_present":        {"points_per": 4,  "cap": 4},
    "subprocessors_present":           {"points_per": 4,  "cap": 4},
    "dpa_present":                     {"points_per": 4,  "cap": 4},
    "privacy_page_present":            {"points_per": 3,  "cap": 3},
    # Public security voice from people (classifier emits these)
    "person_posted_relevant_topic":    {"points_per": 5,  "cap": 15},
    "person_engaged_with_topic":       {"points_per": 3,  "cap": 9},
    "enterprise_trust_pressure":       {"points_per": 8,  "cap": 16},
}

MATURITY_RECIPE: Dict[str, Dict[str, int]] = {
    # Direct hiring signals
    "security_leadership_hire":     {"points_per": 14, "cap": 28},
    "security_team_expansion":      {"points_per": 8,  "cap": 24},
    # A leadership opening outscores a practitioner one: it usually means no
    # senior owner of security today. Still below security_leadership_hire
    # (14), where someone is actually in the seat and can sign.
    "hiring_security_leadership":   {"points_per": 10, "cap": 20},
    "hiring_security":              {"points_per": 4,  "cap": 20},
    "hiring_grc":                   {"points_per": 4,  "cap": 12},
    "social_hiring_buzz":           {"points_per": 3,  "cap": 9},
    # Capability proof. The generic certification_claim_detected entry
    # stays so pre-2026-06-11 rows keep scoring; new rows arrive as
    # per-cert types (same 7 points each, capped per framework so two
    # frameworks ≈ the old 14-point cap).
    "certification_claim_detected": {"points_per": 7,  "cap": 14},
    "certification_iso_27001":      {"points_per": 7,  "cap": 7},
    "certification_soc_2":          {"points_per": 7,  "cap": 7},
    "certification_pci_dss":        {"points_per": 7,  "cap": 7},
    "certification_cyber_essentials": {"points_per": 5, "cap": 5},
    "certification_cyber_essentials_plus": {"points_per": 7, "cap": 7},
    "certification_crest":          {"points_per": 7,  "cap": 7},
    "certification_check":          {"points_per": 7,  "cap": 7},
    "enterprise_customer_detected": {"points_per": 4,  "cap": 12},
    "regulated_customer_detected":  {"points_per": 6,  "cap": 12},
    # Platform-shape evidence (page probes, 2026-06-11)
    "enterprise_page_detected":     {"points_per": 3,  "cap": 3},
    "api_or_developer_platform":    {"points_per": 3,  "cap": 3},
    # Person types - presence of a security leader on staff is real maturity
    "person_security_leader":       {"points_per": 8,  "cap": 16},
    "person_grc_leader":            {"points_per": 5,  "cap": 10},
    # Pressure rollups (Phase 6 compound signals)
    "security_team_scaling":        {"points_per": 12, "cap": 24},
    "cloud_security_scaling":       {"points_per": 8,  "cap": 16},
    "compliance_operationalisation":{"points_per": 8,  "cap": 16},
}
# NB: security_team_profile.score (0-50) is added separately as the
# strongest Maturity contributor — see _maturity_axis() below.

COMMERCIAL_RECIPE: Dict[str, Dict[str, int]] = {
    "recently_funded":              {"points_per": 30, "cap": 35},
    "social_funding_news":          {"points_per": 18, "cap": 30},
    "recent_acquisition":           {"points_per": 25, "cap": 30},
    "new_leadership":               {"points_per": 12, "cap": 30},
    "social_leadership_change":     {"points_per": 8,  "cap": 24},
    "social_milestone":             {"points_per": 5,  "cap": 20},
    "expansion":                    {"points_per": 8,  "cap": 16},
    "social_vendor_relationship":   {"points_per": 6,  "cap": 18},
    "person_engaged_with_competitor": {"points_per": 9, "cap": 18},
    "person_commercial_leader":     {"points_per": 4,  "cap": 8},
    "revenue_blocker_likely":       {"points_per": 6,  "cap": 12},
}

ALL_AXES = ("risk", "posture", "maturity", "commercial")


# Composite weights — how much each axis pulls on the final Profile score.
# Sum to 100. Tuning here is the single lever that shifts whether reps
# see "high Profile = mature buyer" or "high Profile = active buyer".
_COMPOSITE_WEIGHTS: Dict[str, float] = {
    "risk":       0.25,
    "posture":    0.20,
    "maturity":   0.30,
    "commercial": 0.25,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
@dataclass
class AxisContribution:
    """One line of evidence under an axis — what produced what points."""
    label: str
    points: int
    detail: str = ""  # optional human-readable detail (e.g. "9 people")


def refresh_profile_score(
    db: Client,
    *,
    company_id: str,
    org_id: str,
) -> Optional[Dict[str, Any]]:
    """Compute + persist the Profile score for one company.

    Returns the stored dict, or None on hard failure.

    org_id is required because the signals table needs both axes of
    union-reads: company-keyed rows (written by enrich_company_only)
    AND lead-keyed rows (written by per-lead enrichers). Leads are
    org-scoped; we filter to this org so a company watched by two
    orgs doesn't pollute each other's scores.
    """
    company = _fetch_company(db, company_id)
    if not company:
        logger.warning(
            "refresh_profile_score: company %s not found", company_id,
        )
        return None

    signals = _fetch_signals(
        db, org_id=org_id, company_id=company_id,
        company_domain=company.get("domain"),
    )
    team_profile = company.get("security_team_profile") or {}
    industry = company.get("industry")
    employee_count = _parse_size_band(company.get("size_band"))
    size_band = _size_band(employee_count)
    prior = company.get("profile_score") or {}

    now = datetime.now(timezone.utc)

    risk_score, risk_breakdown = _axis_from_recipe(signals, RISK_RECIPE, now)
    posture_score, posture_breakdown = _axis_from_recipe(signals, POSTURE_RECIPE, now)
    maturity_score, maturity_breakdown = _maturity_axis(signals, team_profile, now)
    commercial_score, commercial_breakdown = _axis_from_recipe(signals, COMMERCIAL_RECIPE, now)

    axis_scores = {
        "risk":       risk_score,
        "posture":    posture_score,
        "maturity":   maturity_score,
        "commercial": commercial_score,
    }
    composite = _composite(axis_scores)

    delta_30d = _compute_delta(prior, axis_scores, composite, now)

    peer_band = _peer_band(
        db,
        composite=composite,
        size_band=size_band,
        industry=industry,
        exclude_company_id=company_id,
    )

    payload = {
        **axis_scores,
        "composite": composite,
        "peer_band": peer_band,
        "axis_breakdown": {
            "risk":       [_serialise(c) for c in risk_breakdown],
            "posture":    [_serialise(c) for c in posture_breakdown],
            "maturity":   [_serialise(c) for c in maturity_breakdown],
            "commercial": [_serialise(c) for c in commercial_breakdown],
        },
        "delta_30d": delta_30d,
        "size_band": size_band,
        "computed_at": now.isoformat(),
    }

    _store(db, company_id=company_id, profile_score=payload)
    return payload


# ---------------------------------------------------------------------------
# Axis calculators
# ---------------------------------------------------------------------------
def _axis_from_recipe(
    signals: List[Dict[str, Any]],
    recipe: Dict[str, Dict[str, int]],
    now: datetime,
) -> Tuple[int, List[AxisContribution]]:
    """Sum recipe contributions across the signals list.

    For each signal_type in the recipe, accumulate points_per ×
    recency_weight across matching signals, capped per signal_type at
    `cap`. Final axis score is the sum of contributions, capped at 100.
    """
    contribs_by_type: Dict[str, float] = {}
    counts_by_type: Dict[str, int] = {}
    for s in signals:
        st = s.get("signal_type")
        if st not in recipe:
            continue
        rule = recipe[st]
        w = _recency_weight(s.get("detected_at"), now)
        if w == 0:
            continue
        contribs_by_type[st] = contribs_by_type.get(st, 0.0) + rule["points_per"] * w
        counts_by_type[st] = counts_by_type.get(st, 0) + 1

    breakdown: List[AxisContribution] = []
    total = 0.0
    for st, raw in contribs_by_type.items():
        capped = min(raw, recipe[st]["cap"])
        if capped <= 0:
            continue
        total += capped
        n = counts_by_type[st]
        detail = f"{n} signal{'s' if n != 1 else ''}"
        breakdown.append(AxisContribution(
            label=_humanise_signal_type(st),
            points=int(round(capped)),
            detail=detail,
        ))

    score = int(min(100, round(total)))
    # Sort breakdown by points desc so the dossier shows top contributors first
    breakdown.sort(key=lambda c: c.points, reverse=True)
    return score, breakdown


def _maturity_axis(
    signals: List[Dict[str, Any]],
    team_profile: Dict[str, Any],
    now: datetime,
) -> Tuple[int, List[AxisContribution]]:
    """Maturity is the only axis with a non-signal input — the cached
    security_team_profile score (0-50) is the single biggest contributor.

    Then signal-driven contributions stack on top, capped collectively
    at 100.
    """
    # Signal-driven part (uses the recipe)
    sig_score, breakdown = _axis_from_recipe(signals, MATURITY_RECIPE, now)

    # Team profile contribution (0-50)
    team_score = 0
    if isinstance(team_profile, dict):
        try:
            team_score = int(team_profile.get("score") or 0)
        except (TypeError, ValueError):
            team_score = 0
    if team_score > 0:
        total = team_profile.get("total")
        band = team_profile.get("expected_band") or [0, 0]
        detail = f"{total} people, expected {band[0]}-{band[1]}"
        breakdown.insert(0, AxisContribution(
            label="Security team headcount",
            points=team_score,
            detail=detail,
        ))

    final = int(min(100, sig_score + team_score))
    # Re-sort after inserting the team contribution at the top
    breakdown.sort(key=lambda c: c.points, reverse=True)
    return final, breakdown


def _composite(axis_scores: Dict[str, int]) -> int:
    total = 0.0
    for axis, w in _COMPOSITE_WEIGHTS.items():
        total += axis_scores.get(axis, 0) * w
    return int(round(total))


# ---------------------------------------------------------------------------
# Peer banding — percentile within (size_band, industry) cohort
# ---------------------------------------------------------------------------
def _peer_band(
    db: Client,
    *,
    composite: int,
    size_band: str,
    industry: Optional[str],
    exclude_company_id: str,
) -> Dict[str, Any]:
    """Percentile rank of this company's composite score among peer
    companies in Vera. Peer = same size_band, broadly same industry
    (we coalesce industries to keep cohorts populated).

    Returns:
      {percentile: int (0-100),
       cohort_size: int,
       cohort_label: str (e.g. "financial services firms Vera tracks")}
    """
    cohort_industry_filter = _industry_cohort(industry)
    try:
        q = db.table("companies").select(
            "id, profile_score, industry, size_band"
        ).neq("id", exclude_company_id)
        res = q.execute()
    except Exception as e:
        logger.warning("peer_band query failed: %s", e)
        return {"percentile": 50, "cohort_size": 0, "cohort_label": "Vera-tracked accounts"}

    peer_composites: List[int] = []
    for row in res.data or []:
        ps = row.get("profile_score") or {}
        if not isinstance(ps, dict):
            continue
        if _size_band(_parse_size_band(row.get("size_band"))) != size_band:
            continue
        if cohort_industry_filter and not _industry_matches(
            row.get("industry"), cohort_industry_filter,
        ):
            continue
        try:
            c = int(ps.get("composite") or 0)
        except (TypeError, ValueError):
            continue
        peer_composites.append(c)

    if not peer_composites:
        return {
            "percentile": 50,
            "cohort_size": 0,
            "cohort_label": _cohort_label(industry, size_band),
        }

    below = sum(1 for c in peer_composites if c < composite)
    pct = int(round(100 * below / len(peer_composites)))
    return {
        "percentile": pct,
        "cohort_size": len(peer_composites),
        "cohort_label": _cohort_label(industry, size_band),
    }


def _industry_cohort(industry: Optional[str]) -> Optional[str]:
    """Map a raw industry string into a broad cohort label used for
    peer filtering. Coarse on purpose — too narrow and most cohorts
    have <5 members and the percentile is meaningless."""
    if not industry:
        return None
    lower = industry.lower()
    if any(kw in lower for kw in ["bank", "insurance", "financial", "fintech", "payments"]):
        return "financial_services"
    if any(kw in lower for kw in ["health", "hospital", "medical", "pharma", "biotech"]):
        return "healthcare"
    if any(kw in lower for kw in ["retail", "ecommerce", "e-commerce", "consumer goods"]):
        return "retail"
    if any(kw in lower for kw in ["energy", "utilities", "oil", "gas"]):
        return "energy"
    if any(kw in lower for kw in ["government", "public sector", "federal", "defense", "defence"]):
        return "public_sector"
    if any(kw in lower for kw in ["software", "saas", "technology", "internet"]):
        return "software"
    if any(kw in lower for kw in ["manufacturing", "industrial", "automotive"]):
        return "manufacturing"
    return None


def _industry_matches(industry: Optional[str], cohort: str) -> bool:
    return _industry_cohort(industry) == cohort


def _cohort_label(industry: Optional[str], size_band: str) -> str:
    cohort = _industry_cohort(industry)
    name_map = {
        "financial_services": "financial services firms",
        "healthcare":          "healthcare organisations",
        "retail":              "retail companies",
        "energy":              "energy firms",
        "public_sector":       "public-sector organisations",
        "software":            "software companies",
        "manufacturing":       "manufacturers",
    }
    base = name_map.get(cohort, "accounts")
    size_prefix = {
        "tiny":  "small ",
        "small": "small ",
        "mid":   "mid-market ",
        "large": "large ",
        "enterprise": "enterprise ",
    }.get(size_band, "")
    return f"{size_prefix}{base} Vera tracks".strip()


def _size_band(employee_count: Optional[int]) -> str:
    n = employee_count or 0
    if n <= 50:    return "tiny"
    if n <= 200:   return "small"
    if n <= 1_000: return "mid"
    if n <= 5_000: return "large"
    return "enterprise"


# ---------------------------------------------------------------------------
# Delta vs prior compute
# ---------------------------------------------------------------------------
def _compute_delta(
    prior: Dict[str, Any],
    axis_scores: Dict[str, int],
    composite: int,
    now: datetime,
) -> Dict[str, int]:
    """Compute delta vs the prior stored score, IF that prior was
    computed >7 days ago (otherwise it's just churn from this same week
    and reads as ±0 anyway).
    """
    if not isinstance(prior, dict):
        return {axis: 0 for axis in ALL_AXES + ("composite",)}
    prior_ts = prior.get("computed_at")
    if not isinstance(prior_ts, str):
        return {axis: 0 for axis in ALL_AXES + ("composite",)}
    try:
        last = datetime.fromisoformat(prior_ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return {axis: 0 for axis in ALL_AXES + ("composite",)}
    if (now - last) < timedelta(days=7):
        # Too close to compare — return zeros so the dossier doesn't show
        # noisy intra-week wiggles.
        return {axis: 0 for axis in ALL_AXES + ("composite",)}
    out: Dict[str, int] = {}
    for axis in ALL_AXES:
        out[axis] = axis_scores.get(axis, 0) - int(prior.get(axis) or 0)
    out["composite"] = composite - int(prior.get("composite") or 0)
    return out


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def _fetch_company(db: Client, company_id: str) -> Optional[Dict[str, Any]]:
    try:
        res = (
            db.table("companies")
            .select(
                "id, domain, name, size_band, industry, "
                "security_team_profile, profile_score"
            )
            .eq("id", company_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.warning("_fetch_company failed for %s: %s", company_id, e)
        return None
    rows = res.data or []
    return rows[0] if rows else None


def _parse_size_band(raw: Any) -> Optional[int]:
    """Parse companies.size_band into an employee count.

    Two formats coexist in this column depending on which Apollo path
    populated the row:
      - Clean int as string  : "660"        -> 660
      - Range "lo,hi"        : "1001,5000"  -> 3000 (midpoint)
    Returns None when neither parses.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        pass
    if "," in s:
        parts = s.split(",")
        if len(parts) == 2:
            try:
                lo = int(parts[0].strip())
                hi = int(parts[1].strip())
                return (lo + hi) // 2
            except (TypeError, ValueError):
                return None
    return None


def _fetch_signals(
    db: Client,
    *,
    org_id: str,
    company_id: str,
    company_domain: Optional[str] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Pull every signal attributable to this company in this org.

    Critical: signals get written along TWO axes:
      - company_id   — written by enrich_company_only paths (PL news,
                       funding, CVEs, hiring, the social listener)
      - lead_id only — written by older per-lead enrichers (website
                       posture detector, person intent, etc.) BEFORE
                       the 2026-05-29 company_id column was added

    Skipping the lead-keyed path means the Posture recipe (largely
    fed by the website detector) shows 0 on every account — which
    is exactly what happened in the first backfill.

    We resolve lead_ids by joining leads on company_domain within
    the org, then OR the two paths in a single signals query.
    """
    # 1. lead_ids for this company in this org
    lead_ids: List[str] = []
    if company_domain:
        try:
            leads_res = (
                db.table("leads")
                .select("id")
                .eq("org_id", org_id)
                .ilike("company_domain", company_domain)
                .execute()
            )
            lead_ids = [
                row["id"] for row in (leads_res.data or [])
                if row.get("id")
            ]
        except Exception as e:
            logger.warning(
                "_fetch_signals lead lookup failed for %s/%s: %s",
                org_id, company_domain, e,
            )

    # 2. Signals query — UNION across company_id and lead_id paths
    try:
        q = (
            db.table("signals")
            .select("signal_type, detected_at, axis, signal_strength")
            .eq("org_id", org_id)
        )
        if lead_ids:
            joined = ",".join(lead_ids)
            q = q.or_(f"company_id.eq.{company_id},lead_id.in.({joined})")
        else:
            q = q.eq("company_id", company_id)
        res = (
            q.order("detected_at", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception as e:
        logger.warning("_fetch_signals failed for %s: %s", company_id, e)
        return []
    return res.data or []


def _store(db: Client, *, company_id: str, profile_score: Dict[str, Any]) -> None:
    try:
        db.table("companies").update(
            {"profile_score": profile_score}
        ).eq("id", company_id).execute()
    except Exception as e:
        logger.warning("Persist profile_score failed for %s: %s", company_id, e)


def _serialise(c: AxisContribution) -> Dict[str, Any]:
    return {"label": c.label, "points": c.points, "detail": c.detail}


def _humanise_signal_type(st: str) -> str:
    """Convert signal_type slug into a dossier-friendly label. Mirrors
    the lib/dashboard/labels.ts canonical map — kept in sync by hand
    because the engine doesn't import the TS file."""
    overrides = {
        "breach_proximity":              "Breach exposure",
        "social_breach_mention":         "Breach mention",
        "cve_in_stack":                  "Known CVE in stack",
        "bug_bounty_present":            "Bug bounty programme",
        "responsible_disclosure_present":"Responsible disclosure policy",
        "trust_center_present":          "Trust center",
        "security_page_present":         "Security page",
        "compliance_page_present":       "Compliance page",
        "status_page_present":           "Status page",
        "security_contact_present":      "Security contact",
        "subprocessors_present":         "Subprocessors disclosed",
        "dpa_present":                   "DPA published",
        "privacy_page_present":          "Privacy page",
        "person_posted_relevant_topic":  "Public security voice",
        "person_engaged_with_topic":     "Security topic engagement",
        "enterprise_trust_pressure":     "Enterprise trust pressure",
        "security_leadership_hire":      "Security leadership hire",
        "security_team_expansion":       "Security team expansion",
        "hiring_security_leadership":    "Security leadership hiring",
        "hiring_security":               "Security hiring",
        "hiring_grc":                    "GRC hiring",
        "social_hiring_buzz":            "Hiring chatter",
        "certification_claim_detected":  "Certification claim",
        "certification_iso_27001":       "ISO 27001 claimed",
        "certification_soc_2":           "SOC 2 claimed",
        "certification_pci_dss":         "PCI-DSS claimed",
        "certification_cyber_essentials": "Cyber Essentials claimed",
        "certification_cyber_essentials_plus": "Cyber Essentials Plus claimed",
        "certification_crest":           "CREST claimed",
        "certification_check":           "CHECK claimed",
        "enterprise_page_detected":      "Enterprise page",
        "api_or_developer_platform":     "API / developer platform",
        "enterprise_customer_detected":  "Enterprise customers",
        "regulated_customer_detected":   "Regulated customers",
        "person_security_leader":        "Security leader on staff",
        "person_grc_leader":             "GRC leader on staff",
        "security_team_scaling":         "Security team scaling",
        "cloud_security_scaling":        "Cloud security scaling",
        "compliance_operationalisation": "Compliance operationalisation",
        "recently_funded":               "Recently funded",
        "social_funding_news":           "Funding news",
        "recent_acquisition":            "Acquisition activity",
        "new_leadership":                "Leadership change",
        "social_leadership_change":      "Leadership change",
        "social_milestone":              "Company milestone",
        "expansion":                     "Market expansion",
        "social_vendor_relationship":    "Vendor relationship",
        "person_engaged_with_competitor":"Engaged with competitor",
        "person_commercial_leader":      "Commercial leader on staff",
        "revenue_blocker_likely":        "Revenue blocker likely",
    }
    if st in overrides:
        return overrides[st]
    return st.replace("_", " ").capitalize()
