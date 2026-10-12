"""Market radar — the "Your market" page (gated on features.market_v1).

Turns an org's OBSERVED targeting (the accounts they actually watch) into a
countable market universe plus weekly "what moved" slices, all via Apollo
searches (the engine's normal non-reveal path):

  - observed_profile: aggregate the org's active watchlist joined to
    `companies` — which industries / sizes / countries they really target.
  - market_criteria: blend observed with the declared persona ICP into
    Apollo filters. Observed leads (it reflects behaviour); declared fills
    the gaps for a young watchlist.
  - compute_overview: one universe count + three stacked-filter slices
    (hiring security / funding events / headcount growth). Slice rows are
    junk-filtered by the suggestions classifier, then verified before
    display: hiring rows must have live security postings (checked against
    the org's actual job postings), funding rows must carry a dated event
    within 180 days (the freshness rule). Growth is presented as a
    supporting signal only — Apollo's modelled number, no claim made.

Accuracy notes from the 2026-08-13 radar test: the stacked filters are
reliable (no silent no-ops; 10/10 hiring rows verified against live
postings), but raw keyword-tag universes carry 20-40% junk (vendors that
SERVE the sector, recruiters, trade bodies) — hence the classifier pass and
the visible raw-vs-filtered count. Apollo's funding dates are accurate but
its event TYPE labels need the enrich lookup (a "funding" row can be a debt
facility or an acquisition).

Snapshots persist to `market_snapshots` so the next run can diff "new this
week" and the page can render instantly from the latest snapshot.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import settings
from app.database import get_supabase
from app.services.markets import get_market, market_deal_context, market_icp_like
from app.services.suggestion_classifier import build_seller_profile, classify_candidates

logger = logging.getLogger(__name__)

_APOLLO_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_companies/search"
_APOLLO_ENRICH_URL = "https://api.apollo.io/api/v1/organizations/enrich"
_APOLLO_JOBS_URL = "https://api.apollo.io/api/v1/organizations/{org_id}/job_postings"
_TIMEOUT = 45

# Security titles for the hiring slice — matches the taxonomy the jobs
# enricher uses (leadership / engineering / SOC / IR / IAM shorthand).
SECURITY_JOB_TITLES = [
    "security engineer", "security analyst", "soc analyst", "ciso",
    "security architect", "penetration tester", "information security",
    "cyber security", "security operations", "detection engineer",
]

# Keywords that mark a posting title as a security role when verifying.
_SEC_TITLE_KEYWORDS = [
    "security", "soc ", "cyber", "ciso", "penetration", "infosec",
    "detection", "threat", "incident response", "dfir", "iam ",
]

# Verified rows returned per slice. The page displays 6 at a time and
# replenishes from the rest as the user adds accounts (user feedback
# 2026-08-14: "adding is hard, deleting is easy" — the radar should be a
# conveyor, not a fixed shortlist), so verify a deeper bench per run.
SLICE_DISPLAY_ROWS = 12
# Candidate rows fetched per slice before classify/verify whittle them down.
_SLICE_FETCH_ROWS = 50

FUNDING_WINDOW_DAYS = 90
# The 6-month freshness rule: never surface a dated event older than this.
_FRESHNESS_DAYS = 180

_COUNTRY_PATTERNS: List[Tuple[str, List[str]]] = [
    ("United Kingdom", ["united kingdom", "england", "scotland", "wales", "northern ireland"]),
    ("United States", ["united states", " usa"]),
    ("Canada", ["canada"]),
    ("France", ["france"]),
    ("Germany", ["germany"]),
    ("Ireland", ["ireland"]),
]

# Apollo-valid employee bands used when deriving ranges from observed data.
_OBSERVED_BANDS: List[Tuple[int, int]] = [
    (201, 500), (501, 1000), (1001, 2000), (2001, 5000), (5001, 10000),
]


def _norm_range(raw: str) -> Optional[str]:
    """Employee range → Apollo "min,max". Same normalisation contract as
    account_suggestions._normalise_employee_range (kept local so a service
    never imports from a router)."""
    if not raw:
        return None
    s = str(raw).strip().replace(" ", "")
    if "-" in s and "," in s:
        s = s.replace(",", "")
    if "+" in s:
        lo = "".join(c for c in s if c.isdigit())
        return f"{lo},100000" if lo else None
    for sep in ("-", ","):
        if sep in s:
            parts = s.split(sep)
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                return f"{parts[0]},{parts[1]}"
            return None
    return f"{s},{s}" if s.isdigit() else None


def _country_of(hq_country: Optional[str]) -> Optional[str]:
    low = f" {(hq_country or '').lower()}"
    for label, pats in _COUNTRY_PATTERNS:
        if any(p in low for p in pats):
            return label
    return (hq_country or "").strip() or None


def observed_profile(db, org_id: str, market: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What this org's active watchlist says about who they target.

    With a market row (054): scoped to that market's accounts. NULL
    market_id belongs to the primary market, so the primary reads
    untagged legacy rows too and non-primary markets read only their own.
    """
    q = (db.table("watchlist").select("company_domain")
         .eq("org_id", org_id).eq("status", "active"))
    if market and not market.get("is_primary"):
        q = q.eq("market_id", market["id"])
    elif market:
        q = q.or_(f"market_id.is.null,market_id.eq.{market['id']}")
    wl = q.execute().data or []
    domains = sorted({(w.get("company_domain") or "").lower() for w in wl if w.get("company_domain")})
    profile: Dict[str, Any] = {"accounts": len(domains), "industries": [], "countries": [], "size_bands": []}
    if not domains:
        return profile

    rows = (db.table("companies").select("domain, industry, size_band, hq_country")
            .in_("domain", domains).execute().data or [])

    ind: Dict[str, int] = {}
    geo: Dict[str, int] = {}
    sizes: Dict[str, int] = {}
    for r in rows:
        label = (r.get("industry") or "").strip().lower()
        if label:
            ind[label] = ind.get(label, 0) + 1
        c = _country_of(r.get("hq_country"))
        if c:
            geo[c] = geo.get(c, 0) + 1
        m = re.search(r"\d+", str(r.get("size_band") or ""))
        if m:
            n = int(m.group())
            for lo, hi in _OBSERVED_BANDS:
                if lo <= n <= hi:
                    sizes_key = f"{lo},{hi}"
                    sizes[sizes_key] = sizes.get(sizes_key, 0) + 1
                    break

    profile["industries"] = sorted(ind.items(), key=lambda kv: -kv[1])
    profile["countries"] = sorted(geo.items(), key=lambda kv: -kv[1])
    profile["size_bands"] = sorted(sizes.items(), key=lambda kv: -kv[1])
    return profile



