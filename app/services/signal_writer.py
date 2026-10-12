"""Validated signal-write path.

All four signal-insert sites in the engine route through ``insert_signal``
or ``insert_signals`` so the same schema check runs every time. The check
catches malformed payloads at the boundary — empty signal_data, blank
headline, missing org_id/lead_id, missing signal_type — and logs+rejects
them rather than letting empty rows reach the dashboard.

The enrichment pipeline's canonical store is ``leads.enrichment_data``,
not the signals table; a rejected projection-write here does not lose
information. It just keeps junk out of the dashboard.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from supabase import Client

logger = logging.getLogger(__name__)


# Canonical signal_type values produced by the engine today. Unknown
# types are accepted (Vera Brain tool calls and external webhooks can
# legitimately introduce new ones) but logged at INFO so drift is
# visible. A migration adds a CHECK constraint covering this set.
KNOWN_SIGNAL_TYPES = {
    # ---- Account-level (subject=account) ----
    "recent_acquisition",
    "recently_funded",
    "product_launch",
    "new_partnership",
    "new_leadership",
    "expansion",
    # ---- Account hiring (subject=account) ----
    "hiring_sales_bd",
    "hiring_security",            # practitioner / IC security roles
    "hiring_security_leadership", # CISO, Head of Security, Security Manager
    "hiring_grc",
    # ---- Cyber correlation (subject=account or tech) ----
    "security_leadership_hire",   # subject=person — requires recency evidence
    "security_team_expansion",
    "cve_in_stack",
    "compliance_gap",
    "breach_proximity",
    "breach_mention",  # PL-news-sourced breach / incident report (Risk)
    # ---- Person-level (subject=person) — Phase 3 ----
    "person_new_in_role",
    "person_promoted",
    "person_changed_company",
    "person_security_leader",
    "person_grc_leader",
    "person_technical_leader",
    "person_commercial_leader",
    "person_prior_vendor_experience",
    "person_active_linkedin",
    "person_posted_relevant_topic",
    # Person engagement signals (added 2026-06-08, Phase D person intel
    # build). person_posted_relevant_topic = their OWN post on a relevant
    # topic. The next two cover comments/reactions on OTHERS' posts —
    # what they're listening to, not just what they're saying.
    "person_engaged_with_topic",
    "person_engaged_with_competitor",
    # ---- Job-keyword (subject=job) — Phase 4 ----
    "job_mentions_soc2",
    "job_mentions_iso27001",
    "job_mentions_security_questionnaires",
    "job_mentions_vendor_risk",
    "job_mentions_audit",
    "job_mentions_cloud_security",
    "job_mentions_identity",
    "job_mentions_siem",
    "job_mentions_edr",
    "job_mentions_vuln_mgmt",
    "job_mentions_incident_response",
    "job_mentions_devsecops",
    # Job pressure rollups
    "compliance_workflow_pressure",
    "security_operations_pressure",
    "cloud_security_pressure",
    "appsec_pressure",
    "enterprise_trust_pressure",
    # ---- Website / posture (subject=website) — Phase 5 ----
    "trust_center_present",
    "security_page_present",
    "compliance_page_present",
    "privacy_page_present",
    "dpa_present",
    "subprocessors_present",
    "status_page_present",
    "responsible_disclosure_present",
    "bug_bounty_present",
    "security_contact_present",
    "certification_claim_detected",  # legacy generic — replaced by per-cert types 2026-06-11
    # Per-certification claim types (split from certification_claim_detected
    # so reps can filter by framework). Keys match COMPLIANCE_FLAGS in
    # website_posture.py: certification_{flag_key}.
    "certification_iso_27001",
    "certification_soc_2",
    "certification_pci_dss",
    "certification_cyber_essentials",
    "certification_cyber_essentials_plus",
    "certification_crest",
    "certification_check",
    # certification_gap REMOVED 2026-05-28 — absence-of-evidence framing,
    # Folded into public_proof_not_visible.
    # security_proof_gap REMOVED 2026-05-28 — same reason. Renamed for
    # clarity to public_proof_not_visible (visibility, not deficiency).
    "public_proof_not_visible",
    "enterprise_page_detected",
    "api_or_developer_platform",
    # ---- Customer / market (subject=account) ----
    "enterprise_customer_detected",
    "regulated_customer_detected",
    "public_sector_customer_detected",
    # ---- Compound cyber-pressure (subject=account) — Phase 6 ----
    "enterprise_readiness_pressure",
    "customer_security_review_pressure",
    "post_funding_security_maturity_push",
    "upmarket_security_friction",
    "compliance_operationalisation",
    "security_team_scaling",
    "cloud_security_scaling",
    "revenue_blocker_likely",
    # ---- Social listening — org-scoped sector pulse (subject=news, no lead/company)
    # Legacy from the v1 org-keyword scanner. Kept so old rows still validate;
    # do not write new rows of these types.
    "reddit_mention",
    "reddit_comment_mention",
    "linkedin_post_mention",
    "twitter_mention",
    "forum_mention",
    # ---- Social listening v2 — per-account (subject=account, company_id set)
    # Written by social_listener.scan_account. Each represents a real,
    # signal-bearing social post attributable to the watched company.
    "social_company_mention",      # generic named mention worth a rep's eye
    "social_company_post",         # 1st-party post from the company's LinkedIn page
    "social_employee_chatter",     # current/ex-employee discussing the company
    "social_breach_mention",       # company named in breach/incident discussion
    "social_hiring_buzz",          # 3rd-party chatter about their hiring (validates PL hiring signal)
    "social_complaint",            # public customer complaint / poor review
    "social_customer_review",      # neutral/positive review or testimonial
    "social_vendor_relationship",  # confirms what tech/vendors the company uses
    # Added 2026-05-31 — broadened beyond cyber-only after user feedback:
    # the rep just needs a credible reason to reach out citing the post.
    # Funding, leadership moves, milestones (raises, customer wins, awards,
    # expansion) are all valid openers even if not directly security-related.
    "social_funding_news",         # raise, M&A, IPO, investment round
    "social_leadership_change",    # exec hire/departure (CEO, CTO, CISO, anyone)
    "social_milestone",            # customer win, award, expansion, partnership
}


# Allowed values for the three classification axes added in the
# 2026-05-28 schema migration (signals_subject_inference_source_quality).
# A DB CHECK constraint also enforces these — duplicating here gives a
# nicer error at the boundary.
SUBJECT_TYPES = {"account", "person", "job", "website", "tech", "news"}
INFERENCE_LEVELS = {"observed", "derived", "inferred", "speculative"}
SOURCE_QUALITIES = {
    "first_party",
    "third_party",
    "inferred_from_multiple_sources",
    "weak_unverified",
}

# Cyber lens v1 — axis tagging (spec 2026-06-09). Every signal sits
# under one of four lenses; an unrecognised type leaves axis NULL and
# the dossier shows it under "Other".
#   risk       = live exposure (breach/CVE/dark web/regulator pressure)
#   posture    = public security stance (bug bounty, trust center,
#                public security comms, employee voice)
#   maturity   = capability built (team size/comp, leadership, GRC,
#                certifications, hiring depth, in-house tooling)
#   commercial = universally useful B2B signals not bound to security
#                (funding, M&A, non-security C-suite, IPO, expansion,
#                competitor-engagement on the rep's behalf)
AXIS_TYPES = {"risk", "posture", "maturity", "commercial"}


# Default classification per signal_type. Anything missing falls back
# to account/observed/third_party. Each entry is (subject, inference,
# source_quality) — caller can override but this is the canonical home
# so we don't sprinkle "person | observed | first_party" through the
# enricher.
SIGNAL_DEFAULTS: Dict[str, tuple] = {
    # Account / PL news
    "recent_acquisition": ("account", "observed", "third_party"),
    "recently_funded": ("account", "observed", "third_party"),
    "product_launch": ("account", "observed", "third_party"),
    "new_partnership": ("account", "observed", "third_party"),
    "new_leadership": ("account", "observed", "third_party"),
    "expansion": ("account", "observed", "third_party"),
    # Hiring (job descriptions are first-party — careers page is theirs)
    "hiring_sales_bd": ("account", "observed", "first_party"),
    "hiring_security": ("account", "observed", "first_party"),
    "hiring_security_leadership": ("account", "observed", "first_party"),
    "hiring_grc": ("account", "observed", "first_party"),
    "security_team_expansion": ("account", "derived", "first_party"),
    # Cyber correlation — derived from CVE/breach feeds matched to stack
    # Reborn 2026-08-04 as observed/first_party: the version comes from our
    # own probe of their own host, not inferred from a third-party dataset.
    # The retired version was correctly "derived" — it guessed.
    "cve_in_stack": ("tech", "observed", "first_party"),
    "compliance_gap": ("account", "inferred", "inferred_from_multiple_sources"),
    "breach_proximity": ("account", "derived", "third_party"),
    # Person — requires evidence
    "security_leadership_hire": ("person", "observed", "third_party"),
    "person_new_in_role": ("person", "observed", "first_party"),
    "person_promoted": ("person", "observed", "first_party"),
    "person_changed_company": ("person", "observed", "first_party"),
    "person_security_leader": ("person", "derived", "first_party"),
    "person_grc_leader": ("person", "derived", "first_party"),
    "person_technical_leader": ("person", "derived", "first_party"),
    "person_commercial_leader": ("person", "derived", "first_party"),
    "person_prior_vendor_experience": ("person", "derived", "first_party"),
    "person_active_linkedin": ("person", "observed", "first_party"),
    "person_posted_relevant_topic": ("person", "observed", "first_party"),
    # Engagement on others' posts. Subject is still the person doing
    # the engaging; source_quality is third_party because the post
    # itself is on someone else's profile (not first-party content).
    "person_engaged_with_topic": ("person", "observed", "third_party"),
    "person_engaged_with_competitor": ("person", "observed", "third_party"),
    # Job-keyword — JD text is first-party
    "job_mentions_soc2": ("job", "observed", "first_party"),
    "job_mentions_iso27001": ("job", "observed", "first_party"),
    "job_mentions_security_questionnaires": ("job", "observed", "first_party"),
    "job_mentions_vendor_risk": ("job", "observed", "first_party"),
    "job_mentions_audit": ("job", "observed", "first_party"),
    "job_mentions_cloud_security": ("job", "observed", "first_party"),
    "job_mentions_identity": ("job", "observed", "first_party"),
    "job_mentions_siem": ("job", "observed", "first_party"),
    "job_mentions_edr": ("job", "observed", "first_party"),
    "job_mentions_vuln_mgmt": ("job", "observed", "first_party"),
    "job_mentions_incident_response": ("job", "observed", "first_party"),
    "job_mentions_devsecops": ("job", "observed", "first_party"),
    "compliance_workflow_pressure": ("account", "derived", "inferred_from_multiple_sources"),
    "security_operations_pressure": ("account", "derived", "inferred_from_multiple_sources"),
    "cloud_security_pressure": ("account", "derived", "inferred_from_multiple_sources"),
    "appsec_pressure": ("account", "derived", "inferred_from_multiple_sources"),
    "enterprise_trust_pressure": ("account", "derived", "inferred_from_multiple_sources"),
    # Website / posture — their own site is first-party
    "trust_center_present": ("website", "observed", "first_party"),
    "security_page_present": ("website", "observed", "first_party"),
    "compliance_page_present": ("website", "observed", "first_party"),
    "privacy_page_present": ("website", "observed", "first_party"),
    "dpa_present": ("website", "observed", "first_party"),
    "subprocessors_present": ("website", "observed", "first_party"),
    "status_page_present": ("website", "observed", "first_party"),
    "responsible_disclosure_present": ("website", "observed", "first_party"),
    "bug_bounty_present": ("website", "observed", "first_party"),
    "security_contact_present": ("website", "observed", "first_party"),
    "certification_claim_detected": ("website", "observed", "first_party"),
    "certification_iso_27001": ("website", "observed", "first_party"),
    "certification_soc_2": ("website", "observed", "first_party"),
    "certification_pci_dss": ("website", "observed", "first_party"),
    "certification_cyber_essentials": ("website", "observed", "first_party"),
    "certification_cyber_essentials_plus": ("website", "observed", "first_party"),
    "certification_crest": ("website", "observed", "first_party"),
    "certification_check": ("website", "observed", "first_party"),
    # certification_gap + security_proof_gap removed 2026-05-28.
    # public_proof_not_visible replaces them — visibility, not deficiency.
    "public_proof_not_visible": ("account", "inferred", "inferred_from_multiple_sources"),
    "enterprise_page_detected": ("website", "observed", "first_party"),
    "api_or_developer_platform": ("website", "observed", "first_party"),
    # Customer / market
    "enterprise_customer_detected": ("account", "observed", "first_party"),
    "regulated_customer_detected": ("account", "derived", "first_party"),
    "public_sector_customer_detected": ("account", "derived", "first_party"),
    # Compound — by definition inferred from multiple atomic signals
    "enterprise_readiness_pressure": ("account", "inferred", "inferred_from_multiple_sources"),
    "customer_security_review_pressure": ("account", "inferred", "inferred_from_multiple_sources"),
    "post_funding_security_maturity_push": ("account", "inferred", "inferred_from_multiple_sources"),
    "upmarket_security_friction": ("account", "inferred", "inferred_from_multiple_sources"),
    "compliance_operationalisation": ("account", "inferred", "inferred_from_multiple_sources"),
    "security_team_scaling": ("account", "inferred", "inferred_from_multiple_sources"),
    "cloud_security_scaling": ("account", "inferred", "inferred_from_multiple_sources"),
    "revenue_blocker_likely": ("account", "inferred", "inferred_from_multiple_sources"),
    # Social listening v2 — account-scoped. 1st-party (company's own LinkedIn
    # posts) get first_party; everything else is third_party. All observed
    # (we have the actual post URL) except hiring_buzz which is derived
    # from chatter rather than the company's own job board.
    "social_company_mention": ("account", "observed", "third_party"),
    "social_company_post": ("account", "observed", "first_party"),
    "social_employee_chatter": ("account", "observed", "third_party"),
    "social_breach_mention": ("account", "observed", "third_party"),
    "social_hiring_buzz": ("account", "derived", "third_party"),
    "social_complaint": ("account", "observed", "third_party"),
    "social_customer_review": ("account", "observed", "third_party"),
    "social_vendor_relationship": ("account", "observed", "third_party"),
    "social_funding_news": ("account", "observed", "third_party"),
    "social_leadership_change": ("account", "observed", "third_party"),
    "social_milestone": ("account", "observed", "third_party"),
}


def classify_signal(signal_type: str) -> tuple:
    """Return (subject_type, inference_level, source_quality) for a signal_type.

    Falls back to ('account','observed','third_party') for unknown types
    so legacy callers don't blow up — the unknown-type INFO log in
    _validate_one is the early-warning signal that this map needs an
    entry.
    """
    return SIGNAL_DEFAULTS.get(signal_type, ("account", "observed", "third_party"))


# Cyber lens v1 — signal_type → axis. Unmapped types stay NULL and show
# under "Other" on the dossier. Refining this map is the lightest-weight
# way to evolve the lens — no AI prompt changes required.
SIGNAL_AXIS: Dict[str, str] = {
    # ---- Commercial — B2B-universal sales signals ----
    "recently_funded": "commercial",
    "social_funding_news": "commercial",
    "recent_acquisition": "commercial",
    "new_leadership": "commercial",          # PL "new_leadership" is non-security exec by default
    "social_leadership_change": "commercial",
    "social_milestone": "commercial",        # customer wins, awards, expansion
    "expansion": "commercial",
    "person_commercial_leader": "commercial",
    "person_engaged_with_competitor": "commercial",  # rep's competitor — sales context
    "social_vendor_relationship": "commercial",      # which vendors they use = sales context
    "revenue_blocker_likely": "commercial",

    # ---- Risk — live exposure ----
    "breach_proximity": "risk",
    "social_breach_mention": "risk",
    "breach_mention": "risk",        # PL-news-sourced breach event
    "cve_in_stack": "risk",
    "credential_leak": "risk",               # employee creds in stealer logs (LeakRadar)
    "credential_leak_supply_chain": "risk",  # third-party / supply-chain creds (LeakRadar)

    # ---- Posture — public security stance / behaviour ----
    "bug_bounty_present": "posture",
    "responsible_disclosure_present": "posture",
    "trust_center_present": "posture",
    "security_page_present": "posture",
    "compliance_page_present": "posture",
    "privacy_page_present": "posture",
    "dpa_present": "posture",
    "subprocessors_present": "posture",
    "status_page_present": "posture",
    "security_contact_present": "posture",
    "person_posted_relevant_topic": "posture",   # Haiku already confirmed topic relevance
    "person_engaged_with_topic": "posture",      # Haiku already confirmed topic relevance
    "enterprise_trust_pressure": "posture",
    # Demoted to null (Other bucket) 2026-06-09 after first-look review:
    # these signal_types are content-agnostic, so promoting them straight
    # into Posture pollutes the lens with random chatter (e.g. insurance
    # customers complaining about car insurance got tagged as "employee
    # chatter" and landed in Posture). Until the social classifier emits
    # a content-aware axis judgement, leave these unmapped and let them
    # surface in Other rather than misrepresent them as security posture.
    #   social_company_post       — 1st-party LinkedIn = recruitment / marketing / product / posture mix
    #   social_employee_chatter   — over-fires on customer complaints
    #   person_active_linkedin    — just "active on LinkedIn", not posture

    # ---- Maturity — capability built ----
    "security_leadership_hire": "maturity",
    "security_team_expansion": "maturity",
    "hiring_security": "maturity",
    "hiring_security_leadership": "maturity",
    "hiring_grc": "maturity",
    "social_hiring_buzz": "maturity",
    "person_new_in_role": "maturity",
    "person_promoted": "maturity",
    "person_changed_company": "maturity",
    "person_security_leader": "maturity",
    "person_grc_leader": "maturity",
    "person_technical_leader": "maturity",
    "certification_claim_detected": "maturity",
    "certification_iso_27001": "maturity",
    "certification_soc_2": "maturity",
    "certification_pci_dss": "maturity",
    "certification_cyber_essentials": "maturity",
    "certification_cyber_essentials_plus": "maturity",
    "certification_crest": "maturity",
    "certification_check": "maturity",
    "enterprise_customer_detected": "maturity",
    "regulated_customer_detected": "maturity",
    "public_sector_customer_detected": "maturity",
    "enterprise_page_detected": "maturity",
    "api_or_developer_platform": "maturity",
    # Job-keyword tells us what capability they're hiring for
    "job_mentions_soc2": "maturity",
    "job_mentions_iso27001": "maturity",
    "job_mentions_security_questionnaires": "maturity",
    "job_mentions_vendor_risk": "maturity",
    "job_mentions_audit": "maturity",
    "job_mentions_cloud_security": "maturity",
    "job_mentions_identity": "maturity",
    "job_mentions_siem": "maturity",
    "job_mentions_edr": "maturity",
    "job_mentions_vuln_mgmt": "maturity",
    "job_mentions_incident_response": "maturity",
    "job_mentions_devsecops": "maturity",
    # Pressure rollups — all describe capability buildout
    "compliance_workflow_pressure": "maturity",
    "security_operations_pressure": "maturity",
    "cloud_security_pressure": "maturity",
    "appsec_pressure": "maturity",
    "enterprise_readiness_pressure": "maturity",
    "customer_security_review_pressure": "maturity",
    "post_funding_security_maturity_push": "maturity",
    "upmarket_security_friction": "maturity",
    "compliance_operationalisation": "maturity",
    "security_team_scaling": "maturity",
    "cloud_security_scaling": "maturity",

    # Deliberately NOT mapped (axis=NULL, shows under "Other"):
    #   product_launch, new_partnership, hiring_sales_bd  — media/not-cyber
    #   compliance_gap, public_proof_not_visible          — absence-of-evidence framing
    #   social_company_mention, social_complaint,
    #   social_customer_review                            — too thin to anchor an axis
    #   person_prior_vendor_experience                    — profile attribute, not state-change
    #   reddit_*, linkedin_post_mention, twitter_mention,
    #   forum_mention                                     — legacy v1 social, deprecated
}


def derive_axis(signal_type: str) -> Optional[str]:
    """Return the cyber-lens axis for a signal_type, or None if unmapped.

    Unmapped types render under the "Other" section on the dossier.
    Add an entry to SIGNAL_AXIS to promote a type into a lens.
    """
    return SIGNAL_AXIS.get(signal_type)


class SignalData(BaseModel):
    """Minimum contract for ``signals.signal_data``.

    Built by ``signal_payload.build_signal_payload``; re-checked here so
    a new caller can't bypass the floor. Extra structured fields are
    allowed and pass through unchanged.
    """

    model_config = ConfigDict(extra="allow")

    headline: str = Field(..., min_length=1)
    detail: Optional[str] = None
    entity: Optional[str] = None
    occurred_at: Optional[str] = None

    @field_validator("headline")
    @classmethod
    def _headline_not_blank(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("headline must not be blank")
        return s


class SignalWrite(BaseModel):
    """One row destined for the signals table.

    Either `lead_id` (lead-scoped — a person/lead-level signal) OR
    `company_id` (account-scoped — a company-level signal with no
    person attached) MUST be present. Both is also valid: a
    lead-derived signal that carries the company link for queries.
    """

    org_id: str = Field(..., min_length=1)
    lead_id: Optional[str] = None
    company_id: Optional[str] = None
    signal_type: str = Field(..., min_length=1)
    signal_source: Optional[str] = None
    signal_data: SignalData
    signal_strength: float = Field(..., ge=0.0, le=1.0)
    expires_at: Optional[str] = None
    # Classification axes — see SIGNAL_DEFAULTS above. Callers may omit
    # and we'll fill from the map; explicit values win.
    signal_subject_type: Optional[str] = None
    inference_level: Optional[str] = None
    source_quality: Optional[str] = None
    # Cyber lens v1 axis — risk/posture/maturity/commercial. Auto-derived
    # from signal_type when not provided (see SIGNAL_AXIS map).
    axis: Optional[str] = None
    last_checked: Optional[str] = None
    snippet: Optional[str] = None

    @model_validator(mode="after")
    def _require_subject(self) -> "SignalWrite":
        if not (self.lead_id or self.company_id):
            # Sector-level org signals (social_listener) carry no lead
            # or company — they're scoped to the org's keyword space.
            # Distinguished by signal_subject_type='news'.
            if self.signal_subject_type != "news":
                raise ValueError(
                    "either lead_id or company_id must be set "
                    "(unless signal_subject_type='news' for org-scoped signals)"
                )
        return self

    @field_validator("signal_subject_type")
    @classmethod
    def _valid_subject(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in SUBJECT_TYPES:
            raise ValueError(f"signal_subject_type must be one of {sorted(SUBJECT_TYPES)}")
        return v

    @field_validator("inference_level")
    @classmethod
    def _valid_inference(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in INFERENCE_LEVELS:
            raise ValueError(f"inference_level must be one of {sorted(INFERENCE_LEVELS)}")
        return v

    @field_validator("source_quality")
    @classmethod
    def _valid_quality(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in SOURCE_QUALITIES:
            raise ValueError(f"source_quality must be one of {sorted(SOURCE_QUALITIES)}")
        return v

    @field_validator("axis")
    @classmethod
    def _valid_axis(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in AXIS_TYPES:
            raise ValueError(f"axis must be one of {sorted(AXIS_TYPES)}")
        return v


# Signal types that never reach the table. These carry no cyber-lens or
# commercial information for a rep — media noise (PL "launches"/
# "partners_with"), content-agnostic mentions, and pure activity markers.
# Dropping at write time (decision 2026-06-10) keeps the signals
# table lens-relevant; the raw vendor payloads remain in raw_artifacts
# so nothing is lost if a type is ever promoted back.
DROP_AT_WRITE: frozenset = frozenset({
    "product_launch",
    "new_partnership",
    "person_active_linkedin",
    # Social noise — chatter with no B2B-sales relevance (2026-06-11).
    # A rep cares about breach/funding/leadership/product-milestone/
    # vendor/security-hiring social; NOT generic mentions, customer
    # complaints, consumer reviews, employee small-talk, or first-party
    # marketing posts. The two-pass classifier still tags the useful
    # ones (social_breach_mention, social_funding_news, social_milestone,
    # social_leadership_change, social_vendor_relationship,
    # social_hiring_buzz) — only these residual buckets are dropped.
    "social_company_mention",
    "social_complaint",
    "social_customer_review",
    "social_employee_chatter",
    "social_company_post",
})


def _validate_one(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a validated row dict, or None if it should be rejected.

    Auto-fills signal_subject_type / inference_level / source_quality from
    SIGNAL_DEFAULTS when caller didn't set them. This keeps existing call
    sites working while the classification axes are populated on every
    new signal without per-site edits.
    """
    if row.get("signal_type") in DROP_AT_WRITE:
        logger.debug(
            "Dropped noise signal_type %r at write time (org=%s)",
            row.get("signal_type"), row.get("org_id"),
        )
        return None
    try:
        sw = SignalWrite.model_validate(row)
    except ValidationError as e:
        logger.warning(
            "Rejected malformed signal (org=%s lead=%s type=%s): %s",
            row.get("org_id"),
            row.get("lead_id"),
            row.get("signal_type"),
            e.errors(include_url=False, include_input=False),
        )
        return None
    if sw.signal_type not in KNOWN_SIGNAL_TYPES:
        logger.info(
            "Unknown signal_type %r — accepted but worth tracking (org=%s)",
            sw.signal_type,
            sw.org_id,
        )

    # Auto-classify if caller didn't.
    subj_def, infer_def, qual_def = classify_signal(sw.signal_type)
    if sw.signal_subject_type is None:
        sw.signal_subject_type = subj_def
    if sw.inference_level is None:
        sw.inference_level = infer_def
    if sw.source_quality is None:
        sw.source_quality = qual_def
    # Cyber lens axis — derive from signal_type when caller didn't set it.
    # Unmapped types stay None and surface under "Other" on the dossier.
    if sw.axis is None:
        sw.axis = derive_axis(sw.signal_type)

    return sw.model_dump(exclude_none=True)


