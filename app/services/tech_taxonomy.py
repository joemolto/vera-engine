"""Self-growing technology taxonomy.

Classification is three-tier and gets cheaper over time:
  1. keyword rules  — free, in `predictleads._classify_tech` (runs upstream)
  2. `tech_category` table  — learned mappings (this module's `apply_taxonomy`)
  3. AI batch job  — `categorize_pending_techs` categorises whatever's still
     unknown and writes the answer back into the table.

The key cost trick: a tool's category is GLOBAL ("Snowflake" is a data
warehouse on every account), so we categorise distinct *names* once and cache
forever. AI usage therefore decays as the table fills. No agent/RAG needed —
the taxonomy is small enough to live in the prompt, and the task is one-shot
classification, so a single Haiku call per batch does it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from supabase import Client

from app.config import settings
from app.services.claude import call_claude

logger = logging.getLogger(__name__)

# Canonical category set. The AI must pick exactly one of these. Kept aligned
# with the keyword rules' categories, plus the obvious buckets that otherwise
# fall to "other". "security" is intentionally broad (Vera sells to cyber).
TECH_CATEGORIES: List[str] = [
    "security",
    "crm_sales",
    "sales_intelligence",
    "marketing",
    "analytics",
    "data",
    "ai_ml",
    "development",
    "infrastructure",
    "hosting_cdn",
    "collaboration",
    "productivity",
    "hr",
    "finance_payments",
    "ecommerce",
    "cms",
    "other",
]

_BATCH_PROMPT = """You categorise software / technology product names into a fixed taxonomy.

Categories — use EXACTLY one of these strings:
{categories}

Rules:
- "security" is broad: any cybersecurity tool — EDR/XDR, SIEM, SOC, threat
  intelligence, IAM / identity / SSO / MFA / PAM, firewalls, network/DNS
  security, email & web security, vulnerability management, AppSec, cloud
  security, fraud / bot defence — AND security/compliance frameworks
  (NIS2, HIPAA, SOC 2, ISO 27001, PCI DSS, GDPR, Cyber Essentials, DORA).
  When something is security-related, prefer "security".
- "data" = databases, warehouses, BI, pipelines (Snowflake, MongoDB, Power BI).
- "ai_ml" = AI/ML platforms and tooling (OpenAI, LangChain, Copilot).
- Use "other" ONLY when it genuinely fits nothing above.

Return ONLY a JSON object mapping each input name to its category string.
No prose, no markdown.

Names:
{names}"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def apply_taxonomy(db: Client, techs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Refine still-"other" techs against the learned `tech_category` table,
    and queue any genuinely-unknown names as 'pending' for the AI batch job.

    Mutates and returns `techs`. Best-effort: any DB error leaves the techs
    exactly as the keyword pass produced them.
    """
    if not techs:
        return techs

    def cat_of(t: Dict[str, Any]) -> str:
        return (t.get("category") or "other") or "other"

    unknown_names = sorted(
        {(t.get("name") or "").lower() for t in techs if cat_of(t) == "other" and t.get("name")}
    )
    if not unknown_names:
        return techs

    # Tier 2: learned table lookup (skip rows still pending / still 'other').
    learned: Dict[str, str] = {}
    try:
        res = (
            db.table("tech_category")
            .select("name, category, source")
            .in_("name", unknown_names)
            .execute()
        )
        for r in res.data or []:
            if r.get("category") and r["category"] != "other" and r.get("source") != "pending":
                learned[r["name"]] = r["category"]
    except Exception as e:
        logger.warning("tech_category lookup failed: %s", e)

    for t in techs:
        if cat_of(t) == "other":
            cat = learned.get((t.get("name") or "").lower())
            if cat:
                t["category"] = cat

    # Queue what's STILL unknown so the batch job can categorise it. Insert
    # only (ignore_duplicates) so we never clobber an existing ai/manual row.
    still_unknown = sorted(
        {t.get("name") for t in techs if cat_of(t) == "other" and t.get("name")}
    )
    if still_unknown:
        rows = [{"name": n.lower(), "display_name": n} for n in still_unknown]
        try:
            db.table("tech_category").upsert(
                rows, on_conflict="name", ignore_duplicates=True
            ).execute()
        except Exception as e:
            logger.warning("tech_category queue failed: %s", e)

    return techs


def categorize_pending_techs(
    db: Client, limit: int = 600, batch_size: int = 150
) -> Dict[str, Any]:
    """AI batch job: categorise 'pending' rows in `tech_category` and write the
    results back (source='ai'). Run on a cron — each run shrinks the backlog,
    and once a tool is categorised it's never sent to the AI again.
    """
    try:
        res = (
            db.table("tech_category")
            .select("name, display_name")
            .eq("source", "pending")
            .limit(limit)
            .execute()
        )
    except Exception as e:
        logger.error("tech_category pending fetch failed: %s", e)
        return {"pending": 0, "categorized": 0, "error": str(e)}

    pending = res.data or []
    if not pending:
        return {"pending": 0, "categorized": 0}

    categories_str = ", ".join(TECH_CATEGORIES)
    categorized = 0

    for i in range(0, len(pending), batch_size):
        chunk = pending[i : i + batch_size]
        names = [p.get("display_name") or p["name"] for p in chunk]
        try:
            result = call_claude(
                _BATCH_PROMPT.format(categories=categories_str, names=json.dumps(names)),
                model=settings.CLAUDE_FAST_MODEL,
                cache_system=False,
                max_tokens=4000,
            )
        except Exception as e:
            logger.error("tech taxonomy AI call failed: %s", e)
            continue

        mapping = result if isinstance(result, dict) and "error" not in result else None
        if mapping is None and isinstance(result, dict):
            raw = result.get("raw_text") or ""
            try:
                mapping = json.loads(raw)
            except Exception:
                mapping = None
        if not isinstance(mapping, dict):
            logger.warning("tech taxonomy AI returned no usable mapping for a batch")
            continue

        mlow = {str(k).lower(): v for k, v in mapping.items()}
        rows = []
        now = _now_iso()
        for p in chunk:
            disp = (p.get("display_name") or p["name"]).lower()
            cat = mlow.get(disp)
            if not cat:
                continue
            cat = cat if cat in TECH_CATEGORIES else "other"
            rows.append(
                {"name": p["name"], "category": cat, "source": "ai", "updated_at": now}
            )
        if rows:
            try:
                db.table("tech_category").upsert(rows, on_conflict="name").execute()
                categorized += len(rows)
            except Exception as e:
                logger.error("tech_category writeback failed: %s", e)

    return {"pending": len(pending), "categorized": categorized}
