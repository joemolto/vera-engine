"""Resolver fixtures: agreement, single-A, single PL-weak, single job-post,
conflict, empty.

Tests use canonical names from security_tools.yaml so the canonicalizer is
exercised end-to-end via the collectors elsewhere — here we feed DetectedTech
directly to keep the resolver test pure.
"""

import pytest

from app.services.stack_detection.resolve_confidence import resolve
from app.services.stack_detection.signal_confidence import cap_confidence
from app.services.stack_detection.types import (
    Category,
    Confidence,
    DetectedTech,
    Source,
    Tier,
)


def _pl(name, category=Category.EDR, method="DataAnalysis", tier=Tier.B):
    return DetectedTech(
        name=name,
        category=category,
        source=Source.PREDICTLEADS,
        raw_evidence=f"PredictLeads(method={method})",
        source_tier=tier,
        pl_detection_method=method,
    )


def _dns(name, category=Category.EMAIL_SECURITY, evidence="MX=mx.example.com"):
    return DetectedTech(
        name=name,
        category=category,
        source=Source.DNS,
        raw_evidence=evidence,
        source_tier=Tier.A,
    )


def _job(name, category=Category.EDR, count=1):
    return DetectedTech(
        name=name,
        category=category,
        source=Source.JOB_POST,
        raw_evidence=f"mentioned_in={count}_postings",
        source_tier=Tier.B,
    )


class TestResolveConfidence:
    def test_empty_input(self):
        assert resolve([]) == []

    def test_agreement_two_sources_is_high(self):
        # DNS + PL both report Proofpoint -> HIGH regardless of PL tier
        result = resolve([
            _dns("Proofpoint", Category.EMAIL_SECURITY),
            _pl("Proofpoint", Category.EMAIL_SECURITY, method="DataAnalysis"),
        ])
        assert len(result) == 1
        assert result[0].confidence == Confidence.HIGH
        assert Source.DNS in result[0].sources
        assert Source.PREDICTLEADS in result[0].sources

    def test_single_tier_a_dns_is_high(self):
        result = resolve([_dns("Mimecast", Category.EMAIL_SECURITY)])
        assert len(result) == 1
        assert result[0].confidence == Confidence.HIGH

    def test_single_tier_a_pl_strong_is_high(self):
        # PL self-reports detection via DNS -> Tier A -> single-source HIGH
        result = resolve([_pl("Okta", Category.IAM, method="DNS", tier=Tier.A)])
        assert len(result) == 1
        assert result[0].confidence == Confidence.HIGH

    def test_single_pl_weak_is_medium(self):
        result = resolve([_pl("Splunk", Category.SIEM)])
        assert len(result) == 1
        assert result[0].confidence == Confidence.MEDIUM
        assert result[0].sources == (Source.PREDICTLEADS,)

    def test_single_job_post_is_medium(self):
        # Per chosen tier scheme (b): job-post alone is MEDIUM, never HIGH
        result = resolve([_job("CrowdStrike")])
        assert len(result) == 1
        assert result[0].confidence == Confidence.MEDIUM
        assert result[0].sources == (Source.JOB_POST,)

    def test_two_tier_b_independent_sources_is_high(self):
        # PL-weak + job-post on same tech: independent sources => HIGH
        result = resolve([
            _pl("SentinelOne", Category.EDR),
            _job("SentinelOne", Category.EDR),
        ])
        assert len(result) == 1
        assert result[0].confidence == Confidence.HIGH

    def test_category_conflict_dns_wins(self):
        # DNS says email_security; PL-weak says OTHER. DNS (Tier A) wins.
        result = resolve([
            _dns("Proofpoint", Category.EMAIL_SECURITY),
            _pl("Proofpoint", Category.OTHER, method="DataAnalysis"),
        ])
        assert result[0].category == Category.EMAIL_SECURITY
        # Both evidence lines preserved
        assert len(result[0].evidence) == 2

    def test_category_conflict_pl_strong_beats_jobpost(self):
        # PL-strong (Tier A) vs job-post (Tier B): PL wins
        result = resolve([
            _pl("Okta", Category.IAM, method="DNS", tier=Tier.A),
            _job("Okta", Category.OTHER),
        ])
        assert result[0].category == Category.IAM

    def test_multiple_techs_grouped_independently(self):
        result = resolve([
            _dns("Cloudflare", Category.WAF_CDN, evidence="NS=ns.cloudflare.com"),
            _pl("Splunk", Category.SIEM),
            _job("CrowdStrike", Category.EDR),
        ])
        names = {r.name for r in result}
        assert names == {"Cloudflare", "Splunk", "CrowdStrike"}
        by_name = {r.name: r for r in result}
        assert by_name["Cloudflare"].confidence == Confidence.HIGH
        assert by_name["Splunk"].confidence == Confidence.MEDIUM
        assert by_name["CrowdStrike"].confidence == Confidence.MEDIUM


class TestCapConfidence:
    @pytest.mark.parametrize("signal,stack,expected", [
        (Confidence.HIGH, Confidence.HIGH, Confidence.HIGH),
        (Confidence.HIGH, Confidence.MEDIUM, Confidence.MEDIUM),
        (Confidence.MEDIUM, Confidence.HIGH, Confidence.MEDIUM),
        (Confidence.HIGH, Confidence.LOW, Confidence.LOW),
        (Confidence.LOW, Confidence.HIGH, Confidence.LOW),
        (Confidence.MEDIUM, Confidence.MEDIUM, Confidence.MEDIUM),
    ])
    def test_cap_is_min(self, signal, stack, expected):
        assert cap_confidence(signal, stack) == expected
