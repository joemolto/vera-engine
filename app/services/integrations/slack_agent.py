"""@Vera in Slack — conversational agent over org-scoped tools.

A Slack message (app mention or DM) arrives via the events router,
gets normalized out of Slack's markup, and runs through a Claude tool
loop. The MODEL does the reading — "go research Monzo.com", a bare
"monzo.com", "what's new at GoCardless?" all resolve to tool calls; no
regex intent matching. Replies post back with the org's bot token.

Tenant isolation: every tool closes over the org_id resolved from the
Slack workspace (team_id → org_integrations). Tools never accept an
org parameter and always filter reads to that org. Research spends run
through the same cap + credit path as the dashboard research bar.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

import anthropic
import httpx
from supabase import Client

from app.config import settings

logger = logging.getLogger(__name__)

_ACCOUNTS = f"{settings.DASHBOARD_URL}/dashboard/accounts"
MAX_TURNS = 6
REPLY_TOKEN_BUDGET = 700


# ---------------------------------------------------------------------------
# Slack message normalization
# ---------------------------------------------------------------------------

def normalize_slack_text(text: str) -> str:
    """Strip Slack's markup so the model sees what the human typed.

    - <@U12345> mentions → removed (it's us being addressed)
    - <http://monzo.com|monzo.com> → monzo.com (prefer the label)
    - <http://monzo.com> → http://monzo.com
    - &amp; &lt; &gt; unescaped
    """
    text = re.sub(r"<@[A-Z0-9]+>", "", text or "")
    text = re.sub(r"<(?:mailto:)?([^>|]+)\|([^>]+)>", r"\2", text)
    text = re.sub(r"<([^>|]+)>", r"\1", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return text.strip()


# ---------------------------------------------------------------------------
# Org-scoped tools
# ---------------------------------------------------------------------------

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "list_watched_accounts",
        "description": "List the accounts this workspace's org is watching in Vera (name, domain, monitoring state).",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "account_overview",
        "description": "Full picture of one watched account: why it matters, profile score, key contacts, freshest signals. Use for questions like 'what do we know about X' or 'who should I talk to at X'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Company domain, e.g. monzo.com"}
            },
            "required": ["domain"],
            "additionalProperties": False,
        },
    },
    {
        "name": "whats_new",
        "description": "Recent signals for a watched account over the last N days.",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 7},
            },
            "required": ["domain"],
            "additionalProperties": False,
        },
    },
    {
        "name": "security_leaders",
        "description": "Search Vera's OWN records for who runs security at a company: leadership named in Vera's research signals, the cached key contacts, and the security-team profile. Check this BEFORE Apollo or the web when asked who the CISO / security leader is.",
        "input_schema": {
            "type": "object",
            "properties": {"domain": {"type": "string"}},
            "required": ["domain"],
            "additionalProperties": False,
        },
    },
    {
        "name": "apollo_people_search",
        "description": "Search the Apollo B2B people database for people at a company by title. Free (no credits). Use when Vera's own records don't answer a who-works-there question. Results may have obfuscated surnames.",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "titles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Job titles to search, e.g. [\"CISO\", \"Head of Security\"]",
                },
            },
            "required": ["domain", "titles"],
            "additionalProperties": False,
        },
    },
    {
        "name": "web_search",
        "description": "Live web search. Last resort after Vera's records and Apollo — for facts neither has (e.g. a newly appointed exec, breaking news). Treat results as leads to report with their source, never as verified Vera data.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "research_company",
        "description": "Run Vera's full research on a company by DOMAIN and add it to the org's accounts. Spends research budget and counts against plan caps — only call when the user clearly asked to research/enrich/add a company, and only with a domain you are confident is the company they mean. If you are not sure of the exact domain, ask the user to confirm it instead of guessing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "e.g. monzo.com"},
                "company_name": {"type": "string", "description": "Optional display-name hint"},
            },
            "required": ["domain"],
            "additionalProperties": False,
        },
    },
]


def _signals_for(db: Client, org_id: str, company_id: Optional[str], domain: str, days: int, limit: int = 8) -> List[str]:
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows: List[dict] = []
    if company_id:
        rows += (
            db.table("signals")
            .select("signal_type, signal_data, detected_at")
            .eq("org_id", org_id)
            .eq("company_id", company_id)
            .gte("detected_at", since)
            .order("detected_at", desc=True)
            .limit(limit)
            .execute()
            .data
            or []
        )
    lead_rows = (
        db.table("leads").select("id").eq("org_id", org_id).eq("company_domain", domain).limit(20).execute().data or []
    )
    for lr in lead_rows[:5]:
        rows += (
            db.table("signals")
            .select("signal_type, signal_data, detected_at")
            .eq("org_id", org_id)
            .eq("lead_id", lr["id"])
            .gte("detected_at", since)
            .order("detected_at", desc=True)
            .limit(3)
            .execute()
            .data
            or []
        )
    rows.sort(key=lambda r: r.get("detected_at") or "", reverse=True)
    out: List[str] = []
    for r in rows:
        data = r.get("signal_data") or {}
        line = (data.get("headline") if isinstance(data, dict) else None) or str(
            r.get("signal_type", "")
        ).replace("_", " ")
        stamp = str(r.get("detected_at") or "")[:10]
        entry = f"{line} ({stamp})" if stamp else line
        if entry not in out:
            out.append(entry)
        if len(out) >= limit:
            break
    return out


def _watch_row(db: Client, org_id: str, domain: str) -> Optional[dict]:
    rows = (
        db.table("watchlist")
        .select("id, company_name, status, monitoring_active")
        .eq("org_id", org_id)
        .eq("company_domain", domain.lower().strip())
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def run_tool(db: Client, org_id: str, name: str, args: dict) -> dict:
    """Execute one tool, org-scoped. Returns a JSON-safe dict."""
    if name == "list_watched_accounts":
        rows = (
            db.table("watchlist")
            .select("company_name, company_domain, status, monitoring_active")
            .eq("org_id", org_id)
            .eq("status", "active")
            .limit(30)
            .execute()
            .data
            or []
        )
        return {
            "accounts": [
                {
                    "name": r.get("company_name"),
                    "domain": r.get("company_domain"),
                    "monitoring": bool(r.get("monitoring_active")),
                }
                for r in rows
            ]
        }

    domain = str(args.get("domain") or "").lower().strip().removeprefix("www.")
    if not domain and name != "web_search":
        return {"error": "domain_required"}

    if name == "account_overview":
        if not _watch_row(db, org_id, domain):
            return {"error": "not_watched", "hint": "This account isn't in the org's Vera accounts. Offer to research it."}
        comps = (
            db.table("companies")
            .select("id, name, why_this_matters, profile_score, key_contacts")
            .eq("domain", domain)
            .limit(1)
            .execute()
            .data
        )
        comp = comps[0] if comps else {}
        wtm = comp.get("why_this_matters") or {}
        profile = comp.get("profile_score") or {}
        contacts = comp.get("key_contacts") or []
        return {
            "name": comp.get("name") or domain,
            "domain": domain,
            "why_this_matters": wtm.get("paragraph") if isinstance(wtm, dict) else None,
            "profile_score": profile.get("composite") if isinstance(profile, dict) else None,
            "key_contacts": [
                {"name": c.get("name"), "title": c.get("title")}
                for c in contacts[:4]
                if isinstance(c, dict)
            ],
            "recent_signals": _signals_for(db, org_id, comp.get("id"), domain, days=30),
            "dossier_url": f"{_ACCOUNTS}/{domain}",
        }

    if name == "whats_new":
        if not _watch_row(db, org_id, domain):
            return {"error": "not_watched", "hint": "Offer to research it."}
        days = int(args.get("days") or 7)
        comps = (
            db.table("companies").select("id").eq("domain", domain).limit(1).execute().data
        )
        cid = comps[0]["id"] if comps else None
        return {
            "domain": domain,
            "days": days,
            "signals": _signals_for(db, org_id, cid, domain, days=days),
            "dossier_url": f"{_ACCOUNTS}/{domain}",
        }

    if name == "security_leaders":
        comps = (
            db.table("companies")
            .select("id, name, key_contacts, security_team_profile")
            .eq("domain", domain)
            .limit(1)
            .execute()
            .data
        )
        comp = comps[0] if comps else {}
        leaders: list = []
        try:
            from app.services.key_contacts import _signal_leaders
            if comp.get("id"):
                leaders = _signal_leaders(db, company_id=comp["id"], domain=domain)
        except Exception as e:  # noqa: BLE001
            logger.warning("security_leaders signal lookup failed: %s", e)
        profile = comp.get("security_team_profile") or {}
        return {
            "leaders_named_in_vera_research": [
                {"name": l["name"], "title": l["title"], "found": str(l.get("detected_at") or "")[:10]}
                for l in leaders
            ],
            "key_contacts": [
                {"name": c.get("name"), "title": c.get("title"), "source": c.get("source") or "apollo"}
                for c in (comp.get("key_contacts") or [])[:5]
                if isinstance(c, dict)
            ],
            "security_team_top_titles": (profile.get("top_titles") or [])[:6],
        }

    if name == "apollo_people_search":
        import httpx as _httpx
        if not settings.APOLLO_API_KEY:
            return {"error": "apollo_not_configured"}
        titles = [str(t) for t in (args.get("titles") or [])][:15]
        if not titles:
            return {"error": "titles_required"}
        try:
            res = _httpx.post(
                "https://api.apollo.io/api/v1/mixed_people/api_search",
                json={
                    "person_titles": titles,
                    "q_organization_domains_list": [domain],
                    "per_page": 10,
                    "page": 1,
                },
                headers={"X-Api-Key": settings.APOLLO_API_KEY},
                timeout=20,
            )
            res.raise_for_status()
            people = res.json().get("people") or []
        except Exception as e:  # noqa: BLE001
            logger.warning("agent apollo search failed for %s: %s", domain, e)
            return {"error": "apollo_search_failed"}
        return {
            "results": [
                {
                    "name": " ".join(
                        x for x in [p.get("first_name"), p.get("last_name")] if x
                    ),
                    "title": p.get("title"),
                    "linkedin_url": p.get("linkedin_url"),
                }
                for p in people[:10]
            ]
        }

    if name == "web_search":
        import httpx as _httpx
        if not settings.TAVILY_API_KEY:
            return {"error": "web_search_not_configured"}
        query = str(args.get("query") or "").strip()
        if not query:
            return {"error": "query_required"}
        try:
            res = _httpx.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": settings.TAVILY_API_KEY,
                    "query": query,
                    "max_results": 5,
                    "search_depth": "basic",
                },
                timeout=20,
            )
            res.raise_for_status()
            hits = res.json().get("results") or []
        except Exception as e:  # noqa: BLE001
            logger.warning("agent web search failed: %s", e)
            return {"error": "web_search_failed"}
        return {
            "results": [
                {
                    "title": h.get("title"),
                    "url": h.get("url"),
                    "snippet": str(h.get("content") or "")[:400],
                }
                for h in hits
            ]
        }

    if name == "research_company":
        from fastapi import HTTPException
        from app.services.company_enricher import enrich_company_only, ensure_watchlist_row
        from app.services import credits as _credits
        from app.services.plan_limits import enforce_account_cap

        try:
            if not _watch_row(db, org_id, domain):
                enforce_account_cap(org_id, db)
        except HTTPException as e:
            return {"error": "account_cap", "detail": str(e.detail)}
        try:
            result = enrich_company_only(db, org_id, domain, company_name_hint=args.get("company_name"))
        except Exception as e:  # noqa: BLE001
            logger.warning("slack agent research failed for %s/%s: %s", org_id, domain, e)
            return {"error": "research_failed", "detail": str(e)[:200]}
        _credits.charge(
            db, org_id=org_id, action="deep_enrich",
            related_id=result.get("company_id"),
            metadata={"domain": domain, "source": "slack_agent"},
        )
        company = result.get("company") or {}
        ensure_watchlist_row(db, org_id, domain, company_name=company.get("name"))
        wtm = company.get("why_this_matters") or {}
        return {
            "status": "researched_and_added",
            "name": company.get("name") or domain,
            "domain": domain,
            "why_this_matters": wtm.get("paragraph") if isinstance(wtm, dict) else None,
            "sources_completed": result.get("sources_completed", []),
            "dossier_url": f"{_ACCOUNTS}/{domain}",
        }

    return {"error": f"unknown_tool:{name}"}


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

_SYSTEM = """You are Vera, the sales-intelligence agent for cybersecurity sellers, talking in Slack.

Voice: concise, concrete, plain sentences. No hype, no filler, no emojis unless the user uses them first. You are talking to a sales rep who is busy.

Rules:
- Use the tools for ANY factual claim about accounts, signals or contacts. Never invent names, dates, signals or numbers. If the data isn't there, say so.
- When asked to FIND something (a person, a role, a fact), exhaust your sources in order before giving up: (1) Vera's own records — account_overview and security_leaders; (2) the Apollo database — apollo_people_search; (3) the open web — web_search. Only after all three may you conclude, phrased like: "From everything Vera can see — our own research, the B2B database and public sources — I can't find a named CISO at X." Distinguish "I can't find evidence of one" from "there isn't one".
- Name your source and its date for factual claims ("Vera's research, Jun 2026" / "Apollo" / "per <url>"). Web results are reported with their source, never presented as verified Vera data.
- research_company spends the org's research budget. Only call it when the user clearly asked to research/enrich/add a company. If you're not confident of the exact domain (e.g. they said a company name and you'd be guessing), ask them to confirm the domain first.
- If an account isn't watched, say so and offer to research it.
- Format for Slack mrkdwn: *bold*, bullet lines starting with •, links as <url|label>. Keep replies short — a few lines, not essays. Link the dossier when you reference an account.
- You act for ONE organisation. Never mention other customers or any account the tools didn't return."""


def run_agent(db: Client, org_id: str, user_text: str, client_name: Optional[str] = None) -> str:
    """One Slack message in, one reply out. Bounded tool loop."""
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    system = _SYSTEM + (f"\nThe org you work for: {client_name}." if client_name else "")
    messages: List[dict] = [{"role": "user", "content": user_text}]

    for _ in range(MAX_TURNS):
        resp = client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=REPLY_TOKEN_BUDGET,
            system=system,
            messages=messages,
            tools=TOOLS,
        )
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            texts = [b.text for b in resp.content if b.type == "text"]
            return ("\n".join(texts)).strip() or "I didn't catch that — try asking about one of your accounts."
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for tu in tool_uses:
            try:
                out = run_tool(db, org_id, tu.name, dict(tu.input or {}))
            except Exception as e:  # noqa: BLE001
                logger.warning("slack agent tool %s crashed: %s", tu.name, e)
                out = {"error": "tool_failed"}
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(out)[:6000],
                }
            )
        messages.append({"role": "user", "content": results})

    return "That took more digging than I can do in one message — open Vera and I'll have it all there."


# ---------------------------------------------------------------------------
# Reply transport
# ---------------------------------------------------------------------------

def post_reply(bot_token: str, channel: str, text: str, thread_ts: Optional[str] = None) -> None:
    payload: Dict[str, Any] = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    with httpx.Client(timeout=10) as client:
        resp = client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
            json=payload,
        )
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400 or not body.get("ok"):
        logger.warning("slack reply failed: %s %s", resp.status_code, body.get("error"))