# Stored industry labels too generic to use as Apollo keyword tags — each
# one expands to a huge cross-sector universe (dry-run 2026-08-13: three
# IT&S accounts pulled Anthropic/Cloudflare/YC into an MDR vendor's market
# and tripled the count). Rows from these accounts still shape sizes/geos;
# they just don't become search tags.
_GENERIC_INDUSTRY_TAGS = {
    "information technology & services", "computer software", "internet",
    "information services", "management consulting", "professional training & coaching",
}


def market_criteria(icp_config: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, List[str]]:
    """Turn observed targeting (plus declared persona as fallback) into
    Apollo filters.

    Observed-FIRST, not a union: the market should reflect who the org
    actually watches (minimum 2 accounts per industry so one odd add
    doesn't steer it). The declared persona only tops up when the
    watchlist is too young to read — a union of both inflated one test org's
    universe to 37k companies in the 2026-08-13 dry-run. The stored
    `companies.industry` labels double as Apollo keyword tags — that
    equivalence is what the count test exercised.
    """
    personas = icp_config.get("personas") or []
    persona = personas[0] if personas and isinstance(personas[0], dict) else {}

    industries: List[str] = []
    for label, n in profile.get("industries") or []:
        if n >= 2 and label not in _GENERIC_INDUSTRY_TAGS and label not in industries:
            industries.append(label)
    if len(industries) < 3:
        for label in persona.get("target_industries") or []:
            low = str(label).strip().lower()
            if low and low not in _GENERIC_INDUSTRY_TAGS and low not in industries:
                industries.append(low)
    industries = industries[:10]

    locations: List[str] = [str(x) for x in (persona.get("target_locations") or []) if x]
    if not locations:
        locations = [c for c, _ in (profile.get("countries") or [])[:3]]

    ranges: List[str] = []
    for raw in persona.get("target_company_size") or []:
        r = _norm_range(str(raw))
        if r and r not in ranges:
            ranges.append(r)
    if not ranges:
        ranges = [band for band, _ in (profile.get("size_bands") or [])[:4]]

    return {"industries": industries, "locations": locations, "employee_ranges": ranges}


def _apollo_search(criteria: Dict[str, List[str]], extra: Dict[str, Any], per_page: int, page: int = 1) -> Tuple[int, List[Dict[str, Any]]]:
    """One mixed_companies search. Returns (total_entries, rows) with BOTH
    response buckets merged — `organizations` (net-new) and `accounts`
    (already saved in the Apollo workspace) — since a radar must see both."""
    body: Dict[str, Any] = {"per_page": per_page, "page": page, **extra}
    if criteria.get("industries"):
        body["q_organization_keyword_tags"] = criteria["industries"]
    if criteria.get("locations"):
        body["organization_locations"] = criteria["locations"]
    if criteria.get("employee_ranges"):
        body["organization_num_employees_ranges"] = criteria["employee_ranges"]

    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            res = client.post(_APOLLO_SEARCH_URL, json=body, headers={"X-Api-Key": settings.APOLLO_API_KEY})
            res.raise_for_status()
            payload = res.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("market radar Apollo search failed: %s", e)
        return 0, []

    total = int((payload.get("pagination") or {}).get("total_entries") or 0)
    rows: List[Dict[str, Any]] = []
    seen: set = set()
    for o in payload.get("organizations") or []:
        dom = (o.get("primary_domain") or "").lower().strip()
        if dom and dom not in seen:
            seen.add(dom)
            rows.append({"name": o.get("name"), "domain": dom, "apollo_org_id": o.get("id"), "industry": None})
    for a in payload.get("accounts") or []:
        dom = (a.get("domain") or "").lower().strip()
        if dom and dom not in seen:
            seen.add(dom)
            rows.append({"name": a.get("name"), "domain": dom, "apollo_org_id": a.get("organization_id"), "industry": None})
    return total, rows


