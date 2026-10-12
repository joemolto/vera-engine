"""Intelligence Map graph builder — v2 (situation graph).

Demo/dev feature, gated per-org by ``icp_config.features.intelligence_map``.
Returns ``{nodes, edges, meta}`` ready for the dashboard's force-directed
graph.

v2 — what changed and why
-------------------------
v1 was a *similarity* graph: it linked companies that shared an industry,
a tech stack or a CVE. That answered "which companies are alike?" — not
"which companies share a live, sellable situation?" — so the canvas was
dense but not actionable.

v2 is a *situation* graph. It reads the org's detected signals from the
canonical ``signals`` table and:

- attaches each company's live signals to its node, so a dot becomes
  "a company *with a situation*", and
- links two companies when they share a situation *layer* (a new
  security leader, a growth/investment event, a threat surface),
  recency-weighted so a stale event doesn't masquerade as a trigger.

Why the ``signals`` table and not ``enrichment_data.findings``: the
``findings`` blob is a *rendered* copy that only exists once a later
pipeline step has run, and it is not always populated. ``signals`` is
the canonical detection output — always present — so the map is never
empty for an org that has been researched. The raw signal carries no
pre-written sales copy; we attach a per-layer guidance line instead.

Org-scoped: only ever reads one ``org_id``. Cheap, read-only.
The raw ``signal_source`` tool name is never emitted.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from typing import Any, Dict, List, Optional, Tuple

from supabase import Client

logger = logging.getLogger(__name__)


# Cap on rendered nodes — beyond this the canvas physics slow and the
# graph turns into a hairball. Most-recently-enriched companies win.
MAX_NODES = 120

# How many signals to attach to each node. The cross-reference panel
# shows the strongest few; the long tail adds weight without insight.
MAX_SIGNALS_PER_NODE = 6

# Within one layer, connecting every pair explodes O(n^2). Connect only
# the strongest-situation companies in each layer; the rest still render
# as nodes (with their signals) but don't add edge noise.
MAX_LAYER_FANOUT = 14

# A shared tech stack only counts as a "lookalike" link at >=2 matches —
# a single shared tool (everyone uses HubSpot) is noise.
MIN_SHARED_TECHS = 2

# Raw detected ``signal_type`` -> Intelligence Map edge layer. Types not
# listed here still show on the node as a signal; they just don't form
# edges in v2.
SIGNAL_TYPE_TO_LAYER: Dict[str, str] = {
    "security_leadership_hire": "leadership",
    "new_leadership": "leadership",
    "security_team_expansion": "growth",
    "hiring_sales_bd": "growth",
    "new_partnership": "growth",
    "product_launch": "growth",
    "recently_funded": "growth",
    "recent_acquisition": "growth",
    "expansion": "growth",
    "cve_in_stack": "threat",
    "breach_proximity": "threat",
    "compliance_gap": "threat",
}

# Short, shared-situation labels for each edge layer — the edge ``why``.
LAYER_EDGE_LABEL: Dict[str, str] = {
    "leadership": "Both have new security leadership",
    "growth": "Both showing growth / investment signals",
    "threat": "Shared threat surface — peer context only",
}

# Per-layer rep guidance, attached to every signal as ``recommended_use``.
# Raw signals carry no pre-written play; this is the floor. The threat
# line bakes in the no-ambulance-chasing rule.
LAYER_PLAY: Dict[str, str] = {
    "leadership": (
        "New security leadership re-evaluates the vendor stack in the "
        "first 90 days — open with a useful POV for their first quarter."
    ),
    "growth": (
        "Growth and investment free up budget — reference the momentum "
        "and offer to help them scale securely."
    ),
    "threat": (
        "Use as sector / peer context only — never lead with their "
        "specific exposure. Open by being helpful."
    ),
}

LAYER_RISK_NOTE_THREAT = (
    "Do not tell the prospect they are vulnerable — frame as sector / "
    "peer context only."
)

# Raw ``signal_source`` -> clean, client-facing label. The raw value is
# a backend tool name and must never reach the dashboard.
SOURCE_LABEL: Dict[str, str] = {
    "linkedin": "Verified profile",
    "predictleads": "Company intelligence",
    "jobs_enrichment": "Hiring data",
    "threat_intel": "Threat intelligence",
    "news": "News",
}
DEFAULT_SOURCE_LABEL = "Vera intelligence"


def build_intelligence_map(db: Client, org_id: str) -> Dict[str, Any]:
    """Return ``{nodes, edges, meta}`` for the Intelligence Map view.

    Node shape::
        {
          "id":           "<company_domain>",
          "label":        "<company_name>",
          "industry":     "<industry or 'unknown'>",
          "lead_rating":  "hot" | "warm" | "cold" | "unrated",
          "lead_count":   int,
          "signal_count": int,
          "categories":   ["leadership", "growth", ...],  # layers present
          "has_threat":   bool,
          "signals":      [<cleaned signal>, ...],          # sorted, capped
        }

    Edge shape::
        {
          "source":    "<domain_a>",
          "target":    "<domain_b>",
          "edge_type": "leadership" | "growth" | "threat" | "tech",
          "weight":    float,   # 0..1-ish, higher = stronger / fresher
          "why":       "<short shared-situation label>",
        }
    """
    # --- Companies (nodes) come from the org's enriched leads ----------
    leads_resp = (
        db.table("leads")
        .select(
            "id, company_name, company_domain, company_industry, "
            "lead_rating, enriched_at, enrichment_data"
        )
        .eq("org_id", org_id)
        .in_("enrichment_status", ["suggested", "enriched"])
        .neq("status", "disqualified")
        .order("enriched_at", desc=True)
        .limit(500)  # node cap is applied after aggregation
        .execute()
    )
    leads = leads_resp.data or []

    by_domain: Dict[str, Dict[str, Any]] = {}
    lead_to_domain: Dict[Any, str] = {}
    for lead in leads:
        domain = (lead.get("company_domain") or "").strip().lower()
        if not domain:
            continue
        lead_to_domain[lead.get("id")] = domain
        bucket = by_domain.setdefault(domain, {
            "domain": domain,
            "company_name": lead.get("company_name") or domain,
            "industry": (lead.get("company_industry") or "").strip().lower(),
            "lead_rating": "unrated",
            "lead_count": 0,
            "techs": set(),
            "signals": [],            # cleaned signals, deduped by headline
            "seen_headlines": set(),
            "latest_enriched_at": lead.get("enriched_at") or "",
        })
        bucket["lead_count"] += 1
        bucket["lead_rating"] = _strongest_rating(
            bucket["lead_rating"], lead.get("lead_rating") or "unrated",
        )
        enriched_at = lead.get("enriched_at") or ""
        if enriched_at > (bucket["latest_enriched_at"] or ""):
            bucket["latest_enriched_at"] = enriched_at
        if not bucket["industry"] and lead.get("company_industry"):
            bucket["industry"] = lead["company_industry"].strip().lower()
        bucket["techs"].update(
            t.lower() for t in _extract_techs(lead.get("enrichment_data") or {})
            if t
        )

    # --- Signals come from the canonical signals table ----------------
    sig_resp = (
        db.table("signals")
        .select(
            "lead_id, signal_type, signal_strength, signal_source, "
            "signal_data, detected_at, expires_at"
        )
        .eq("org_id", org_id)
        .execute()
    )
    for row in (sig_resp.data or []):
        domain = lead_to_domain.get(row.get("lead_id"))
        if not domain or _is_expired(row.get("expires_at")):
            continue
        sig = _clean_signal(row)
        if not sig:
            continue
        bucket = by_domain[domain]
        key = sig["headline"].lower()
        if key not in bucket["seen_headlines"]:
            bucket["seen_headlines"].add(key)
            bucket["signals"].append(sig)

    # Keep the most-recently-enriched MAX_NODES companies.
    companies = sorted(
        by_domain.values(),
        key=lambda c: c["latest_enriched_at"] or "",
        reverse=True,
    )[:MAX_NODES]

    # Rank + trim each company's signals (strongest, freshest first).
    for c in companies:
        c["signals"].sort(key=_signal_rank, reverse=True)
        c["signals"] = c["signals"][:MAX_SIGNALS_PER_NODE]

    nodes = [_node(c) for c in companies]
    edges = _build_edges(companies)

    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "edge_types": _edge_type_breakdown(edges),
            "max_nodes": MAX_NODES,
        },
    }


# ---------------------------------------------------------------------
# Signal cleaning
# ---------------------------------------------------------------------


def _clean_signal(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Turn one raw ``signals`` row into a node-ready signal, or None if
    it can't be made sense of. The raw ``signal_source`` is dropped."""
    stype = (row.get("signal_type") or "").strip()
    if not stype:
        return None
    data = row.get("signal_data")
    if not isinstance(data, dict):
        data = {}

    layer = SIGNAL_TYPE_TO_LAYER.get(stype)
    headline, detail, evidence, occurred = _signal_text(stype, data)
    if not headline:
        return None

    raw_source = (row.get("signal_source") or "").strip().lower()
    return {
        "layer": layer,                       # may be None (node-only)
        "type": stype,
        "headline": headline,
        "detail": detail,
        "evidence": evidence,
        "recommended_use": LAYER_PLAY.get(layer, "") if layer else "",
        "risk_note": LAYER_RISK_NOTE_THREAT if layer == "threat" else "",
        "strength": _as_unit_float(row.get("signal_strength"), 0.5),
        "freshness": "unknown",               # recency is timestamp-driven
        "occurred_at": occurred or (row.get("detected_at") or ""),
        "source": SOURCE_LABEL.get(raw_source, DEFAULT_SOURCE_LABEL),
    }


