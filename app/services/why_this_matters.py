"""AI-generated 'Why this account matters' paragraph.

Spec 2026-06-08: replace the broken deterministic synthesis on the
account dossier with a Haiku 4.5 paragraph that actually reads like a
senior rep wrote it. Strict facts-only prompt, cached on
companies.why_this_matters.

Trigger plan:
  1. First enrichment (initial paragraph for a brand-new account)
  2. New buying-trigger signal fires in Phase D of monitor pass
  3. Weekly safety-net cron — catches relative-time drift on quiet
     accounts ("$X raised 3 weeks ago" becoming stale silently)
  4. Manual refresh endpoint (operator / debug)

Cost: ~$0.001 per generation, well under $1/month per org.
Negligible — quality dominates.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from supabase import Client

from app.config import settings
from app.services.claude import call_claude

logger = logging.getLogger(__name__)

# Buying-trigger signal types that justify mentioning. Anything outside
# this set (social_company_mention, partnerships, launches, etc.) is
# noise and stays out of the prompt — same shape as the front-end's
# lib/dashboard/buyingTriggers.ts and HomeV2/AnalyticsV2 whitelists.
_BUYING_TRIGGER_TYPES: set[str] = {
    "social_breach_mention",
    "breach_proximity",
    "security_leadership_hire",
    "social_leadership_change",
    "new_leadership",
    "recently_funded",
    "social_funding_news",
    "recent_acquisition",
    "security_team_expansion",
    "hiring_security",
    "person_engaged_with_competitor",
    "person_new_in_role",
    "person_posted_relevant_topic",
    "person_engaged_with_topic",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def refresh_why_this_matters(
    db: Client,
    *,
    company_id: str,
    org_id: str,
    trigger: str = "manual",
    force: bool = False,
) -> Dict[str, Any]:
    """Generate (or refresh) the 'why this account matters' paragraph.

    Returns the persisted shape:
      {paragraph, generated_at, model, facts_hash, trigger}

    Behaviour:
      - Collects facts (company row + recent buying-trigger signals)
      - Skips regeneration if facts_hash matches the cached one
        (unless force=True or it's a cron tick — the cron should
        always rewrite to refresh relative-time language)
      - Calls Haiku 4.5 with a strict facts-only prompt
      - Falls back gracefully — never raises out to the caller
    """
    company = _fetch_company(db, company_id)
    if not company:
        return {"error": "company not found"}

    signals = _fetch_recent_buying_signals(db, org_id=org_id, company_id=company_id)
    seller = _fetch_seller_context(db, org_id)
    facts = _build_facts(company=company, signals=signals, seller=seller)
    facts_hash = _hash_facts(facts)

    cached = company.get("why_this_matters") or {}
    if (
        not force
        and trigger != "cron"
        and isinstance(cached, dict)
        and cached.get("facts_hash") == facts_hash
        and cached.get("paragraph")
    ):
        # Nothing changed — skip the LLM call entirely.
        return cached

    paragraph = _generate_paragraph(facts)
    if not paragraph:
        # LLM failed; don't overwrite a good cached version with junk.
        return cached or {"error": "generation_failed"}

    record = {
        "paragraph": paragraph,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": "claude-haiku-4-5",
        "facts_hash": facts_hash,
        "trigger": trigger,
        "signal_count_at_generation": len(signals),
    }
    try:
        db.table("companies").update({"why_this_matters": record}).eq(
            "id", company_id
        ).execute()
    except Exception as e:
        logger.warning("why_this_matters store failed for %s: %s", company_id, e)
    return record


# ---------------------------------------------------------------------------
# Facts collection — what the LLM gets as ground truth
# ---------------------------------------------------------------------------
def _fetch_company(db: Client, company_id: str) -> Optional[Dict[str, Any]]:
    try:
        res = (
            db.table("companies")
            .select(
                "id, name, domain, industry, size_band, hq_country, hq_city, "
                "founded_year, description, technologies, compliance, "
                "predictleads_data, apollo_data, why_this_matters"
            )
            .eq("id", company_id)
            .maybe_single()
            .execute()
        )
        return res.data
    except Exception as e:
        logger.warning("Fetch company %s failed: %s", company_id, e)
        return None


def _fetch_seller_context(db: Client, org_id: str) -> Dict[str, Any]:
    """The seller's own identity + focus, so the paragraph frames the account
    THROUGH their lens (a pentest firm and a GRC platform care about different
    things). Returns {name, focus}; best-effort — empty on any failure so the
    paragraph still generates in a neutral voice."""
    try:
        res = (
            db.table("organisations")
            .select("name, client_context")
            .eq("id", org_id)
            .maybe_single()
            .execute()
        )
        row = res.data or {}
    except Exception as e:
        logger.warning("seller context fetch failed for %s: %s", org_id, e)
        return {}
    cc = row.get("client_context") if isinstance(row.get("client_context"), dict) else {}
    focus = str((cc or {}).get("product_pitch") or "").strip()
    return {
        "name": (str(row.get("name") or "").strip() or None),
        "focus": (focus[:300] or None),
    }


def _fetch_recent_buying_signals(
    db: Client, *, org_id: str, company_id: str, limit: int = 20
) -> List[Dict[str, Any]]:
    """Recent (last 90 days) buying-trigger signals for this account,
    most recent first. Capped — the LLM only needs the top few."""
    try:
        res = (
            db.table("signals")
            .select(
                "signal_type, signal_data, detected_at, signal_strength"
            )
            .eq("org_id", org_id)
            .eq("company_id", company_id)
            .order("detected_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = res.data or []
    except Exception as e:
        logger.warning("Fetch signals for %s failed: %s", company_id, e)
        return []
    from app.services.signal_visibility import fetch_hidden_signal_types
    hidden = fetch_hidden_signal_types(db, org_id)
    return [r for r in rows
            if r.get("signal_type") in _BUYING_TRIGGER_TYPES
            and r.get("signal_type") not in hidden]


def _build_facts(
    *, company: Dict[str, Any], signals: List[Dict[str, Any]],
    seller: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compact dict of facts the LLM gets in the user message.

    Kept tight — only fields that genuinely shape the paragraph.
    No vendor names (Apollo, PredictLeads etc.) — those would leak
    into the output.
    """
    apollo = company.get("apollo_data") or {}
    revenue = (
        apollo.get("organization_revenue_printed")
        or apollo.get("annual_revenue_printed")
        or None
    )

    compliance_list: List[str] = []
    comp = company.get("compliance")
    if isinstance(comp, list):
        compliance_list = [str(c) for c in comp if c]
    elif isinstance(comp, dict):
        compliance_list = [k for k, v in comp.items() if v]

    # Compact each signal — just the headline + type + age in days.
    # The LLM doesn't need the full signal_data payload.
    now = datetime.now(timezone.utc)
    compact_signals: List[Dict[str, Any]] = []
    for s in signals[:8]:  # top 8 most recent buying triggers
        data = s.get("signal_data") or {}
        if not isinstance(data, dict):
            data = {}
        headline = (
            (data.get("headline") if isinstance(data.get("headline"), str) else None)
            or (data.get("summary") if isinstance(data.get("summary"), str) else None)
            or (data.get("quoted_excerpt") if isinstance(data.get("quoted_excerpt"), str) else None)
            or ""
        )
        headline = headline.strip()[:200]
        age_days = None
        if s.get("detected_at"):
            try:
                d = datetime.fromisoformat(s["detected_at"].replace("Z", "+00:00"))
                age_days = (now - d).days
            except Exception:
                pass
        compact_signals.append(
            {
                "type": s.get("signal_type"),
                "headline": headline,
                "age_days": age_days,
            }
        )

    return {
        "name": company.get("name"),
        "domain": company.get("domain"),
        "industry": company.get("industry"),
        "size_band": company.get("size_band"),
        "hq_country": company.get("hq_country"),
        "hq_city": company.get("hq_city"),
        "founded_year": company.get("founded_year"),
        "revenue": revenue,
        "compliance": compliance_list,
        "description_snippet": (company.get("description") or "")[:400] or None,
        "seller": {k: v for k, v in (seller or {}).items() if v},
        "buying_triggers": compact_signals,
    }


