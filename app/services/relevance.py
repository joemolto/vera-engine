"""ICP title-relevance service — the single source of truth for judging whether
a person's job title makes them a relevant buyer / influencer / champion for a
seller's ICP. Every people + suggestion surface should call judge_titles() so
relevance is consistent everywhere (no more per-surface string matching, which
is where the recurring "Social Media Manager in a security ICP" bugs came from).

Two layers:
  1. Deterministic hard-negative pre-filter (free, instant): obvious non-buyers
     (sales, marketing, social, HR, ...) are dropped without an LLM call, and
     word boundaries stop the classic false positive ("SOC" matching "SOCial").
  2. Claude Haiku (the judgment): everything that survives the pre-filter is
     scored 0..1 with a one-line "why they fit" reason, judged by the seller's
     ARCHETYPE (MDR / pentest / GRC / ...). Cached in title_relevance keyed by
     (archetype, title_norm) so each unique title is judged once per archetype
     then reused forever — cheap and instant.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from supabase import Client

from app.config import settings
from app.services.claude import call_claude

logger = logging.getLogger(__name__)

RELEVANT_THRESHOLD = 0.35

# Roles that are never a cyber buyer/influencer — dropped free, no LLM spend.
# Word boundaries so "soc" can't hide inside "social" etc.
_HARD_NEGATIVE_RE = re.compile(
    r"\b(sales|account executive|\bae\b|sdr|bdr|business development|"
    r"marketing|social media|content|copywriter|seo|brand|"
    r"communications?|comms|public relations|\bpr\b|community|"
    r"human resources|\bhr\b|recruit(?:er|ing|ment)|talent|people ops|"
    r"customer success|customer support|customer service|account manager|"
    r"designer|design lead|ux|ui|creative|"
    r"receptionist|office manager|executive assistant|administrative|"
    r"facilities|janitor|driver|warehouse|logistics coordinator|"
    r"teacher|lecturer|professor|nurse|clinical|physician|surgeon|"
    r"accountant|bookkeeper|payroll)\b",
    re.IGNORECASE,
)


def normalize_title(title: str) -> str:
    t = (title or "").lower().strip()
    t = re.sub(r"[^a-z0-9/&+ ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def is_hard_negative(title: str) -> bool:
    return bool(_HARD_NEGATIVE_RE.search(title or ""))


# ---------------------------------------------------------------------------
# Cache (shared table title_relevance)
# ---------------------------------------------------------------------------
def _cache_get(db: Client, archetype: str, title_norms: List[str]) -> Dict[str, Dict[str, Any]]:
    if not title_norms:
        return {}
    try:
        rows = (
            db.table("title_relevance")
            .select("title_norm, score, relevant, reason")
            .eq("archetype", archetype)
            .in_("title_norm", title_norms)
            .execute()
            .data
        ) or []
    except Exception as e:  # noqa: BLE001
        logger.info("title_relevance cache read failed: %s", e)
        return {}
    return {r["title_norm"]: r for r in rows}


def _cache_put(db: Client, archetype: str, verdicts: Dict[str, Dict[str, Any]], source: str) -> None:
    if not verdicts:
        return
    rows = [
        {
            "archetype": archetype,
            "title_norm": tn,
            "score": float(v.get("score") or 0.0),
            "relevant": bool(v.get("relevant")),
            "reason": (v.get("reason") or "")[:240] or None,
            "source": source,
        }
        for tn, v in verdicts.items()
    ]
    try:
        db.table("title_relevance").upsert(rows, on_conflict="archetype,title_norm").execute()
    except Exception as e:  # noqa: BLE001
        logger.info("title_relevance cache write failed: %s", e)


# ---------------------------------------------------------------------------
# LLM judgment
# ---------------------------------------------------------------------------
_SYSTEM = """You judge how relevant each job title is as a buyer, influencer, or champion for a specific cybersecurity vendor, given their ICP.

Who buys security: security leaders (CISO, CSO, VP/Head/Director of Security, SecOps, SOC managers); IT / technology leaders (CIO, CTO, VP Engineering, Head of IT) who own security when there is no dedicated security hire; and for GRC / compliance vendors also risk, compliance, audit, privacy (DPO) and sometimes legal leaders. Founders/CEOs of small companies can be the buyer.

NOT relevant: sales, marketing, social media, communications, HR/recruiting, finance/accounting (unless the ICP explicitly targets them), operations/admin, product/design, customer success/support, and individual contributors far from the decision.

Judge each title through THIS vendor's lens (their archetype + what they sell). A title that's a strong buyer for one vendor may be weak for another.

