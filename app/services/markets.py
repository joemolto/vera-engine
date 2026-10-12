"""Markets — first-class market records (multi-ICP). Added 2026-08-23.

An org has had ONE implicit market: targeting derived from icp_config
(personas[0]) blended with the observed watchlist. The `markets` table makes that
a row and allows extra markets behind
icp_config.features.multi_market (e.g. selling to end customers and
to partners as separate markets).

Compatibility contract — what keeps single-market orgs byte-identical:
- market_id NULL on watchlist rows and snapshots means "primary market".
- A primary market's definition is empty by default, which means "derive
  targeting live from icp_config" — persona edits keep working exactly as
  before. A definition saved from the market editor overrides it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Markets allowed per org by organisations.tier — same precedent as
# PERSONA_LIMIT_BY_TIER (personas.py). Packaging (2026-08-23):
# Starter 1 / Individual (solo family) 3 / Team custom via a future
# override column when the first deal needs it.
MARKETS_LIMIT_BY_TIER = {"starter": 1}
MARKETS_LIMIT_DEFAULT = 3

_FIELDS = "id, org_id, name, is_primary, active, definition, avg_deal_size, currency, created_at"

_DEFINITION_KEYS = ("target_industries", "target_locations", "target_company_size")


def markets_limit(tier: Optional[str]) -> int:
    from app.config import settings
    if settings.SELF_HOSTED:
        return 1_000_000
    return MARKETS_LIMIT_BY_TIER.get((tier or "").lower(), MARKETS_LIMIT_DEFAULT)


def clean_definition(raw: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Keep only the targeting keys, as trimmed string lists."""
    out: Dict[str, List[str]] = {}
    for key in _DEFINITION_KEYS:
        vals = (raw or {}).get(key) or []
        if isinstance(vals, list):
            cleaned = [str(v).strip() for v in vals if str(v).strip()]
            if cleaned:
                out[key] = cleaned[:20]
    return out


def _select_primary(db, org_id: str) -> Optional[Dict[str, Any]]:
    rows = (db.table("markets").select(_FIELDS).eq("org_id", org_id)
            .eq("is_primary", True).order("created_at").limit(1).execute().data or [])
    return rows[0] if rows else None


def _create_primary(db, org_id: str) -> Optional[Dict[str, Any]]:
    try:
        ins = db.table("markets").insert({
            "org_id": org_id, "name": "Your market", "is_primary": True,
        }).execute()
        return (ins.data or [None])[0]
    except Exception as e:  # noqa: BLE001
        # Usually: lost the race to a concurrent lazy-create — the page's
        # markets-list and overview calls land together, and the unique
        # index makes the second insert conflict (an org got two "Your
        # market" pills before it existed). The row is there now; serve it.
        existing = _select_primary(db, org_id)
        if existing:
            return existing
        logger.warning("primary market create failed for %s: %s", org_id, e)
        return None


def list_markets(db, org_id: str) -> List[Dict[str, Any]]:
    """All markets for the org, primary first. Creates the primary row on
    first read so every org always has one."""
    rows = (db.table("markets").select(_FIELDS).eq("org_id", org_id)
            .order("is_primary", desc=True).order("created_at").execute().data or [])
    if not any(r.get("is_primary") for r in rows):
        primary = _create_primary(db, org_id)
        if primary:
            rows.insert(0, primary)
    return rows


