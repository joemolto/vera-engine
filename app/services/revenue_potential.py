"""Revenue potential — deterministic account commercial-upside estimate.

This is an *estimate of commercial upside*, never a forecast or a promise.
The number is computed deterministically from the client's own deal
economics times account-fit multipliers; Claude is NOT used here. Prose
explanation (and any per-client narrative refinement) happens later at
briefing time using the structured output of this module.

Design rules (from the engine brief):
  - Deterministic calculation; client average deal size is the baseline.
  - Multipliers/caps on size, fit, urgency, seniority, cyber relevance,
    complexity; a confidence discount for weak data.
  - No fake precision — estimates are rounded to sensible figures.
  - Weak inputs => broad range + low confidence.
  - No client `average_deal_size` => safe "unknown" result, never a guess.
  - Never imply guaranteed revenue.

Output shape:
    {
      estimated_revenue_potential, range_low, range_high, currency,
      confidence, deal_size_band, expansion_potential,
      sales_cycle_complexity, why_this_value[], assumptions[],
      recommended_commercial_angle
    }
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Multiplier clamps — no single dimension may dominate the estimate.
_MULT_FLOOR = 0.5
_MULT_CEIL = 3.0

_SENIOR_C = ("ciso", "cso", "cto", "ceo", "cfo", "cio", "chief", "founder", "owner", "president")
_SENIOR_VP = ("vp", "vice president", "svp", "evp", "head of")
_SENIOR_DIR = ("director", "principal")
_SENIOR_MGR = ("manager", "lead")

_REGULATED = (
    "financial", "fintech", "bank", "insurance", "health", "healthcare",
    "pharma", "government", "defense", "defence", "energy", "utilities",
    "legal", "telecom",
)

_CYBER_FINDING_TYPES = (
    "cve_in_stack", "breach_proximity", "compliance_gap",
    "security_team_expansion", "security_leadership_hire",
)
_GROWTH_SIGNALS = (
    "recent_funding", "recently_funded", "recent_acquisition",
    "expansion", "hiring", "hiring_sales_bd", "new_leadership",
)


def _clamp(v: float, lo: float = _MULT_FLOOR, hi: float = _MULT_CEIL) -> float:
    return max(lo, min(hi, v))


def _parse_employee_count(company_size: Any) -> Optional[int]:
    """company_size may be an int-ish string ("450"), a range ("200-500"),
    a bucket ("201-500 employees"), or None. Return a representative int.
    """
    if company_size is None:
        return None
    s = str(company_size).strip().lower().replace(",", "")
    if not s:
        return None
    nums = [int(n) for n in re.findall(r"\d+", s)]
    if not nums:
        return None
    if len(nums) >= 2:
        return (nums[0] + nums[1]) // 2  # midpoint of a range
    return nums[0]


def _size_multiplier(emp: Optional[int], enterprise_threshold: Optional[int]) -> Tuple[float, str]:
    if emp is None:
        return 1.0, "Company size unknown (no size adjustment)"
    if enterprise_threshold and emp >= enterprise_threshold:
        return 2.2, f"Enterprise account (~{emp} employees, ≥ client enterprise threshold)"
    if emp < 10:
        return 0.6, f"Very small company (~{emp} employees)"
    if emp < 50:
        return 0.8, f"Small company (~{emp} employees)"
    if emp < 200:
        return 1.0, f"Mid-size company (~{emp} employees)"
    if emp < 1000:
        return 1.4, f"Large company (~{emp} employees)"
    if emp < 5000:
        return 1.9, f"Major enterprise (~{emp} employees)"
    return 2.5, f"Global enterprise (~{emp} employees)"


def _revenue_multiplier(annual_revenue: Optional[float]) -> Tuple[float, Optional[str]]:
    """Budget-capacity signal from the account's own annual revenue. Kept
    modest (and independent of headcount) so it refines the estimate
    without double-counting size — a lean fintech can carry far more
    revenue per head than a labour-heavy firm, so revenue adds real
    signal about how much they can actually spend.
    """
    if annual_revenue is None or annual_revenue <= 0:
        return 1.0, None
    r = annual_revenue
    if r < 10_000_000:
        return 0.9, f"Smaller revenue base (~{_money_short(r)})"
    if r < 50_000_000:
        return 1.0, None
    if r < 250_000_000:
        return 1.15, f"Substantial revenue base (~{_money_short(r)})"
    if r < 1_000_000_000:
        return 1.3, f"Large revenue base (~{_money_short(r)})"
    if r < 10_000_000_000:
        return 1.45, f"Major revenue base (~{_money_short(r)})"
    return 1.6, f"Enterprise-scale revenue (~{_money_short(r)})"


def _money_short(n: float) -> str:
    if n >= 1_000_000_000:
        return f"${n / 1_000_000_000:.1f}B".replace(".0B", "B")
    if n >= 1_000_000:
        return f"${n / 1_000_000:.0f}M"
    return f"${n / 1_000:.0f}k"


def _industry_multiplier(
    industry: str, high_value: List[str], low_value: List[str]
) -> Tuple[float, Optional[str]]:
    ind = (industry or "").lower()
    if ind:
        for seg in high_value:
            if seg and seg.lower() in ind:
                return 1.3, f"High-value segment for this client ({industry})"
        for seg in low_value:
            if seg and seg.lower() in ind:
                return 0.7, f"Lower-value segment for this client ({industry})"
        if any(k in ind for k in _REGULATED):
            return 1.15, f"Regulated industry ({industry})"
    return 1.0, None


def _seniority_multiplier(title: str) -> Tuple[float, Optional[str]]:
    t = (title or "").lower()
    if not t:
        return 1.0, None
    if any(k in t for k in _SENIOR_C):
        return 1.25, "Executive-level contact (budget owner)"
    if any(k in t for k in _SENIOR_VP):
        return 1.15, "VP / Head-of contact (senior buyer)"
    if any(k in t for k in _SENIOR_DIR):
        return 1.08, "Director-level contact"
    if any(k in t for k in _SENIOR_MGR):
        return 1.0, "Manager-level contact"
    return 0.9, "Individual-contributor contact (limited budget authority)"


def _urgency_multiplier(active: List[str]) -> Tuple[float, Optional[str]]:
    growth_hits = [s for s in active if s in _GROWTH_SIGNALS]
    n = len(set(growth_hits))
    if n == 0:
        return 1.0, None
    mult = min(1.4, 1.0 + 0.12 * n)
    return mult, f"{n} active growth/timing signal(s) — live buying window"


def _cyber_relevance_multiplier(finding_types: List[str]) -> Tuple[float, Optional[str]]:
    hits = sorted({f for f in finding_types if f in _CYBER_FINDING_TYPES})
    if not hits:
        return 1.0, None
    mult = min(1.35, 1.0 + 0.09 * len(hits))
    pretty = ", ".join(h.replace("_", " ") for h in hits)
    return mult, f"Strong cyber relevance ({pretty})"


def _complexity(pricing_model: str, emp: Optional[int]) -> str:
    pm = (pricing_model or "").lower()
    big = emp is not None and emp >= 1000
    if pm in ("platform", "enterprise", "retainer") or big:
        return "complex"
    if pm in ("project", "service", "usage"):
        return "moderate"
    return "simple" if pm in ("per-seat", "per_seat", "self-serve") else "moderate"


def _round_money(v: float) -> int:
    """Sensible rounding — no fake precision. Larger numbers round coarser."""
    if v <= 0:
        return 0
    if v < 10_000:
        return int(round(v / 500.0) * 500)
    if v < 100_000:
        return int(round(v / 1_000.0) * 1_000)
    if v < 1_000_000:
        return int(round(v / 5_000.0) * 5_000)
    return int(round(v / 25_000.0) * 25_000)


def _deal_size_band(estimate: float, baseline: float) -> str:
    """Relative to the client's own baseline, not absolute money."""
    if baseline <= 0:
        return "unknown"
    r = estimate / baseline
    if r < 0.75:
        return "below_baseline"
    if r < 1.5:
        return "baseline"
    if r < 3.0:
        return "mid_market"
    return "enterprise"