Return STRICT JSON only:
{"verdicts": [{"i": <index>, "score": <0..1>, "relevant": <bool>, "reason": "<8-14 words, why they fit or don't, in the seller's terms>"}]}
score: 1.0 = the decision-maker, 0.6 = strong influencer, 0.3 = adjacent/weak, 0 = irrelevant. relevant = score >= 0.35. No prose outside the JSON."""


def _llm_judge(
    archetype: str, sells_what: str, icp_titles: List[str], titles: List[str]
) -> Dict[str, Dict[str, Any]]:
    """Judge a batch of titles in one Haiku call. Returns {title: verdict}."""
    if not titles:
        return {}
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(titles))
    prompt = (
        f"VENDOR ARCHETYPE: {archetype or 'cybersecurity vendor'}\n"
        f"WHAT THEY SELL: {sells_what or 'cybersecurity products/services'}\n"
        f"THEIR ICP TARGET TITLES: {', '.join(icp_titles) if icp_titles else '(not specified — use security/IT buyers)'}\n\n"
        f"Judge each title below:\n{numbered}"
    )
    model = getattr(settings, "CLAUDE_FAST_MODEL", "claude-haiku-4-5")
    result = call_claude(prompt=prompt, system=_SYSTEM, model=model, cache_system=True, max_tokens=1200)
    out: Dict[str, Dict[str, Any]] = {}
    verdicts = result.get("verdicts") if isinstance(result, dict) else None
    if not isinstance(verdicts, list):
        # LLM failed — fail OPEN conservatively: keep them at a low score so a
        # transient LLM error never hides real buyers, but they rank last.
        logger.warning("relevance LLM returned no verdicts for %d titles", len(titles))
        return {t: {"score": 0.4, "relevant": True, "reason": ""} for t in titles}
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        i = v.get("i")
        if not isinstance(i, int) or i < 0 or i >= len(titles):
            continue
        score = float(v.get("score") or 0.0)
        out[titles[i]] = {
            "score": score,
            "relevant": bool(v.get("relevant", score >= RELEVANT_THRESHOLD)),
            "reason": str(v.get("reason") or "").strip(),
        }
    # Any title the LLM skipped → conservative low-but-kept.
    for t in titles:
        out.setdefault(t, {"score": 0.4, "relevant": True, "reason": ""})
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def build_relevance_context(db: Client, org_id: str) -> Dict[str, Any]:
    """Pull the seller's archetype + ICP titles + what they sell from the org,
    to drive + cache-key the relevance judgment."""
    try:
        row = (
            db.table("organisations").select("icp_config")
            .eq("id", org_id).maybe_single().execute().data
        ) or {}
    except Exception:
        row = {}
    icp = row.get("icp_config") or {}
    subs = icp.get("sub_sectors")
    if not isinstance(subs, list):
        personas = icp.get("personas")
        subs = (personas[0].get("sub_sectors") if personas and isinstance(personas[0], dict) else None) or []
    archetype = (subs[0] if subs else "").strip().lower() or "cybersecurity vendor"
    titles = icp.get("target_titles")
    if not isinstance(titles, list):
        personas = icp.get("personas")
        titles = (personas[0].get("target_titles") if personas and isinstance(personas[0], dict) else None) or []
    cc = icp.get("client_context") or {}
    sells = str(cc.get("product_pitch") or icp.get("sells_what") or "").strip()[:300]
    return {"archetype": archetype, "icp_titles": [str(t) for t in titles if t], "sells_what": sells}


def judge_titles(
    db: Client,
    *,
    archetype: str,
    icp_titles: List[str],
    sells_what: str,
    titles: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Verdict per input title: {score, relevant, reason}. Deterministic
    hard-negatives are dropped free; everything else is LLM-judged and cached."""
    archetype = (archetype or "cybersecurity vendor").strip().lower()
    verdicts: Dict[str, Dict[str, Any]] = {}

    # Unique normalized titles → judge once, map back to every original.
    norm_of = {t: normalize_title(t) for t in titles}
    unique_norms = sorted({n for n in norm_of.values() if n})

    # 1. Hard negatives — free.
    hard_neg = {n for n in unique_norms if is_hard_negative(n)}
    for n in hard_neg:
        verdicts[n] = {"score": 0.0, "relevant": False, "reason": "not a security buyer"}

    remaining = [n for n in unique_norms if n not in hard_neg]

    # 2. Cache.
    cached = _cache_get(db, archetype, remaining)
    for n, r in cached.items():
        verdicts[n] = {"score": r.get("score"), "relevant": r.get("relevant"), "reason": r.get("reason") or ""}

    # 3. LLM for the uncached remainder.
    to_judge = [n for n in remaining if n not in cached]
    if to_judge:
        fresh = _llm_judge(archetype, sells_what, icp_titles, to_judge)
        _cache_put(db, archetype, fresh, source="llm")
        verdicts.update(fresh)

    _cache_put(db, archetype, {n: verdicts[n] for n in hard_neg}, source="deterministic")

    # Map normalized verdicts back to the caller's original titles.
    return {t: verdicts.get(norm_of[t], {"score": 0.4, "relevant": True, "reason": ""}) for t in titles}