def _apollo_org_lookup(domain: str) -> Optional[Dict[str, Any]]:
    """Primary industry + keyword tags via organizations/enrich (search rows
    carry no industry field at all — verified 2026-08-13). Non-reveal call.
    Returns None on FAILURE (throttle/network) vs a dict whose industry may
    be None (genuinely no industry on file) — callers cache only successes,
    so a 429 never poisons the cache as 'known unknown'."""
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            res = client.get(_APOLLO_ENRICH_URL,
                             headers={"X-Api-Key": settings.APOLLO_API_KEY},
                             params={"domain": domain})
            res.raise_for_status()
            org = res.json().get("organization") or {}
        return {
            "industry": (org.get("industry") or "").lower().strip() or None,
            "keywords": [str(k).lower() for k in (org.get("keywords") or [])],
        }
    except Exception as e:  # noqa: BLE001
        logger.info("market radar industry enrich failed (%s): %s", domain, e)
        return None


# ---------------------------------------------------------------------------
# Hard exclusions (2026-08-14): competitors / cybersecurity vendors /
# pentest firms / recruiters must NEVER appear as market matches, full stop.
# Deterministic, runs on every slice row BEFORE the allowed-industries gate
# and the classifier, so the guarantee doesn't rest on an LLM's judgment.
# Deliberately biased to false drops over false shows (empty > wrong).
# ---------------------------------------------------------------------------

_DENY_INDUSTRIES = {
    "computer & network security",
    "security & investigations",
    "staffing & recruiting",
    "human resources",
}

_DENY_NAME_RE = re.compile(
    r"recruit|staffing|headhunt|\btalent\b|cyber ?security|infosec|pentest",
    re.IGNORECASE,
)

# Keyword tags that mark a company as a security VENDOR (what they sell,
# not what they buy) — checked when an enrich payload is available.
_DENY_KEYWORDS = {
    "cyber security", "cybersecurity", "penetration testing", "pentesting",
    "information security", "network security", "managed security services",
    "security testing", "vulnerability management", "threat intelligence",
    "incident response", "ethical hacking", "soc as a service", "mssp",
}


def _is_denied(row: Dict[str, Any]) -> bool:
    if (row.get("industry") or "") in _DENY_INDUSTRIES:
        return True
    if _DENY_NAME_RE.search(row.get("name") or ""):
        return True
    kws = row.get("keywords")
    if kws and any(k in _DENY_KEYWORDS for k in kws):
        return True
    return False


def _annotate_industries(db, rows: List[Dict[str, Any]]) -> None:
    """Fill r['industry']/r['keywords'] in place from the free caches: the
    shared companies table first, then org_industry_cache (every enrich
    lookup ever made, persisted). Rows the caches know as
    genuinely-unknown get r['looked']=True so the enrich budget is never
    re-spent on them (the 2026-08-14 low-yield bug: each topup re-enriched
    the same non-qualifying page-1 domains and never reached page 2)."""
    domains = [r["domain"] for r in rows]
    if not domains:
        return
    known: Dict[str, str] = {}
    try:
        for c in (db.table("companies").select("domain, industry")
                  .in_("domain", domains).execute().data or []):
            dom = (c.get("domain") or "").lower()
            ind = (c.get("industry") or "").lower().strip()
            if dom and ind:
                known[dom] = ind
    except Exception as e:  # noqa: BLE001
        logger.info("market radar industry cache lookup failed: %s", e)
    looked_up: Dict[str, Dict[str, Any]] = {}
    try:
        for c in (db.table("org_industry_cache").select("domain, industry, keywords")
                  .in_("domain", domains).execute().data or []):
            dom = (c.get("domain") or "").lower()
            if dom:
                looked_up[dom] = c
    except Exception as e:  # noqa: BLE001
        logger.info("market radar industry-cache read failed: %s", e)
    for r in rows:
        if r.get("industry"):
            continue
        if r["domain"] in known:
            r["industry"] = known[r["domain"]]
        elif r["domain"] in looked_up:
            hit = looked_up[r["domain"]]
            r["industry"] = (hit.get("industry") or "").lower().strip() or None
            r["keywords"] = hit.get("keywords")
            r["looked"] = True


