"""Email accuracy pass (2026-08-25).

Pins the waterfall's post-resolution checks: discovery placeholders are
never trusted as Apollo addresses, Apollo emails start at 80 (not a
blanket 95), a Findymail verify promotes/collapses the layer confidence,
a dead email domain short-circuits to 5 without spending a verify, and an
address on a different apex than the account domain is flagged + capped.
"""

import pytest

import app.services.email_finder as ef


@pytest.fixture
def quiet_vendors(monkeypatch):
    """No live vendors: Findymail finder off, verifier 'couldn't check',
    DNS says the domain can receive mail, no sibling-pattern basis.
    Individual tests override."""
    monkeypatch.setattr(ef.settings, "FINDYMAIL_API_KEY", "")
    monkeypatch.setattr(ef, "verify_email", lambda email: None)
    monkeypatch.setattr(ef, "domain_has_mail_dns", lambda domain: True)
    monkeypatch.setattr(ef, "check_pattern_consistency", lambda *a: None)


def _explode(email):  # a verify stub that must never run
    raise AssertionError(f"verify_email should not have been called ({email})")


# ---------------------------------------------------------------------------
# mail_domains_match
# ---------------------------------------------------------------------------

def test_mail_domains_match_rules():
    assert ef.mail_domains_match("acme.com", "acme.com")
    assert ef.mail_domains_match("us.gt.com", "gt.com")  # subdomain estate
    assert ef.mail_domains_match("gt.com", "us.gt.com")
    assert ef.mail_domains_match("acme.com", "www.acme.com")
    assert not ef.mail_domains_match("dh.com", "finastra.com")  # pre-merger trap
    assert ef.mail_domains_match("", "acme.com")  # nothing to compare
    assert ef.mail_domains_match("acme.com", "")


# ---------------------------------------------------------------------------
# Layer 0: Apollo email handling
# ---------------------------------------------------------------------------

def test_placeholder_apollo_email_is_ignored(quiet_vendors):
    result = ef.find_email(
        apollo_email="placeholder_abc123@unresolved.local",
        first_name="Jane", last_name="Doe", company_domain="acme.com",
    )
    assert result["source"] == "permutation"
    assert result["email"] == "jane.doe@acme.com"
    assert result["confidence"] == 35


def test_apollo_email_unverifiable_starts_at_80(quiet_vendors):
    result = ef.find_email(apollo_email="jane@acme.com", company_domain="acme.com")
    assert result["source"] == "apollo"
    assert result["confidence"] == 80
    assert result["email_verified"] is None


def test_apollo_email_verified_bumps_to_99(quiet_vendors, monkeypatch):
    monkeypatch.setattr(ef, "verify_email", lambda email: True)
    result = ef.find_email(apollo_email="jane@acme.com", company_domain="acme.com")
    assert result["confidence"] == 99
    assert result["email_verified"] is True


def test_apollo_email_bouncing_collapses_to_15(quiet_vendors, monkeypatch):
    monkeypatch.setattr(ef, "verify_email", lambda email: False)
    result = ef.find_email(apollo_email="jane@acme.com", company_domain="acme.com")
    assert result["confidence"] == 15
    assert result["email_verified"] is False


# ---------------------------------------------------------------------------
# Domain mismatch (finastra.com contact revealed with a dh.com address)
# ---------------------------------------------------------------------------

def test_domain_mismatch_caps_unverified_at_55(quiet_vendors):
    result = ef.find_email(apollo_email="bob@dh.com", company_domain="finastra.com")
    assert result["confidence"] == 55
    assert result["checks"]["domain_mismatch"] is True


def test_domain_mismatch_verified_keeps_confidence_but_flags(quiet_vendors, monkeypatch):
    monkeypatch.setattr(ef, "verify_email", lambda email: True)
    result = ef.find_email(apollo_email="bob@dh.com", company_domain="finastra.com")
    assert result["confidence"] == 99  # deliverable — but the flag stays
    assert result["checks"]["domain_mismatch"] is True


def test_subdomain_email_is_not_flagged(quiet_vendors):
    result = ef.find_email(apollo_email="bob@us.gt.com", company_domain="gt.com")
    assert result["confidence"] == 80
    assert "checks" not in result


# ---------------------------------------------------------------------------
# Dead email domain
# ---------------------------------------------------------------------------

def test_dead_domain_short_circuits_without_a_verify(quiet_vendors, monkeypatch):
    monkeypatch.setattr(ef, "domain_has_mail_dns", lambda domain: False)
    monkeypatch.setattr(ef, "verify_email", _explode)
    result = ef.find_email(apollo_email="bob@gone-corp.com", company_domain="acme.com")
    assert result["confidence"] == 5
    assert result["email_verified"] is False
    assert result["checks"]["dead_domain"] is True


# ---------------------------------------------------------------------------
# Permutation runner-up promotion
# ---------------------------------------------------------------------------