def _signal_text(
    stype: str, data: Dict[str, Any],
) -> Tuple[str, str, List[str], str]:
    """Derive (headline, detail, evidence, occurred_at) from a raw
    signal's thin ``signal_data``. Built against the live signal shapes
    in the Vera org; unknown types fall back to ``summary``."""
    summary = (data.get("summary") or "").strip()
    occurred = (data.get("date") or "").strip()

    if stype == "cve_in_stack":
        titles = [
            (m.get("cve_title") or "").strip()
            for m in (data.get("matches") or [])
            if isinstance(m, dict) and (m.get("cve_title") or "").strip()
        ]
        count = data.get("count")
        n = count if isinstance(count, int) else len(titles)
        headline = (
            f"{n} known CVEs in the detected stack" if n
            else "Known CVEs in the detected stack"
        )
        return (
            headline,
            "Detected technologies overlap known-exploited vulnerabilities.",
            _dedup(titles)[:4],
            occurred,
        )

    if stype == "hiring_sales_bd":
        roles = [r.strip() for r in (data.get("roles") or []) if isinstance(r, str) and r.strip()]
        headline = "Hiring — building outbound capacity"
        if roles:
            extra = f" +{len(roles) - 1} more" if len(roles) > 1 else ""
            headline = f"Hiring — {roles[0]}{extra}"
        return (
            headline,
            "Open sales / BD roles — a growth motion to support.",
            _dedup(roles)[:4],
            occurred,
        )

    if stype == "security_team_expansion":
        titles = [t.strip() for t in (data.get("sample_titles") or []) if isinstance(t, str) and t.strip()]
        cnt = data.get("security_role_count")
        n = cnt if isinstance(cnt, int) else len(titles)
        headline = (
            f"Scaling the security team — {n} open roles" if n
            else "Scaling the security team"
        )
        return (
            headline,
            "Active security build-out — tooling decisions are live now.",
            _dedup(titles)[:4],
            occurred,
        )

    if stype == "security_leadership_hire":
        company = (data.get("company") or "").strip()
        title = (data.get("primary_title") or "a security leader").strip()
        headline = f"{company} — new {title}" if company else f"New {title}"
        detail = (data.get("hire_event_summary") or "").strip() or (
            "New security leadership — a vendor-stack review window."
        )
        return headline, detail, [], occurred

    # new_leadership / new_partnership / product_launch / recently_funded
    # / recent_acquisition / expansion — all carry a `summary`.
    headline = summary or stype.replace("_", " ").strip().capitalize()
    product = (data.get("product") or "").strip()
    detail = f"Product: {product}" if product else ""
    return headline, detail, [], occurred


