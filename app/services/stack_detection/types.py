"""Schemas for the stack-detection layer."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Source(str, Enum):
    PREDICTLEADS = "predictleads"
    DNS = "dns"
    JOB_POST = "job_post"


class Tier(str, Enum):
    A = "A"
    B = "B"


class Category(str, Enum):
    EMAIL_SECURITY = "email_security"
    WAF_CDN = "waf_cdn"
    EDR = "edr"
    SIEM = "siem"
    IAM = "iam"
    CLOUD = "cloud"
    OTHER = "other"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class DetectedTech:
    name: str
    category: Category
    source: Source
    raw_evidence: str
    source_tier: Tier
    pl_detection_method: Optional[str] = None


@dataclass(frozen=True)
class ResolvedTech:
    name: str
    category: Category
    confidence: Confidence
    sources: tuple
    evidence: tuple
