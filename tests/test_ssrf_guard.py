"""SSRF guard + DNS-rebinding pin tests for the public free-report fetch path.

The free-report tool fetches a user-supplied domain server-side, so the guard
must (a) reject any host that resolves to internal infra and (b) pin the
validated IP for the connection so httpx can't be rebound onto an internal
host between the check and the connect.
"""

import ipaddress
import socket
from unittest.mock import MagicMock

from app.services.enrichers import tech_stack as ts


def _ai(ip, port=443):
    """One getaddrinfo-shaped record for `ip`."""
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]


class _FakeResp:
    def __init__(self, status=200, text="ok", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308)


# --- IP classification -----------------------------------------------------

def test_ip_is_public_accepts_public():
    assert ts._ip_is_public(ipaddress.ip_address("93.184.216.34"))


def test_ip_is_public_rejects_internal_ranges():
    for bad in ["127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254",
                "100.64.0.1", "0.0.0.0", "224.0.0.1"]:
        assert not ts._ip_is_public(ipaddress.ip_address(bad)), bad


def test_ip_is_public_unwraps_ipv4_mapped_ipv6():
    assert not ts._ip_is_public(ipaddress.ip_address("::ffff:169.254.169.254"))
    assert not ts._ip_is_public(ipaddress.ip_address("::ffff:127.0.0.1"))


def test_ip_is_public_blocks_nat64():
    # 64:ff9b::a9fe:a9fe embeds 169.254.169.254 (cloud metadata) via NAT64.
    assert not ts._ip_is_public(ipaddress.ip_address("64:ff9b::a9fe:a9fe"))


# --- name-level screening --------------------------------------------------

def test_resolve_rejects_internal_and_rebind_names():
    assert ts._resolve_public_ips("foo.internal") is None
    assert ts._resolve_public_ips("1.2.3.4.nip.io") is None
    assert ts._resolve_public_ips("") is None


def test_resolve_returns_ips_for_public_host(monkeypatch):
    monkeypatch.setattr(ts, "_ORIG_GETADDRINFO", lambda h, p, *a, **k: _ai("93.184.216.34"))
    assert ts._resolve_public_ips("example.com") == ["93.184.216.34"]


def test_resolve_rejects_private_host(monkeypatch):
    monkeypatch.setattr(ts, "_ORIG_GETADDRINFO", lambda h, p, *a, **k: _ai("127.0.0.1"))
    assert ts._resolve_public_ips("evil.test") is None


# --- pinning defeats DNS rebinding -----------------------------------------

def test_pinned_getaddrinfo_delegates_when_unpinned(monkeypatch):
    seen = {}

    def orig(h, p, *a, **k):
        seen["host"] = h
        return _ai("93.184.216.34")

    monkeypatch.setattr(ts, "_ORIG_GETADDRINFO", orig)
    assert ts._pinned_getaddrinfo("example.com", 443) == _ai("93.184.216.34")
    assert seen["host"] == "example.com"


def test_pin_wins_over_rebinding_resolver(monkeypatch):
    # Resolver answers "public" once (validation) then flips to a private IP,
    # exactly as a rebinding nameserver would. IP literals resolve to self.
    flip_answers = ["93.184.216.34", "127.0.0.1"]

    def flip(h, p, *a, **k):
        try:
            ipaddress.ip_address(h)
            return _ai(h)
        except ValueError:
            return _ai(flip_answers.pop(0))

    monkeypatch.setattr(ts, "_ORIG_GETADDRINFO", flip)

    ips = ts._resolve_public_ips("rebind.test")  # consumes the public answer
    assert ips == ["93.184.216.34"]

    with ts._pin_host("rebind.test", ips):
        pinned = ts._pinned_getaddrinfo("rebind.test", 443)
    assert pinned == _ai("93.184.216.34"), "pin must override the rebound answer"

    # Without the pin the resolver would now leak the private IP — proving the
    # pin is what closes the window.
    assert ts._pinned_getaddrinfo("rebind.test", 443) == _ai("127.0.0.1")


def test_pin_is_cleared_after_context(monkeypatch):
    with ts._pin_host("example.com", ["93.184.216.34"]):
        assert getattr(ts._PIN, "map", {}).get("example.com") == ["93.184.216.34"]
    assert "example.com" not in getattr(ts._PIN, "map", {})


# --- guarded_get end to end ------------------------------------------------

def test_guarded_get_refuses_private_host(monkeypatch):
    monkeypatch.setattr(ts, "_ORIG_GETADDRINFO", lambda h, p, *a, **k: _ai("10.0.0.5"))
    client = MagicMock()
    assert ts.guarded_get(client, "https://evil.test") is None
    client.get.assert_not_called()


def test_guarded_get_pins_for_the_request(monkeypatch):
    monkeypatch.setattr(ts, "_ORIG_GETADDRINFO", lambda h, p, *a, **k: _ai("93.184.216.34"))
    captured = {}

    def fake_get(url, **kwargs):
        captured["pin"] = dict(getattr(ts._PIN, "map", {}))
        return _FakeResp()

    client = MagicMock()
    client.get.side_effect = fake_get

    resp = ts.guarded_get(client, "https://example.com")
    assert resp.status_code == 200
    # The validated IP was pinned while the request was in flight...
    assert captured["pin"].get("example.com") == ["93.184.216.34"]
    # ...and cleared afterwards.
    assert "example.com" not in getattr(ts._PIN, "map", {})