# ---------------------------------------------------------------------------
# In-market membership. The search matches Apollo's fuzzy keyword TAGS, which
# is how Y Combinator (tagged financial services because it invests) and IT
# shops that serve banks leak into a finserv market (seen live 2026-08-13) —
# so membership reads the PRIMARY industry. But real vertical brands rarely
# carry the vertical as primary (UK Retailers, 2026-08-27: ME+EM is "apparel
# & fashion", Bulk is "health, wellness & fitness", Protein Works is "food
# production" — an exact-match gate could never verify a retail market).
# Two routes in, both deterministic:
#   1. primary industry is the term or a curated equivalent — common ICP
#      words like "ecommerce"/"finance"/"healthcare" aren't Apollo primary
#      strings and used to match nothing at all;
#   2. primary industry is in the term's tag-admissible family AND the org's
#      keyword tags contain the term itself. Tags alone are worthless:
#      MoonPay/Primer/Admiral all tag "retail"+"e-commerce" (verified live
#      2026-08-27) — payments firms selling TO retailers. A fashion brand
#      tagged retail IS a retailer; a fintech tagged retail is not.
# Apollo primaries use "&" for "and"; tags hyphenate ("e-commerce"), so all
# comparisons run on squashed strings. Tag matches are exact-equality —
# Primer's "open banking payments" tag must not put it in a banking market.
# ---------------------------------------------------------------------------

# Every string in these maps is a REAL Apollo `organization.industry`
# value — observed in companies/org_industry_cache or live-verified via
# enrich (2026-08-27 probe: Morrisons="retail" not "supermarkets", TUI=
# "hospitality", Rockwell="machinery", Maersk/GXO/Wincanton all="logistics
# & supply chain", ITV="media production", Camira="textiles"). Apollo
# collapses niche verticals into these broader primaries; a string it
# never returns can only dead-match, so don't add one without evidence —
# tests pin membership to the verified taxonomy.
_CONSUMER_PRIMARIES = [
    "apparel & fashion", "automotive", "computer games", "consumer electronics",
    "consumer goods", "consumer services", "cosmetics", "entertainment",
    "food & beverages", "food production", "furniture",
    "health, wellness & fitness", "luxury goods & jewelry", "retail",
    "sporting goods", "sports",
]

# NO venture capital & private equity here: YC leaking into a finserv
# market is the original 2026-08-13 failure this gate exists to stop. A
# market that truly targets VC/PE names it as a term (exact match works).
_FINANCE_PRIMARIES = [
    "financial services", "banking", "insurance", "investment management",
    "investment banking", "capital markets",
]

# term → primaries that ARE the term (route 1, no tag needed).
_TERM_PRIMARY_ALIASES: Dict[str, List[str]] = {
    "ecommerce": ["retail"],
    "finance": _FINANCE_PRIMARIES,
    "financial services": _FINANCE_PRIMARIES,
    "fintech": ["financial services", "banking"],
    "healthcare": ["hospital & health care", "medical practice",
                   "mental health care", "medical devices", "pharmaceuticals",
                   "biotechnology"],
    "health care": ["hospital & health care", "medical practice",
                    "mental health care", "medical devices", "pharmaceuticals",
                    "biotechnology"],
    "pharma": ["pharmaceuticals", "biotechnology"],
    "pharmaceuticals": ["biotechnology"],
    "legal": ["law practice", "legal services"],
    "manufacturing": ["mechanical or industrial engineering",
                      "electrical/electronic manufacturing", "machinery",
                      "packaging & containers", "food production", "automotive",
                      "chemicals", "textiles", "aviation & aerospace",
                      "semiconductors", "mining & metals"],
    "energy": ["oil & energy", "utilities", "renewables & environment"],
    "education": ["higher education", "education management",
                  "primary/secondary education", "e-learning"],
    "government": ["government administration", "public policy",
                   "public safety"],
    "public sector": ["government administration", "public policy",
                      "public safety"],
    "logistics": ["logistics & supply chain", "package/freight delivery",
                  "airlines/aviation"],
    "hospitality": ["gambling & casinos"],
    "media": ["online media", "publishing", "newspapers",
              "media production", "entertainment"],
    "telecoms": ["telecommunications"],
    "telecom": ["telecommunications"],
    "software": ["computer software"],
    "saas": ["computer software", "internet"],
}

# term → primaries admitted only when the org also TAGS the term (route 2).
_TERM_TAG_PRIMARIES: Dict[str, List[str]] = {
    "retail": _CONSUMER_PRIMARIES,
    "ecommerce": _CONSUMER_PRIMARIES,
    "banking": ["financial services"],
    "insurance": ["financial services"],  # Admiral: primary finserv, tags insurance
    "healthcare": ["health, wellness & fitness", "veterinary"],
    "health care": ["health, wellness & fitness", "veterinary"],
}

_SQUASH_RE = re.compile(r"[^a-z0-9]+")


def _squash(s: str) -> str:
    return _SQUASH_RE.sub("", (s or "").lower())


def _squash_map(table: Dict[str, List[str]]) -> Dict[str, frozenset]:
    return {_squash(k): frozenset(_squash(v) for v in vals)
            for k, vals in table.items()}