def _hash_facts(facts: Dict[str, Any]) -> str:
    """Stable hash of the facts dict — lets us skip regeneration when
    nothing material has changed. JSON-serialised with sorted keys so
    field ordering doesn't matter."""
    s = json.dumps(facts, sort_keys=True, default=str)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# LLM generation
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """You write a 2-3 sentence "why this account matters" paragraph for a rep at a cybersecurity vendor evaluating whether to act on an account today. The `seller` object gives that vendor's name and what they do — frame the whole paragraph THROUGH their lens.

STRICT RULES:
- FRAME FOR THE SELLER. Different vendors care about different things: a pentest / offensive-security firm cares about attack surface and exposure; a GRC / compliance vendor about regulatory posture and audit pressure; a threat-intelligence vendor about active threats and leaked data; an MDR / SOC about detection-and-response demand; an IAM vendor about identity risk. Read `seller.focus` and frame why THIS account matters to a vendor like that. You MAY name the seller (`seller.name`) once, naturally, for a personable touch. But NEVER recommend outreach, suggest what to say, or invent reasons the account is a fit — state only the account's real facts, framed for this seller.
- Use ONLY facts provided in the user message. Never invent numbers, dates, names, partnerships, funding amounts, products, or competitors.
- If a fact is absent, do not mention it. Do not say "no data" — just leave it out.
- 2-3 sentences. No more. No bullet points. No headers. Plain prose.
- LEAD PRIORITY, in order:
  1. If a relevant buying-trigger signal is present — a security leadership hire, security/GRC hiring, funding, acquisition, or expansion — lead with it, then add 1-2 sentences of context (scale, sector, regulation).
  2. NEVER lead with the account's OWN breach, credential leak, or security exposure — that reads as ambulance-chasing. And NEVER lead with an operational or non-business hire (e.g. a clinical, medical, or departmental role) — it's irrelevant to a security buyer.
  3. If there is no relevant signal to lead with, DO NOT force a weak one. Instead lead with why this account is a strong security target: its scale, sector, regulatory exposure (compliance certifications), and the stakes a breach would carry given what it operates. Frame it through the seller's focus (per `seller`).
- Sound like a senior rep briefing a colleague — direct, factual, no marketing language. NO "exciting", "leverages", "innovative", "leader in", "robust", "cutting-edge", "synergy". NO em-dashes used decoratively.
- Relative time references are OK ("last month", "this quarter") when the age_days is known.
- Never mention internal tool names (Apollo, PredictLeads, Haiku, Vera, etc.).

Return JSON only — no markdown, no prose wrapper:
{"paragraph": "<2-3 sentences>"}
"""


