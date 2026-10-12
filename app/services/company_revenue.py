"""Deterministic multi-provider company-revenue reconciliation.

The prospect company's own annual revenue, blended from the providers
that report a real figure — NEVER an LLM guess. "Educated" comes from
agreeing real numbers + a confidence derived from how well they agree,
not from a model inventing one.

Providers
---------
- Apollo  : real numeric `annual_revenue` (USD).
- Perplexity / Sonar : free-text `estimated_revenue` ("$70M", "$50-70m",
  "Not found"). Parsed deterministically by regex — no model in the loop.
- Companies House : a coarse accounts BRACKET (UK only), e.g.
  "£10.2m - £36m". It is a GUARDRAIL ONLY (decision 1A) — it never
  becomes the displayed number and is never averaged in. It can only
  corroborate the consensus or flag a conflict (which forces a cautious
  range + low confidence).

Honesty rules
-------------
- Zero numeric providers -> None -> the column is NULL -> UI shows "—".
  Companies House alone never produces a number.
- Providers disagree materially -> show a RANGE + low confidence, never a
  falsely-precise single figure.
- Every provider's raw value is retained in `sources` — that visible
  audit trail is the no-hallucination guarantee.
- No FX conversion is invented. Apollo is USD and anchors the figure;
  the Companies House band is GBP and is used only as a coarse
  order-of-magnitude guardrail (generous tolerance absorbs FX).
"""

from __future__ import annotations

import logging
import re
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Tokens that mean "no figure" — treated as missing, not parsed.
_NO_VALUE = {
    "", "not found", "notfound", "unknown", "n/a", "na", "none", "-", "—",
    "private", "not disclosed", "undisclosed", "not available", "not public",
}

# Agreement tolerance: providers within ~30% of each other (high/low ratio
# <= 1.30) are "agreeing" -> show a point. Wider -> show a range.
_AGREE_RATIO = 1.30

# Companies House band is a coarse GBP bracket; widen it generously before
# the guardrail comparison so currency (≈1.25 GBP/USD) and bracket
# coarseness don't trigger false conflicts. Only an order-of-magnitude
# disagreement should flag.
_CH_LOW_MULT = 0.5
_CH_HIGH_MULT = 2.0

_SUFFIX = {
    "k": 1_000, "thousand": 1_000,
    "m": 1_000_000, "mm": 1_000_000, "mn": 1_000_000, "million": 1_000_000,
    "b": 1_000_000_000, "bn": 1_000_000_000, "billion": 1_000_000_000,
}

_SFX_ALT = r"(billion|million|thousand|mm|mn|bn|k|m|b)"
# A - B (optionally each with a scale word; low end may inherit the
# high end's scale, e.g. "$50-70m").
_RANGE_RE = re.compile(
    r"(\d+\.?\d*)\s*" + _SFX_ALT + r"?\s*(?:-|–|—|to)\s*(\d+\.?\d*)\s*" + _SFX_ALT + r"?",
    re.IGNORECASE,
)
_NUM_RE = re.compile(r"(\d+\.?\d*)\s*" + _SFX_ALT + r"?", re.IGNORECASE)


def _scale(num: str, sfx: str) -> Optional[float]:
    """A numeric token + optional scale word -> absolute value. Returns
    None for an unscaled value below 100k (a bare 2015 / 70 is not a
    credible revenue figure — don't invent a magnitude)."""
    try:
        n = float(num)
    except (TypeError, ValueError):
        return None
    if sfx:
        n *= _SUFFIX[sfx.lower()]
    elif n < 100_000:
        return None
    return n if n > 0 else None


def is_uk_company(country: str = "", location: str = "", domain: str = "") -> bool:
    """Gate the Companies House lookup to UK companies — a name search on
    CH for a non-UK firm returns junk or a wrong match."""
    blob = " ".join([country or "", location or ""]).lower()
    uk_markers = (
        "united kingdom", "great britain", " uk", "uk ", "u.k", "england",
        "scotland", "wales", "northern ireland", "britain", "gbr",
    )
    if country and country.strip().lower() in {"uk", "gb", "gbr", "united kingdom"}:
        return True
    if any(m in blob for m in uk_markers):
        return True
    d = (domain or "").strip().lower()
    return d.endswith(".co.uk") or d.endswith(".uk") or d.endswith(".org.uk")