def _unknown_result(currency: str, reason: str) -> Dict[str, Any]:
    return {
        "estimated_revenue_potential": None,
        "range_low": None,
        "range_high": None,
        "currency": currency,
        "confidence": "unknown",
        "deal_size_band": "unknown",
        "expansion_potential": "unknown",
        "sales_cycle_complexity": "unknown",
        "why_this_value": [],
        "assumptions": [reason],
        "recommended_commercial_angle": (
            "Insufficient commercial baseline to estimate account value. "
            "Qualify deal economics before prioritising on revenue."
        ),
    }


def estimate_revenue_potential(
    *,
    lead: Optional[Dict[str, Any]] = None,
    client_ctx: Optional[Dict[str, Any]] = None,
    signals: Optional[Dict[str, bool]] = None,
    findings: Optional[List[Dict[str, Any]]] = None,
    scoring_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Estimate an account's commercial upside. Pure, deterministic, no LLM.

    Returns the documented output shape. Never raises — on bad/empty input
    it returns a safe "unknown" result so callers can stay best-effort.
    """
    lead = lead or {}
    client_ctx = client_ctx or {}
    signals = signals or {}
    findings = findings or []
    scoring_data = scoring_data or {}

    currency = (client_ctx.get("currency") or "GBP").upper()

    try:
        baseline_raw = client_ctx.get("average_deal_size")
        baseline = float(baseline_raw) if baseline_raw not in (None, "", 0) else 0.0
    except (TypeError, ValueError):
        baseline = 0.0

    # Guardrail: no baseline => no guess.
    if baseline <= 0:
        return _unknown_result(
            currency,
            "No client average_deal_size configured — revenue potential not estimated.",
        )

    def _num(key: str) -> Optional[float]:
        try:
            v = client_ctx.get(key)
            return float(v) if v not in (None, "", 0) else None
        except (TypeError, ValueError):
            return None

    min_deal = _num("min_deal_size")
    max_deal = _num("max_deal_size")
    try:
        enterprise_threshold = int(client_ctx.get("enterprise_threshold_employee_count")) \
            if client_ctx.get("enterprise_threshold_employee_count") else None
    except (TypeError, ValueError):
        enterprise_threshold = None
    pricing_model = client_ctx.get("pricing_model") or ""
    high_value = client_ctx.get("high_value_segments") or []
    low_value = client_ctx.get("low_value_segments") or []
    if isinstance(high_value, str):
        high_value = [s.strip() for s in high_value.split(",") if s.strip()]
    if isinstance(low_value, str):
        low_value = [s.strip() for s in low_value.split(",") if s.strip()]

    emp = _parse_employee_count(lead.get("company_size"))
    industry = lead.get("company_industry") or ""
    title = lead.get("title") or ""
    try:
        _rev_raw = lead.get("annual_revenue")
        annual_revenue = float(_rev_raw) if _rev_raw not in (None, "", 0) else None
    except (TypeError, ValueError):
        annual_revenue = None
    active_signals = [k for k, v in signals.items() if v]
    finding_types = [f.get("signal_type") or f.get("type") for f in findings if isinstance(f, dict)]
    finding_types = [t for t in finding_types if t]

    # --- Multipliers ---
    why: List[str] = []
    size_m, size_reason = _size_multiplier(emp, enterprise_threshold)
    if size_reason:
        why.append(size_reason)

    ind_m, ind_reason = _industry_multiplier(industry, high_value, low_value)
    if ind_reason:
        why.append(ind_reason)

    sen_m, sen_reason = _seniority_multiplier(title)
    if sen_reason:
        why.append(sen_reason)

    urg_m, urg_reason = _urgency_multiplier(active_signals)
    if urg_reason:
        why.append(urg_reason)

    cyb_m, cyb_reason = _cyber_relevance_multiplier(finding_types)
    if cyb_reason:
        why.append(cyb_reason)

    rev_m, rev_reason = _revenue_multiplier(annual_revenue)
    if rev_reason:
        why.append(rev_reason)

    combined = (
        _clamp(size_m) * _clamp(ind_m) * _clamp(sen_m)
        * _clamp(urg_m) * _clamp(cyb_m) * _clamp(rev_m)
    )

    # --- Confidence: how much real input did we actually have? ---
    evidence_pts = sum([
        emp is not None,
        annual_revenue is not None,
        bool(industry),
        bool(title),
        bool(active_signals),
        bool(finding_types),
        (scoring_data.get("lead_rating") in ("hot", "warm")),
    ])
    if evidence_pts >= 5:
        confidence = "high"
        spread = 0.35
        conf_discount = 1.0
    elif evidence_pts >= 3:
        confidence = "medium"
        spread = 0.55
        conf_discount = 0.95
    else:
        confidence = "low"
        spread = 0.8
        conf_discount = 0.85
        why.append("Limited account data — estimate is indicative only")

    raw_estimate = baseline * combined * conf_discount

    # Respect client min/max caps if configured.
    capped = raw_estimate
    if min_deal is not None:
        capped = max(capped, min_deal)
    if max_deal is not None:
        capped = min(capped, max_deal)

    estimate = _round_money(capped)
    range_low = _round_money(max(min_deal or 0, capped * (1 - spread)))
    range_high = _round_money(
        min(max_deal, capped * (1 + spread)) if max_deal is not None
        else capped * (1 + spread)
    )
    if range_low > estimate:
        range_low = estimate
    if range_high < estimate:
        range_high = estimate

    band = _deal_size_band(capped, baseline)
    complexity = _complexity(pricing_model, emp)

    # Expansion potential — bigger orgs with multiple live signals expand more.
    expansion_signals = len(set(finding_types)) + len(
        [s for s in active_signals if s in _GROWTH_SIGNALS]
    )
    if (emp or 0) >= 1000 and expansion_signals >= 3:
        expansion = "high"
    elif expansion_signals >= 2:
        expansion = "moderate"
    else:
        expansion = "low"

    assumptions = [
        f"Baseline = client average deal size ({currency} {int(baseline):,})",
        "No confirmed budget data — estimate from account fit, not a quote",
    ]
    if emp is None:
        assumptions.append("Company size unavailable — no size adjustment applied")
    if min_deal is not None or max_deal is not None:
        assumptions.append("Client min/max deal-size caps applied")

    angle = _commercial_angle(band, finding_types, active_signals, complexity, currency, estimate, range_low, range_high)

    return {
        "estimated_revenue_potential": estimate,
        "range_low": range_low,
        "range_high": range_high,
        "currency": currency,
        "confidence": confidence,
        "deal_size_band": band,
        "expansion_potential": expansion,
        "sales_cycle_complexity": complexity,
        "why_this_value": why or ["Client baseline with no account-specific adjustments"],
        "assumptions": assumptions,
        "recommended_commercial_angle": angle,
    }


def _commercial_angle(
    band: str,
    finding_types: List[str],
    active_signals: List[str],
    complexity: str,
    currency: str,
    estimate: int,
    low: int,
    high: int,
) -> str:
    """Deterministic baseline angle. Briefing (Phase 6) may refine the prose
    with Claude, but the structured number/range is never invented by an LLM.
    """
    has_compliance = "compliance_gap" in finding_types
    has_team = "security_team_expansion" in finding_types
    strategic = band in ("mid_market", "enterprise") or complexity == "complex"

    head = f"Estimated upside {currency} {low:,}–{high:,} (point {currency} {estimate:,})."
    if strategic and (has_compliance or has_team):
        body = (
            " Treat as a strategic security-maturity / compliance-readiness "
            "conversation with multiple stakeholders, not a tactical tool sale."
        )
    elif strategic:
        body = (
            " Larger, multi-stakeholder opportunity — lead with business "
            "outcomes and expect a considered buying process."
        )
    else:
        body = (
            " Right-size the approach to a focused, single-owner deal; keep "
            "the motion lightweight and value-led."
        )
    return head + body
