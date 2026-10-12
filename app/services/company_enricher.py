"""Company-only enrichment — no person/lead context.

Used by:
- `POST /api/v1/leads/quick-enrich` with a domain payload (the rep pasted a
  company website into the dashboard research bar)
- `POST /api/v1/leads/quick-enrich` with a LinkedIn URL, as a follow-up
  pass when the resolved company has no companies-table row yet

Aggregates the same data sources used by the lead enrichment path
(PredictLeads bundle, Apollo orgs/enrich, Perplexity deep_research, Jina
website content) and writes them into the canonical `companies` table via
`upsert_company`.

All underlying enrichers cache their responses, so calling this twice for
the same domain in one quick-enrich call is cheap.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from supabase import Client

from app.services.companies import (
    get_company_by_domain,
    upsert_company,
)
from app.services.enrichers.deep_research import enrich_deep_research
from app.services.enrichers.predictleads import enrich_predictleads
from app.services.enrichers.tech_stack import _fetch_apollo_org_data, enrich_tech_stack

logger = logging.getLogger(__name__)


def enrich_company_only(
    db: Client,
    org_id: str,
    domain: str,
    *,
    company_name_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """Run company-level enrichment for a domain and upsert into `companies`.

    Args:
        db: Supabase client.
        org_id: Caller's org — used to fetch client_context for the
            client-aware Perplexity prompt + similar-companies filter.
        domain: Company domain (e.g. "relexsolutions.com"). Will be
            lowercased.
        company_name_hint: Optional name to use when PredictLeads /
            Apollo can't resolve one (e.g. the rep typed a name we want
            to fall back to).

    Returns:
        Dict with shape `{ "domain", "company_id", "company": <row>,
        "sources_completed": [...] }`. Raises on hard failures (e.g.
        bad domain). Never raises on individual source failures —
        partial data is still written and the source name is omitted
        from sources_completed.
    """
    domain = (domain or "").strip().lower()
    if not domain:
        raise ValueError("domain is required")

    # Org context — drives Perplexity's client-aware prompt + the
    # similar-companies relevance filter.
    org_resp = (
        db.table("organisations")
        .select("client_context")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    client_context = (org_resp.data or [{}])[0].get("client_context") or {}

    enrichment_data: Dict[str, Any] = {}
    sources_completed: list[str] = []

    # ------------------------------------------------------------------
    # PredictLeads bundle — name/industry/size/location from company_info,
    # plus news/jobs/tech/financing/similar for the broader signal set.
    # ------------------------------------------------------------------
    try:
        pl = enrich_predictleads(domain, client_context=client_context, db=db)
    except Exception as e:
        logger.warning("PredictLeads bundle failed for %s: %s", domain, e)
        pl = None

    pl_company_info: Dict[str, Any] = {}
    if pl:
        pl_company_info = pl.get("company_info") or {}
        enrichment_data["predictleads_company"] = pl_company_info
        enrichment_data["predictleads_news"] = pl.get("news_events")
        enrichment_data["predictleads_tech"] = pl.get("technology")
        enrichment_data["predictleads_jobs"] = pl.get("jobs")
        enrichment_data["predictleads_financing"] = pl.get("financing")
        enrichment_data["predictleads_similar"] = pl.get("similar_companies")
        sources_completed.append("predictleads")

    # Resolve a company name. Order: caller hint → PredictLeads → Apollo
    # (domain-matched, below) → web resolver (below) → bare domain root.
    # Left None here when no structured source has it yet, so the web
    # fallback can fill it before we ever settle for the domain root.
    company_name = company_name_hint or pl_company_info.get("name") or None

    # ------------------------------------------------------------------
    # Apollo orgs/enrich — business intel (revenue, funding, headcount,
    # founded year). Also returns technologies which `enrich_tech_stack`
    # uses internally as a fallback when PL has nothing.
    # ------------------------------------------------------------------
    try:
        _techs, _jobs, apollo_bi = _fetch_apollo_org_data(domain)
    except Exception as e:
        logger.warning("Apollo orgs/enrich failed for %s: %s", domain, e)
        apollo_bi = None

    if apollo_bi:
        enrichment_data["apollo"] = apollo_bi
        # Apollo's name is usually more authoritative than the PL one —
        # but ONLY when Apollo's enrich-by-domain actually matched the org
        # whose PRIMARY domain is the one we asked for. Apollo sometimes
        # matches a subsidiary that merely lists the domain as a secondary
        # (e.g. tesco.com -> "Tesco Bengaluru"), which mislabels the whole
        # account. On a primary-domain mismatch, keep the PredictLeads /
        # domain-derived name instead of promoting the subsidiary's.
        apollo_name = apollo_bi.get("name")
        apollo_primary = (apollo_bi.get("primary_domain") or "").strip().lower()
        if apollo_name and apollo_primary == domain:
            company_name = apollo_name
        elif apollo_name:
            # Domain mismatch: Apollo matched a different / subsidiary org
            # (e.g. tesco.com → "Tesco Bengaluru"). Don't trust its name —
            # leave it to PredictLeads or the web resolver below.
            logger.warning(
                "Apollo name '%s' not trusted for %s: primary_domain '%s' != requested; deferring to web/PredictLeads",
                apollo_name, domain, apollo_primary,
            )
        sources_completed.append("apollo")

    # ------------------------------------------------------------------
    # Web/LLM name fallback — if neither PredictLeads nor a domain-matched
    # Apollo record named the company, ask Perplexity/Claude what company
    # owns this domain BEFORE settling for the bare domain root. Fires only
    # on this gap path (no cost on the happy path). This is what makes a
    # pasted URL (e.g. tesco.com) resolve to the real company even when
    # Apollo is absent or matched a subsidiary.
    # ------------------------------------------------------------------
    if not company_name:
        from app.services.domain_resolver import resolve_company_name_from_domain
        try:
            resolved = resolve_company_name_from_domain(domain)
        except Exception as e:
            logger.warning("Web name resolution failed for %s: %s", domain, e)
            resolved = None
        if resolved:
            company_name = resolved
            sources_completed.append("name_resolver")
        else:
            company_name = domain.split(".")[0].capitalize()

    # ------------------------------------------------------------------
    # Tech stack — PL-first, falls back to Apollo + Jina + Claude. The
    # `predictleads_tech=` pass-through avoids a duplicate PL fetch.
    # ------------------------------------------------------------------
    try:
        tech = enrich_tech_stack(
            db,
            domain,
            client_context=client_context,
            predictleads_tech=enrichment_data.get("predictleads_tech"),
        )
        if tech:
            enrichment_data["tech_stack"] = tech
            sources_completed.append("tech_stack")
    except Exception as e:
        logger.warning("Tech stack enrichment failed for %s: %s", domain, e)

    # ------------------------------------------------------------------
    # Merge live DNS detections (Tier A — observable infra, no inference):
    # MX / SPF / DMARC → email-security + edge vendors. Runs on the ACCOUNT
    # path too (not just the lead path) so every watched account gets its
    # email-security posture, not only ones enriched via a person.
    # ------------------------------------------------------------------
    try:
        from app.services.stack_detection import merge_dns_into_tech_stack
        enrichment_data["tech_stack"] = merge_dns_into_tech_stack(
            domain, enrichment_data.get("tech_stack")
        )
        if "tech_stack" not in sources_completed:
            sources_completed.append("tech_stack")
    except Exception as e:
        logger.warning("DNS merge failed for %s: %s", domain, e)

    # ------------------------------------------------------------------
    # Perplexity deep research — 7d cached per (domain, client_context),
    # so cheap on repeat calls. Provides description + compliance +
    # security_posture, all surfaced on the dossier.
    # ------------------------------------------------------------------
    try:
        deep = enrich_deep_research(
            company_name, domain, client_context=client_context, db=db,
        )
        if deep:
            enrichment_data["deep_research"] = deep
            sources_completed.append("deep_research")
    except Exception as e:
        logger.warning("Deep research failed for %s: %s", domain, e)

    # ------------------------------------------------------------------
    # Credential exposure — LeakRadar stealer-log / dark-web check. Free
    # searches (we never unlock plain-text), runs on the ACCOUNT path so
    # every watched account gets a fresh exposure read on each monitor
    # pass (Phase B → here → Phase G re-scores). Projected into Risk
    # signals below. Surfaces exposure, never credentials (2026-06-18).
    # ------------------------------------------------------------------
    try:
        from app.services.enrichers.credential_exposure import check_credential_exposure
        cred = check_credential_exposure(db, domain)
        if cred is not None:
            enrichment_data["credential_exposure"] = cred
            sources_completed.append("credential_exposure")
    except Exception as e:
        logger.warning("Credential exposure check failed for %s: %s", domain, e)

    # ------------------------------------------------------------------
    # Upsert canonical companies row. Reuses the same field mapping that
    # the lead path uses via `upsert_company_from_enrichment`, but called
    # directly so we can pass company_name explicitly (no lead row).
    # ------------------------------------------------------------------
    deep = enrichment_data.get("deep_research") or {}
    tech_payload = enrichment_data.get("tech_stack") or {}
    techs = (
        tech_payload.get("technologies_detected") if isinstance(tech_payload, dict) else None
    )
    # Tier 2/3 categorisation: refine "other" techs against the learned
    # tech_category table and queue genuinely-unknown names for the AI job.
    if techs:
        try:
            from app.services.tech_taxonomy import apply_taxonomy
            techs = apply_taxonomy(db, techs)
        except Exception as e:
            logger.warning("apply_taxonomy failed for %s: %s", domain, e)

    company_id = upsert_company(
        db,
        domain,
        name=company_name,
        industry=(
            pl_company_info.get("industry")
            or (apollo_bi or {}).get("industry")
        ),
        size_band=(
            pl_company_info.get("size")
            or _bucket_employees((apollo_bi or {}).get("estimated_num_employees"))
        ),
        hq_country=(
            pl_company_info.get("location")
            or (apollo_bi or {}).get("country")
        ),
        founded_year=(
            pl_company_info.get("founded_year")
            or (apollo_bi or {}).get("founded_year")
        ),
        linkedin_url=(
            pl_company_info.get("linkedin_url")
            or (apollo_bi or {}).get("linkedin_url")
        ),
        description=(
            pl_company_info.get("description")
            or deep.get("company_description")
            or (apollo_bi or {}).get("short_description")
        ),
        technologies=techs,
        compliance=deep.get("compliance") if isinstance(deep, dict) else None,
        predictleads_data={
            "company": enrichment_data.get("predictleads_company"),
            "news": enrichment_data.get("predictleads_news"),
            "tech": enrichment_data.get("predictleads_tech"),
            "jobs": enrichment_data.get("predictleads_jobs"),
            "financing": enrichment_data.get("predictleads_financing"),
            "similar": enrichment_data.get("predictleads_similar"),
        },
        apollo_data=enrichment_data.get("apollo"),
        deep_research_data=deep if isinstance(deep, dict) else None,
        credential_exposure=enrichment_data.get("credential_exposure"),
    )

    company_row = get_company_by_domain(db, domain)

    # ------------------------------------------------------------------
    # Account-level signals — write rows keyed by company_id (NOT
    # lead_id). Accounts-first paradigm: a watched company surfaces
    # signals (funding events, news, CVEs) even when no person has
    # been fetched yet. Rule since 2026-05-29.
    # ------------------------------------------------------------------
    if company_id:
        try:
            _write_account_level_signals(
                db,
                org_id=org_id,
                company_id=company_id,
                domain=domain,
                pl_news=enrichment_data.get("predictleads_news"),
                pl_financing=enrichment_data.get("predictleads_financing"),
                pl_jobs=enrichment_data.get("predictleads_jobs"),
                threat_alerts=(company_row or {}).get("threat_alerts") if company_row else None,
                credential_exposure=enrichment_data.get("credential_exposure"),
            )
        except Exception as e:
            logger.warning("Account-level signal write failed for %s: %s", domain, e)

    return {
        "domain": domain,
        "company_id": company_id,
        "company": company_row,
        "sources_completed": sources_completed,
    }


# ---------------------------------------------------------------------------
# Public "lite" enrichment — powers the free homepage lead-magnet tool.
# COMPANY-ONLY by design: NO contacts/people/emails/phones, NO Perplexity
# Sonar (the expensive bit), NO org-scoped signal-table writes, NO credit
# charge. Just PredictLeads + Apollo firmographics + FREE DNS posture →
# upsert into the global `companies` table → return safe public
# facts + a public-safe signal summary. Cost ≈ 2-4¢ on a NEW domain; cached
# domains are free. Neutral client_context (no org positioning leak).
# ---------------------------------------------------------------------------
# Public-safe account signal categories (company-level only — never person).
_PUBLIC_REPORT_CATEGORIES = {
    "is_acquired_by": "Acquisition",
    "acquires": "Acquisition",
    "launches": "Product launch",
    "partners_with": "New partnership",
    "expands_offices_to": "Expansion",
    "expands_offices_in": "Expansion",
    "has_issues_with": "Breach / incident reported",
}

# Well-known security vendors — pulled out of the detected stack for the report
# (competitive / displacement intel a cyber seller acts on).
_SECURITY_VENDORS = [
    ("crowdstrike", "CrowdStrike"), ("sentinelone", "SentinelOne"),
    ("microsoft defender", "Microsoft Defender"), ("defender for", "Microsoft Defender"),
    ("palo alto", "Palo Alto Networks"), ("fortinet", "Fortinet"), ("fortigate", "Fortinet"),
    ("zscaler", "Zscaler"), ("okta", "Okta"), ("ping identity", "Ping Identity"),
    ("cyberark", "CyberArk"), ("proofpoint", "Proofpoint"), ("mimecast", "Mimecast"),
    ("abnormal", "Abnormal Security"), ("darktrace", "Darktrace"), ("rapid7", "Rapid7"),
    ("tenable", "Tenable"), ("qualys", "Qualys"), ("splunk", "Splunk"),
    ("sophos", "Sophos"), ("trend micro", "Trend Micro"), ("carbon black", "Carbon Black"),
    ("cloudflare", "Cloudflare"), ("duo security", "Cisco Duo"), ("knowbe4", "KnowBe4"),
    ("varonis", "Varonis"), ("netskope", "Netskope"), ("snyk", "Snyk"),
    ("1password", "1Password"), ("auth0", "Auth0"), ("onelogin", "OneLogin"),
    ("shape security", "Shape Security"), ("imperva", "Imperva"),
    ("cisco umbrella", "Cisco Umbrella"), ("check point", "Check Point"),
    ("sailpoint", "SailPoint"), ("beyondtrust", "BeyondTrust"),
]


def _tech_name(t: Any) -> str:
    if isinstance(t, dict):
        return str(t.get("name") or "")
    return str(t or "")


def _extract_security_vendors(techs) -> list:
    """Pull well-known security vendors out of the detected tech stack. Deduped + capped."""
    if not techs:
        return []
    lowered = [_tech_name(t).lower() for t in techs]
    found: list = []
    for needle, label in _SECURITY_VENDORS:
        if label in found:
            continue
        if any(needle in n for n in lowered):
            found.append(label)
    return found[:8]


def _funding_is_recent(date_str, days: int = 180) -> bool:
    """True only if a financing event's date is within `days` (default 180) —
    mirrors the engine's recently_funded freshness rule. No/unparseable date =
    not recent (drop), so years-old debt/loans never surface as current funding."""
    if not date_str:
        return False
    from datetime import datetime, timezone, timedelta

    try:
        d = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
    except ValueError:
        return False
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - d) <= timedelta(days=days)


def enrich_company_lite(db: Client, domain: str) -> Dict[str, Any]:
    """Cheap, company-only enrichment for the public free-report tool.

    Reuses the same cheap sources as enrich_company_only but drops Sonar,
    contacts, org-scoped signal writes and the credit charge. Cache-first:
    upsert_company does a non-destructive JSONB merge, so this never wipes
    a domain that was already enriched in full — and a recently
    enriched domain costs ~£0 (the vendor bundles are themselves cached).

    Returns: { domain, company_id, company: <safe public row>,
               highlights: [{label, headline}], sources_completed }.
    Never surfaces contact/person data.
    """
    domain = (domain or "").strip().lower()
    if not domain:
        raise ValueError("domain is required")

    enrichment_data: Dict[str, Any] = {}
    sources_completed: list[str] = []

    # PredictLeads bundle — neutral context (no org positioning).
    try:
        pl = enrich_predictleads(domain, client_context={}, db=db)
    except Exception as e:
        logger.warning("[lite] PredictLeads failed for %s: %s", domain, e)
        pl = None
    pl_company_info: Dict[str, Any] = {}
    if pl:
        pl_company_info = pl.get("company_info") or {}
        enrichment_data["predictleads_news"] = pl.get("news_events")
        enrichment_data["predictleads_financing"] = pl.get("financing")
        sources_completed.append("predictleads")

    company_name = pl_company_info.get("name") or None

    # Apollo org firmographics (business intel only — NOT job postings, which
    # are a contact-discovery vector).
    try:
        _techs, _jobs, apollo_bi = _fetch_apollo_org_data(domain)
    except Exception as e:
        logger.warning("[lite] Apollo failed for %s: %s", domain, e)
        apollo_bi = None
    if apollo_bi:
        apollo_name = apollo_bi.get("name")
        apollo_primary = (apollo_bi.get("primary_domain") or "").strip().lower()
        if apollo_name and apollo_primary == domain:
            company_name = apollo_name
        sources_completed.append("apollo")

    if not company_name:
        from app.services.domain_resolver import resolve_company_name_from_domain
        try:
            company_name = resolve_company_name_from_domain(domain)
        except Exception:
            company_name = None
        if not company_name:
            company_name = domain.split(".")[0].capitalize()

    # Tech stack (PL-first) + FREE DNS email-security posture.
    try:
        tech = enrich_tech_stack(
            db, domain, client_context={},
            predictleads_tech=(pl.get("technology") if pl else None),
        )
        if tech:
            enrichment_data["tech_stack"] = tech
            sources_completed.append("tech_stack")
    except Exception as e:
        logger.warning("[lite] tech stack failed for %s: %s", domain, e)
    try:
        from app.services.stack_detection import merge_dns_into_tech_stack
        enrichment_data["tech_stack"] = merge_dns_into_tech_stack(
            domain, enrichment_data.get("tech_stack")
        )
    except Exception as e:
        logger.warning("[lite] DNS merge failed for %s: %s", domain, e)

    tech_payload = enrichment_data.get("tech_stack") or {}
    techs = (
        tech_payload.get("technologies_detected")
        if isinstance(tech_payload, dict) else None
    )

    # Upsert into the global companies table. Non-destructive
    # merge: only the fields we pass are touched; deep_research / credential /
    # contact data from a prior full enrich is preserved untouched. We pass
    # NO contact fields.
    company_id = upsert_company(
        db,
        domain,
        name=company_name,
        industry=pl_company_info.get("industry") or (apollo_bi or {}).get("industry"),
        size_band=(
            pl_company_info.get("size")
            or _bucket_employees((apollo_bi or {}).get("estimated_num_employees"))
        ),
        hq_country=pl_company_info.get("location") or (apollo_bi or {}).get("country"),
        founded_year=pl_company_info.get("founded_year") or (apollo_bi or {}).get("founded_year"),
        linkedin_url=pl_company_info.get("linkedin_url") or (apollo_bi or {}).get("linkedin_url"),
        description=(
            pl_company_info.get("description")
            or (apollo_bi or {}).get("short_description")
        ),
        technologies=techs,
    )

    company_row = get_company_by_domain(db, domain) or {}

    # Public-safe highlights. ONE per category — collapses same-event spam
    # (e.g. three "expanded offices to Barcelona / Madrid / Europe" rows from
    # one announcement → a single Expansion line), keeping the longest/most
    # informative headline. Freshness-gated, then ordered by how useful the
    # trigger is to a seller, capped at 4. Company-level only; derived inline.
    by_cat: Dict[str, str] = {}
    events = (enrichment_data.get("predictleads_news") or {}).get("events") \
        if isinstance(enrichment_data.get("predictleads_news"), dict) else None
    if isinstance(events, list):
        for ev in events:
            if not isinstance(ev, dict):
                continue
            cat = (ev.get("category") or "").lower()
            label = _PUBLIC_REPORT_CATEGORIES.get(cat)
            if not label:
                continue
            headline = (ev.get("summary") or ev.get("article_title") or "").strip()
            if not headline:
                continue
            if _summary_event_is_stale(
                headline, allow_prior_year=(cat == "has_issues_with")
            ):
                continue
            if len(headline) > len(by_cat.get(label, "")):
                by_cat[label] = headline[:280]

    # Funding (financing bundle) — the strongest budget signal.
    fin = enrichment_data.get("predictleads_financing") or {}
    rounds = fin.get("rounds") if isinstance(fin, dict) else None
    if isinstance(rounds, list) and rounds:
        latest = rounds[0] if isinstance(rounds[0], dict) else None
        if latest and latest.get("amount") and _funding_is_recent(latest.get("date")):
            by_cat["Funding"] = (
                f"Raised {latest.get('amount')}"
                + (f" · {latest.get('type')}" if latest.get("type") else "")
            )

    _HIGHLIGHT_PRIORITY = [
        "Funding", "Breach / incident reported", "Acquisition",
        "New partnership", "Expansion", "Product launch",
    ]
    highlights: list[Dict[str, str]] = [
        {"label": label, "headline": by_cat[label]}
        for label in _HIGHLIGHT_PRIORITY
        if label in by_cat
    ][:4]
    signals_count = len(by_cat)

    # Email-security posture (public DNS) — the headline cyber signal.
    try:
        from app.services.stack_detection.collectors.dns_email import (
            email_security_posture,
        )
        email_security = email_security_posture(domain)
    except Exception as e:
        logger.warning("[lite] email posture failed for %s: %s", domain, e)
        email_security = {}

    # Security vendors in their stack (displacement / competitive intel).
    security_stack = _extract_security_vendors(techs)

    # Credential exposure (LeakRadar — free domain search, company-level counts
    # only; we never unlock/reveal credentials). The differentiating hook.
    try:
        from app.services.enrichers.credential_exposure import (
            check_credential_exposure,
        )
        ce = check_credential_exposure(db, domain) or {}
    except Exception as e:
        logger.warning("[lite] credential exposure failed for %s: %s", domain, e)
        ce = {}
    credential_exposure = {
        "employees": int(ce.get("employees_compromised") or 0),
        "recent_90d": int(ce.get("recent_employees_90d") or 0),
        "third_parties": int(ce.get("third_parties_compromised") or 0),
        "most_recent": ce.get("most_recent_surfaced"),
        "has_recent": bool(ce.get("has_recent_exposure")),
    }

    return {
        "domain": domain,
        "company_id": company_id,
        "company": {
            "name": company_row.get("name") or company_name,
            "industry": company_row.get("industry"),
            "size_band": company_row.get("size_band"),
            "hq_country": company_row.get("hq_country"),
            "founded_year": company_row.get("founded_year"),
            "linkedin_url": company_row.get("linkedin_url"),
            "description": company_row.get("description"),
            "technologies": techs or [],
        },
        "highlights": highlights,
        "signals_count": signals_count,
        "email_security": email_security,
        "security_stack": security_stack,
        "credential_exposure": credential_exposure,
        "sources_completed": sources_completed,
    }


def enrich_company_deep(db: Client, domain: str) -> Dict[str, Any]:
    """The emailed free report: the cheap payload plus the security picture.

    Split from `enrich_company_lite` because of latency, not data. The
    overview probes pages, searches Apollo and reads Certificate
    Transparency, which together run well past the web route's timeout —
    so the site shows the lite payload immediately and this lands by email
    minutes later.

    The security overview is best-effort: if it fails entirely the caller
    still gets the same report the site already sends today.
    """
    report = enrich_company_lite(db, domain)

    # Apollo's org payload is cached by the lite call above, so this is a
    # cache read rather than a second API charge.
    job_postings = None
    try:
        from app.services.enrichers.tech_stack import fetch_apollo_job_postings
        job_postings = fetch_apollo_job_postings(report["domain"])
    except Exception as e:
        logger.warning("[deep] job postings failed for %s: %s", domain, e)

    try:
        from app.services.security_overview import build_security_overview
        report["security_overview"] = build_security_overview(
            db,
            domain=report["domain"],
            company_id=report.get("company_id"),
            job_postings=job_postings,
        )
    except Exception as e:
        logger.warning("[deep] security overview failed for %s: %s", domain, e)

    return report


# ---------------------------------------------------------------------------
# Account-level signal projection from company-enrichment payloads
# ---------------------------------------------------------------------------

# Rule (2026-06-05): NO signal older than 6 months may be written to
# the signals table from PredictLeads news. PL's `date` field is the
# article crawl date, not the underlying event date — so historical
# events (e.g. a 2023 acquisition) ship with a fresh date. The real
# event date is embedded in the natural-language summary
# ("on Jul 1st '23.", "in 2024", etc.). This extractor pulls any
# year token (4-digit or "'YY" suffix) and rejects if every found
# year is before the current calendar year. Permissive on purpose —
# summaries without a year token pass through.
_YEAR_TOKEN_RE = re.compile(r"\b(19|20)(\d{2})\b|'(\d{2})\b")


def _summary_event_is_stale(
    summary: str,
    now: Optional[datetime] = None,
    allow_prior_year: bool = False,
) -> bool:
    if not summary:
        return False
    now = now or datetime.now(timezone.utc)
    years: List[int] = []
    for m in _YEAR_TOKEN_RE.finditer(summary):
        if m.group(1) and m.group(2):
            years.append(int(m.group(1) + m.group(2)))
        elif m.group(3):
            years.append(2000 + int(m.group(3)))
    if not years:
        return False
    # Breach news (allow_prior_year, 2026-06-19) gets a 12-month window:
    # keep the current AND previous calendar year so a breach from late last
    # year still surfaces. Everything else stays current-calendar-year only.
    threshold = now.year - 1 if allow_prior_year else now.year
    return all(y < threshold for y in years)


# Map PredictLeads news event categories → canonical signal_type. Source:
# PL's category vocabulary (has_revenue, launches, partners_with, etc.).
# Anything not mapped is dropped — we don't surface noise as signals.
_PL_NEWS_CATEGORY_TO_TYPE: Dict[str, str] = {
    "launches": "product_launch",
    "partners_with": "new_partnership",
    "is_acquired_by": "recent_acquisition",
    "acquires": "recent_acquisition",
    "expands_offices_to": "expansion",
    "expands_offices_in": "expansion",
    # PL "has_issues_with" = journalist reporting a breach / incident /
    # outage. Distinct source from social mentions (signal_source =
    # predictleads_news here vs apidirect_* for social). Mapped to the
    # same axis (Risk) via SIGNAL_AXIS in signal_writer.py.
    "has_issues_with": "breach_mention",
    # PL "hires" / "promotes" / "leaves" all become new_leadership
    # (Commercial axis) — security titles are stripped upstream by
    # enricher._create_predictleads_signals so anything left is a
    # non-security exec change. Previously "hires" went to a now-
    # deprecated "hiring_leadership" type — collapsed 2026-06-10.
    "hires": "new_leadership",
    "promotes": "new_leadership",
    "leaves": "new_leadership",
}

# new_leadership is only a buying signal when it's a senior BUSINESS / tech /
# finance / legal change (new CFO, VP Engineering, General Counsel...). An
# operational or clinical hire ("clinical lead for radiology") tells a
# cybersecurity seller nothing, so it must NOT fire. Require a senior-role
# token AND reject clearly non-business (clinical/medical/academic) roles.
_SENIOR_ROLE_RE = re.compile(
    r"\b(chief|ceo|cfo|coo|cto|cio|ciso|cmo|cro|cpo|cdo|cco|"
    r"president|vice[\s-]?president|\bvp\b|managing director|general counsel|"
    r"head of|director of|\bdirector\b|founder|co-?founder|"
    r"chair(?:man|woman|person)?)\b",
    re.IGNORECASE,
)
_NON_BUSINESS_ROLE_RE = re.compile(
    r"\b(clinical|radiolog|nurs|physician|surgeon|medical|teacher|professor|"
    r"lecturer|coach|chaplain|midwife|paramedic|therapist|pharmacist|dentist)\b",
    re.IGNORECASE,
)


def _is_relevant_leadership(headline: str) -> bool:
    """True only for a senior business/tech/finance/legal leadership change —
    the kind a cyber seller can act on. Filters out operational/clinical hires."""
    h = headline or ""
    if _NON_BUSINESS_ROLE_RE.search(h):
        return False
    return bool(_SENIOR_ROLE_RE.search(h))


def _write_account_level_signals(
    db: Client,
    *,
    org_id: str,
    company_id: str,
    domain: str,
    pl_news: Optional[Dict[str, Any]],
    pl_financing: Optional[Dict[str, Any]],
    pl_jobs: Optional[Dict[str, Any]],
    threat_alerts: Any,
    credential_exposure: Optional[Dict[str, Any]] = None,
) -> None:
    """Project enrichment payloads into per-company signal rows.

    Idempotent-ish: we don't dedupe here; the engine's downstream
    dashboard query is the dedup point. Each enrichment run will
    re-create signals — fine for the chat surfaces that group by
    type, fine for the dashboard which shows the latest per domain.
    """
    from app.services.signal_writer import insert_signals

    now_iso = datetime.now(timezone.utc).isoformat()
    rows: List[Dict[str, Any]] = []

    # PL news → signals. PL events have category, summary, date, optional amount.
    #
    # COLLAPSING SAME-EVENT NOISE (spec 2026-06-10): PL frequently
    # returns multiple "events" that are really one announcement parsed
    # at different geographic specificity — Lemonade showed THREE
    # expansion rows ("US", "Indiana, US", "United Kingdom") from a
    # single article. Within ONE enrichment pass we collapse by
    # (signal_type, event_date) and keep the LONGEST headline (most
    # informative). Different-date events still flow through as
    # separate signals so we don't collapse genuinely distinct news.
    events = (pl_news or {}).get("events") if isinstance(pl_news, dict) else None
    pending_by_key: Dict[tuple, Dict[str, Any]] = {}
    if isinstance(events, list):
        for ev in events:
            if not isinstance(ev, dict):
                continue
            cat = (ev.get("category") or "").lower()
            signal_type = _PL_NEWS_CATEGORY_TO_TYPE.get(cat)
            if not signal_type:
                continue
            headline = (ev.get("summary") or ev.get("article_title") or "").strip()
            if not headline:
                continue
            # A leadership event only counts if it's a senior business change —
            # a clinical/operational hire is noise for a cyber seller.
            if signal_type == "new_leadership" and not _is_relevant_leadership(headline):
                continue
            # Drop events anchored to a past calendar year. PL.date is the
            # article crawl date, not the event date — see _summary_event_is_stale.
            # Breach news gets a 12-month window (current + prior calendar year)
            # (2026-06-19); all other news stays current-year-only.
            if _summary_event_is_stale(
                headline, allow_prior_year=(signal_type == "breach_mention")
            ):
                continue
            detected_at = ev.get("date") or now_iso
            collapse_key = (signal_type, detected_at[:10] if detected_at else "")
            row = {
                "org_id": org_id,
                "company_id": company_id,
                "signal_type": signal_type,
                "signal_source": "predictleads_news",
                "signal_data": {
                    "headline": headline,
                    "category": cat,
                    "amount": ev.get("amount") or None,
                    "url": ev.get("article_url") or None,
                },
                "signal_strength": 0.7,
                "signal_subject_type": "account",
                "detected_at": detected_at,
            }
            existing = pending_by_key.get(collapse_key)
            if existing is None:
                pending_by_key[collapse_key] = row
            else:
                # Same-day same-type — keep the longer (more specific)
                # headline; first writer's signal_data fields stay
                # otherwise unchanged.
                if len(headline) > len(existing["signal_data"]["headline"]):
                    existing["signal_data"]["headline"] = headline
                    if ev.get("article_url"):
                        existing["signal_data"]["url"] = ev.get("article_url")
    rows.extend(pending_by_key.values())

    # PL financing → funding signals
    #
    # 90-DAY FRESHNESS GATE (2026-07-07, tightened from the original
    # 180-day HARD rule of 2026-06-10): only surface rounds with an
    # announce date inside the last 90 days — a five-month-old round is
    # not an opener. PL returns the full historical funding history of a
    # company — Lemonade has Series B/C/D + multiple infusions going back
    # to 2017 — and none of those are buying signals for a cyber rep
    # today. Without a parseable date we drop entirely; an undated funding
    # row is just noise. The drop logging below measures how much this
    # gate discards (recently_funded fired once in the 90 days to
    # 2026-07-07, so watch the drop reasons before loosening/tightening
    # again).
    funding_cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    rounds = (pl_financing or {}).get("rounds") if isinstance(pl_financing, dict) else None
    if isinstance(rounds, list):
        for r in rounds:
            if not isinstance(r, dict):
                continue
            raw_date = (r.get("date") or "").strip()
            if not raw_date:
                # no date = can't verify freshness = drop. Logged because
                # production showed 1 recently_funded row in 90 days — we
                # need the drop rate visible to judge if PL date coverage
                # is starving the funding signal (signal-tidyup 2026-07-07).
                logger.info("funding gate: dropped undated round for %s (%s %s)",
                            domain, r.get("amount") or "?", r.get("type") or "?")
                continue
            try:
                round_dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                if round_dt.tzinfo is None:
                    round_dt = round_dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                logger.info("funding gate: dropped unparseable date %r for %s",
                            raw_date, domain)
                continue
            if round_dt < funding_cutoff:
                logger.info("funding gate: dropped stale round for %s (%s, %d days old)",
                            domain, raw_date,
                            (datetime.now(timezone.utc) - round_dt).days)
                continue

            amount = (r.get("amount") or "").strip()
            round_type = (r.get("type") or "funding").strip()
            # Headline shape: "$300M Series D · 2026-05" so reps see
            # the round AND when it happened in one line.
            month_label = round_dt.strftime("%Y-%m")
            headline_parts: List[str] = []
            if amount:
                headline_parts.append(amount)
            if round_type:
                headline_parts.append(round_type)
            base = " ".join(headline_parts) or "Funding round"
            headline = f"{base} · {month_label}"
            rows.append(
                {
                    "org_id": org_id,
                    "company_id": company_id,
                    "signal_type": "recently_funded",
                    "signal_source": "predictleads_financing",
                    "signal_data": {
                        "headline": headline,
                        "amount": amount,
                        "round_type": round_type,
                        "announced_on": raw_date,
                    },
                    "signal_strength": 0.85,
                    "signal_subject_type": "account",
                    "detected_at": raw_date,
                }
            )

    # CVEs (threat_alerts) → cve_in_stack — REMOVED 2026-08-04.
    # This was the company-level twin of the lead-level emitter retired in
    # enricher.py (see the full write-up there). Two extra faults lived here:
    # matched_technologies was copied through verbatim including when it was
    # empty, and detected_at fell back to now_iso whenever the alert carried
    # no published_at — so an 18-year-old CVE was stamped as detected today.
    # Not re-enabled until CVE matching is product-level and source-gated.

    # PL jobs → hiring signals (just the top-line counters; per-job
    # signal projection lives in the lead enrichment path).
    if isinstance(pl_jobs, dict):
        if pl_jobs.get("is_hiring_sales"):
            rows.append(
                {
                    "org_id": org_id,
                    "company_id": company_id,
                    "signal_type": "hiring_sales_bd",
                    "signal_source": "predictleads_jobs",
                    "signal_data": {
                        "headline": f"Active sales hiring ({pl_jobs.get('total_jobs', 0)} open roles)",
                    },
                    "signal_strength": 0.6,
                    "signal_subject_type": "account",
                    "detected_at": now_iso,
                }
            )
        # NB: is_hiring_leadership + is_hiring_technical intentionally
        # REMOVED (2026-06-10). Both were over-broad:
        #   - is_hiring_leadership fired on ANY senior title (CTO,
        #     CFO, COO, Marketing VP). Now handled by the jobs_enrichment
        #     Claude path which only fires the canonical new_leadership /
        #     security_leadership_hire types when the cyber lens applies.
        #   - is_hiring_technical was the worse bug: it mapped to
        #     hiring_security on ANY engineer/developer role. That made
        #     accounts hiring a backend dev look like accounts hiring
        #     security people. Now hiring_security only fires from the
        #     jobs_enrichment Claude path with the exhaustive security-
        #     title taxonomy.

    # Credential exposure (LeakRadar stealer-log / dark-web) → Risk signals.
    # Shared builder keeps the projection identical here and in the backfill.
    try:
        from app.services.enrichers.credential_exposure import build_signals as _cred_signals
        rows.extend(
            _cred_signals(
                org_id=org_id,
                company_id=company_id,
                summary=credential_exposure,
                now_iso=now_iso,
            )
        )
    except Exception as e:
        logger.warning("Credential signal projection failed for %s: %s", domain, e)

    if rows:
        # Clean any prior account-level signals for this company so the
        # row set stays in sync with the latest enrichment. Don't touch
        # lead-attached signals — those have their own lifecycle via
        # the lead enrichment path. Scoped to this org: companies are
        # global rows, so an unscoped delete here wipes every other
        # org's signals for the same domain (service role bypasses RLS).
        try:
            db.table("signals").delete().eq("org_id", org_id).eq("company_id", company_id).is_("lead_id", "null").execute()
        except Exception as e:
            logger.warning("Failed to clear prior account signals for %s: %s", domain, e)

        try:
            inserted = insert_signals(db, rows)
            logger.info(
                "Account-level signals for %s: %d projected, %d inserted",
                domain, len(rows), inserted,
            )
        except Exception as e:
            logger.warning("insert_signals failed for %s: %s", domain, e)


def _bucket_employees(n: Any) -> Optional[str]:
    """Apollo returns a raw integer employee count; the companies table
    expects an Apollo-format bucket like "201,500". Map roughly so the
    Accounts list size cell reads cleanly without an empty dash."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    if n < 11:
        return "1,10"
    if n < 51:
        return "11,50"
    if n < 201:
        return "51,200"
    if n < 501:
        return "201,500"
    if n < 1001:
        return "501,1000"
    if n < 5001:
        return "1001,5000"
    if n < 10001:
        return "5001,10000"
    return "10001+"


