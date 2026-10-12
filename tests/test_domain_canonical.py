"""Add-time domain canonicalisation (2026-08-25).

Pins the contract: a same-brand redirect target is adopted (the site told
us where it lives), a cross-brand redirect is only ever a suggestion, and
an Apollo-blind domain whose company Apollo covers under a sibling domain
(octopusenergy.com vs octopus.energy) produces a suggestion — never a
silent swap (the barricade.ai / barricadesystems.com near-miss).
"""

import pytest

import app.services.domain_resolver as dr


@pytest.fixture
def offline(monkeypatch):
    """No live network: unreachable site, Apollo knows every domain.
    Individual tests override the seams they exercise."""
    monkeypatch.setattr(dr, "_redirect_target", lambda domain: None)
    monkeypatch.setattr(dr, "_apollo_knows_domain", lambda domain: True)


def test_registrable_brand():
    assert dr._registrable_brand("octopusenergy.com") == "octopusenergy"
    assert dr._registrable_brand("octopus.energy") == "octopus"
    assert dr._registrable_brand("acme.co.uk") == "acme"
    assert dr._registrable_brand("us.gt.com") == "gt"


def test_unreachable_domain_passes_through(offline):
    out = dr.canonicalise_domain("acme.com", "Acme")
    assert out == {"domain": "acme.com", "canonicalised_from": None, "suggestion": None}


def test_url_input_is_cleaned(offline):
    out = dr.canonicalise_domain("https://www.acme.com/about?x=1", "Acme")
    assert out["domain"] == "acme.com"


def test_same_brand_redirect_is_adopted(offline, monkeypatch):
    monkeypatch.setattr(dr, "_redirect_target", lambda domain: "postman.com")
    out = dr.canonicalise_domain("getpostman.com", "Postman")
    assert out["domain"] == "postman.com"
    assert out["canonicalised_from"] == "getpostman.com"
    assert out["suggestion"] is None


def test_subdomain_redirect_is_same_site(offline, monkeypatch):
    monkeypatch.setattr(dr, "_redirect_target", lambda domain: "app.acme.com")
    out = dr.canonicalise_domain("acme.com", "Acme")
    assert out["domain"] == "acme.com"
    assert out["canonicalised_from"] is None


def test_cross_brand_redirect_is_suggested_not_adopted(offline, monkeypatch):
    monkeypatch.setattr(dr, "_redirect_target", lambda domain: "finastra.com")
    out = dr.canonicalise_domain("dh.com", "D+H")
    assert out["domain"] == "dh.com"  # never silently refiled
    assert out["suggestion"]["domain"] == "finastra.com"


def test_apollo_blind_sibling_is_suggested(offline, monkeypatch):
    monkeypatch.setattr(dr, "_apollo_knows_domain", lambda domain: False)
    monkeypatch.setattr(
        "app.services.apollo_orgs.search_by_name",
        lambda name, limit=5: [{"name": "Octopus Energy", "domain": "octopus.energy"}],
    )
    out = dr.canonicalise_domain("octopusenergy.com", "Octopus Energy")
    assert out["domain"] == "octopusenergy.com"
    assert out["suggestion"]["domain"] == "octopus.energy"
    assert "no data for octopusenergy.com" in out["suggestion"]["reason"]


def test_apollo_blind_name_mismatch_gives_no_suggestion(offline, monkeypatch):
    monkeypatch.setattr(dr, "_apollo_knows_domain", lambda domain: False)
    monkeypatch.setattr(
        "app.services.apollo_orgs.search_by_name",
        lambda name, limit=5: [{"name": "Rampart Security", "domain": "rampartsec.com"}],
    )
    out = dr.canonicalise_domain("barricade.ai", "Barricade")
    assert out["suggestion"] is None


def test_no_company_name_skips_the_apollo_check(offline, monkeypatch):
    def explode(domain):
        raise AssertionError("Apollo check must not run without a company name")

    monkeypatch.setattr(dr, "_apollo_knows_domain", explode)
    out = dr.canonicalise_domain("acme.com")
    assert out["suggestion"] is None