def _dedup(items: List[str]) -> List[str]:
    """De-duplicate a list of strings, preserving first-seen order."""
    return list(dict.fromkeys(items))


# ---------------------------------------------------------------------
# Nodes / edges
# ---------------------------------------------------------------------


def _node(c: Dict[str, Any]) -> Dict[str, Any]:
    """Serialise one company bucket into a graph node. Only JSON-safe
    fields are emitted — the bucket's internal sets stay behind."""
    layers = sorted({s["layer"] for s in c["signals"] if s.get("layer")})
    return {
        "id": c["domain"],
        "label": c["company_name"],
        "industry": c["industry"] or "unknown",
        "lead_rating": c["lead_rating"],
        "lead_count": c["lead_count"],
        "signal_count": len(c["signals"]),
        "categories": layers,
        "has_threat": "threat" in layers,
        "signals": c["signals"],
    }


def _build_edges(companies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One edge per shared situation.

    Situation layers (leadership / growth / threat) connect companies
    that share a signal layer; the tech layer keeps the v1 'lookalike'
    overlap. Each company-pair gets at most one edge per layer — the
    frontend filters by ``edge_type`` to keep the canvas readable.
    """
    edges: List[Dict[str, Any]] = []

    # ---- situation layers ----
    for layer in ("leadership", "growth", "threat"):
        # (company, best signal rank in this layer) for each member.
        members: List[Tuple[Dict[str, Any], float]] = []
        for c in companies:
            ranks = [
                _signal_rank(s) for s in c["signals"]
                if s.get("layer") == layer
            ]
            if ranks:
                members.append((c, max(ranks)))
        if len(members) < 2:
            continue
        # Strongest situations first; cap the fan-out so a big cohort
        # doesn't carpet the canvas.
        members.sort(key=lambda m: m[1], reverse=True)
        members = members[:MAX_LAYER_FANOUT]
        for (a, rank_a), (b, rank_b) in combinations(members, 2):
            edges.append({
                "source": a["domain"],
                "target": b["domain"],
                "edge_type": layer,
                "weight": round((rank_a + rank_b) / 2.0, 3),
                "why": LAYER_EDGE_LABEL[layer],
            })

    # ---- tech lookalike layer ----
    for a, b in combinations(companies, 2):
        shared = a["techs"] & b["techs"]
        if len(shared) >= MIN_SHARED_TECHS:
            preview = ", ".join(sorted(shared)[:3])
            more = "…" if len(shared) > 3 else ""
            edges.append({
                "source": a["domain"],
                "target": b["domain"],
                "edge_type": "tech",
                "weight": round(min(1.0, 0.3 + 0.1 * len(shared)), 3),
                "why": f"{len(shared)} shared technologies ({preview}{more})",
            })

    return edges


def _edge_type_breakdown(edges: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for e in edges:
        counts[e["edge_type"]] += 1
    return dict(counts)


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------


def _strongest_rating(current: str, candidate: str) -> str:
    """Promote to the stronger of two ratings. hot > warm > cold > unrated."""
    order = {"unrated": 0, "cold": 1, "warm": 2, "hot": 3}
    if order.get(candidate or "unrated", 0) > order.get(current or "unrated", 0):
        return candidate
    return current


def _extract_techs(enrichment: Dict[str, Any]) -> List[str]:
    """Pull a flat list of detected technology names. The names (e.g.
    "Splunk") are user-facing; the store keys are not surfaced."""
    out: List[str] = []
    pl_tech = (enrichment.get("predictleads_tech") or {}).get("technologies") or []
    for t in pl_tech:
        name = (t.get("name") or t.get("category") or "") if isinstance(t, dict) else str(t)
        if name:
            out.append(name)
    if not out:
        ts = enrichment.get("tech_stack") or {}
        for key in ("technologies_detected", "technologies"):
            for t in ts.get(key) or []:
                name = (t.get("name") or "") if isinstance(t, dict) else str(t)
                if name:
                    out.append(name)
    return out


def _as_unit_float(v: Any, default: float) -> float:
    """Coerce to a float clamped to [0, 1]; ``default`` on anything odd.
    Tolerates numeric strings — the signals table stores strength as text."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return max(0.0, min(1.0, f))


def _parse_dt(ts: Any) -> Optional[datetime]:
    """Parse an ISO-8601 string into an aware datetime, or None.
    Tolerates a trailing 'Z' (Python 3.9's fromisoformat does not)."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age_days(ts: Any) -> Optional[float]:
    """Age of an ISO-8601 timestamp in days, or None if unparseable."""
    dt = _parse_dt(ts)
    if dt is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)


def _is_expired(ts: Any) -> bool:
    """True if an ISO-8601 ``expires_at`` is in the past."""
    dt = _parse_dt(ts)
    return dt is not None and dt < datetime.now(timezone.utc)


def _recency(sig: Dict[str, Any]) -> float:
    """0..1 — how 'now' this signal is, from its timestamp. Half-life
    ~90 days, floored so an old-but-real event still counts a little."""
    age = _age_days(sig.get("occurred_at"))
    if age is None:
        return 0.5
    return max(0.1, 0.5 ** (age / 90.0))


def _signal_rank(sig: Dict[str, Any]) -> float:
    """Sort key for signals — strong AND fresh floats to the top."""
    return sig["strength"] * _recency(sig)
