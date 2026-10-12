from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


# ---------------------------------------------------------------------------
# Status transition rules
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: Dict[str, List[str]] = {
    "new": ["qualified", "disqualified", "parked", "dead"],
    "qualified": ["sequenced", "parked", "dead"],
    "sequenced": ["replied", "parked", "dead", "bounced"],
    "replied": ["meeting", "parked", "dead"],
    "meeting": ["closed", "parked", "dead"],
    "parked": ["new", "qualified"],
    "bounced": ["dead"],
    "disqualified": ["new", "qualified"],
}

ALL_STATUSES = {"new", "qualified", "disqualified", "sequenced", "replied", "meeting", "closed", "dead", "parked", "bounced"}


# ---------------------------------------------------------------------------
# Signal write contract
# ---------------------------------------------------------------------------
# Canonical set of signal_type values any writer of the signals table must
# use. Adding a new type? Add it here AND to the signal_type CHECK
# constraint (in a new migration) so the DB rejects rogue writes too.
VALID_SIGNAL_TYPES = frozenset({
    # Cyber-specific extractors (enricher cyber phase)
    "security_leadership_hire",
    "security_team_expansion",
    "cve_in_stack",
    "compliance_gap",
    "breach_proximity",
    # Hiring + growth
    "hiring",
    "hiring_sales_bd",
    "hiring_security",
    "hiring_leadership",
    # PredictLeads news event categories + alias forms used by the play matcher
    "new_partnership",
    "new_leadership",
    "expansion",
    "recent_funding",
    "recently_funded",
    "product_launch",
    "product_launch_or_partnership",
    "recent_acquisition",
    "growing_tech_stack",
    # Generic business intel (manual /signals callers, brain tool output)
    "growth",
    "challenge",
    "vendor_review",
    "budget",
    "strategy_change",
    "event",
    "regulated_industry",
    # Catch-all - allowed but should be replaced with a sharper type
    "other",
})


