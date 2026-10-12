"""Guards for the LLM relevance pass over the dossier's key-contacts (Phase D).

The 2026-06-10 silent-drop bug came from an unguarded re-rank
that emptied the people list. These tests fix the contract of
`_apply_relevance`: re-rank + annotate on success, and NEVER lose the
deterministic list on a misfire or an engine error.
"""
from unittest.mock import patch

from app.services import key_contacts as K


def _contacts():
    return [
        {"name": "A", "title": "IT Director", "icp_score": 0.7},
        {"name": "B", "title": "SecOps Manager", "icp_score": 0.7},
        {"name": "C", "title": "Head of Internal Audit", "icp_score": 0.7},
    ]


def test_reranks_annotates_and_drops_non_buyers(mock_db):
    verdicts = {
        "IT Director": {"score": 0.80, "relevant": True, "reason": "owns security"},
        "SecOps Manager": {"score": 0.90, "relevant": True, "reason": "runs the SOC"},
        "Head of Internal Audit": {"score": 0.20, "relevant": False, "reason": "audit, not buyer"},
    }
    with patch.object(K, "build_relevance_context", return_value={
        "archetype": "mdr / soc", "icp_titles": ["CISO"], "sells_what": "MDR",
    }), patch.object(K, "judge_titles", return_value=verdicts):
        out = K._apply_relevance(mock_db, "org1", _contacts())

    titles = [c["title"] for c in out]
    assert titles == ["SecOps Manager", "IT Director"]      # dropped audit, sorted by fit
    assert out[0]["icp_score"] == 0.90                       # LLM score overrode the flat 0.7
    assert out[0]["relevance_reason"] == "runs the SOC"      # reason surfaced for the dossier


def test_empty_guard_keeps_deterministic_list_if_judge_drops_all(mock_db):
    verdicts = {c["title"]: {"score": 0.1, "relevant": False, "reason": "x"} for c in _contacts()}
    with patch.object(K, "build_relevance_context", return_value={
        "archetype": "mdr / soc", "icp_titles": ["CISO"], "sells_what": "MDR",
    }), patch.object(K, "judge_titles", return_value=verdicts):
        out = K._apply_relevance(mock_db, "org1", _contacts())
    assert len(out) == 3  # misfire that empties everyone → fall back, never surface []


def test_judge_error_falls_back_to_input(mock_db):
    with patch.object(K, "build_relevance_context", side_effect=RuntimeError("engine down")):
        original = _contacts()
        out = K._apply_relevance(mock_db, "org1", original)
    assert out is original  # engine hiccup never breaks discovery
