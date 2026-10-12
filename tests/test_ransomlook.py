"""Leak-site matching must never accuse the wrong company.

Replaces a `breach_mention` signal that filed a carmaker's safety recall and
a retailer's shareholder revolt as security breaches. The new one names a ransomware group, so a false match is
a serious accusation about a real business rather than a bit of noise.
"""

from unittest.mock import patch

from app.services.ransomlook import (
    LeakVictim,
    _normalise,
    build_breach_signal,
    fetch_recent_victims,
    match_company,
)


def _v(name, group="lockbit"):
    return LeakVictim(name=name, group=group, discovered="2026-08-04 13:46:04",
                      description="d", link="/blog/x")


class TestNormalisation:
    def test_strips_a_single_legal_form(self):
        assert _normalise("Northwind plc") == "northwind"
        assert _normalise("Acme GmbH") == "acme"

    def test_does_not_chain_suffix_stripping(self):
        """'Northwind Installation Group, Inc.' collapsed to 'northwind
        installation' when suffixes chained, eating a real part of the name."""
        assert _normalise("Northwind Installation Group, Inc.") == "northwind installation group"

    def test_group_and_holdings_are_never_stripped(self):
        """Collapsing 'Fabrikam Group' to 'Fabrikam' would match a different company."""
        assert _normalise("Fabrikam Group") == "fabrikam group"
        assert _normalise("Contoso Holdings") == "contoso holdings"

    def test_domain_reduces_to_its_stem(self):
        assert _normalise("examplehealthaccess.org") == "examplehealthaccess"
        assert _normalise("https://www.northwind.com") == "northwind"

    def test_case_and_trailing_punctuation_are_ignored(self):
        assert _normalise("NORTHWIND PLC") == _normalise("Northwind plc")
        assert _normalise("Acme, Inc.") == "acme"

    def test_dotted_initialisms_stay_separated_and_so_fail_to_match(self):
        """'A.C.M.E.' normalises to 'a c m e', which will NOT match 'acme'.

        That is a miss, and the right kind. For a signal that names a
        ransomware group, a false negative costs a lead; a false positive
        accuses a real business of being breached. The domain path
        ('acme.com' -> 'acme') still catches these when a domain is known.
        """
        assert _normalise("A.C.M.E., Inc.") == "a c m e"
        assert match_company("A.C.M.E., Inc.", None, [_v("ACME")]) is None
        assert match_company(None, "acme.com", [_v("ACME")]) is not None


class TestMatching:
    def test_exact_normalised_match(self):
        vs = [_v("Northwind plc", group="akira")]
        assert match_company("Northwind", None, vs).group == "akira"

    def test_domain_matches_victim_named_by_domain(self):
        vs = [_v("examplehealthaccess.org")]
        assert match_company(None, "examplehealthaccess.org", vs) is not None

    def test_partial_name_does_not_match(self):
        """Substring matching would put every 'Fabrikam' victim on Fabrikam Group."""
        assert match_company("Fabrikam Group", None, [_v("Fabrikam")]) is None
        assert match_company("Acme", None, [_v("Acme Financial Services")]) is None

    def test_no_identifiers_never_matches(self):
        assert match_company(None, None, [_v("Anything")]) is None
        assert match_company("", "", [_v("Anything")]) is None

    def test_empty_feed_returns_none(self):
        assert match_company("Northwind", "northwind.com", []) is None


class TestFetchResilience:
    def test_non_200_returns_empty(self):
        class R:
            status_code = 503
        with patch("app.services.ransomlook.httpx.get", return_value=R()):
            assert fetch_recent_victims() == []

    def test_network_error_returns_empty(self):
        with patch("app.services.ransomlook.httpx.get", side_effect=OSError("x")):
            assert fetch_recent_victims() == []

    def test_rows_without_a_title_are_dropped(self):
        class R:
            status_code = 200
            @staticmethod
            def json():
                return [{"post_title": "", "group_name": "g"},
                        {"post_title": "Real Co", "group_name": "g"}]
        with patch("app.services.ransomlook.httpx.get", return_value=R()):
            out = fetch_recent_victims()
        assert [v.name for v in out] == ["Real Co"]


class TestSignal:
    def test_signal_names_the_group_and_is_flagged_internal(self):
        row = build_breach_signal(_v("Northwind plc", group="akira"),
                                  "org", "co", "Northwind")
        d = row["signal_data"]
        assert "akira" in d["headline"]
        assert row["signal_type"] == "breach_mention"
        assert row["signal_source"] == "ransomlook"
        assert row["signal_strength"] == 0.95
        # Must never reach outbound copy — reads as ambulance-chasing.
        assert d["outbound_safe"] is False

    def test_signal_carries_a_verifiable_source_link(self):
        row = build_breach_signal(_v("X"), "org", "co")
        assert row["signal_data"]["source_link"].startswith("http")
