"""Tests for the free report's security overview.

Focus is the pure logic: role matching, date windows, the baseline verdict
and the shape guarantees the email renderer depends on. Network-backed
collectors are exercised only through their failure paths, because the whole
module's contract is that a partial report still beats no report.
"""
from datetime import datetime, timedelta, timezone

from app.services.security_overview import (
    _collect_hiring,
    _is_security_title,
    _posted_within,
    _team_verdict,
    build_security_overview,
)


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


class TestSecurityTitleMatching:
    def test_matches_practitioner_and_leadership_roles(self):
        assert _is_security_title("Senior Security Engineer")
        assert _is_security_title("SOC Analyst")
        assert _is_security_title("Head of Information Security")
        assert _is_security_title("CISO")
        assert _is_security_title("GRC Lead")

    def test_ignores_unrelated_roles(self):
        assert not _is_security_title("Sales Engineer")
        assert not _is_security_title("DevOps Engineer")
        assert not _is_security_title("Security Guard")  # no keyword match
        assert not _is_security_title("")


class TestPostedWithin:
    def test_recent_posting_counts(self):
        assert _posted_within(_days_ago(10), 90)

    def test_old_posting_does_not(self):
        assert not _posted_within(_days_ago(200), 90)

    def test_date_only_format_parses(self):
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
        assert _posted_within(recent, 90)

    def test_missing_or_unparseable_date_is_not_recent(self):
        # An undated posting is not evidence of RECENT hiring. Guessing here
        # is exactly what got the old cve_in_stack signal retired.
        assert not _posted_within(None, 90)
        assert not _posted_within("", 90)
        assert not _posted_within("sometime last spring", 90)


class TestTeamVerdict:
    def test_below_band_is_called_out(self):
        v = _team_verdict(4, [8, 15])
        assert "4 security staff" in v
        assert "below" in v

    def test_in_band(self):
        assert "in line with" in _team_verdict(10, [8, 15])

    def test_above_band(self):
        assert "above" in _team_verdict(20, [8, 15])

    def test_zero_reads_as_none_found(self):
        v = _team_verdict(0, [8, 15])
        assert "No security-titled staff found" in v
        assert "8 to 15" in v

    def test_missing_band_yields_no_claim(self):
        assert _team_verdict(4, []) == ""
        assert _team_verdict(4, [8]) == ""


class TestUnknownSizeMakesNoComparison:
    """Regression, 2026-08-04.

    `_expected_band` falls back to the smallest band when headcount is
    unknown, so a 5,000-person manufacturer with a null `size_band` rendered
    as "5 security staff, above the 0 to 1 typical". Sane for an internal
    score, a falsehood in a report going to a stranger.
    """

    def _collect(self, monkeypatch, *, size_band, industry="mining & metals"):
        from app.services import security_overview as so

        monkeypatch.setattr(
            "app.services.security_team_profiler.refresh_security_team_profile",
            # expected_band here is deliberately the STALE [0, 1] that a
            # profile built while size_band was null would have cached.
            lambda *a, **k: {
                "counts_by_bucket": {"leadership": 1, "engineering": 2, "soc_ops": 0,
                                     "grc": 2, "iam": 0},
                "total": 5,
                "expected_band": [0, 1],
                "gap_buckets": ["soc_ops", "iam"],
                "top_titles": ["Chief Information Security Officer"],
                "is_regulated_industry": False,
            },
        )
        monkeypatch.setattr(
            "app.services.companies.get_company_by_domain",
            lambda *a, **k: {"size_band": size_band, "industry": industry},
        )
        return so._collect_team(object(), company_id="abc", domain="northwind.com")

    def test_unknown_size_withholds_the_baseline(self, monkeypatch):
        team = self._collect(monkeypatch, size_band=None)
        assert team["total"] == 5           # the count is observed, keep it
        assert team["gaps"]                 # so are the empty disciplines
        assert team["expected_band"] == []  # but no baseline may be shown
        assert team["verdict"] == ""

    def test_stale_cached_band_is_never_trusted(self, monkeypatch):
        # The original case: size arrived AFTER the profile cached
        # its [0, 1]. The band must be recomputed, not read back.
        team = self._collect(monkeypatch, size_band="1001,5000")
        assert team["expected_band"] == [8, 25]
        assert "below the 8 to 25" in team["verdict"]

    def test_unparseable_size_format_makes_no_claim(self, monkeypatch):
        # "1001-5000" (hyphen) and "10001+" are not formats _parse_size_band
        # understands; silently scoring them as a 0-employee company is how
        # the 0-to-1 falsehood got out in the first place.
        for raw in ("1001-5000", "10001+", ""):
            team = self._collect(monkeypatch, size_band=raw)
            assert team["expected_band"] == [], raw
            assert team["verdict"] == "", raw