def parse_money_text(text: Any) -> Optional[Tuple[float, float]]:
    """Deterministically parse a free-text revenue string into a
    (low, high) absolute-USD-ish tuple, or None when there's no usable
    figure. Conservative: when ambiguous, returns None rather than guess.

    "$70M" -> (7e7, 7e7); "$50-70m" -> (5e7, 7e7);
    "Not found" -> None; "70" -> None (no scale, not credible as revenue).
    """
    if not isinstance(text, str):
        return None
    s = text.strip().lower()
    if s in _NO_VALUE:
        return None
    # Drop currency/grouping noise so the numeric grammar is clean.
    s = re.sub(r"[,$£€]", "", s)

    # Range first: "50-70m", "$50m to $70m", "50 to 70 million". The low
    # end inherits the high end's scale word when it has none of its own.
    rm = _RANGE_RE.search(s)
    if rm:
        a, sa, b, sb = rm.group(1), rm.group(2), rm.group(3), rm.group(4)
        sa = sa or sb
        sb = sb or sa
        lo, hi = _scale(a, sa), _scale(b, sb)
        vals = [v for v in (lo, hi) if v is not None]
        if vals:
            return (min(vals), max(vals))

    # Otherwise take every credible single figure.
    vals = []
    for raw_num, suffix in _NUM_RE.findall(s):
        v = _scale(raw_num, suffix)
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    return (min(vals), max(vals))


def _bracket_to_band(bracket: Any) -> Optional[Tuple[float, float]]:
    """Companies House accounts bracket -> (low_gbp, high_gbp). high is a
    large sentinel for the open-ended top band. None when unmappable."""
    if not isinstance(bracket, str):
        return None
    b = bracket.strip().lower()
    table = {
        "under £632k": (0.0, 632_000.0),
        "£632k - £10.2m": (632_000.0, 10_200_000.0),
        "£10.2m - £36m": (10_200_000.0, 36_000_000.0),
        "over £36m": (36_000_000.0, 5_000_000_000.0),
        "dormant": (0.0, 0.0),
    }
    return table.get(b)


def _fmt(n: float) -> str:
    n = float(n)
    if n >= 1_000_000_000:
        return "$" + f"{n / 1_000_000_000:.1f}".rstrip("0").rstrip(".") + "B"
    if n >= 1_000_000:
        return "$" + f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"${round(n / 1_000)}k"
    return f"${round(n)}"


def _coerce_amount(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    if isinstance(v, str):
        parsed = parse_money_text(v)
        return parsed[0] if parsed else None
    return None


def reconcile(
    apollo_amount: Any = None,
    perplexity_text: Any = None,
    companies_house_bracket: Any = None,
) -> Optional[Dict[str, Any]]:
    """Blend the providers into one honest figure. See module docstring.

    Returns the compact JSONB written to leads.company_revenue, or None
    when no provider reported a real number (column stays NULL -> "—").
    """
    sources: List[Dict[str, Any]] = []
    points: List[float] = []          # one representative point per provider
    endpoints: List[float] = []       # every numeric endpoint, for the range

    apollo = _coerce_amount(apollo_amount)
    if apollo is not None:
        points.append(apollo)
        endpoints.append(apollo)
        sources.append({"provider": "apollo", "value": apollo})

    p_range = parse_money_text(perplexity_text)
    if p_range is not None:
        p_lo, p_hi = p_range
        points.append((p_lo + p_hi) / 2.0)
        endpoints.extend([p_lo, p_hi])
        sources.append({
            "provider": "perplexity",
            "text": str(perplexity_text).strip(),
            "parsed_low": p_lo,
            "parsed_high": p_hi,
        })

    # Decision 1A: Companies House never produces the number on its own.
    if not points:
        return None

    low = min(endpoints)
    high = max(endpoints)
    consensus = float(median(points))

    ratio = (high / low) if low > 0 else float("inf")
    agree = ratio <= _AGREE_RATIO
    confidence = "high" if agree else "low"

    band = _bracket_to_band(companies_house_bracket)
    corroborated = False
    conflict = False
    if band is not None:
        b_lo, b_hi = band
        tol_lo = b_lo * _CH_LOW_MULT
        tol_hi = (b_hi * _CH_HIGH_MULT) if b_hi > 0 else 0.0
        # Overlap of [low, high] with the tolerance-widened CH band.
        if tol_hi > 0 and high >= tol_lo and low <= tol_hi:
            corroborated = True
        else:
            conflict = True
            confidence = "low"  # filed accounts contradict the figure

    point_display = agree and not conflict
    if point_display:
        printed = _fmt(consensus)
    else:
        printed = (
            _fmt(low) if low == high else f"{_fmt(low)}–{_fmt(high)}"
        )

    return {
        "amount": consensus if point_display else None,
        "low": low,
        "high": high,
        "currency": "USD",
        "printed": printed,
        "confidence": confidence,
        "basis": "reconciled",
        "sources": sources,
        "companies_house_band": (
            str(companies_house_bracket) if band is not None else None
        ),
        "corroborated_by_companies_house": corroborated,
        "companies_house_conflict": conflict,
    }
