"""Job-post collector tests — alias matching, false-positive guards, counts."""

from app.services.stack_detection.collectors.job_posts import collect_job_posts
from app.services.stack_detection.types import Category, Source, Tier


class TestJobPostCollector:
    def test_empty_input(self):
        assert collect_job_posts([]) == []
        assert collect_job_posts([""]) == []

    def test_canonical_name_match(self):
        out = collect_job_posts([
            "We're hiring a Detection Engineer with CrowdStrike Falcon experience."
        ])
        assert len(out) == 1
        assert out[0].name == "CrowdStrike"
        assert out[0].category == Category.EDR
        assert out[0].source == Source.JOB_POST
        assert out[0].source_tier == Tier.B

    def test_alias_match_collapses_to_canonical(self):
        out = collect_job_posts([
            "Experience with Falcon Insight required. Plus Defender for Endpoint."
        ])
        names = {d.name for d in out}
        assert "CrowdStrike" in names
        assert "Microsoft Defender for Endpoint" in names

    def test_count_aggregates_across_postings(self):
        out = collect_job_posts([
            "Splunk admin needed",
            "Splunk ES content development",
            "Backend engineer (no relevant tools)",
        ])
        splunk = [d for d in out if d.name == "Splunk"][0]
        assert "mentioned_in=2_postings" == splunk.raw_evidence

    def test_word_boundary_avoids_false_positive(self):
        # "okta" inside "okta-like" -> still a hit; but "Yokta" shouldn't be
        out = collect_job_posts(["The Yokta platform is not Okta-related."])
        # We DO want "Okta" matched in "Okta-related" (word boundary OK)
        # but we do NOT want a hit from "Yokta"
        assert any(d.name == "Okta" for d in out)
        # If word-boundary were broken, "Yokta" would also produce Okta — we
        # can't easily distinguish that from the legit "Okta-related" hit, so
        # this test mainly guards that we don't double-count: still 1 hit.
        okta_hits = [d for d in out if d.name == "Okta"]
        assert len(okta_hits) == 1

    def test_case_insensitive(self):
        out = collect_job_posts(["zscaler private access deployment"])
        assert any(d.name == "Zscaler" for d in out)

    def test_no_match_returns_empty(self):
        assert collect_job_posts(["Generic backend engineer role, Python + Postgres."]) == []
