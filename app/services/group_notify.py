"""
Smart-group change detection — the notify half of account smart groups.

The dashboard evaluates smart-group membership live (lib/dashboard/groups.ts
smartMatchRefs), so nothing ever stored membership and the groups.notify
toggle drove no notifications (2026-07-16 simulation, ABM persona). This
module mirrors the dashboard matcher in Python, diffs each notify-enabled
account smart group against its stored snapshot (groups.smart_snapshot), persists the new snapshot, and returns what entered — the
daily digest turns that into "X entered your 'US healthcare' group" lines.

First evaluation of a group seeds the snapshot silently so day one doesn't
announce the entire existing membership. Best-effort throughout: any failure
returns [] and the digest sends without group lines.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from supabase import Client

logger = logging.getLogger(__name__)


def _band_low(band: Any) -> Optional[int]:
    """Lower bound of a size band: '1001-5000' -> 1001, '40' -> 40."""
    m = re.search(r"\d+", str(band or ""))
    return int(m.group(0)) if m else None


def _matches(rules: Dict[str, Any], domain: str,
             fact: Dict[str, Any], score_row: Dict[str, Any]) -> bool:
    """Python mirror of the dashboard's account smartMatchRefs branch —
    rules the org didn't set never filter."""
    min_score = rules.get("min_score") or 0
    if min_score > 0 and (score_row.get("score") or 0) < min_score:
        return False
    min_signals = rules.get("min_signals_30d") or 0
    if min_signals > 0 and (score_row.get("signal_count") or 0) < min_signals:
        return False
    industries = [
        str(i).lower().strip()
        for i in [*(rules.get("industries") or []),
                  *( [rules["industry"]] if rules.get("industry") else [] )]
        if str(i).strip()
    ]
    if industries:
        ind = str(fact.get("industry") or "").lower()
        if not any(x in ind for x in industries):
            return False
    regions = [
        str(r).lower().strip()
        for r in [*(rules.get("regions") or []),
                  *( [rules["region"]] if rules.get("region") else [] )]
        if str(r).strip()
    ]
    if regions:
        geo = f"{fact.get('hq_country') or ''} {fact.get('hq_city') or ''}".lower()
        if not any(x in geo for x in regions):
            return False
    min_emp, max_emp = rules.get("min_employees"), rules.get("max_employees")
    if min_emp is not None or max_emp is not None:
        low = _band_low(fact.get("size_band"))
        if low is None:
            return False
        if min_emp is not None and low < min_emp:
            return False
        if max_emp is not None and low > max_emp:
            return False
    return True


def evaluate_org_group_changes(db: Client, org_id: str) -> List[Dict[str, Any]]:
    """Evaluate every notify-enabled account smart group for an org.

    Returns [{"group": name, "entered": [account names]}] for groups that
    gained members since the stored snapshot, updating snapshots as it goes.
    """
    try:
        groups = (
            db.table("groups")
            .select("id, name, rules, smart_snapshot")
            .eq("org_id", org_id)
            .eq("entity_type", "account")
            .eq("mode", "smart")
            .eq("notify", True)
            .execute()
        ).data or []
        if not groups:
            return []

        watch = (
            db.table("watchlist")
            .select("company_domain, company_name")
            .eq("org_id", org_id)
            .eq("status", "active")
            .execute()
        ).data or []
        domains = sorted({
            (w.get("company_domain") or "").lower()
            for w in watch if w.get("company_domain")
        })
        if not domains:
            return []
        name_by_domain = {
            (w.get("company_domain") or "").lower():
                w.get("company_name") or w.get("company_domain")
            for w in watch
        }

        facts: Dict[str, Dict[str, Any]] = {}
        res = (
            db.table("companies")
            .select("domain, industry, hq_country, hq_city, size_band")
            .in_("domain", domains)
            .execute()
        )
        for c in res.data or []:
            if c.get("domain"):
                facts[c["domain"].lower()] = c

        scores: Dict[str, Dict[str, Any]] = {}
        try:
            since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            rows = db.rpc(
                "get_account_scores", {"p_org_id": org_id, "p_since": since}
            ).execute().data or []
            for row in rows:
                if row.get("domain"):
                    scores[row["domain"].lower()] = {
                        "score": float(row.get("account_score") or 0),
                        "signal_count": row.get("signal_count") or 0,
                    }
        except Exception as e:
            logger.warning("group_notify: score RPC failed for %s: %s", org_id, e)

        changes: List[Dict[str, Any]] = []
        for g in groups:
            rules = g.get("rules") or {}
            matched = sorted(
                d for d in domains
                if _matches(rules, d, facts.get(d, {}), scores.get(d, {}))
            )
            prev = g.get("smart_snapshot")
            first_run = not isinstance(prev, list)  # column default is [], so
            # treat a null/absent snapshot as first run; an empty list from a
            # previous evaluation is a real "matched nothing" baseline.
            prev_set = set(prev) if isinstance(prev, list) else set()
            if matched != sorted(prev_set):
                db.table("groups").update(
                    {"smart_snapshot": matched,
                     "updated_at": datetime.now(timezone.utc).isoformat()}
                ).eq("id", g["id"]).execute()
            if first_run:
                continue
            entered = [d for d in matched if d not in prev_set]
            if entered:
                changes.append({
                    "group": g.get("name") or "your group",
                    "entered": [name_by_domain.get(d, d) for d in entered],
                })
        return changes
    except Exception as e:
        logger.warning("group_notify failed for %s: %s", org_id, e)
        return []
