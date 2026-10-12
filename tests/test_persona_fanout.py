"""Per-persona Apollo fan-out — the precision fix.

Locks the behaviour that replaced the single union search: one Apollo
search per active buyer profile, deduped across profiles, each lead
attributed to the profile whose query found it. Legacy flat configs
(no personas array) must still run exactly one union search.
"""

from unittest.mock import patch

from app.services import discovery
from app.services.discovery import (
    _active_personas_from_icp,
    _dedupe_key,
    run_persona_fanout,
    synthesise_union_persona,
)
from app.models.schemas import LeadInput


def _person(pid, title="CISO", domain="co.com"):
    return {
        "id": pid,
        "first_name": "A",
        "last_name": "B",
        "title": title,
        "linkedin_url": f"https://linkedin.com/in/{pid}",
        "organization": {"name": "Co", "primary_domain": domain},
    }


def _persona(pid, name, **fields):
    p = {"id": pid, "name": name, "active": True}
    p.update(fields)
    return p


# ---------------------------------------------------------------------------
# Union fallback — legacy flat configs must behave exactly as before
# ---------------------------------------------------------------------------

class TestUnionFallback:
    def test_synthesise_union_persona_from_flat_fields(self):
        icp = {"target_titles": ["CISO"], "target_industries": ["Fintech"]}
        u = synthesise_union_persona(icp)
        assert u["id"] == "__union__"
        assert u["target_titles"] == ["CISO"]
        assert u["target_industries"] == ["Fintech"]

    def test_no_personas_array_falls_back_to_single_union(self):
        icp = {"target_titles": ["CISO"]}
        personas = _active_personas_from_icp(icp)
        assert len(personas) == 1
        assert personas[0]["id"] == "__union__"

    def test_all_personas_inactive_falls_back_to_union(self):
        icp = {
            "target_titles": ["CISO"],
            "personas": [_persona("p1", "A", active=False, target_titles=["CTO"])],
        }
        personas = _active_personas_from_icp(icp)
        assert len(personas) == 1 and personas[0]["id"] == "__union__"

    def test_union_path_runs_exactly_one_search(self):
        icp = {"target_titles": ["CISO"]}
        with patch.object(discovery, "_apollo_search",
                          return_value=[_person("a1")]) as mock:
            res = run_persona_fanout(icp)
        assert mock.call_count == 1
        assert res["count"] == 1


# ---------------------------------------------------------------------------
# Persona selection + dedupe identity
# ---------------------------------------------------------------------------

class TestPersonaSelection:
    def test_only_active_personas_used(self):
        icp = {"personas": [
            _persona("p1", "Active", target_titles=["CISO"]),
            _persona("p2", "Muted", active=False, target_titles=["CTO"]),
        ]}
        active = _active_personas_from_icp(icp)
        assert [p["id"] for p in active] == ["p1"]

    def test_dedupe_key_prefers_apollo_id_then_linkedin_then_email(self):
        assert _dedupe_key(LeadInput(email="x@x.com", apollo_id="A1")) == "apollo:A1"
        assert _dedupe_key(LeadInput(
            email="x@x.com", linkedin_url="https://LI/Alex")) == "li:https://li/alex"
        assert _dedupe_key(LeadInput(email="X@X.com")) == "email:x@x.com"


# ---------------------------------------------------------------------------
# Fan-out core behaviour
# ---------------------------------------------------------------------------

class TestFanout:
    def test_one_search_per_active_persona(self):
        icp = {"personas": [
            _persona("p1", "CISOs", target_titles=["CISO"]),
            _persona("p2", "CTOs", target_titles=["CTO"]),
        ]}
        with patch.object(discovery, "_apollo_search",
                          side_effect=[[_person("a1")], [_person("a2")]]) as mock:
            res = run_persona_fanout(icp)
        assert mock.call_count == 2
        assert res["count"] == 2
        assert res["per_persona"] == {"CISOs": 1, "CTOs": 1}

    def test_each_lead_tagged_with_sourcing_persona(self):
        icp = {"personas": [_persona("p1", "CISOs", target_titles=["CISO"])]}
        with patch.object(discovery, "_apollo_search",
                          return_value=[_person("a1")]):
            res = run_persona_fanout(icp)
        lead = res["leads"][0]
        assert lead.sourced_persona_id == "p1"
        assert lead.sourced_persona_name == "CISOs"

    def test_cross_persona_dedupe_attributes_to_first(self):
        # Same person surfaced by both profiles → counted once, attributed
        # to the profile that found them first.
        icp = {"personas": [
            _persona("p1", "First", target_titles=["CISO"]),
            _persona("p2", "Second", target_titles=["Head of Security"]),
        ]}
        with patch.object(discovery, "_apollo_search", side_effect=[
            [_person("dup")],
            [_person("dup"), _person("uniq")],
        ]):
            res = run_persona_fanout(icp)
        assert res["count"] == 2  # dup once + uniq
        by_id = {l.apollo_id: l for l in res["leads"]}
        assert by_id["dup"].sourced_persona_name == "First"
        assert res["per_persona"] == {"First": 1, "Second": 1}

    def test_empty_persona_is_skipped_not_searched(self):
        # A profile with no filters would pull Apollo's firehose — must skip.
        icp = {"personas": [
            _persona("p1", "Real", target_titles=["CISO"]),
            _persona("p2", "Empty"),
        ]}
        with patch.object(discovery, "_apollo_search",
                          return_value=[_person("a1")]) as mock:
            res = run_persona_fanout(icp)
        assert mock.call_count == 1  # only the filtered profile searched
        assert res["per_persona"]["Empty"] == 0

    def test_budget_split_across_personas_with_floor(self):
        # 25 budget / 3 personas = ceil 9, floored to MIN_PER_PERSONA (10).
        icp = {"personas": [
            _persona(f"p{i}", f"N{i}", target_titles=["CISO"]) for i in range(3)
        ]}
        captured = []
        with patch.object(discovery, "_apollo_search",
                          side_effect=lambda payload: captured.append(payload) or []):
            run_persona_fanout(icp, total_budget=25)
        assert all(pl["per_page"] == 10 for pl in captured)
        assert len(captured) == 3

    def test_large_budget_splits_evenly(self):
        icp = {"personas": [
            _persona("p1", "A", target_titles=["CISO"]),
            _persona("p2", "B", target_titles=["CTO"]),
        ]}
        captured = []
        with patch.object(discovery, "_apollo_search",
                          side_effect=lambda payload: captured.append(payload) or []):
            run_persona_fanout(icp, total_budget=60)  # 60/2 = 30 each
        assert all(pl["per_page"] == 30 for pl in captured)


# ---------------------------------------------------------------------------
# _run_apollo_search_for_icp now delegates to the fan-out
# ---------------------------------------------------------------------------

class TestIcpSearchDelegates:
    def test_returns_count_and_leads_shape(self):
        from app.routers.leads import _run_apollo_search_for_icp
        icp = {"personas": [_persona("p1", "CISOs", target_titles=["CISO"])]}
        with patch.object(discovery, "_apollo_search",
                          return_value=[_person("a1")]):
            out = _run_apollo_search_for_icp(icp)
        assert set(out) == {"count", "leads"}
        assert out["count"] == 1
        assert out["leads"][0].source == "vera_brain_apollo"
        assert out["leads"][0].sourced_persona_id == "p1"