def _generate_paragraph(facts: Dict[str, Any]) -> Optional[str]:
    """Call Haiku 4.5 with the facts → paragraph. Returns None on failure."""
    if not facts.get("name"):
        return None
    # Don't call the LLM if there's nothing to say. An account with no
    # buying triggers AND no industry AND no notable facts produces
    # generic filler — better to leave the field empty and let the FE
    # fall back to the deterministic summary.
    if not facts.get("buying_triggers") and not facts.get("industry"):
        return None

    prompt = json.dumps(facts, indent=2, default=str)

    # Use the Haiku model id from settings if configured, else hardcode.
    haiku_model = getattr(settings, "CLAUDE_FAST_MODEL", "claude-haiku-4-5")

    result = call_claude(
        prompt=prompt,
        system=_SYSTEM_PROMPT,
        model=haiku_model,
        cache_system=True,
        max_tokens=400,
    )
    if not isinstance(result, dict):
        return None
    paragraph = result.get("paragraph")
    if not isinstance(paragraph, str):
        return None
    paragraph = paragraph.strip()
    # Belt-and-braces: strip any decorative quotes the LLM might wrap with
    if paragraph.startswith('"') and paragraph.endswith('"'):
        paragraph = paragraph[1:-1].strip()
    # Em-dashes read as AI — the prompt forbids them but Haiku
    # slips them in, so replace any that survive with a comma.
    paragraph = paragraph.replace(" — ", ", ").replace("—", ", ")
    while "  " in paragraph:
        paragraph = paragraph.replace("  ", " ")
    paragraph = paragraph.replace(" ,", ",")
    return paragraph or None