def get_market(db, org_id: str, market_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Resolve one market. No id → the primary (created if missing).
    Unknown or foreign ids → None — callers treat that as a bad request,
    never fall through to another org's market."""
    if market_id:
        rows = (db.table("markets").select(_FIELDS).eq("org_id", org_id)
                .eq("id", market_id).limit(1).execute().data or [])
        return rows[0] if rows else None
    return _select_primary(db, org_id) or _create_primary(db, org_id)


def market_icp_like(market: Optional[Dict[str, Any]], icp_config: Dict[str, Any]) -> Dict[str, Any]:
    """The icp_config-shaped dict market_criteria reads. A primary market
    with an empty definition → the real icp_config (legacy behaviour).
    Any saved definition → a synthesized personas[0] from it."""
    definition = (market or {}).get("definition") or {}
    if not any(definition.get(k) for k in _DEFINITION_KEYS):
        return icp_config
    like = dict(icp_config)
    like["personas"] = [{
        "target_industries": definition.get("target_industries") or [],
        "target_locations": definition.get("target_locations") or [],
        "target_company_size": definition.get("target_company_size") or [],
    }]
    return like


def market_deal_context(market: Optional[Dict[str, Any]], icp_config: Dict[str, Any]) -> Dict[str, Any]:
    """Deal economics for the value tile: the market's own numbers when
    set, else the org's client_context (existing behaviour)."""
    ctx = dict(icp_config.get("client_context") or {})
    if market and market.get("avg_deal_size"):
        ctx["average_deal_size"] = market["avg_deal_size"]
        if market.get("currency"):
            ctx["currency"] = market["currency"]
    return ctx


_PROPOSE_BANDS = ["11,50", "51,200", "201,500", "501,1000", "1001,5000",
                  "5001,10000", "10001,100000"]


def propose_market(instruction: str, draft: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Plain-English market description → a proposed definition plus
    adjacent-industry suggestions, each grounded with a LIVE Apollo count
    (free searches) before it is shown — the numbers are real, never model
    guesses. Vera proposes, the user corrects (the same contract as the
    onboarding signal step: "smart, not the user ticking
    boxes"). The adjacency suggestions exist because the radar's
    positive-identification gate filters on a company's PRIMARY industry
    label — a user who types "retail" silently loses companies whose
    primary label is "apparel & fashion" (seen on a UK retailers
    market, hiring slice 73 matched / 0 shown).

    Fail-open: any AI failure returns {"error": "parse_failed"} and the
    drawer stays a manual form.
    """
    from app.config import settings
    from app.services.claude import call_claude
    from app.services.market_radar import _GENERIC_INDUSTRY_TAGS, universe_count

    current = clean_definition(draft)
    system = (
        "You turn a seller's plain-English description of a target market "
        "into structured targeting for a B2B sales-intelligence product. "
        "Reply with ONLY a JSON object, no prose. Industry labels must be "
        "lowercase LinkedIn-taxonomy labels as companies carry them (e.g. "
        "'retail', 'consumer goods', 'apparel & fashion', 'financial "
        "services', 'insurance', 'hospital & health care'). Never invent "
        "niche labels. Never use generic catch-all labels (information "
        "technology & services, computer software, internet, information "
        "services, management consulting) — they match half the economy; "
        "prefer the specific label (for fintech use 'financial technology' "
        "and 'financial services', not an IT label)."
    )
    prompt = (
        f"MARKET DESCRIPTION (from the user):\n{instruction.strip()}\n\n"
        f"CURRENT DRAFT (preserve anything the description doesn't change):\n"
        f"{current or 'none'}\n\n"
        f"ALLOWED EMPLOYEE BANDS (pick every band that covers the user's "
        f"intent): {_PROPOSE_BANDS}\n\n"
        "Return a JSON object with exactly these keys:\n"
        '  "name": 2-4 word market name (e.g. "UK retailers"),\n'
        '  "industries": the CORE industry labels the user explicitly '
        "asked for,\n"
        '  "locations": country names,\n'
        '  "employee_ranges": from the allowed bands only,\n'
        '  "additions": 2-4 ADJACENT industry labels that companies in '
        "this market often carry as their primary label instead of the "
        'core one, each {"industry": ..., "why": one short plain sentence '
        "addressed to the user}. Never repeat a core label.\n"
        '  "message": one plain factual sentence summarising what you set '
        "up. No exclamation marks. No em dashes."
    )
    result = call_claude(prompt, system=system, model=settings.CLAUDE_MODEL)
    if not isinstance(result, dict) or result.get("error"):
        logger.warning("propose_market Claude call failed: %s", result)
        return {"error": "parse_failed"}

    # Strip generic catch-all tags (found live by the 2026-08-23 four-persona
    # sim: "UK fintech" parsed to financial services + information technology
    # & services → a 79k count the saved market would never reproduce,
    # because market_criteria drops generic tags at compute time). The
    # propose-time number must match the market the user actually gets.
    core_labels = [
        str(x).strip().lower() for x in (result.get("industries") or [])
        if str(x).strip().lower() and str(x).strip().lower() not in _GENERIC_INDUSTRY_TAGS
    ]
    definition = clean_definition({
        "target_industries": core_labels,
        "target_locations": result.get("locations") or [],
        "target_company_size": [
            b for b in (result.get("employee_ranges") or []) if b in _PROPOSE_BANDS
        ],
    })
    if not definition.get("target_industries"):
        # Everything the user asked for is a generic tag the radar refuses
        # to track (it would match half the economy) — better an honest
        # "too broad" than a market that computes to nothing.
        return {"error": "too_generic"}

    criteria = {
        "industries": definition.get("target_industries") or [],
        "locations": definition.get("target_locations") or [],
        "employee_ranges": definition.get("target_company_size") or [],
    }
    total = universe_count(criteria)

    # Ground every suggested adjacency with a real count under the same
    # geo/size filters; a label Apollo doesn't recognise counts 0 and is
    # silently dropped — Vera never suggests an industry that adds nothing.
    additions = []
    core = set(criteria["industries"])
    for a in (result.get("additions") or [])[:5]:
        label = str((a or {}).get("industry") or "").strip().lower()
        why = str((a or {}).get("why") or "").strip()
        if not label or label in core or label in _GENERIC_INDUSTRY_TAGS:
            continue
        n = universe_count({**criteria, "industries": [label]})
        if n > 0:
            additions.append({"industry": label, "why": why, "count": n})
        if len(additions) >= 4:
            break

    return {
        "name": str(result.get("name") or "").strip()[:60],
        "definition": definition,
        "total": total,
        "additions": additions,
        "message": str(result.get("message") or "").strip(),
    }
