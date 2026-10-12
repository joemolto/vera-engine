"""Geo policy — the deterministic gate that stops US incidents reaching a
UK-only client. Conservative by design: only drops a clearly-foreign
localized incident; global CVEs and unmarked threats always pass.
"""

from app.services.geo_policy import (
    geo_excluded, is_global_threat, target_countries,
)

UK = {"target_locations": ["United Kingdom"]}
GLOBAL = {"target_locations": ["Global"]}
NONE = {}


class TestTargetCountries:
    def test_uk_aliases_normalise(self):
        assert target_countries({"target_locations": ["UK"]}) == {"united kingdom"}
        assert target_countries({"target_locations": ["United Kingdom"]}) == {"united kingdom"}
        assert target_countries({"target_locations": ["England"]}) == {"united kingdom"}

    def test_global_or_empty_is_unrestricted(self):
        assert target_countries(GLOBAL) == set()
        assert target_countries(NONE) == set()
        assert target_countries({"target_locations": []}) == set()


class TestGeoGate:
    def _ex(self, text, icp=UK, glob=False):
        return geo_excluded(text=text, icp_config=icp, is_global_threat=glob)[0]

    def test_us_incident_excluded_for_uk_client(self):
        assert self._ex("U.S. bank discloses security lapse after AI app leak")
        assert self._ex("New York insurer Northwind Life discloses data breach")

    def test_uk_incident_kept_for_uk_client(self):
        assert not self._ex("UK fintech hit by ransomware; FCA opens review")
        assert not self._ex("London-based bank breach reported by NCSC")

    def test_global_cve_never_geo_excluded(self):
        # Even though it says "U.S.", a global technical threat passes.
        assert not self._ex(
            "Critical CVE in Apache Struts exploited; U.S. CISA warns",
            glob=True,
        )

    def test_unrestricted_client_never_excluded(self):
        assert not self._ex("U.S. bank breach", icp=GLOBAL)
        assert not self._ex("U.S. bank breach", icp=NONE)

    def test_no_country_markers_kept(self):
        # Generic threat with no geo signal — keep (benefit of the doubt).
        assert not self._ex("Ransomware group targets healthcare providers")

    def test_target_country_present_keeps_even_if_foreign_also(self):
        # Mentions both UK and US — client targets UK, so keep.
        assert not self._ex(
            "US and UK banks warned over shared supply-chain breach"
        )

    def test_returns_reason(self):
        excluded, reason = geo_excluded(
            text="New York data breach", icp_config=UK,
        )
        assert excluded and "united states" in reason.lower()


class TestIsGlobalThreat:
    def test_cisa_kev_source_is_global(self):
        assert is_global_threat({"source": "cisa_kev"}, {})

    def test_vulnerability_type_is_global(self):
        assert is_global_threat({}, {"threat_type": "vulnerability"})
        assert is_global_threat({}, {"is_actively_exploited": True})

    def test_cve_in_title_is_global(self):
        assert is_global_threat({"title": "CVE-2026-1234 in nginx"}, {})

    def test_news_incident_is_not_global(self):
        assert not is_global_threat(
            {"source": "tavily_news", "title": "Bank breach"},
            {"threat_type": "breach"},
        )
