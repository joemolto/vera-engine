"""Discovery was the binding constraint on perimeter coverage.

Guessing 24 hostnames found something on 16% of accounts. Arqiva alone
publishes 261. These tests pin the behaviour that matters: the wordlist stays
as a floor so a crt.sh outage degrades to the old behaviour rather than to
silence, and edge-looking hosts sort first because callers cap probing.
"""

from unittest.mock import patch

from app.services.subdomain_discovery import (
    FALLBACK_HOSTNAMES,
    discover_subdomains,
    rank_for_probing,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or []

    def json(self):
        return self._payload


def _crtsh(names):
    return FakeResponse(payload=[{"name_value": "\n".join(names)}])


class TestDiscovery:
    def test_ct_hosts_are_returned(self):
        with patch("app.services.subdomain_discovery.httpx.get",
                   return_value=_crtsh(["vpn.example.com", "odd.example.com"])):
            out = discover_subdomains("example.com")
        assert "odd.example.com" in out

    def test_wordlist_is_always_unioned_in(self):
        with patch("app.services.subdomain_discovery.httpx.get",
                   return_value=_crtsh(["only.example.com"])):
            out = discover_subdomains("example.com")
        for p in FALLBACK_HOSTNAMES:
            assert f"{p}.example.com" in out

    def test_crtsh_failure_degrades_to_wordlist_not_silence(self):
        with patch("app.services.subdomain_discovery.httpx.get",
                   side_effect=OSError("down")):
            out = discover_subdomains("example.com")
        assert len(out) == len(FALLBACK_HOSTNAMES)
        assert "vpn.example.com" in out

    def test_non_200_degrades_to_wordlist(self):
        with patch("app.services.subdomain_discovery.httpx.get",
                   return_value=FakeResponse(status_code=502)):
            out = discover_subdomains("example.com")
        assert len(out) == len(FALLBACK_HOSTNAMES)

    def test_wildcards_and_foreign_domains_are_dropped(self):
        with patch("app.services.subdomain_discovery.httpx.get",
                   return_value=_crtsh([
                       "*.example.com", "good.example.com",
                       "someoneelse.com", "x@example.com",
                   ])):
            out = discover_subdomains("example.com")
        assert "someoneelse.com" not in out
        assert not any(h.startswith("*") for h in out)
        assert not any("@" in h for h in out)
        assert "good.example.com" in out

    def test_wildcard_stem_is_kept_as_the_bare_host(self):
        with patch("app.services.subdomain_discovery.httpx.get",
                   return_value=_crtsh(["*.vpn.example.com"])):
            out = discover_subdomains("example.com")
        assert "vpn.example.com" in out

    def test_empty_domain_returns_nothing(self):
        assert discover_subdomains("") == []
        assert discover_subdomains(None) == []


class TestRanking:
    def test_edge_hosts_sort_before_marketing_hosts(self):
        hosts = {"blog.example.com", "vpn.example.com", "careers.example.com"}
        assert rank_for_probing(hosts, "example.com")[0] == "vpn.example.com"

    def test_owa_and_mft_rank_above_generic(self):
        hosts = {"cdn.example.com", "owa.example.com", "mft.example.com"}
        ranked = rank_for_probing(hosts, "example.com")
        assert ranked[-1] == "cdn.example.com"

    def test_ranking_is_stable_and_total(self):
        hosts = {f"h{i}.example.com" for i in range(20)}
        assert len(rank_for_probing(hosts, "example.com")) == 20
