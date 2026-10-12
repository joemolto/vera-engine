"""CVE matching must refuse to guess.

NVD indexes Exchange as `2016:cumulative_update_23`, not as the build number
the server reports (`15.1.2507.61`). Querying NVD with the raw build returns
1999-era CVEs — silently wrong rather than empty. So an unmapped build must
produce no claim at all.
"""

from unittest.mock import patch

from app.services.cve_match import (
    EXCHANGE_BUILDS,
    NVD_ATTRIBUTION,
    NORMALISERS,
    build_cpe,
    lookup_cves,
    match_findings,
)
from app.services.perimeter import PerimeterFinding


def _finding(product="Microsoft Exchange", version="15.1.2507.61"):
    return PerimeterFinding(
        host="mail.example.com", ip="1.2.3.4", product=product,
        version=version, cpe="x", evidence="test",
    )


class TestCpeBuilding:
    def test_known_build_maps_to_nvd_vocabulary(self):
        cpe = build_cpe("Microsoft Exchange", "15.1.2507.61")
        assert cpe == (
            "cpe:2.3:a:microsoft:exchange_server:2016:"
            "cumulative_update_23:*:*:*:*:*:*"
        )

    def test_unmapped_build_makes_no_claim(self):
        """An unknown CU is dropped, never approximated to a nearby one."""
        assert build_cpe("Microsoft Exchange", "15.1.9999.1") is None

    def test_product_without_normaliser_makes_no_claim(self):
        assert build_cpe("Cisco ASA WebVPN", "9.16.1") is None

    def test_missing_version_makes_no_claim(self):
        assert build_cpe("Microsoft Exchange", None) is None
        assert build_cpe("Microsoft Exchange", "") is None

    def test_malformed_version_makes_no_claim(self):
        assert build_cpe("Microsoft Exchange", "not-a-version") is None

    def test_every_normaliser_product_is_a_real_perimeter_product(self):
        """A normaliser for a product the collector cannot emit is dead code
        that looks like coverage. Server-header products are parsed
        dynamically, so they are declared in SERVER_HEADER_PRODUCTS."""
        from app.services.perimeter import (
            BANNER_PATHS, HEADER_PRODUCTS, SERVER_HEADER_PRODUCTS,
        )
        known = {v[0] for v in BANNER_PATHS.values()}
        known |= {v[0] for v in HEADER_PRODUCTS.values()}
        known |= SERVER_HEADER_PRODUCTS
        for product in NORMALISERS:
            assert product in known, f"{product} is not produced by the collector"


class TestPlainVersionProducts:
    """Most products are indexed by their own version string. Exchange is the
    exception that needs a build table, not the rule."""

    def test_nginx_maps_to_f5_not_nginx_vendor(self):
        """NVD moved nginx under F5. cpe:2.3:a:nginx:nginx:1.14.0 returns 0
        CVEs; cpe:2.3:a:f5:nginx:1.14.0 returns 11."""
        assert build_cpe("nginx", "1.14.0") == "cpe:2.3:a:f5:nginx:1.14.0:*:*:*:*:*:*:*"

    def test_apache_maps_to_http_server(self):
        assert build_cpe("Apache", "2.2.22") == (
            "cpe:2.3:a:apache:http_server:2.2.22:*:*:*:*:*:*:*"
        )

    def test_trailing_build_noise_is_trimmed(self):
        assert build_cpe("nginx", "1.24.0 (Ubuntu)").startswith(
            "cpe:2.3:a:f5:nginx:1.24.0:"
        )

    def test_non_numeric_version_makes_no_claim(self):
        assert build_cpe("nginx", "stable") is None


class TestIisDeliberatelyExcluded:
    def test_iis_makes_no_claim(self):
        """All 12 companies found running IIS are on 10.0, which ships with
        every supported Windows Server. A CVE claim would fire on all of them
        and separate none — no discriminating power, so no signal."""
        assert build_cpe("Microsoft-IIS", "10.0") is None

    def test_iis_is_not_in_the_normaliser_map(self):
        assert not any("IIS" in p for p in NORMALISERS)


class TestBuildTableHygiene:
    def test_builds_are_three_part_prefixes(self):
        """Keys must be major.minor.build — the fourth segment is the patch
        revision and varies per host, so including it would never match."""
        for key in EXCHANGE_BUILDS:
            assert key.count(".") == 2, f"{key} should be major.minor.build"

    def test_no_duplicate_cu_designations(self):
        values = list(EXCHANGE_BUILDS.values())
        assert len(values) == len(set(values)), "two builds map to one CU"


class TestMatchFindings:
    def test_unversioned_findings_are_skipped_entirely(self):
        with patch("app.services.cve_match.lookup_cves") as m:
            out = match_findings([_finding(version=None)])
        assert out == {}
        m.assert_not_called()

    def test_unmappable_product_never_hits_the_api(self):
        with patch("app.services.cve_match.lookup_cves") as m:
            out = match_findings([_finding(product="Citrix Gateway", version="13.0")])
        assert out == {}
        m.assert_not_called()

    def test_mapped_finding_is_looked_up(self):
        with patch("app.services.cve_match.lookup_cves", return_value=["cve"]) as m:
            out = match_findings([_finding()])
        assert len(out) == 1
        m.assert_called_once()


class TestLookupResilience:
    def test_non_200_returns_empty_not_exception(self):
        class R:
            status_code = 503
        with patch("app.services.cve_match.httpx.get", return_value=R()):
            assert lookup_cves("cpe:x") == []

    def test_network_error_returns_empty(self):
        with patch("app.services.cve_match.httpx.get", side_effect=OSError("boom")):
            assert lookup_cves("cpe:x") == []


class TestAttribution:
    def test_nvd_attribution_string_is_exact(self):
        """NVD terms mandate this wording wherever CVE data is surfaced."""
        assert NVD_ATTRIBUTION == (
            "This product uses the NVD API but is not endorsed or "
            "certified by the NVD."
        )