class SignalWrite(BaseModel):
    """Strict shape for every row written to the signals table.

    Rejection here is intentional. Empty or unrecognised signals produce
    useless briefings and pollute the score. Better to drop and log than
    write a row the dashboard cannot render.
    """
    org_id: str
    lead_id: str
    signal_type: str
    signal_source: Optional[str] = None
    signal_data: Dict[str, Any]
    signal_strength: float = Field(ge=0.0, le=1.0)
    expires_at: Optional[str] = None

    @classmethod
    def _validate_payload_field(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        if not v:
            raise ValueError("signal_data is empty")
        if not (v.get("headline") or v.get("summary") or v.get("detail")):
            raise ValueError(
                "signal_data must carry at least one of: headline, summary, detail"
            )
        return v

    @classmethod
    def __get_validators__(cls):
        # Pydantic v2 uses field_validator; we want to support both v1 and v2
        # cleanly. Fall back to a model-level check at construction time.
        yield from super().__get_validators__()

    def model_post_init(self, __context):
        if self.signal_type not in VALID_SIGNAL_TYPES:
            raise ValueError(
                f"unknown signal_type {self.signal_type!r}; add it to "
                f"VALID_SIGNAL_TYPES and the DB CHECK constraint if it's a real new "
                f"category, or use 'other' as the catch-all"
            )
        self._validate_payload_field(self.signal_data)


# ---------------------------------------------------------------------------
# Shared / nested models
# ---------------------------------------------------------------------------

class LeadInput(BaseModel):
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    title: Optional[str] = None
    company_name: Optional[str] = None
    company_domain: Optional[str] = None
    company_size: Optional[str] = None
    company_industry: Optional[str] = None
    # company_location is the human-readable string (e.g. "Atlanta, Georgia, United States").
    # Built by the ingest caller from Apollo's real organisation HQ data.
    company_location: Optional[str] = None
    # Structured location fields for strict country-level matching against
    # icp_config.target_locations. Optional so older callers / non-Apollo
    # sources can omit them.
    company_country: Optional[str] = None
    company_city: Optional[str] = None
    company_state: Optional[str] = None
    linkedin_url: Optional[str] = None
    phone: Optional[str] = None
    apollo_id: Optional[str] = None
    source: str = "apollo"
    enrichment_status: Optional[str] = None  # "suggested" or None — defaults to "suggested"
    # Set only by the per-persona fan-out search: the buyer profile whose
    # Apollo query actually surfaced this person. Ground-truth attribution —
    # /discover writes it straight to leads.matched_persona_id at insert so
    # the persona is the search that found them, not a post-hoc guess. Other
    # callers omit these (stay None); the post-scoring
    # persona_matcher still refines attribution as before.
    sourced_persona_id: Optional[str] = None
    sourced_persona_name: Optional[str] = None


class SignalInput(BaseModel):
    lead_email: str
    signal_type: str
    signal_source: Optional[str] = None
    signal_data: Dict[str, Any] = Field(default_factory=dict)
    signal_strength: float = 0.5


class LinkedInPost(BaseModel):
    text: str
    date: Optional[str] = None
    likes: Optional[int] = None
    comments: Optional[int] = None


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class LeadIngestRequest(BaseModel):
    org_id: UUID
    leads: List[LeadInput]


class ManualLeadRequest(BaseModel):
    org_id: UUID
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    title: Optional[str] = None
    company_name: Optional[str] = None
    company_domain: Optional[str] = None
    company_size: Optional[str] = None
    company_industry: Optional[str] = None
    company_location: Optional[str] = None
    linkedin_url: Optional[str] = None
    phone: Optional[str] = None
    source: str = "manual"
    notes: Optional[str] = None


class StatusUpdateRequest(BaseModel):
    org_id: UUID
    status: str
    notes: Optional[str] = None


class ScoreRequest(BaseModel):
    org_id: UUID
    lead_ids: Optional[List[UUID]] = None


class ClassifyRequest(BaseModel):
    org_id: UUID
    lead_email: str
    campaign_id: Optional[str] = None
    campaign_name: Optional[str] = None
    reply_text: str
    reply_subject: Optional[str] = None
    reply_html: Optional[str] = None
    email_account: Optional[str] = None
    webhook_data: Optional[Dict[str, Any]] = None


class SignalCheckRequest(BaseModel):
    org_id: UUID
    signals: List[SignalInput]


class WebsiteEnrichRequest(BaseModel):
    org_id: UUID
    domains: List[str]
    enrichment_mode: str = "cyber_company_gtm"


class LinkedInContentRequest(BaseModel):
    org_id: UUID
    lead_id: UUID
    linkedin_url: Optional[str] = None
    recent_posts: Optional[List[LinkedInPost]] = None


class NewsEnrichRequest(BaseModel):
    org_id: UUID
    search_queries: List[str]


class TechStackRequest(BaseModel):
    org_id: UUID
    domains: List[str]


class JobsEnrichRequest(BaseModel):
    org_id: UUID
    domains: List[str]
    company_names: List[str]


class FullEnrichRequest(BaseModel):
    org_id: UUID
    lead_ids: List[UUID]
    enrichment_sources: List[str] = Field(
        default_factory=lambda: ["website", "tech_stack", "news", "jobs"]
    )
    enrichment_mode: str = "cyber_company_gtm"


class PushLeadsRequest(BaseModel):
    org_id: UUID
    campaign_id: str
    lead_ids: Optional[List[UUID]] = None
    min_score: float = 60.0


class SyncStatsRequest(BaseModel):
    org_id: UUID


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class IngestResponse(BaseModel):
    new_count: int
    duplicate_count: int
    new_lead_ids: List[str]
    suggestions_shown: Optional[int] = None
    suggestions_filtered: Optional[int] = None


class ScoreResult(BaseModel):
    lead_id: str
    lead_rating: str = ""  # hot, warm, cold
    rating_reason: str = ""
    buying_signals: List[str] = Field(default_factory=list)
    icp_fit: Dict[str, Any] = Field(default_factory=dict)
    recommended_action: Optional[str] = None
    best_approach_angle: str = ""
    # Backwards compatibility
    icp_score: float = 0
    timing_score: float = 0
    total_score: float = 0
    reasoning: str = ""


class ScoreResponse(BaseModel):
    results: List[ScoreResult]
    total_scored: int
    errors: List[str] = Field(default_factory=list)


class ExtractedIntent(BaseModel):
    buying_timeline: Optional[str] = None
    competitor_mentioned: Optional[str] = None
    objection: Optional[str] = None
    referral_to: Optional[str] = None
    follow_up_date: Optional[str] = None
    sentiment: Optional[str] = None
    key_quote: Optional[str] = None


class ClassifyResponse(BaseModel):
    classification: str
    confidence: float
    extracted_intent: ExtractedIntent
    suggested_next_action: Optional[str] = None
    lead_id: Optional[str] = None
    lead_status_updated: Optional[str] = None


class SignalCheckResponse(BaseModel):
    signals_registered: int
    leads_updated: List[Dict[str, Any]]
    errors: List[str] = Field(default_factory=list)


class PushLeadsResponse(BaseModel):
    pushed_count: int
    skipped_count: int
    pushed_lead_ids: List[str]
    errors: List[str] = Field(default_factory=list)


class WeeklyStat(BaseModel):
    week: str
    leads_sourced: int = 0
    leads_qualified: int = 0
    emails_sent: int = 0
    replies: int = 0
    positive_replies: int = 0
    meetings: int = 0


class ActivityItem(BaseModel):
    type: str
    lead_name: Optional[str] = None
    company: Optional[str] = None
    detail: Optional[str] = None
    timestamp: Optional[str] = None


class TopLead(BaseModel):
    name: Optional[str] = None
    title: Optional[str] = None
    company: Optional[str] = None
    score: float = 0
    signals: List[str] = Field(default_factory=list)
    status: Optional[str] = None


class DashboardSummary(BaseModel):
    total_leads: int = 0
    qualified_leads: int = 0
    in_sequence: int = 0
    replies_total: int = 0
    replies_positive: int = 0
    meetings_booked: int = 0
    pipeline_value: float = 0


class DashboardResponse(BaseModel):
    org_id: str
    org_name: Optional[str] = None
    summary: DashboardSummary
    weekly_stats: List[WeeklyStat] = Field(default_factory=list)
    recent_activity: List[ActivityItem] = Field(default_factory=list)
    top_leads: List[TopLead] = Field(default_factory=list)
    signal_breakdown: Dict[str, int] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str
    database: str
    timestamp: str
    last_scoring_run: Optional[str] = None
    last_reply_classified: Optional[str] = None
    # When the daily monitor pass last completed (system_heartbeat) —
    # powers the public status page. Read-only; never triggers alerts.
    last_monitor_beat: Optional[str] = None


class WebhookResponse(BaseModel):
    status: str
    event_type: Optional[str] = None
    detail: Optional[str] = None


# ---------------------------------------------------------------------------
# Content engine models
# ---------------------------------------------------------------------------

class LinkedInPostInput(BaseModel):
    text: str
    date: Optional[str] = None
    likes: Optional[int] = None
    comments: Optional[int] = None


class StyleAnalysisRequest(BaseModel):
    org_id: str
    person_name: str
    person_context: Optional[str] = None
    linkedin_url: Optional[str] = None
    posts: List[LinkedInPostInput]


class VoiceAnalysisRequest(BaseModel):
    org_id: str
    client_name: str
    company_name: str
    posts: List[LinkedInPostInput]


class ContentPreferencesRequest(BaseModel):
    org_id: str
    preferences: Dict[str, Any]


class WatchlistAddRequest(BaseModel):
    org_id: str
    company_name: str
    company_domain: str
    target_contact_name: Optional[str] = None
    target_contact_title: Optional[str] = None
    target_contact_linkedin: Optional[str] = None
    notes: Optional[str] = None
    priority: Optional[str] = "medium"
    # Optional link to a lead in the leads table so the existing enrichment
    # and GTM briefing can seed the watchlist baseline snapshot.
    lead_id: Optional[str] = None


class WatchlistDeleteRequest(BaseModel):
    org_id: str


class WatchlistMonitorRequest(BaseModel):
    org_id: str


class WatchlistRefreshRequest(BaseModel):
    org_id: str
    domain: str


class WatchlistAlertUpdateRequest(BaseModel):
    org_id: str
    status: str


class WatchlistContactPostsRequest(BaseModel):
    org_id: str
    posts: List[LinkedInPostInput]


class ScrapeAndAnalyseStyleRequest(BaseModel):
    org_id: str
    person_name: str
    person_context: Optional[str] = None
    linkedin_url: str


class ScrapeAndAnalyseVoiceRequest(BaseModel):
    org_id: str
    client_name: str
    company_name: str
    linkedin_url: str


class ContentGenerateRequest(BaseModel):
    org_id: str


# ---------------------------------------------------------------------------
# Threat intel models
# ---------------------------------------------------------------------------

class ThreatMonitorRequest(BaseModel):
    since_hours: Optional[int] = 48


class ThreatAlertUpdateRequest(BaseModel):
    org_id: str
    status: str
