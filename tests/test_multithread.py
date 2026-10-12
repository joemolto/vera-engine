"""Tests for multithreading role-family logic — exclusion + adjacency."""

import pytest


class TestRoleFamilyDetection:
    """detect_role_family() maps free-text titles to a canonical family."""

    @pytest.mark.parametrize("title,expected", [
        # CISO variants
        ("CISO", "ciso"),
        ("Chief Information Security Officer", "ciso"),
        ("Chief Security Officer", "ciso"),
        # CTO variants — including the parenthetical form Apollo returns
        ("CTO", "cto"),
        ("Chief Technology Officer", "cto"),
        ("Chief Technology Officer (Engineering)", "cto"),
        ("Chief Technical Officer", "cto"),
        # CIO
        ("CIO", "cio"),
        ("Chief Information Officer", "cio"),
        # CEO + founder (separate families — order in ROLE_FAMILY_PATTERNS matters)
        ("CEO", "ceo"),
        ("Chief Executive Officer", "ceo"),
        # Founder family — match before checking CEO patterns? Actually CEO comes
        # first in patterns dict so "CEO & Co-Founder" hits CEO. Acceptable —
        # CEO is the dominant role for that title. Founder-only titles still
        # detect.
        ("Co-Founder", "founder"),
        ("Co-Founder & Chief Commercial Officer", "founder"),  # founder pattern matches first since pattern dict iteration
        ("Founder", "founder"),
        # CFO + finance director
        ("CFO", "cfo"),
        ("Chief Financial Officer", "cfo"),
        # COO
        ("COO", "coo"),
        ("Chief Operating Officer", "coo"),
        # Heads
        ("Head of Cyber Security", "head_of_cyber"),
        ("Head of Information Security", "head_of_security"),
        ("Head of Security", "head_of_security"),
        ("VP Security", "head_of_security"),
        ("Director of Information Security", "head_of_security"),
        ("Head of SOC", "head_of_soc"),
        ("Head of Risk", "head_of_risk"),
        ("Chief Risk Officer", "head_of_risk"),
        ("Head of Compliance", "head_of_compliance"),
        ("IT Director", "head_of_it"),
        ("Head of IT", "head_of_it"),
        ("Head of Engineering", "head_of_engineering"),
        ("VP Engineering", "head_of_engineering"),
        ("Head of Sales", "head_of_sales"),
        ("VP Sales", "head_of_sales"),
        # Unknown / not matched
        ("Software Engineer", None),
        ("Marketing Executive", None),
        ("SOC Analyst", None),
        ("", None),
    ])
    def test_title_to_family(self, title: str, expected):
        from app.services.multithread import detect_role_family
        assert detect_role_family(title) == expected

    def test_handles_non_string_input(self):
        from app.services.multithread import detect_role_family
        assert detect_role_family(None) is None
        assert detect_role_family(123) is None  # type: ignore[arg-type]


class TestAdjacencyMap:
    """ADJACENT_ROLE_FAMILIES encodes who's a 'dotted-line' buyer for each
    role. Sanity-check the cyber-buyer relationships."""

    def test_ciso_adjacency_includes_expected_dotted_line_roles(self):
        from app.services.multithread import ADJACENT_ROLE_FAMILIES
        ciso_adj = set(ADJACENT_ROLE_FAMILIES["ciso"])
        # CISO's natural buying committee
        assert "cio" in ciso_adj
        assert "cto" in ciso_adj
        assert "cfo" in ciso_adj  # signs the budget
        assert "head_of_cyber" in ciso_adj
        assert "head_of_security" in ciso_adj
        assert "head_of_soc" in ciso_adj
        assert "head_of_risk" in ciso_adj
        assert "head_of_compliance" in ciso_adj
        assert "head_of_it" in ciso_adj
        # Should NOT contain CISO itself (we exclude same family elsewhere)
        assert "ciso" not in ciso_adj

    def test_ceo_adjacency_includes_co_founders_and_cfo(self):
        from app.services.multithread import ADJACENT_ROLE_FAMILIES
        ceo_adj = set(ADJACENT_ROLE_FAMILIES["ceo"])
        assert "founder" in ceo_adj
        assert "coo" in ceo_adj
        assert "cfo" in ceo_adj


