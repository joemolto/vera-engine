"""Relevance verdicts endpoint — the single ICP title-relevance judgment used
by the live people surfaces (the account-drawer people list calls this via the
apollo-people edge function). Keeps the LLM + cache in one place so every
surface agrees on who's a relevant buyer.
"""
from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.database import get_supabase
from app.services.api_auth import ApiContext, verify_api_key
from app.services.relevance import build_relevance_context, judge_titles

router = APIRouter()


class TitlesRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    titles: List[str] = Field(default_factory=list)


@router.post("/relevance/titles")
def relevance_titles(req: TitlesRequest, ctx: ApiContext = Depends(verify_api_key)):
    """Verdict per title: {score, relevant, reason}, judged for the org's ICP
    archetype. Deterministic hard-negatives are free; the rest are LLM-judged
    and cached, so repeat titles are instant."""
    db = get_supabase()
    ctxd = build_relevance_context(db, req.org_id)
    titles = [t for t in (req.titles or []) if t][:60]
    verdicts = judge_titles(
        db,
        archetype=ctxd["archetype"],
        icp_titles=ctxd["icp_titles"],
        sells_what=ctxd["sells_what"],
        titles=titles,
    )
    return {"verdicts": verdicts, "archetype": ctxd["archetype"]}
