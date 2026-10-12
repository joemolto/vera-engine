"""Security hiring must separate leadership openings from practitioner ones.

Commercially they are opposite reads: a CISO vacancy usually means no senior
owner of security exists today, while a SOC analyst opening means a mature
team is expanding. Before 2026-08-04 both were filed as `hiring_security`.
"""

from app.services.enrichers.jobs import (
    SECURITY_LEADERSHIP_TITLES,
    split_security_roles,
)


class TestSplitSecurityRoles:
    def test_leadership_titles_are_separated(self):
        leadership, practitioner = split_security_roles([
            "Chief Information Security Officer",
            "SOC Analyst",
        ])
        assert leadership == ["Chief Information Security Officer"]
        assert practitioner == ["SOC Analyst"]

    def test_manager_grades_count_as_leadership(self):
        """2026-08-04: a Head-of/Manager role owns budget and vendor
        choice. 'Senior Information Security Manager' was previously filed
        next to 'SOC Analyst'."""
        leadership, practitioner = split_security_roles([
            "Senior Information Security Manager",
        ])
        assert leadership == ["Senior Information Security Manager"]
        assert practitioner == []

    def test_practitioner_roles_never_promoted(self):
        roles = [
            "Security Engineer",
            "Penetration Tester",
            "Detection Engineer",
            "IAM Engineer",
            "Threat Intelligence Analyst",
        ]
        leadership, practitioner = split_security_roles(roles)
        assert leadership == []
        assert practitioner == roles

    def test_mixed_batch_splits_both_ways(self):
        leadership, practitioner = split_security_roles([
            "Head of Cyber",
            "Security Architect",
            "VP Security",
            "Malware Analyst",
        ])
        assert leadership == ["Head of Cyber", "VP Security"]
        assert practitioner == ["Security Architect", "Malware Analyst"]

    def test_matching_is_case_insensitive(self):
        leadership, _ = split_security_roles(["hEaD oF sEcUrItY"])
        assert leadership == ["hEaD oF sEcUrItY"]

    def test_empty_and_falsy_entries_are_dropped(self):
        leadership, practitioner = split_security_roles(["", None, "CISO"])
        assert leadership == ["CISO"]
        assert practitioner == []

    def test_no_roles_returns_two_empty_lists(self):
        assert split_security_roles([]) == ([], [])
        assert split_security_roles(None) == ([], [])

    def test_engineer_titles_do_not_match_leadership_vocab(self):
        """Guards the 2026-06-10 regression where hiring_security fired on
        any engineer. 'Security Engineer' must not contain a leadership kw."""
        for kw in SECURITY_LEADERSHIP_TITLES:
            assert kw not in "security engineer"
            assert kw not in "soc analyst"


class TestSignalEmission:
    """The split must produce distinct signal types with distinct strengths."""

    def _emit(self, roles):
        from app.services.enrichers.jobs import split_security_roles
        leadership, practitioner = split_security_roles(roles)
        out = []
        strength = 0.8
        if leadership:
            out.append(("hiring_security_leadership", min(0.95, strength + 0.1)))
        if practitioner:
            out.append(("hiring_security", strength))
        return out

    def test_leadership_scores_above_practitioner(self):
        emitted = dict(self._emit(["CISO", "SOC Analyst"]))
        assert emitted["hiring_security_leadership"] > emitted["hiring_security"]

    def test_practitioner_only_emits_one_signal(self):
        emitted = self._emit(["Security Engineer"])
        assert [t for t, _ in emitted] == ["hiring_security"]

    def test_leadership_only_emits_one_signal(self):
        emitted = self._emit(["Head of Information Security"])
        assert [t for t, _ in emitted] == ["hiring_security_leadership"]