def test_bouncing_best_guess_promotes_verified_runner_up(quiet_vendors, monkeypatch):
    verdicts = iter([False, True])  # jane.doe@ bounces, janedoe@ verifies
    monkeypatch.setattr(ef, "verify_email", lambda email: next(verdicts))
    result = ef.find_email(first_name="Jane", last_name="Doe", company_domain="acme.com")
    assert result["email"] == "janedoe@acme.com"
    assert result["confidence"] == 85
    assert result["email_verified"] is True


def test_all_guesses_bouncing_collapses_to_15(quiet_vendors, monkeypatch):
    monkeypatch.setattr(ef, "verify_email", lambda email: False)
    monkeypatch.setattr(ef, "_ai_guess_email", lambda *a: None)
    result = ef.find_email(first_name="Jane", last_name="Doe", company_domain="acme.com")
    assert result["email"] == "jane.doe@acme.com"
    assert result["confidence"] == 15
    assert result["email_verified"] is False


# ---------------------------------------------------------------------------
# A bouncing candidate falls through the waterfall (regression:
# Apollo's address bounced and we shipped it at 15 without asking Findymail)
# ---------------------------------------------------------------------------

def test_bouncing_apollo_email_falls_through_to_findymail(quiet_vendors, monkeypatch):
    monkeypatch.setattr(ef.settings, "FINDYMAIL_API_KEY", "test-key")
    monkeypatch.setattr(ef, "verify_email", lambda email: False)  # Apollo's bounces
    monkeypatch.setattr(ef, "_findymail_by_linkedin", lambda url: None)
    monkeypatch.setattr(
        ef, "_findymail_by_name",
        lambda name, domain: {"email": "s.taylor@acme.com", "verified": True},
    )
    result = ef.find_email(
        apollo_email="simon.taylor@acme.com",
        first_name="Simon", last_name="Taylor", company_domain="acme.com",
    )
    assert result["source"] == "findymail_name"
    assert result["email"] == "s.taylor@acme.com"
    assert result["confidence"] == 90
    assert result["email_verified"] is True


def test_everything_bouncing_returns_apollo_candidate_at_15(quiet_vendors, monkeypatch):
    monkeypatch.setattr(ef, "verify_email", lambda email: False)
    monkeypatch.setattr(ef, "_ai_guess_email", lambda *a: None)
    result = ef.find_email(
        apollo_email="simon.taylor@acme.com",
        first_name="Simon", last_name="Taylor", company_domain="acme.com",
    )
    # Apollo's candidate (the most trusted layer that produced one) comes
    # back as the honest ≤15 answer, not a random bounced permutation.
    assert result["source"] == "apollo"
    assert result["email"] == "simon.taylor@acme.com"
    assert result["confidence"] == 15
    assert result["email_verified"] is False


# ---------------------------------------------------------------------------
# Findymail finder-verified addresses skip the redundant re-verify
# ---------------------------------------------------------------------------

def test_infer_email_pattern():
    assert ef.infer_email_pattern("pnewton@northwind.com", "Pat", "Newton") == "{fi}{l}"
    assert ef.infer_email_pattern("jane.doe@acme.com", "Jane", "Doe") == "{f}.{l}"
    # tsam = surname-initial + first name: not a convention we generate
    assert ef.infer_email_pattern("tsam@northwind.com", "Sam", "Thorne") is None


def test_pattern_conflict_caps_unverifiable_at_40(quiet_vendors, monkeypatch):
    monkeypatch.setattr(ef, "check_pattern_consistency", lambda *a: False)
    result = ef.find_email(
        apollo_email="tsam@northwind.com",
        first_name="Sam", last_name="Thorne", company_domain="northwind.com",
    )
    assert result["confidence"] == 40
    assert result["checks"]["pattern_conflict"] is True


def test_pattern_consistent_address_keeps_confidence(quiet_vendors, monkeypatch):
    monkeypatch.setattr(ef, "check_pattern_consistency", lambda *a: True)
    result = ef.find_email(
        apollo_email="sthorne@northwind.com",
        first_name="Sam", last_name="Thorne", company_domain="northwind.com",
    )
    assert result["confidence"] == 80
    assert "checks" not in result


def test_verified_address_skips_pattern_check(quiet_vendors, monkeypatch):
    monkeypatch.setattr(ef, "verify_email", lambda email: True)
    monkeypatch.setattr(ef, "check_pattern_consistency", lambda *a: (_ for _ in ()).throw(AssertionError("must not run")))
    result = ef.find_email(apollo_email="jane@acme.com", company_domain="acme.com")
    assert result["confidence"] == 99


def test_findymail_verified_result_skips_reverify(quiet_vendors, monkeypatch):
    monkeypatch.setattr(ef.settings, "FINDYMAIL_API_KEY", "test-key")
    monkeypatch.setattr(
        ef, "_findymail_by_linkedin",
        lambda url: {"email": "jane@acme.com", "verified": True},
    )
    monkeypatch.setattr(ef, "verify_email", _explode)
    result = ef.find_email(
        linkedin_url="https://linkedin.com/in/janedoe", company_domain="acme.com",
    )
    assert result["source"] == "findymail_linkedin"
    assert result["confidence"] == 90
    assert result["email_verified"] is True