def ensure_watchlist_row(
    db: Client,
    org_id: str,
    domain: str,
    company_name: Optional[str] = None,
) -> Optional[str]:
    """Idempotent watchlist insert for org+domain. Returns the watchlist id.

    No tier cap enforcement here — the caller (quick-enrich) is the human
    paste path, and the credit model handles cost rather than a hard
    watchlist cap.
    """
    existing = (
        db.table("watchlist")
        .select("id, status")
        .eq("org_id", org_id)
        .ilike("company_domain", domain)
        .limit(1)
        .execute()
    )
    if existing.data:
        row0 = existing.data[0]
        if (row0.get("status") or "active").lower() in ("deleted", "dismissed"):
            # Re-adding a previously removed account must actually restore
            # it (2026-08-24: deleted accounts "re-added"
            # successfully while the soft-deleted row made every add an
            # invisible no-op — the response was indistinguishable from a
            # real add). Reactivation counts against the cap like any add.
            from app.services.plan_limits import enforce_account_cap
            enforce_account_cap(org_id, db)
            from app.services.org_features import monitoring_default
            db.table("watchlist").update({
                "status": "active",
                "monitoring_active": monitoring_default(org_id),
                "dismissed_at": None,
            }).eq("id", row0["id"]).execute()
        return row0["id"]

    # Solo plan: hard 20-account ceiling. No-op for other tiers.
    from app.services.plan_limits import enforce_account_cap
    enforce_account_cap(org_id, db)

    from app.services.org_features import monitoring_default
    row = {
        "org_id": org_id,
        "company_name": company_name or domain,
        "company_domain": domain,
        # Research by default for decoupled-monitoring orgs (snapshot,
        # not monitored until the rep commits); monitored otherwise.
        "monitoring_active": monitoring_default(org_id),
        "priority": "medium",
    }
    try:
        insert_resp = db.table("watchlist").insert(row).execute()
        if insert_resp.data:
            return insert_resp.data[0]["id"]
    except Exception as e:
        logger.warning("Watchlist insert failed for %s: %s", domain, e)
    return None