_ALIASES_SQ = _squash_map(_TERM_PRIMARY_ALIASES)
_TAG_PRIMARIES_SQ = _squash_map(_TERM_TAG_PRIMARIES)


def _in_market(row: Dict[str, Any], allowed: List[str]) -> bool:
    """True when the row's known primary industry places it inside the
    market. Rows whose keywords weren't cached simply can't take the tag
    route — conservative, matches the positive-identification rule."""
    ind = _squash(row.get("industry") or "")
    if not ind:
        return False
    terms = {_squash(t) for t in allowed}
    if ind in terms:
        return True
    if any(ind in _ALIASES_SQ.get(t, ()) for t in terms):
        return True
    tags = {_squash(str(k)) for k in (row.get("keywords") or [])}
    if tags:
        return any(t in tags and ind in _TAG_PRIMARIES_SQ.get(t, ())
                   for t in terms)
    return False


def _industry_gate(rows: List[Dict[str, Any]], allowed: List[str], *, strict: bool) -> List[Dict[str, Any]]:
    """Keep rows the market claims (see _in_market). Rows with no industry:
    kept when strict=False (hiring/funding rows get per-row verification
    later), dropped when strict=True (growth has no other check).
    """
    out = []
    for r in rows:
        if _in_market(r, allowed) or (r.get("industry") is None and not strict):
            out.append(r)
    return out


def _classify_keep(rows: List[Dict[str, Any]], seller_profile: str) -> List[Dict[str, Any]]:
    """Junk-filter rows via the suggestions classifier. classify_candidates
    passes everything through unchanged on any failure, so this can only
    shrink or preserve the list, never error out."""
    if not rows:
        return rows
    # Give the classifier the industry as context — judging on name alone
    # kept "Smart IT"-style vendors that serve the sector.
    candidates = [
        {"name": r["name"], "domain": r["domain"],
         "matched_reasons": [f"industry: {r['industry']}"] if r.get("industry") else []}
        for r in rows
    ]
    kept_domains = {c.get("domain") for c in classify_candidates(candidates, seller_profile=seller_profile)}
    return [r for r in rows if r["domain"] in kept_domains]