def _normalize_headline(signal_data: Any) -> str:
    """Stable headline form used as the dedupe discriminator.

    Lowercase, whitespace-collapsed, first 200 chars. Two writes are
    "the same signal" iff their normalized headlines match — small
    casing/spacing tweaks don't break dedupe; genuinely new wording
    creates a new row.

    Special case for person_*_leader signal types: collapse by
    person_name only, with one name treated as duplicate of another
    when one is a prefix/substring of the other. Apollo sometimes
    writes the same CISO with varying name fidelity ("Jane" vs
    "Jane Smith"), producing duplicate signals byte-headline dedupe
    can't catch. The person-level collapse handles that.
    """
    if not isinstance(signal_data, dict):
        return ""
    h = signal_data.get("headline")
    if not isinstance(h, str):
        return ""
    return " ".join(h.lower().split())[:200]


_PERSON_LEADER_TYPES = frozenset({
    "person_security_leader",
    "person_grc_leader",
    "person_technical_leader",
    "person_commercial_leader",
})


def _normalize_person_name(signal_data: Any) -> str:
    """Lowercased, whitespace-collapsed person name for person_*_leader
    dedupe. Falls back to the headline when person_name isn't set."""
    if not isinstance(signal_data, dict):
        return ""
    raw = signal_data.get("person_name") or signal_data.get("headline") or ""
    if not isinstance(raw, str):
        return ""
    return " ".join(raw.lower().split())


