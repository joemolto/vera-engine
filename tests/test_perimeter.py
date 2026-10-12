"""Perimeter fingerprinting must never invent a product or a version.

These tests exist because the first cut of this collector reproduced the bug
that killed cve_in_stack: it reported "F5 BIG-IP APM v10.0" on a host with no
F5 on it, borrowing the version from Exchange's IIS Server header and
"corroborating" the product with a redirect that merely echoed the path we
asked for.
"""

from unittest.mock import patch

import pytest

from app.services.perimeter import (
    BANNER_PATHS,
    PerimeterFinding,
    PRODUCT_VERSION_HEADERS,
    _probe_host,
    scan_perimeter,
)


class FakeResponse:
    def __init__(self, status_code=200, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


def _responder(mapping, root_headers=None):
    """Return a fake httpx.get driven by a {path: FakeResponse} map."""
    def _get(url, **kwargs):
        path = url.split("host", 1)[-1]
        for p, resp in mapping.items():
            if url.endswith(p):
                return resp
        if url.count("/") == 2:  # https://host
            return FakeResponse(headers=root_headers or {})
        return FakeResponse(status_code=404)
    return _get


class TestVersionAttribution:
    def test_generic_server_header_is_not_a_product_version_source(self):
        """The Server header belongs to the web server, not to whatever sits
        behind it. Borrowing it invented 'F5 BIG-IP APM v10.0' from IIS."""
        assert "server" not in PRODUCT_VERSION_HEADERS

    def test_banner_product_gets_no_version_without_its_own_header(self):
        responses = {
            "/my.policy": FakeResponse(
                status_code=200,
                headers={"server": "Microsoft-IIS/10.0",
                         "set-cookie": "BIGipServerpool=123"},
                text="F5 Networks login",
            ),
        }
        with patch("app.services.perimeter.httpx.get",
                   side_effect=_responder(responses, {"server": "Microsoft-IIS/10.0"})):
            out = _probe_host("host", "1.2.3.4")
        f5 = [f for f in out if "F5" in f.product]
        assert f5, "F5 should be detected via its cookie"
        assert f5[0].version is None, "must not borrow IIS's 10.0"
        assert f5[0].is_versioned is False


class TestCorroboration:
    def test_echoed_request_path_does_not_corroborate(self):
        """Exchange redirecting /my.policy -> /owa/my.policy must NOT be read
        as an F5 detection. This was a real false positive seen in testing."""
        responses = {
            "/my.policy": FakeResponse(
                status_code=302, headers={"location": "/owa/my.policy"},
            ),
        }
        with patch("app.services.perimeter.httpx.get",
                   side_effect=_responder(responses)):
            out = _probe_host("host", "1.2.3.4")
        assert not [f for f in out if "F5" in f.product]

    def test_product_specific_redirect_does_corroborate(self):
        responses = {
            "/owa/": FakeResponse(
                status_code=302,
                headers={"location": "https://host/owa/auth/logon.aspx",
                         "x-owa-version": "15.1.2507.61"},
            ),
        }
        with patch("app.services.perimeter.httpx.get",
                   side_effect=_responder(responses)):
            out = _probe_host("host", "1.2.3.4")
        ex = [f for f in out if f.product == "Microsoft Exchange"]
        assert ex and ex[0].version == "15.1.2507.61"
        assert ex[0].cpe == "cpe:2.3:a:microsoft:exchange_server:15.1.2507.61"

    def test_bare_redirect_with_no_marker_is_ignored(self):
        responses = {
            p: FakeResponse(status_code=302, headers={"location": "/home"})
            for p in BANNER_PATHS
        }
        with patch("app.services.perimeter.httpx.get",
                   side_effect=_responder(responses)):
            out = _probe_host("host", "1.2.3.4")
        assert out == []

    def test_unexpected_status_is_ignored(self):
        responses = {p: FakeResponse(status_code=500) for p in BANNER_PATHS}
        with patch("app.services.perimeter.httpx.get",
                   side_effect=_responder(responses)):
            out = _probe_host("host", "1.2.3.4")
        assert out == []


class TestMarkerHygiene:
    def test_no_marker_equals_its_own_request_path(self):
        """A marker that appears in the path we request can be echoed back by
        any redirect, making it meaningless as evidence."""
        for path, (_product, _v, _p, markers) in BANNER_PATHS.items():
            stripped = path.strip("/+").lower()
            for m in markers:
                assert m != stripped, (
                    f"marker {m!r} for {path} is the request path itself"
                )


class TestScanBehaviour:
    def test_empty_domain_returns_nothing(self):
        assert scan_perimeter("") == []
        assert scan_perimeter(None) == []

    def test_no_resolving_hosts_returns_nothing(self):
        with patch("app.services.perimeter._resolve", return_value=None):
            assert scan_perimeter("nothing-here.example") == []

    def test_findings_dedupe_across_hostnames(self):
        finding = PerimeterFinding(
            host="a.example", ip="1.2.3.4", product="Microsoft Exchange",
            version="15.1", cpe="cpe:2.3:a:microsoft:exchange_server:15.1",
            evidence="test",
        )
        dupe = PerimeterFinding(
            host="b.example", ip="5.6.7.8", product="Microsoft Exchange",
            version="15.1", cpe="cpe:2.3:a:microsoft:exchange_server:15.1",
            evidence="test",
        )
        with patch("app.services.perimeter._resolve", return_value="1.2.3.4"), \
             patch("app.services.perimeter._probe_host",
                   side_effect=lambda h, i: [finding, dupe]):
            out = scan_perimeter("example.com", hostnames=("vpn", "mail"))
        assert len(out) == 1


class TestFindingContract:
    @pytest.mark.parametrize("version,expected", [("15.1.2507.61", True), (None, False)])
    def test_is_versioned_gates_cve_claims(self, version, expected):
        f = PerimeterFinding("h", "1.1.1.1", "P", version, "cpe", "e")
        assert f.is_versioned is expected
