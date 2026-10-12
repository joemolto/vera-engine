"""Relevance service regression guards — the CI safety net for ICP title
matching, so a matching regression fails the build, not a customer demo.

Covers the deterministic layer (pure, no LLM) and asserts the LLM layer is
only invoked for genuinely ambiguous titles (hard-negatives are dropped free).
"""
from unittest.mock import patch

from app.services import relevance as R


# --- Hard-negative pre-filter: obvious non-buyers, dropped free ------------
def test_hard_negatives_are_recognized():
    for t in [
        "Social Media Manager", "Social Media and Comms", "VP of Sales",
        "Head of Marketing", "HR Business Partner", "Technical Recruiter",
        "Customer Success Manager", "Content Designer", "SDR", "Brand Manager",
        "Communications Lead", "Talent Acquisition Partner",
    ]:
        assert R.is_hard_negative(t), f"{t!r} should be a hard negative"


def test_security_and_it_roles_survive_the_prefilter():
    for t in [
        "SecOps Manager", "Chief Information Security Officer", "CISO",
        "IT Director", "CTO", "Head of Security", "SOC Manager",
        "Head of Information Security", "VP Engineering", "Head of GRC",
    ]:
        assert not R.is_hard_negative(t), f"{t!r} must not be dropped"


def test_soc_does_not_false_match_social():
    """The classic bug: 'SOC' (a security term) must not fire on 'SOCial'."""
    assert R.is_hard_negative("Social Media Manager")   # caught by 'social media'
    assert not R.is_hard_negative("SOC Analyst")        # a real SOC role survives
    assert not R.is_hard_negative("Head of SOC")


def test_normalize_title():
    assert (
        R.normalize_title("  Chief Information Security Officer (CISO)! ")
        == "chief information security officer ciso"
    )


# --- judge_titles orchestration -------------------------------------------
def test_pure_negatives_never_call_the_llm(mock_db):
    with patch("app.services.relevance.call_claude") as mc:
        v = R.judge_titles(
            mock_db, archetype="mdr / soc", icp_titles=["CISO"],
            sells_what="MDR", titles=["Social Media Manager", "VP of Sales"],
        )
    mc.assert_not_called()
    assert v["Social Media Manager"]["relevant"] is False
    assert v["VP of Sales"]["relevant"] is False


def test_llm_judges_only_the_ambiguous_survivors(mock_db):
    def fake_claude(*, prompt, **kwargs):
        # verdicts index into the (sorted, normalized) non-negative titles
        return {"verdicts": [
            {"i": 0, "score": 0.9, "relevant": True, "reason": "security buyer"},
            {"i": 1, "score": 0.8, "relevant": True, "reason": "security leader"},
        ]}

    with patch("app.services.relevance.call_claude", side_effect=fake_claude) as mc:
        v = R.judge_titles(
            mock_db, archetype="mdr / soc", icp_titles=["CISO"], sells_what="MDR",
            titles=["Social Media Manager", "SecOps Manager", "CISO"],
        )
    assert mc.call_count == 1  # one batched call for the two survivors
    assert v["Social Media Manager"]["relevant"] is False  # deterministic drop
    assert v["SecOps Manager"]["relevant"] is True
    assert v["CISO"]["relevant"] is True