class TestSameRoleFamilyExclusion:
    """End-to-end: when source is a CISO, candidates with CISO titles must
    not appear in the results. Mocks the Apollo HTTP layer."""

    def test_ciso_source_excludes_other_cisos(self, monkeypatch):
        from app.services import multithread
        from app.config import settings

        monkeypatch.setattr(settings, "APOLLO_API_KEY", "test_key")

        # Mock Apollo response: one CISO + several adjacent decision-makers
        fake_apollo_response = {
            "people": [
                {"id": "1", "first_name": "Alice", "last_name": "Smith", "title": "CISO",
                 "linkedin_url": "x", "organization": {"name": "Bank", "primary_domain": "bank.com"}},
                {"id": "2", "first_name": "Bob", "last_name": "Jones", "title": "CTO",
                 "linkedin_url": "x", "organization": {"name": "Bank", "primary_domain": "bank.com"}},
                {"id": "3", "first_name": "Carol", "last_name": "Khan", "title": "Head of SOC",
                 "linkedin_url": "x", "organization": {"name": "Bank", "primary_domain": "bank.com"}},
                {"id": "4", "first_name": "Dan", "last_name": "Lee",
                 "title": "Chief Information Security Officer",
                 "linkedin_url": "x", "organization": {"name": "Bank", "primary_domain": "bank.com"}},
            ]
        }

        # Mock httpx.Client so no real Apollo call is made
        class FakeResponse:
            status_code = 200
            is_success = True
            def json(self):
                return fake_apollo_response

        class FakeClient:
            def __init__(self, *a, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def post(self, *a, **kw): return FakeResponse()

        monkeypatch.setattr(multithread.httpx, "Client", FakeClient)

        # Mock the existing-leads-at-domain DB lookup
        class FakeDB:
            def table(self, name):
                return self
            def select(self, *a, **kw):
                return self
            def eq(self, *a, **kw):
                return self
            def limit(self, *a, **kw):
                return self
            def execute(self):
                return type("R", (), {"data": []})()

        candidates = multithread.find_others_at_company(
            db=FakeDB(),
            org_id="any",
            company_domain="bank.com",
            company_name="Bank",
            source_lead_title="Chief Information Security Officer",
        )

        titles = [c["title"] for c in candidates]
        # Both CISOs (Alice + Dan) must be excluded
        assert "CISO" not in titles
        assert "Chief Information Security Officer" not in titles
        # Adjacent decision-makers should remain
        assert "CTO" in titles
        assert "Head of SOC" in titles

    def test_adjacent_candidates_rank_first(self, monkeypatch):
        """For a CISO source, CTO and Head of SOC should appear before
        unrelated senior roles like Head of Sales."""
        from app.services import multithread
        from app.config import settings

        monkeypatch.setattr(settings, "APOLLO_API_KEY", "test_key")

        fake_apollo_response = {
            "people": [
                # Apollo returns Sales first (unrelated to CISO)
                {"id": "1", "first_name": "S", "last_name": "S", "title": "Head of Sales",
                 "linkedin_url": "x", "organization": {"primary_domain": "bank.com"}},
                # Then the CTO (adjacent to CISO)
                {"id": "2", "first_name": "C", "last_name": "C", "title": "CTO",
                 "linkedin_url": "x", "organization": {"primary_domain": "bank.com"}},
                # Then Head of SOC (adjacent to CISO)
                {"id": "3", "first_name": "Q", "last_name": "Q", "title": "Head of SOC",
                 "linkedin_url": "x", "organization": {"primary_domain": "bank.com"}},
            ]
        }

        class FakeResponse:
            status_code = 200
            is_success = True
            def json(self): return fake_apollo_response

        class FakeClient:
            def __init__(self, *a, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def post(self, *a, **kw): return FakeResponse()

        monkeypatch.setattr(multithread.httpx, "Client", FakeClient)

        class FakeDB:
            def table(self, name): return self
            def select(self, *a, **kw): return self
            def eq(self, *a, **kw): return self
            def limit(self, *a, **kw): return self
            def execute(self):
                return type("R", (), {"data": []})()

        candidates = multithread.find_others_at_company(
            db=FakeDB(),
            org_id="any",
            company_domain="bank.com",
            company_name="Bank",
            source_lead_title="CISO",
        )

        titles_in_order = [c["title"] for c in candidates]
        # CTO + Head of SOC (adjacent to CISO) should rank above Head of Sales
        # (unrelated senior person)
        assert titles_in_order.index("CTO") < titles_in_order.index("Head of Sales")
        assert titles_in_order.index("Head of SOC") < titles_in_order.index("Head of Sales")


def _fake_db(icp_config=None):
    """Table-aware fake Supabase client: empty leads, optional org icp_config."""
    class FakeDB:
        def __init__(self):
            self._table = None
        def table(self, name):
            self._table = name
            return self
        def select(self, *a, **kw): return self
        def eq(self, *a, **kw): return self
        def limit(self, *a, **kw): return self
        def execute(self):
            if self._table == "organisations" and icp_config is not None:
                return type("R", (), {"data": [{"icp_config": icp_config}]})()
            return type("R", (), {"data": []})()
    return FakeDB()


def _fake_client_factory(responses_by_call):
    """FakeClient recording each POST payload; returns queued responses."""
    payloads = []

    class FakeResponse:
        def __init__(self, people):
            self.people = people
        status_code = 200
        is_success = True
        def json(self):
            return {"people": self.people}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, json=None, headers=None):
            payloads.append(json)
            idx = len(payloads) - 1
            people = responses_by_call[idx] if idx < len(responses_by_call) else []
            return FakeResponse(people)

    return FakeClient, payloads


class TestTitledSearch:
    """The Apollo query must be title-constrained (ICP + adjacency), not a
    bare seniority sweep — the regional-bank bug."""

    def test_source_title_with_decoration_detects_ciso(self):
        # Dale's actual stored title from the reported bug
        from app.services.multithread import detect_role_family
        assert detect_role_family("VP - Chief Information Security Officer (CISO)") == "ciso"

    def test_primary_search_uses_adjacency_and_icp_titles(self, monkeypatch):
        from app.services import multithread
        from app.config import settings

        monkeypatch.setattr(settings, "APOLLO_API_KEY", "test_key")
        FakeClient, payloads = _fake_client_factory([
            [{"id": "1", "first_name": "A", "last_name": "B", "title": "CIO",
              "linkedin_url": "x", "organization": {"primary_domain": "bank.com"}}],
        ])
        monkeypatch.setattr(multithread.httpx, "Client", FakeClient)

        candidates = multithread.find_others_at_company(
            db=_fake_db(icp_config={"target_titles": ["CISO", "Director of IT Security"]}),
            org_id="any",
            company_domain="bank.com",
            company_name="Bank",
            source_lead_title="VP - Chief Information Security Officer (CISO)",
        )

        assert len(payloads) == 1  # titled search hit — no fallbacks fired
        primary = payloads[0]
        assert "person_seniorities" not in primary
        titles = primary["person_titles"]
        # Adjacency expansion of CISO
        assert "CIO" in titles
        assert "CTO" in titles
        assert "Head of IT" in titles
        # Org ICP titles included too
        assert "Director of IT Security" in titles
        assert [c["title"] for c in candidates] == ["CIO"]

    def test_broad_fallback_filters_out_unrelated_seniors(self, monkeypatch):
        """Regional-bank regression: when only the seniority fallback
        returns people, bank presidents must not surface — only relevant
        role families survive."""
        from app.services import multithread
        from app.config import settings

        monkeypatch.setattr(settings, "APOLLO_API_KEY", "test_key")
        bank_execs = [
            {"id": "1", "first_name": "Pat", "last_name": "A", "title": "President",
             "linkedin_url": "x", "organization": {"primary_domain": "bank.com"}},
            {"id": "2", "first_name": "Robin", "last_name": "B", "title": "State President - Midwest",
             "linkedin_url": "x", "organization": {"primary_domain": "bank.com"}},
            {"id": "3", "first_name": "Jordan", "last_name": "C",
             "title": "Director of Community Responsibility",
             "linkedin_url": "x", "organization": {"primary_domain": "bank.com"}},
            {"id": "4", "first_name": "Casey", "last_name": "D", "title": "Chief Information Officer",
             "linkedin_url": "x", "organization": {"primary_domain": "bank.com"}},
        ]
        # Titled search (domain) empty, titled search (name) empty, broad returns execs
        FakeClient, payloads = _fake_client_factory([[], [], bank_execs])
        monkeypatch.setattr(multithread.httpx, "Client", FakeClient)

        candidates = multithread.find_others_at_company(
            db=_fake_db(),
            org_id="any",
            company_domain="bank.com",
            company_name="Bank",
            source_lead_title="VP - Chief Information Security Officer (CISO)",
        )

        assert len(payloads) == 3
        assert payloads[2].get("person_seniorities")  # last call was the broad sweep
        # Only the CIO survives the relevance filter
        assert [c["title"] for c in candidates] == ["Chief Information Officer"]

    def test_unknown_source_title_and_no_icp_uses_default_committee(self, monkeypatch):
        from app.services import multithread
        from app.config import settings

        monkeypatch.setattr(settings, "APOLLO_API_KEY", "test_key")
        FakeClient, payloads = _fake_client_factory([[]])
        monkeypatch.setattr(multithread.httpx, "Client", FakeClient)

        multithread.find_others_at_company(
            db=_fake_db(),
            org_id="any",
            company_domain="bank.com",
            source_lead_title="Procurement Specialist",
        )

        assert payloads[0]["person_titles"] == multithread.DEFAULT_MULTITHREAD_TITLES
