"""Market radar in-market membership (2026-08-27).

Pins the contract behind the UK Retailers 73-vs-0 fix: a vertical market
must admit real vertical brands whose Apollo PRIMARY industry is their
product category (ME+EM = apparel & fashion, Bulk = health wellness &
fitness), while vendors that merely TAG the vertical (MoonPay/Primer tag
"retail"+"e-commerce" but sell payments to retailers) stay out. Common ICP
terms that aren't Apollo primary strings (ecommerce, finance, healthcare)
must map onto Apollo's taxonomy instead of silently matching nothing.
"""

from app.services.market_radar import (
    _CONSUMER_PRIMARIES, _DENY_INDUSTRIES, _FINANCE_PRIMARIES,
    _TERM_PRIMARY_ALIASES, _TERM_TAG_PRIMARIES, _in_market, _industry_gate,
)

RETAIL = ["retail", "ecommerce"]

# Apollo `organization.industry` values verified real on 2026-08-27: every
# distinct value ever returned to us (companies + org_industry_cache,
# ~2,700 enriched rows) plus live enrich probes (Morrisons/TUI/Rockwell/
# Maersk/GXO/Wincanton/ITV/Diageo/Camira). Apollo collapses niche verticals
# into broader primaries — "supermarkets"/"warehousing"/"broadcast media"
# etc. were never observed, so a membership map entry using one would
# silently never match. Extend only with observed evidence.
KNOWN_APOLLO_PRIMARIES = {
    "accounting", "airlines/aviation", "alternative medicine", "animation",
    "apparel & fashion", "architecture & planning", "automotive",
    "aviation & aerospace", "banking", "biotechnology", "capital markets",
    "chemicals", "civic & social organization", "civil engineering",
    "computer & network security", "computer games", "computer hardware",
    "computer software", "construction", "consumer electronics",
    "consumer goods", "consumer services", "cosmetics", "defense & space",
    "design", "e-learning", "education management",
    "electrical/electronic manufacturing", "entertainment",
    "environmental services", "events services", "facilities services",
    "financial services", "food & beverages", "food production", "furniture",
    "gambling & casinos", "government administration", "health, wellness & fitness",
    "higher education", "hospital & health care", "hospitality",
    "human resources", "information services",
    "information technology & services", "insurance",
    "international trade & development", "internet", "investment banking",
    "investment management", "law practice", "legal services",
    "logistics & supply chain", "luxury goods & jewelry", "machinery",
    "management consulting", "market research", "marketing & advertising",
    "mechanical or industrial engineering", "media production",
    "medical devices", "medical practice", "mental health care",
    "mining & metals", "newspapers", "nonprofit organization management",
    "oil & energy", "online media", "outsourcing/offshoring",
    "package/freight delivery", "packaging & containers", "pharmaceuticals",
    "primary/secondary education", "printing", "program development",
    "professional training & coaching", "public policy",
    "public relations & communications", "public safety", "publishing",
    "real estate", "renewables & environment", "research", "retail",
    "security & investigations", "semiconductors", "sporting goods",
    "sports", "staffing & recruiting", "telecommunications", "textiles",
    "translation & localization", "utilities", "venture capital & private equity",
    "veterinary",
}


def test_every_mapped_primary_is_a_real_apollo_value():
    mapped = set(_CONSUMER_PRIMARIES) | set(_FINANCE_PRIMARIES) | set(_DENY_INDUSTRIES)
    for vals in _TERM_PRIMARY_ALIASES.values():
        mapped |= set(vals)
    for vals in _TERM_TAG_PRIMARIES.values():
        mapped |= set(vals)
    unknown = mapped - KNOWN_APOLLO_PRIMARIES
    assert not unknown, f"never observed from Apollo, would dead-match: {sorted(unknown)}"


def row(industry, keywords=(), name="Acme"):
    return {"name": name, "domain": "x.com", "industry": industry,
            "keywords": list(keywords)}


# --- route 1: primary industry is the term or a curated equivalent --------

def test_exact_primary_match():
    assert _in_market(row("retail"), RETAIL)


def test_ecommerce_term_matches_retail_primary():
    # Also covers supermarket chains: Apollo returns "retail" for them
    # (Morrisons live probe 2026-08-27), not a "supermarkets" primary.
    assert _in_market(row("retail"), ["ecommerce"])


def test_finance_term_spans_apollo_finance_primaries():
    for primary in ("financial services", "banking", "insurance",
                    "investment management"):
        assert _in_market(row(primary), ["finance"]), primary


def test_vc_never_rides_into_a_finserv_market():
    # Y Combinator leaking into a finserv market was the original
    # 2026-08-13 failure — its PRIMARY industry must not alias in either.
    yc = row("venture capital & private equity", ["financial services"])
    assert not _in_market(yc, ["financial services"])
    assert not _in_market(yc, ["finance"])
    assert _in_market(yc, ["venture capital & private equity"])


def test_healthcare_term_maps_to_apollo_taxonomy():
    assert _in_market(row("hospital & health care"), ["healthcare"])
    assert _in_market(row("pharmaceuticals"), ["healthcare"])
    assert not _in_market(row("retail"), ["healthcare"])


# --- route 2: vertical brand primaries need the tag, services never pass --

def test_fashion_brand_tagged_retail_is_in_market():
    # ME+EM: primary apparel & fashion, tags include retail (live 2026-08-27)
    assert _in_market(row("apparel & fashion", ["fashion", "retail", "d2c"]),
                      RETAIL)


def test_supplement_brand_tagged_ecommerce_is_in_market():
    # Bulk: primary health wellness & fitness, tags e-commerce (hyphenated)
    assert _in_market(
        row("health, wellness & fitness", ["supplements", "e-commerce"]),
        RETAIL)


def test_vertical_brand_without_the_tag_stays_out():
    assert not _in_market(row("apparel & fashion", ["fashion", "d2c"]),
                          RETAIL)


def test_payments_firm_tagged_retail_stays_out():
    # MoonPay/Primer: service-sector primaries, tags retail + e-commerce
    assert not _in_market(
        row("information technology & services", ["crypto", "retail", "e-commerce"]),
        RETAIL)
    assert not _in_market(
        row("financial services", ["payments", "retail", "e-commerce"]),
        RETAIL)


def test_insurer_with_finserv_primary_tagged_insurance():
    # Admiral: primary financial services, tags insurance (live 2026-08-27)
    assert _in_market(row("financial services", ["car insurance", "insurance"]),
                      ["insurance"])


def test_tag_match_is_exact_not_substring():
    # Primer tags "open banking payments" — must not join a banking market
    assert not _in_market(
        row("financial services", ["open banking payments"]), ["banking"])


def test_no_keywords_means_no_tag_route():
    assert not _in_market(row("apparel & fashion", keywords=()), RETAIL)


def test_unknown_industry_never_in_market():
    assert not _in_market(row(None, ["retail"]), RETAIL)


# --- _industry_gate strictness contract unchanged -------------------------

def test_gate_strict_drops_unknown_industry():
    rows = [row("retail"), row(None)]
    assert len(_industry_gate(rows, RETAIL, strict=True)) == 1


def test_gate_lenient_keeps_unknown_industry():
    rows = [row("retail"), row(None)]
    assert len(_industry_gate(rows, RETAIL, strict=False)) == 2
