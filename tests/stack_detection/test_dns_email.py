"""DNS collector tests — mock dns.resolver.Resolver so no live lookups."""

from unittest.mock import MagicMock

import dns.exception
import dns.resolver
import pytest

from app.services.stack_detection.collectors.dns_email import collect_dns
from app.services.stack_detection.types import Category, Source, Tier


class _MX:
    def __init__(self, exchange):
        self.exchange = exchange

    def __str__(self):
        return self.exchange


class _NS:
    def __init__(self, target):
        self.target = target


class _TXT:
    def __init__(self, value):
        self.strings = [value.encode()]


def _resolver_with(responses):
    """Build a mocked Resolver whose resolve() returns from a (name, rtype) map."""
    r = MagicMock(spec=dns.resolver.Resolver)

    def fake_resolve(name, rtype):
        key = (name, rtype)
        if key in responses:
            val = responses[key]
            if isinstance(val, Exception):
                raise val
            return val
        raise dns.resolver.NXDOMAIN()

    r.resolve.side_effect = fake_resolve
    return r


class TestDnsCollector:
    def test_empty_domain_returns_empty(self):
        assert collect_dns("") == []

    def test_proofpoint_mx_detected(self):
        r = _resolver_with({
            ("acme.com", "MX"): [_MX("mx0a-00012345.pphosted.com.")],
        })
        out = collect_dns("acme.com", resolver=r)
        assert len(out) == 1
        assert out[0].name == "Proofpoint"
        assert out[0].category == Category.EMAIL_SECURITY
        assert out[0].source == Source.DNS
        assert out[0].source_tier == Tier.A
        assert "pphosted.com" in out[0].raw_evidence

    def test_microsoft_365_mx(self):
        r = _resolver_with({
            ("acme.com", "MX"): [_MX("acme-com.mail.protection.outlook.com.")],
        })
        out = collect_dns("acme.com", resolver=r)
        assert any(d.name == "Microsoft 365" for d in out)

    def test_cloudflare_ns(self):
        r = _resolver_with({
            ("acme.com", "NS"): [
                _NS("kira.ns.cloudflare.com."),
                _NS("walt.ns.cloudflare.com."),
            ],
        })
        out = collect_dns("acme.com", resolver=r)
        # dedupe collapses both NS to one
        cloudflare = [d for d in out if d.name == "Cloudflare"]
        assert len(cloudflare) == 1
        assert cloudflare[0].category == Category.WAF_CDN

    def test_spf_includes_match(self):
        r = _resolver_with({
            ("acme.com", "TXT"): [_TXT("v=spf1 include:spf.protection.outlook.com -all")],
        })
        out = collect_dns("acme.com", resolver=r)
        assert any(d.name == "Microsoft 365" and "SPF" in d.raw_evidence for d in out)

    def test_dmarc_rua_match(self):
        r = _resolver_with({
            ("_dmarc.acme.com", "TXT"): [_TXT("v=DMARC1; p=reject; rua=mailto:rua@dmarc.proofpoint.com")],
        })
        out = collect_dns("acme.com", resolver=r)
        assert any(d.name == "Proofpoint" and "DMARC" in d.raw_evidence for d in out)

    def test_nxdomain_returns_empty_not_raises(self):
        r = _resolver_with({})  # everything NXDOMAIN
        assert collect_dns("nonexistent-domain-xyz.com", resolver=r) == []

    def test_timeout_per_record_isolated(self):
        # MX times out, NS succeeds -> NS detection still returned
        r = _resolver_with({
            ("acme.com", "MX"): dns.exception.Timeout(),
            ("acme.com", "NS"): [_NS("kira.ns.cloudflare.com.")],
        })
        out = collect_dns("acme.com", resolver=r)
        assert any(d.name == "Cloudflare" for d in out)
        assert not any(d.raw_evidence.startswith("MX=") for d in out)

    def test_unknown_mx_target_no_detection(self):
        r = _resolver_with({
            ("acme.com", "MX"): [_MX("mail.weirdunknownhost.example.")],
        })
        assert collect_dns("acme.com", resolver=r) == []

    def test_non_spf_txt_record_ignored(self):
        # Random TXT (e.g. domain verification) must not match SPF rules
        r = _resolver_with({
            ("acme.com", "TXT"): [_TXT("google-site-verification=abc123")],
        })
        out = collect_dns("acme.com", resolver=r)
        # Should produce no SPF-tagged detections
        assert not any("SPF" in d.raw_evidence for d in out)

    def test_zscaler_txt_verification(self):
        r = _resolver_with({
            ("acme.com", "TXT"): [_TXT("zscaler-verification-151865296-06012026-FChVrzq")],
        })
        out = collect_dns("acme.com", resolver=r)
        zscaler = [d for d in out if d.name == "Zscaler"]
        assert len(zscaler) == 1
        assert zscaler[0].category == Category.WAF_CDN
        assert zscaler[0].raw_evidence.startswith("TXT=")

    def test_microsoft_365_verification_token(self):
        r = _resolver_with({
            ("acme.com", "TXT"): [_TXT("MS=ms72722609")],
        })
        out = collect_dns("acme.com", resolver=r)
        assert any(d.name == "Microsoft 365" and d.category == Category.CLOUD for d in out)

    def test_dmarc_analyzer_platform(self):
        r = _resolver_with({
            ("_dmarc.acme.com", "TXT"): [_TXT("v=DMARC1; p=reject; rua=mailto:abc@rep.dmarcanalyzer.com")],
        })
        out = collect_dns("acme.com", resolver=r)
        assert any(d.name == "DMARC Analyzer" for d in out)

    def test_spf_and_txt_in_same_response_route_correctly(self):
        # Single TXT pull yields both SPF-routed and TXT-routed detections
        r = _resolver_with({
            ("acme.com", "TXT"): [
                _TXT("v=spf1 include:spf.protection.outlook.com -all"),
                _TXT("zscaler-verification-abc123"),
                _TXT("MS=ms99999999"),
            ],
        })
        out = collect_dns("acme.com", resolver=r)
        names = {d.name for d in out}
        assert "Microsoft 365" in names  # both SPF and TXT route here; dedupe collapses
        assert "Zscaler" in names