def _person_names_match(a: str, b: str) -> bool:
    """Two person-name strings refer to the same person iff one is a
    word-aligned prefix/substring of the other. "mike" matches "mike
    bray" (same person, less precision); "mike" does NOT match "michael
    bray" (different first-name form so we let both stand)."""
    if not a or not b:
        return False
    if a == b:
        return True
    a_tokens = a.split()
    b_tokens = b.split()
    if not a_tokens or not b_tokens:
        return False
    # If one name's full token list is the head of the other's, same person.
    shorter, longer = (a_tokens, b_tokens) if len(a_tokens) < len(b_tokens) else (b_tokens, a_tokens)
    return longer[: len(shorter)] == shorter


def _drop_existing_duplicates(
    db: Client, rows: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Drop rows that duplicate an already-stored signal.

    Duplicate = same (org_id, account, signal_type, normalized headline)
    where ACCOUNT spans both axes:
      - company_id direct match, AND
      - every lead_id in this org for the company's domain (resolved
        via the leads table)

    The old lead_id-only key was the root cause of triple-written
    `new_leadership` rows on Monzo (2026-06-10): three different leads
    for the same company each ran their own PL enrichment, each wrote
    the same news event, none collided in dedupe because they keyed on
    different lead_ids. Now we collapse all sibling leads + the
    company_id into one account bucket so byte-identical headlines for
    the same company always collide on the second write.

    On lookup failure we fall back to writing everything; better to
    have the occasional duplicate than lose data on a transient DB
    blip.
    """
    if not rows:
        return rows

    # Step 1 — resolve every incoming row to a company_id (preferred)
    # OR a domain (fallback via leads.company_domain). The "account key"
    # is whatever uniquely identifies the company in this org.
    AccountKey = Tuple[str, str]  # ("company_id" | "domain", value)

    # Bulk-resolve lead_ids → domains in ONE query so we don't issue
    # one round-trip per lead.
    lead_ids_to_resolve = [
        r["lead_id"] for r in rows
        if r.get("lead_id") and not r.get("company_id")
    ]
    lead_to_domain: Dict[str, str] = {}
    if lead_ids_to_resolve:
        try:
            res = (
                db.table("leads")
                .select("id, company_domain")
                .in_("id", list(set(lead_ids_to_resolve)))
                .execute()
            )
            for row in (res.data or []):
                d = (row.get("company_domain") or "").strip().lower()
                if d and row.get("id"):
                    lead_to_domain[row["id"]] = d
        except Exception as e:
            logger.warning(
                "Dedupe lead→domain lookup failed: %s — falling back to lead_id keys", e,
            )

    def account_key(r: Dict[str, Any]) -> Optional[AccountKey]:
        if r.get("company_id"):
            return ("company_id", r["company_id"])
        if r.get("lead_id"):
            d = lead_to_domain.get(r["lead_id"])
            if d:
                return ("domain", d)
            # No domain resolution available — fall back to the
            # narrower lead_id key (better than nothing, matches
            # pre-2026-06-10 behaviour).
            return ("lead_id", r["lead_id"])
        return None

    # Group incoming rows by (org_id, account_key, signal_type)
    GroupKey = Tuple[str, str, str, str]  # org, key_kind, key_val, signal_type
    grouped: Dict[GroupKey, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        org_id = r.get("org_id")
        sig_type = r.get("signal_type")
        if not org_id or not sig_type:
            continue
        ak = account_key(r)
        if not ak:
            continue
        grouped[(org_id, ak[0], ak[1], sig_type)].append(r)

    # Step 2 — for each group, fetch existing headlines across BOTH
    # the company_id axis AND every sibling lead_id-for-domain axis.
    existing: set[Tuple[str, str, str, str, str]] = set()
    for (org_id, key_kind, key_val, sig_type), _ in grouped.items():
        try:
            q = (
                db.table("signals")
                .select("signal_data, lead_id, company_id")
                .eq("org_id", org_id)
                .eq("signal_type", sig_type)
                .limit(500)
            )
            if key_kind == "company_id":
                q = q.eq("company_id", key_val)
            elif key_kind == "lead_id":
                q = q.eq("lead_id", key_val)
            elif key_kind == "domain":
                # Resolve every lead in this org for this domain, then OR
                # match either company_id-for-this-domain OR any of those
                # lead_ids. Captures both write axes.
                sibling_lead_ids: List[str] = []
                try:
                    leads_res = (
                        db.table("leads")
                        .select("id")
                        .eq("org_id", org_id)
                        .ilike("company_domain", key_val)
                        .execute()
                    )
                    sibling_lead_ids = [
                        row["id"] for row in (leads_res.data or [])
                        if row.get("id")
                    ]
                except Exception:
                    pass
                co_id: Optional[str] = None
                try:
                    co_res = (
                        db.table("companies")
                        .select("id")
                        .ilike("domain", key_val)
                        .limit(1)
                        .execute()
                    )
                    if co_res.data:
                        co_id = co_res.data[0].get("id")
                except Exception:
                    pass
                or_clauses: List[str] = []
                if co_id:
                    or_clauses.append(f"company_id.eq.{co_id}")
                if sibling_lead_ids:
                    or_clauses.append(
                        f"lead_id.in.({','.join(sibling_lead_ids)})"
                    )
                if not or_clauses:
                    # Nothing to dedupe against — let rows through
                    continue
                q = q.or_(",".join(or_clauses))
            res = q.execute()
            for existing_row in (res.data or []):
                h = _normalize_headline(existing_row.get("signal_data"))
                if h:
                    existing.add((org_id, key_kind, key_val, sig_type, h))
                # For person_*_leader, ALSO index by person name so the
                # prefix-match collapser below can find sibling rows
                # written with different name fidelity (Apollo "Jane" vs
                # "Jane Smith" for the same CISO).
                if sig_type in _PERSON_LEADER_TYPES:
                    pn = _normalize_person_name(existing_row.get("signal_data"))
                    if pn:
                        existing.add(
                            (org_id, key_kind, key_val, sig_type, f"__person__{pn}")
                        )
        except Exception as e:
            logger.warning(
                "Dedupe lookup failed for %s=%s type=%s: %s — writing all rows",
                key_kind, key_val, sig_type, e,
            )
            # Don't add anything to `existing` for this group; rows fall through.

    # Also dedupe within the incoming batch itself — two writes of the
    # same headline in one call (rare but possible with parallel
    # detectors) shouldn't both land. Uses the same account_key resolver
    # so a batch containing both a lead-keyed AND company-keyed write
    # for the same headline collapses to one.
    seen_in_batch: set[Tuple[str, str, str, str, str]] = set()
    keep: List[Dict[str, Any]] = []
    dropped = 0
    for r in rows:
        org_id = r.get("org_id")
        sig_type = r.get("signal_type")
        if not org_id or not sig_type:
            keep.append(r)
            continue
        ak = account_key(r)
        if not ak:
            keep.append(r)  # news-scoped or unkeyed
            continue
        h = _normalize_headline(r.get("signal_data"))
        if not h:
            keep.append(r)  # blank headline — validator will reject
            continue
        key = (org_id, ak[0], ak[1], sig_type, h)
        # Person-aware secondary key: collapse "Jane" + "Jane Smith" for
        # the same company even though headlines differ.
        person_key: Optional[Tuple[str, str, str, str, str]] = None
        person_collision = False
        if sig_type in _PERSON_LEADER_TYPES:
            pn = _normalize_person_name(r.get("signal_data"))
            if pn:
                person_key = (org_id, ak[0], ak[1], sig_type, f"__person__{pn}")
                # First check exact-token equality via the existing set
                if person_key in existing or person_key in seen_in_batch:
                    person_collision = True
                else:
                    # Then check prefix/substring against any existing
                    # person name in the same bucket
                    prefix = f"__person__"
                    for ek in existing | seen_in_batch:
                        if (
                            len(ek) == 5
                            and ek[0] == org_id
                            and ek[1] == ak[0]
                            and ek[2] == ak[1]
                            and ek[3] == sig_type
                            and ek[4].startswith(prefix)
                        ):
                            other_name = ek[4][len(prefix):]
                            if _person_names_match(pn, other_name):
                                person_collision = True
                                break
        if key in existing or key in seen_in_batch or person_collision:
            dropped += 1
            continue
        seen_in_batch.add(key)
        if person_key is not None:
            seen_in_batch.add(person_key)
        keep.append(r)

    if dropped:
        logger.info(
            "Dedupe: dropped %d duplicate signal(s) already stored; writing %d new",
            dropped, len(keep),
        )
    return keep


# Buying-trigger signal types — fires signal.detected webhook only for
# these. The full signal universe is 50+ types but most are research
# context (social_company_mention, posture detections, job-keyword
# rollups) — useful inside Vera, but a customer doesn't want a
# webhook per chatter mention. Mirrors lib/dashboard/buyingTriggers.ts.
_WEBHOOK_FIRING_SIGNAL_TYPES = frozenset({
    "social_breach_mention", "breach_proximity", "breach_mention",
    "credential_leak", "credential_leak_supply_chain",
    "security_leadership_hire", "social_leadership_change", "new_leadership",
    "recently_funded", "social_funding_news", "recent_acquisition",
    "security_team_expansion", "hiring_security", "hiring_security_leadership",
    "hiring_grc",
    "social_vendor_relationship",
    "person_engaged_with_competitor", "person_new_in_role",
    "person_posted_relevant_topic", "person_engaged_with_topic",
})


def _fire_signal_detected_webhooks(db: Client, rows: List[Dict[str, Any]]) -> None:
    """Fan-out signal.detected webhook events for the buying-trigger
    subset of the rows just written. Best-effort — never raises."""
    try:
        from app.services.webhooks import enqueue_event
    except Exception:
        return

    # Per-org extension (2026-07-08): types the org weights >= 0.8 in
    # icp_config.signal_priorities also fire — e.g. cve_in_stack for an
    # exposure-selling org, excluded globally (anti-ambulance) but their
    # whole product. One
    # lookup per org per call, cached in-function.
    org_extra: Dict[str, frozenset] = {}

    def _allowed(org_id: str, signal_type: str) -> bool:
        if signal_type in _WEBHOOK_FIRING_SIGNAL_TYPES:
            return True
        if org_id not in org_extra:
            extras: frozenset = frozenset()
            try:
                res = (
                    db.table("organisations")
                    .select("icp_config")
                    .eq("id", org_id)
                    .maybe_single()
                    .execute()
                )
                pri = ((res.data or {}).get("icp_config") or {}).get("signal_priorities") or {}
                extras = frozenset(
                    t for t, w in pri.items()
                    if isinstance(w, (int, float)) and w >= 0.8
                )
            except Exception:
                pass
            org_extra[org_id] = extras
        return signal_type in org_extra[org_id]

    # company_id -> {domain, name}, one lookup per company per call, so
    # every payload carries a routable account (demand-gen ask, 2026-07-16
    # simulation: consumers were prose-parsing headlines to find the company).
    cid_cache: Dict[str, Dict[str, Any]] = {}

    def _company(cid: Optional[str]) -> Dict[str, Any]:
        if not cid:
            return {}
        if cid not in cid_cache:
            info: Dict[str, Any] = {}
            try:
                res = (
                    db.table("companies")
                    .select("domain, name")
                    .eq("id", cid)
                    .maybe_single()
                    .execute()
                )
                info = res.data or {}
            except Exception:
                pass
            cid_cache[cid] = info
        return cid_cache[cid]

    for r in rows:
        org_id = r.get("org_id")
        if not org_id:
            continue
        if not _allowed(org_id, r.get("signal_type") or ""):
            continue
        data = r.get("signal_data") or {}
        sd = data if isinstance(data, dict) else {}
        comp = _company(r.get("company_id"))
        payload = {
            "signal_type": r.get("signal_type"),
            "signal_subject_type": r.get("signal_subject_type"),
            "company_id": r.get("company_id"),
            "lead_id": r.get("lead_id"),
            "company_domain": (
                str(sd.get("company_domain") or sd.get("domain") or "").lower()
                or (comp.get("domain") or "").lower()
                or None
            ),
            "company_name": sd.get("company_name") or sd.get("name") or comp.get("name") or None,
            "person_name": str(sd.get("person_name") or "").strip() or None,
            "headline": (sd.get("headline") or "") or "",
            "url": (sd.get("url") or "") or "",
            "detected_at": r.get("detected_at"),
            "strength": r.get("signal_strength"),
        }
        try:
            enqueue_event(db, org_id, "signal.detected", payload)
        except Exception as e:
            logger.warning("signal.detected webhook enqueue failed: %s", e)


def insert_signal(db: Client, row: Dict[str, Any]) -> bool:
    """Validate + insert a single signal. Returns True on success.

    A rejected payload (empty headline, missing type, etc.) is logged
    and skipped — the caller does NOT see an exception, because
    enrichment_data remains the source of truth and one bad projection
    write must not abort the rest of the pipeline.

    Fires signal.detected webhook for buying-trigger signal types
    after successful insert.
    """
    cleaned = _validate_one(row)
    if cleaned is None:
        return False
    after_dedupe = _drop_existing_duplicates(db, [cleaned])
    if not after_dedupe:
        # Already stored — not a failure, just a no-op.
        return True
    try:
        db.table("signals").insert(after_dedupe[0]).execute()
        _fire_signal_detected_webhooks(db, after_dedupe)
        return True
    except Exception as e:
        logger.warning(
            "DB insert failed for signal %s (lead=%s): %s",
            cleaned.get("signal_type"),
            cleaned.get("lead_id"),
            e,
        )
        return False


def insert_signals(db: Client, rows: List[Dict[str, Any]]) -> int:
    """Validate + bulk-insert. Returns count of rows actually written.

    Dedupe runs after validation, before insert: rows whose
    (org, subject, signal_type, normalized headline) match an
    existing row are skipped. The returned count reflects *new*
    inserts only — duplicates aren't counted as failures.

    Fires signal.detected webhook for buying-trigger signal types in
    the inserted set.
    """
    cleaned = [r for r in (_validate_one(row) for row in rows) if r is not None]
    if not cleaned:
        return 0
    deduped = _drop_existing_duplicates(db, cleaned)
    if not deduped:
        return 0
    try:
        db.table("signals").insert(deduped).execute()
        _fire_signal_detected_webhooks(db, deduped)
        return len(deduped)
    except Exception as e:
        logger.warning(
            "DB bulk insert failed (%d signals): %s", len(deduped), e
        )
        return 0
