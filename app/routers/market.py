"""Your market — TAM + market radar endpoints.

Gated per-org on icp_config.features.market_v1: endpoints answer
{"enabled": false} for orgs without the flag so the dashboard can hide the
page without a special error path. Mounted under /api/v1/accounts so the
clara-api proxy path is /accounts/market/*.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from app.database import get_supabase
from app.services.api_auth import ApiContext, verify_api_key
from app.services.market_radar import (
    compute_overview,
    drop_watched_rows,
    latest_snapshot,
    topup_slice,
)
from app.services.markets import (
    clean_definition,
    get_market,
    list_markets,
    market_deal_context,
    markets_limit,
)
from app.services.org_features import feature_enabled
from app.services.plan_limits import org_lapsed

logger = logging.getLogger(__name__)

router = APIRouter(tags=["market"])


def _org_icp(db, org_id: str) -> dict:
    row = (db.table("organisations").select("icp_config")
           .eq("id", org_id).maybe_single().execute().data or {})
    return row.get("icp_config") or {}


def _market_enabled(icp_config: dict) -> bool:
    return bool((icp_config.get("features") or {}).get("market_v1"))


def _apply_current_value(payload: dict, icp_config: dict, market: dict | None = None) -> dict:
    """Re-derive the value tile from the CURRENT deal economics when serving
    a snapshot. The snapshot bakes value at compute time, so a Settings
    change to average_deal_size/currency would otherwise show stale money
    until the next weekly check (spotted 2026-08-16). With markets, a
    market's own deal economics win over the org's."""
    ctx = market_deal_context(market, icp_config)
    try:
        avg = int(ctx.get("average_deal_size") or 0)
    except (TypeError, ValueError):
        avg = 0
    est_clean = (payload.get("universe") or {}).get("est_clean")
    payload["value"] = {
        "estimated_annual": avg * est_clean if avg and est_clean else None,
        "avg_deal_size": avg or None,
        "currency": ctx.get("currency") or None,
    }
    return payload


class MarketOverviewRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    refresh: bool = False
    # poll=true: never compute synchronously — return pending until a
    # snapshot exists. The onboarding finale polls while the background
    # compute (kicked at wizard submit) runs.
    poll: bool = False
    # Empty = the org's primary market (pre-054 behaviour).
    market_id: str = ""


@router.post("/market/overview")
def market_overview(req: MarketOverviewRequest, ctx: ApiContext = Depends(verify_api_key)):
    """Latest market payload for one market (default: primary). Serves the
    stored snapshot when it's under 24h old; refresh=true (or no snapshot)
    recomputes live — ~5 Apollo searches plus per-row verification, all
    non-reveal."""
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    icp_config = _org_icp(db, req.org_id)
    if not _market_enabled(icp_config):
        return {"enabled": False}

    market = get_market(db, req.org_id, req.market_id or None)
    if req.market_id and not market:
        return {"enabled": True, "error": "unknown_market"}

    # Lapsed orgs read their latest snapshot but never trigger a compute —
    # each one is ~5 Apollo searches plus per-row verification, and this
    # door (incl. MCP get_market refresh=true) wasn't covered by the
    # refresh-all filter (2026-08-23 lapsed-org audit).
    lapsed = org_lapsed(req.org_id, db)

    if not req.refresh or lapsed:
        snap = latest_snapshot(db, req.org_id, market=market)
        if snap:
            # Serve-time filter: the snapshot predates any accounts added
            # since, so drop rows the org now watches (they reappeared on
            # page refresh otherwise — bug 2026-08-14).
            snap = _apply_current_value(drop_watched_rows(db, req.org_id, snap), icp_config, market)
            return {"enabled": True, "from_snapshot": True, **snap}
        if req.poll:
            return {"enabled": True, "pending": True}
        if lapsed:
            return {"enabled": False}

    payload = compute_overview(req.org_id, req.market_id or None)
    return {"enabled": True, "from_snapshot": False, **payload}


class MarketCountRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    # Draft wizard answers — industries/geos/sizes as the wizard labels
    # them. Nothing is persisted; this is the live counter on the
    # targeting step (PLAN Phase 3).
    answers: Dict[str, Any] = Field(default_factory=dict)
    # OR direct criteria from the market editor drawer (054):
    # {industries: [], locations: [], employee_ranges: ["min,max", ...]}.
    criteria: Dict[str, Any] = Field(default_factory=dict)


@router.post("/market/count")
def market_count(req: MarketCountRequest, ctx: ApiContext = Depends(verify_api_key)):
    """Universe size for a DRAFT ICP — the live "Your market: ~N companies"
    counter while the user toggles targeting chips (wizard answers or the
    market editor's raw criteria). One free Apollo count per call; callers
    debounce. Not gated on market_v1 (it runs mid-onboarding, before the
    flag flips at completion)."""
    ctx.enforce_org(req.org_id)
    from app.services.onboarding_intel import map_answers_to_icp
    from app.services.market_radar import market_criteria, universe_count

    if req.criteria:
        icp_like = {"personas": [{
            "target_industries": req.criteria.get("industries") or [],
            "target_locations": req.criteria.get("locations") or [],
            "target_company_size": req.criteria.get("employee_ranges") or [],
        }]}
    else:
        icp_like = {"personas": [map_answers_to_icp(req.answers)]}
    criteria = market_criteria(
        icp_like, {"industries": [], "countries": [], "size_bands": [], "accounts": 0},
    )
    if not criteria["industries"]:
        return {"total": None}
    return {"total": universe_count(criteria)}


class SliceTopupRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    slice_key: str = Field(..., pattern="^(hiring_security|funding|growth)$")
    exclude: list[str] = Field(default_factory=list, max_length=200)
    limit: int = Field(default=6, ge=1, le=12)
    market_id: str = ""


@router.post("/market/slice-topup")
def market_slice_topup(req: SliceTopupRequest, ctx: ApiContext = Depends(verify_api_key)):
    """The conveyor's refill: next verified rows for one slice when the
    user has added their way through the bench. Same pipeline as the
    weekly compute, paginated deeper; results append to the latest
    snapshot so a refresh keeps them."""
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    if not _market_enabled(_org_icp(db, req.org_id)) or org_lapsed(req.org_id, db):
        return {"enabled": False}
    rows = topup_slice(req.org_id, req.slice_key, req.exclude, req.limit,
                       market_id=req.market_id or None)
    return {"enabled": True, "rows": rows}


# ---------------------------------------------------------------------------
# Markets CRUD. Listing works for every market_v1 org (it lazily
# creates the primary row). Creating extra markets is gated by TIER limit
# (2026-08-23: Starter 1 / Individual 3 / Team custom) —
# a Starter at their limit gets the standard 429 cap shape so the
# dashboard's shared upgrade modal opens, same journey as every other cap.
# ---------------------------------------------------------------------------


def _org_tier(db, org_id: str) -> str:
    row = (db.table("organisations").select("tier")
           .eq("id", org_id).maybe_single().execute().data or {})
    return (row.get("tier") or "").lower()


class MarketsListRequest(BaseModel):
    org_id: str = Field(..., min_length=1)


@router.post("/market/markets")
def markets_list(req: MarketsListRequest, ctx: ApiContext = Depends(verify_api_key)):
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    icp_config = _org_icp(db, req.org_id)
    if not _market_enabled(icp_config):
        return {"enabled": False}
    return {
        "enabled": True,
        "multi": feature_enabled(icp_config, "multi_market"),
        "limit": markets_limit(_org_tier(db, req.org_id)),
        "markets": list_markets(db, req.org_id),
    }


class MarketSaveRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    market_id: str = ""  # empty = create
    name: str = Field(..., min_length=1, max_length=60)
    # {target_industries: [], target_locations: [], target_company_size: []}
    definition: Dict[str, Any] = Field(default_factory=dict)
    avg_deal_size: int = Field(default=0, ge=0, le=100_000_000)
    currency: str = Field(default="", max_length=3)


@router.post("/market/markets/save")
def markets_save(req: MarketSaveRequest, ctx: ApiContext = Depends(verify_api_key)):
    """Create or update a market from the editor drawer. The drawer sends
    the full record each time. A primary market accepts edits too — a saved
    definition overrides icp_config targeting (the editable-market ask,
    2026-08-17). The caller triggers refresh=true afterwards for a
    recompute with the new criteria."""
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    icp_config = _org_icp(db, req.org_id)
    if not _market_enabled(icp_config) or org_lapsed(req.org_id, db):
        return {"enabled": False}

    fields = {
        "name": req.name.strip(),
        "definition": clean_definition(req.definition),
        "avg_deal_size": req.avg_deal_size or None,
        "currency": (req.currency or "").upper() or None,
    }

    if req.market_id:
        market = get_market(db, req.org_id, req.market_id)
        if not market:
            raise HTTPException(status_code=404, detail="Unknown market")
        db.table("markets").update(fields).eq("id", market["id"]) \
            .eq("org_id", req.org_id).execute()
        return {"enabled": True, "market": {**market, **fields}}

    existing = list_markets(db, req.org_id)
    limit = markets_limit(_org_tier(db, req.org_id))
    if len(existing) >= limit:
        # Same 429 shape as the plan-cap doors — the dashboard's shared
        # upgrade modal opens automatically (claraApi parses this).
        raise HTTPException(status_code=429, detail={
            "error": "solo_limit_reached",
            "cap": "markets",
            "limit": limit,
            "message": f"Your plan includes {limit} market{'s' if limit != 1 else ''}.",
        })
    ins = db.table("markets").insert({"org_id": req.org_id, **fields}).execute()
    return {"enabled": True, "market": (ins.data or [None])[0]}


class MarketProposeRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    instruction: str = Field(..., min_length=3, max_length=500)
    # Current editor draft — the proposal preserves whatever the
    # instruction doesn't change, so refinements compose.
    draft: Dict[str, Any] = Field(default_factory=dict)


@router.post("/market/propose")
def market_propose(req: MarketProposeRequest, ctx: ApiContext = Depends(verify_api_key)):
    """Plain-English market description → proposed definition + adjacent-
    industry suggestions with live Apollo counts. Powers the editor
    drawer's "tell Vera about this market" flow — Vera proposes, the user
    corrects. One Claude pass + up to 5 free Apollo count searches."""
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    if not _market_enabled(_org_icp(db, req.org_id)) or org_lapsed(req.org_id, db):
        return {"enabled": False}
    from app.services.markets import propose_market
    return {"enabled": True, **propose_market(req.instruction, req.draft)}


class MarketDeleteRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    market_id: str = Field(..., min_length=1)


@router.post("/market/markets/delete")
def markets_delete(req: MarketDeleteRequest, ctx: ApiContext = Depends(verify_api_key)):
    """Delete a non-primary market. Its snapshots cascade away; its
    watchlist rows fall back to the primary market (market_id → NULL),
    accounts themselves are never touched."""
    ctx.enforce_org(req.org_id)
    db = get_supabase()
    if not _market_enabled(_org_icp(db, req.org_id)):
        return {"enabled": False}
    market = get_market(db, req.org_id, req.market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Unknown market")
    if market.get("is_primary"):
        raise HTTPException(status_code=400, detail="The primary market can't be deleted")
    db.table("markets").delete().eq("id", market["id"]).eq("org_id", req.org_id).execute()
    return {"enabled": True}


def _refresh_all_markets(pairs: list) -> None:
    """Sequential on purpose — each compute is itself parallel enough
    (Apollo searches + verify lookups), and one market at a time keeps the
    weekly sweep well under vendor rate limits. `pairs` is
    (org_id, market_id-or-None)."""
    for org_id, market_id in pairs:
        try:
            compute_overview(org_id, market_id)
            logger.info("weekly market refresh done for %s (%s)", org_id, market_id or "primary")
        except Exception as e:  # noqa: BLE001
            logger.error("weekly market refresh failed for %s (%s): %s", org_id, market_id, e)


@router.post("/market/refresh-all")
def market_refresh_all(
    background_tasks: BackgroundTasks, ctx: ApiContext = Depends(verify_api_key),
):
    """Weekly cron (external; not run by the built-in scheduler): recompute
    the market snapshot for every org with market_v1 on — every ACTIVE market for multi_market orgs, just the
    primary for everyone else. Fire-and-forget like /watchlist/monitor —
    returns immediately, processes in the background; verify success by
    fresh market_snapshots rows, not this response. This is what makes the
    page's week-over-week diff and 'checked weekly' claim mechanically
    true with nobody logged in."""
    from app.services.plan_limits import REVOKED_STATUSES

    db = get_supabase()
    rows = (db.table("organisations").select("id, icp_config, subscription_status")
            .execute().data or [])
    pairs: list = []
    orgs = 0
    for r in rows:
        icp = r.get("icp_config") or {}
        if not ((icp.get("features") or {}).get("market_v1")):
            continue
        # Lapsed orgs keep their data but get no weekly compute — the
        # 2026-08-23 sweep spent a 263k-universe compute on a cancelled
        # trial org. Reactivation picks them back up automatically.
        if (r.get("subscription_status") or "").lower() in REVOKED_STATUSES:
            continue
        orgs += 1
        try:
            pairs.extend((r["id"], m["id"]) for m in list_markets(db, r["id"])
                         if m.get("active"))
            continue
        except Exception as e:  # noqa: BLE001
            logger.warning("market list failed for %s, falling back to primary: %s", r["id"], e)
        pairs.append((r["id"], None))
    background_tasks.add_task(_refresh_all_markets, pairs)
    return {"status": "started", "orgs": orgs, "markets": len(pairs)}