def _verify_hiring(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only rows whose live postings really include security roles, and
    attach the evidence (count + freshest matching title/date)."""
    out: List[Dict[str, Any]] = []
    for r in rows:
        if len(out) >= SLICE_DISPLAY_ROWS:
            break
        if not r.get("apollo_org_id"):
            continue
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                res = client.get(
                    _APOLLO_JOBS_URL.format(org_id=r["apollo_org_id"]),
                    headers={"X-Api-Key": settings.APOLLO_API_KEY},
                    params={"per_page": 100},
                )
                res.raise_for_status()
                posts = res.json().get("organization_job_postings") or []
        except Exception as e:  # noqa: BLE001
            logger.info("market radar postings fetch failed (%s): %s", r["domain"], e)
            continue
        sec = [p for p in posts
               if any(k in (p.get("title") or "").lower() for k in _SEC_TITLE_KEYWORDS)]
        if not sec:
            continue
        sec.sort(key=lambda p: p.get("posted_at") or "", reverse=True)
        out.append({
            **r,
            "open_security_roles": len(sec),
            "example_title": sec[0].get("title"),
            "example_posted_at": (sec[0].get("posted_at") or "")[:10] or None,
        })
    return out


def _verify_funding(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only rows with a dated capital event inside the freshness window,
    labelled with Apollo's event type (round / debt / acquisition...). Rows
    Apollo matched but can't date are dropped — never surface an undated
    claim."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=_FRESHNESS_DAYS)
    out: List[Dict[str, Any]] = []
    for r in rows:
        if len(out) >= SLICE_DISPLAY_ROWS:
            break
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                res = client.get(_APOLLO_ENRICH_URL,
                                 headers={"X-Api-Key": settings.APOLLO_API_KEY},
                                 params={"domain": r["domain"]})
                res.raise_for_status()
                org = res.json().get("organization") or {}
        except Exception as e:  # noqa: BLE001
            logger.info("market radar enrich failed (%s): %s", r["domain"], e)
            continue
        # Second look with the full enrich payload in hand — a row that
        # slipped annotation caps must still never be a vendor/recruiter.
        if _is_denied({
            "name": r.get("name"),
            "industry": (org.get("industry") or "").lower().strip() or None,
            "keywords": [str(k).lower() for k in (org.get("keywords") or [])],
        }):
            continue
        events = org.get("funding_events") or []
        dated: List[Tuple[datetime, Dict[str, Any]]] = []
        for ev in events:
            try:
                d = datetime.fromisoformat(str(ev.get("date"))[:10]).replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            if d >= cutoff:
                dated.append((d, ev))
        if not dated:
            continue
        dated.sort(key=lambda t: t[0], reverse=True)
        d, ev = dated[0]
        out.append({
            **r,
            "event_type": (ev.get("type") or "Funding event").strip(),
            "event_date": d.date().isoformat(),
            "event_amount": ev.get("amount"),
            "event_currency": ev.get("currency"),
        })
    return out


def _make_lookup(db, budget: int):
    """Budgeted, memoised industry lookup shared across one run — a single
    compute/topup can never burn the Apollo key (2026-08-14 incident).
    Successful lookups persist to org_industry_cache so they're free for
    every future run and every org; failures (throttle) are never cached."""
    memo: Dict[str, Dict[str, Any]] = {}
    left = {"n": budget}

    def _lookup(domain: str) -> Dict[str, Any]:
        if domain in memo:
            return memo[domain]
        if left["n"] <= 0:
            return {"industry": None, "keywords": None}
        left["n"] -= 1
        looked = _apollo_org_lookup(domain)
        if looked is None:
            return {"industry": None, "keywords": None}
        memo[domain] = looked
        try:
            db.table("org_industry_cache").upsert(
                {"domain": domain, "industry": looked["industry"],
                 "keywords": looked["keywords"]},
                on_conflict="domain",
            ).execute()
        except Exception as e:  # noqa: BLE001
            logger.info("industry cache write skipped (%s): %s", domain, e)
        return looked

    return _lookup


def _qualified_slice_rows(
    db,
    criteria: Dict[str, List[str]],
    seller_profile: str,
    watched: set,
    extra: Dict[str, Any],
    *,
    exclude: Optional[set] = None,
    pages: Tuple[int, ...] = (1,),
    lookup=None,
) -> Tuple[int, List[Dict[str, Any]], Tuple[int, int]]:
    """The slice pipeline: search (paginated) → positive identification
    (cache/enrich; unknown = not shown) → hard exclusions → in-market
    membership → classifier with can't-empty backfill. Also returns
    (gated, passed) counts so the caller can estimate how much of the raw
    search total is genuinely in-market (the 73-junk-vs-0-verified header
    contradiction, 2026-08-27)."""
    lookup = lookup or _make_lookup(db, 60)
    exclude = exclude or set()
    allowed = criteria["industries"]
    total = 0
    gated = 0
    qualified: List[Dict[str, Any]] = []
    for page in pages:
        page_total, rows = _apollo_search(criteria, extra, _SLICE_FETCH_ROWS, page=page)
        total = total or page_total
        rows = [r for r in rows if r["domain"] not in watched and r["domain"] not in exclude]
        _annotate_industries(db, rows)  # cache pass, free
        for r in rows:
            gated += 1
            if not r.get("industry") and not r.get("looked"):
                looked = lookup(r["domain"])
                r["industry"] = looked["industry"]
                r["keywords"] = looked["keywords"]
            if not r.get("industry"):
                continue
            if _is_denied(r) or not _in_market(r, allowed):
                continue
            if r["domain"] in {q["domain"] for q in qualified}:
                continue
            qualified.append(r)
            if len(qualified) >= 18:  # bench + classifier headroom
                break
        if len(qualified) >= 18:
            break
    kept = _classify_keep(qualified, seller_profile)
    # The classifier must not be able to EMPTY a slice (2026-08-14):
    # everything in `qualified` already passed the deterministic checks.
    if len(kept) < SLICE_DISPLAY_ROWS // 2:
        kept = kept + [r for r in qualified if r not in kept]
    return total, kept[:SLICE_DISPLAY_ROWS], (gated, len(qualified))


def universe_count(criteria: Dict[str, List[str]]) -> int:
    """Raw universe total for a criteria set — one free search, no rows.
    Powers the wizard's live TAM counter (PLAN Phase 3)."""
    total, _ = _apollo_search(criteria, {}, per_page=1)
    return total


def _slice_extra(slice_key: str) -> Dict[str, Any]:
    if slice_key == "hiring_security":
        return {"q_organization_job_titles": SECURITY_JOB_TITLES}
    if slice_key == "funding":
        since = (date.today() - timedelta(days=FUNDING_WINDOW_DAYS)).isoformat()
        return {"latest_funding_date_range": {"min": since}}
    return {"organization_headcount_growth_past_n_months": 6,
            "organization_headcount_growth_range": {"min": 10}}


def _verify_for_slice(slice_key: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if slice_key == "hiring_security":
        return _verify_hiring(rows)
    if slice_key == "funding":
        return _verify_funding(rows)
    return rows[:SLICE_DISPLAY_ROWS]


def _snapshot_scope(q, market: Optional[Dict[str, Any]]):
    """Filter a market_snapshots query to one market. NULL market_id rows
    belong to the primary market (pre-054 snapshots keep serving)."""
    if market is None:
        return q
    if market.get("is_primary"):
        return q.or_(f"market_id.is.null,market_id.eq.{market['id']}")
    return q.eq("market_id", market["id"])


def _watched_domains(db, org_id: str) -> set:
    watched: set = set()
    for row in (db.table("watchlist").select("company_domain, status")
                .eq("org_id", org_id).execute().data or []):
        dom = (row.get("company_domain") or "").lower().strip()
        if dom and row.get("status") in ("active", "pending"):
            watched.add(dom)
    return watched


def drop_watched_rows(db, org_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Serve-time filter: a snapshot computed before the user added accounts
    still lists them — never show a company the org already watches (bug
    2026-08-14: added rows reappeared on page refresh)."""
    watched = _watched_domains(db, org_id)
    for s in payload.get("slices") or []:
        s["rows"] = [r for r in s.get("rows") or []
                     if (r.get("domain") or "").lower() not in watched]
        # The "N new" badge must count only rows still present — a badge
        # over an emptied list read as nonsense (seen live 2026-08-14).
        remaining = {r.get("domain") for r in s["rows"]}
        s["new_domains"] = [d for d in s.get("new_domains") or [] if d in remaining]
    return payload


def topup_slice(org_id: str, slice_key: str, exclude: List[str], limit: int = 6,
                market_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch the NEXT verified rows for one slice — the conveyor's refill
    when a bench runs dry mid-session (2026-08-14: 1,264 matched but
    nothing new fed in). Same pipeline as the weekly compute, paginated
    deeper, then appended to the latest snapshot so a page refresh keeps
    the topped-up rows."""
    db = get_supabase()
    cfg_row = (db.table("organisations").select("icp_config")
               .eq("id", org_id).maybe_single().execute().data or {})
    icp_config = cfg_row.get("icp_config") or {}
    market = get_market(db, org_id, market_id)
    profile = observed_profile(db, org_id, market)
    criteria = market_criteria(market_icp_like(market, icp_config), profile)
    if not criteria["industries"]:
        return []
    seller_profile = build_seller_profile(icp_config)
    watched = _watched_domains(db, org_id)

    snap = (_snapshot_scope(db.table("market_snapshots").select("id, data")
                            .eq("org_id", org_id), market)
            .order("captured_at", desc=True)
            .limit(1).execute().data or [])
    snap_row = snap[0] if snap else None
    shown: set = {(d or "").lower() for d in exclude}
    if snap_row:
        for s in (snap_row.get("data") or {}).get("slices") or []:
            if s.get("key") == slice_key:
                shown |= {(r.get("domain") or "").lower() for r in s.get("rows") or []}

    _, rows, _ = _qualified_slice_rows(
        db, criteria, seller_profile, watched, _slice_extra(slice_key),
        exclude=shown, pages=(1, 2, 3, 4), lookup=_make_lookup(db, 40),
    )
    rows = _verify_for_slice(slice_key, rows)[:limit]

    # Persist into the latest snapshot so refresh keeps the conveyor state.
    if rows and snap_row:
        try:
            data = snap_row.get("data") or {}
            for s in data.get("slices") or []:
                if s.get("key") == slice_key:
                    have = {(r.get("domain") or "").lower() for r in s.get("rows") or []}
                    s["rows"] = (s.get("rows") or []) + [r for r in rows if r["domain"] not in have]
            db.table("market_snapshots").update({"data": data}).eq("id", snap_row["id"]).execute()
        except Exception as e:  # noqa: BLE001
            logger.warning("market topup snapshot append failed for %s: %s", org_id, e)
    return rows


def compute_overview(org_id: str, market_id: Optional[str] = None) -> Dict[str, Any]:
    """Build the full market payload for one org (one MARKET post-054 —
    no id resolves the primary, so pre-054 callers are unchanged).
    Synchronous — the caller decides whether to serve a snapshot or
    recompute. ~5 searches + up to ~12 per-row verification lookups; all
    non-reveal Apollo calls."""
    db = get_supabase()
    cfg_row = (db.table("organisations").select("icp_config, name")
               .eq("id", org_id).maybe_single().execute().data or {})
    icp_config = cfg_row.get("icp_config") or {}

    market = get_market(db, org_id, market_id)
    if market_id and not market:
        return {"error": "unknown_market"}
    profile = observed_profile(db, org_id, market)
    criteria = market_criteria(market_icp_like(market, icp_config), profile)
    if not criteria["industries"]:
        return {"error": "no_criteria", "profile": profile}

    seller_profile = build_seller_profile(icp_config)
    watched = _watched_domains(db, org_id)

    # Universe: total + a sample page whose classifier keep-ratio estimates
    # how much of the raw count is junk (vendors-to-the-sector, recruiters,
    # trade bodies — see the radar test findings).
    universe_total, universe_sample = _apollo_search(criteria, {}, _SLICE_FETCH_ROWS)
    keep_ratio: Optional[float] = None
    if universe_sample:
        _annotate_industries(db, universe_sample)  # cache-only; 50 enriches would be too many
        gated = _industry_gate(universe_sample, criteria["industries"], strict=False)
        kept = _classify_keep(gated, seller_profile)
        if len(kept) != len(universe_sample):
            keep_ratio = len(kept) / len(universe_sample)
    est_clean = int(round(universe_total * keep_ratio / 10.0) * 10) if keep_ratio else None

    # One budgeted lookup shared by all three slices (2026-08-14 incident:
    # unbudgeted enriching rate-limited the key and blanked the page).
    # 100 lookups: the first compute for a fresh org hits cold caches (the
    # 2026-08-16 healthcare run verified 1 row across three slices on a
    # 60 budget) and it's the onboarding-reveal compute, so depth matters
    # most. Steady-state weekly runs barely touch it once caches warm.
    lookup = _make_lookup(db, 100)

    slices = []
    for key, label in (
        ("hiring_security", "Hiring security roles"),
        ("funding", "New funding & deals"),
        ("growth", "Growing fast"),
    ):
        total, rows, (gated, passed) = _qualified_slice_rows(
            db, criteria, seller_profile, watched, _slice_extra(key), lookup=lookup,
        )
        verified = _verify_for_slice(key, rows)
        # Cleaned slice count: the raw total is Apollo's keyword-tag match
        # and can be almost entirely vendors-to-the-sector on a vertical
        # market (UK Retailers 2026-08-27: 73 raw, 0 in-market). Scale it
        # by the in-market rate of the rows actually gated this run; never
        # claim fewer than the rows we verified.
        est: Optional[int] = None
        if total and gated:
            est = max(int(round(total * passed / gated)), len(verified))
            if est >= 100:
                est = int(round(est / 10.0) * 10)
        slices.append({"key": key, "label": label, "total": total,
                       "est_clean": est, "rows": verified})

    ctx = market_deal_context(market, icp_config)
    try:
        avg_deal = int(ctx.get("average_deal_size") or 0)
    except (TypeError, ValueError):
        avg_deal = 0
    # Value only exists once the classifier has produced a cleaned count —
    # deal size x a junk-inflated raw total is a number we'd have to retract.
    value_basis = est_clean if est_clean is not None else universe_total
    est_value = avg_deal * est_clean if avg_deal and est_clean else None

    payload: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "criteria": criteria,
        "universe": {"total": universe_total, "est_clean": est_clean, "keep_ratio": keep_ratio},
        "coverage": {
            "watching": profile["accounts"],
            "pct": round(100.0 * profile["accounts"] / value_basis, 1) if value_basis else None,
        },
        "value": {"estimated_annual": est_value, "avg_deal_size": avg_deal or None,
                  "currency": (ctx.get("currency") or None)},
        "slices": slices,
    }

    # Diff vs the previous snapshot so the page can badge what's new, then
    # persist this run as the new baseline.
    try:
        prev = (_snapshot_scope(db.table("market_snapshots").select("data")
                                .eq("org_id", org_id), market)
                .order("captured_at", desc=True)
                .limit(1).execute().data or [])
        prev_domains: Dict[str, set] = {}
        prev_rows: Dict[str, List[Dict[str, Any]]] = {}
        if prev:
            for s in (prev[0].get("data") or {}).get("slices") or []:
                prev_domains[s.get("key")] = {r.get("domain") for r in s.get("rows") or []}
                prev_rows[s.get("key")] = [r for r in s.get("rows") or [] if isinstance(r, dict)]
        # Last-good-data guarantee (2026-08-14 rate-limit incident): a fresh
        # compute that comes back thin — vendor throttling, transient errors —
        # must never blank a slice that had verified rows last time. Top thin
        # slices up from the previous snapshot's rows (already verified then;
        # still excluded if since watched). Diff runs BEFORE the top-up so
        # carried rows are never badged "new".
        for s in payload["slices"]:
            before = prev_domains.get(s["key"])
            s["new_domains"] = ([r["domain"] for r in s["rows"] if r["domain"] not in before]
                                if before is not None else [])
            if len(s["rows"]) < 6:
                have = {r["domain"] for r in s["rows"]}
                for p in prev_rows.get(s["key"], []):
                    if len(s["rows"]) >= 6:
                        break
                    dom = (p.get("domain") or "").lower()
                    if dom and dom not in have and dom not in watched:
                        have.add(dom)
                        s["rows"].append(p)
        db.table("market_snapshots").insert({
            "org_id": org_id,
            "market_id": (market or {}).get("id"),
            "data": payload,
        }).execute()
    except Exception as e:  # noqa: BLE001
        logger.warning("market snapshot persist failed for %s: %s", org_id, e)
        for s in payload["slices"]:
            s.setdefault("new_domains", [])

    return payload


def latest_snapshot(db, org_id: str, max_age_hours: int = 24,
                    market: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Most recent stored payload for one market if it's fresh enough."""
    rows = (_snapshot_scope(db.table("market_snapshots").select("data, captured_at")
                            .eq("org_id", org_id), market)
            .order("captured_at", desc=True)
            .limit(1).execute().data or [])
    if not rows:
        return None
    try:
        captured = datetime.fromisoformat(str(rows[0]["captured_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    if datetime.now(timezone.utc) - captured > timedelta(hours=max_age_hours):
        return None
    return rows[0].get("data")
