"""Sector-relevance gate (stops EdTech rationalised into a SaaS client)
+ the shared fear-led detector used by scorer & threat surface.
Conservative: only drops a clearly off-sector threat the client isn't in.
"""

from app.services.relevance_policy import sector_excluded
from app.services.signal_payload import is_fear_led

TP = {"target_industries": ["fintech", "financial services", "saas",
                             "healthtech", "cybersecurity"]}
NONE = {}

EDTECH = ("Back-to-Back Ransomware Attacks on Ed Tech Firm Attract "
          "Scrutiny, schools hit")
FINTECH = ("UK fintech payments provider discloses breach affecting "
           "financial services customers")
GENERIC = "Ransomware group shifts tactics, security teams on alert"


class TestSectorGate:
    def _ex(self, text, icp=TP, glob=False):
        return sector_excluded(text=text, icp_config=icp,
                               is_global_threat=glob)[0]

    def test_edtech_excluded_for_fintech_saas_client(self):
        assert self._ex(EDTECH)

    def test_threat_in_target_industry_kept(self):
        assert not self._ex(FINTECH)            # mentions fintech/financial

    def test_no_off_sector_marker_kept(self):
        assert not self._ex(GENERIC)            # ambiguous → keep

    def test_global_cve_never_sector_excluded(self):
        assert not self._ex(EDTECH, glob=True)  # CVE/KEV are sector-agnostic

    def test_no_target_industries_configured_kept(self):
        assert not self._ex(EDTECH, icp=NONE)

    def test_returns_reason(self):
        excluded, reason = sector_excluded(
            text=EDTECH, icp_config=TP, is_global_threat=False)
        assert excluded and "education" in reason.lower()


class TestIsFearLed:
    def test_fear_openers_detected(self):
        assert is_fear_led("Ransomware attack cripples the firm this week.")
        assert is_fear_led("A critical CVE-2026-1 is being actively exploited.")
        assert is_fear_led("DDoS incident: a live security crisis unfolds.")

    def test_non_fear_not_flagged(self):
        assert not is_fear_led("Fabrikam raised a funding round and is hiring.")
        assert not is_fear_led("")
        assert not is_fear_led("Company launched a new product line in EMEA.")

    def test_fear_only_as_trailing_context_not_flagged(self):
        # Leads with opportunity; breach mentioned later = allowed.
        assert not is_fear_led(
            "Contoso closed a $40M round. Separately, a sector breach was noted."
        )