class TestSubjectPriority:
    """The security line must beat press-release filler, but not money."""

    BASE = {
        "domain": "northwind.com",
        "company": {"name": "Northwind", "industry": "mining & metals"},
        "email_security": {"spf": True, "dmarc": True, "dmarc_policy": "reject"},
    }
    TEAM = {
        "total": 5, "expected_band": [8, 25], "gaps": ["Security operations"],
        "verdict": "5 security staff, below the 8 to 25 typical for a company this size and sector.",
    }

    def _subject(self, highlights, team=None):
        from app.services.email_sender import _build_free_report_subject

        report = dict(self.BASE, highlights=highlights)
        if team is not None:
            report["security_overview"] = {"team": team}
        return _build_free_report_subject(report)

    def test_security_line_beats_a_partnership(self):
        s = self._subject(
            [{"label": "New partnership", "headline": "Northwind partners with Contoso."}],
            self.TEAM,
        )
        assert s == "Northwind: 5 security staff vs 8-25 typical"

    def test_funding_still_wins(self):
        s = self._subject(
            [{"label": "Funding", "headline": "Raised $40m Series C"}], self.TEAM,
        )
        assert "Series C" in s

    def test_breach_still_wins(self):
        s = self._subject(
            [{"label": "Breach / incident reported", "headline": "Ransomware disclosed"}],
            self.TEAM,
        )
        assert "Ransomware" in s

    def test_soft_highlight_still_used_when_there_is_no_team(self):
        # No regression for the lite report, which carries no security overview.
        s = self._subject(
            [{"label": "New partnership", "headline": "Northwind partners with Contoso."}],
        )
        assert "partners with Contoso" in s


class TestCollectHiring:
    def test_counts_only_security_roles(self):
        postings = [
            {"title": "Security Engineer", "posted_date": _days_ago(10), "location": "London"},
            {"title": "Account Executive", "posted_date": _days_ago(10)},
            {"title": "SOC Analyst", "posted_date": _days_ago(200)},
        ]
        out = _collect_hiring(postings)
        assert out["open_security_roles"] == 2
        assert out["recent_90d"] == 1

    def test_titles_carry_no_contact_data(self):
        postings = [{
            "title": "CISO", "location": "Leeds", "posted_date": _days_ago(1),
            "url": "https://example.com/job/1",
        }]
        out = _collect_hiring(postings)
        assert out["titles"] == [{"title": "CISO", "location": "Leeds"}]

    def test_empty_input_is_safe(self):
        out = _collect_hiring(None)
        assert out == {"open_security_roles": 0, "recent_90d": 0, "titles": []}


class TestBuildSecurityOverview:
    def test_requires_a_domain(self):
        try:
            build_security_overview(None, domain="")
        except ValueError:
            return
        raise AssertionError("expected ValueError for empty domain")

    def test_every_section_present_even_when_collectors_fail(self, monkeypatch):
        # db=None makes the Supabase-backed collectors blow up internally.
        # The contract is that they degrade rather than propagate.
        monkeypatch.setattr(
            "app.services.website_posture.probe_posture_pages",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down")),
        )
        monkeypatch.setattr(
            "app.services.subdomain_discovery.discover_subdomains",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crt.sh down")),
        )
        out = build_security_overview(
            None, domain="example.com", company_id=None,
            job_postings=[], run_perimeter_scan=False,
        )
        assert set(out) == {"posture", "team", "hiring", "perimeter"}
        assert out["team"] is None  # no company_id -> no profile, not a crash
        assert out["hiring"]["open_security_roles"] == 0
        assert out["perimeter"]["scanned"] is False
        assert out["perimeter"]["hostnames_published"] == 0

    def test_perimeter_scan_is_skippable(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.subdomain_discovery.discover_subdomains",
            lambda *a, **k: ["vpn.example.com", "mail.example.com"],
        )
        monkeypatch.setattr(
            "app.services.website_posture.probe_posture_pages", lambda *a, **k: {},
        )
        monkeypatch.setattr(
            "app.services.website_posture.probe_security_txt", lambda *a, **k: None,
        )
        out = build_security_overview(
            None, domain="example.com", run_perimeter_scan=False,
        )
        # Hostname count still comes back — it is cheap and cached — but no
        # host was probed, so no kit may be claimed.
        assert out["perimeter"]["hostnames_published"] == 2
        assert out["perimeter"]["scanned"] is False
        assert out["perimeter"]["edge_kit"] == []
