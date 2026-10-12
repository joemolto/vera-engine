-- Vera baseline schema.
--
-- Generated 2026-10-03 from the live schema (structure only, no data).
-- Replaces the 56 historical migrations, which could not rebuild the schema
-- from zero: 22 tables were created by hand in the Supabase dashboard.
--
-- Apply to a fresh Supabase project (SQL editor, `supabase db push`, or psql).

create extension if not exists pgcrypto with schema extensions;
create extension if not exists "uuid-ossp" with schema extensions;

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

create sequence public.rate_limit_events_id_seq;

create table public.action_outcomes (
  id uuid default gen_random_uuid() not null,
  org_id uuid,
  action_plan_id uuid,
  lead_id uuid,
  action_type text not null,
  channel text,
  message_angle text,
  signal_that_triggered text,
  outcome text,
  outcome_detail jsonb,
  days_to_outcome integer,
  created_at timestamp with time zone default now(),
  feedback_source text,
  target_type text,
  target_id uuid,
  given_by_user_id uuid,
  notes text
);

create table public.action_plans (
  id uuid default gen_random_uuid() not null,
  org_id uuid,
  lead_id uuid,
  actions jsonb not null,
  reasoning text,
  lead_score numeric,
  lead_signals jsonb,
  message_angle text,
  created_at timestamp with time zone default now()
);

create table public.activity_log (
  id uuid default gen_random_uuid() not null,
  org_id uuid,
  lead_id uuid,
  activity_type text not null,
  activity_data jsonb default '{}'::jsonb,
  created_at timestamp with time zone default now()
);

create table public.api_keys (
  id uuid default gen_random_uuid() not null,
  org_id uuid not null,
  name text not null,
  prefix text not null,
  key_hash text not null,
  last_used_at timestamp with time zone,
  request_count integer default 0 not null,
  created_by uuid,
  created_at timestamp with time zone default now() not null,
  revoked_at timestamp with time zone
);

create table public.api_request_log (
  id uuid default gen_random_uuid() not null,
  api_key_id uuid not null,
  org_id uuid not null,
  method text not null,
  path text not null,
  status_code integer,
  duration_ms integer,
  created_at timestamp with time zone default now() not null
);

create table public.apollo_people_cache (
  id uuid default gen_random_uuid() not null,
  org_id uuid not null,
  apollo_id text not null,
  company_domain text,
  person_data jsonb not null,
  enriched_at timestamp with time zone default now() not null,
  lead_id uuid
);

create table public.campaigns (
  id uuid default gen_random_uuid() not null,
  org_id uuid,
  name text not null,
  instantly_campaign_id text,
  persona text,
  status text default 'active'::text,
  leads_added integer default 0,
  emails_sent integer default 0,
  replies_received integer default 0,
  positive_replies integer default 0,
  meetings_booked integer default 0,
  created_at timestamp with time zone default now(),
  updated_at timestamp with time zone default now()
);

create table public.cancellation_feedback (
  id uuid default gen_random_uuid() not null,
  org_id uuid,
  reasons text[] default '{}'::text[] not null,
  details text,
  tier text,
  created_at timestamp with time zone default now() not null
);

create table public.chat_memories (
  id uuid default gen_random_uuid() not null,
  user_id uuid not null,
  org_id uuid not null,
  content text not null,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null
);

create table public.chat_messages (
  id uuid default gen_random_uuid() not null,
  session_id uuid not null,
  role text not null,
  content jsonb not null,
  tool_name text,
  tool_call_id text,
  created_at timestamp with time zone default now() not null
);

create table public.chat_sessions (
  id uuid default gen_random_uuid() not null,
  org_id uuid not null,
  user_id uuid not null,
  title text,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null,
  expires_at timestamp with time zone default (now() + '30 days'::interval) not null
);

create table public.companies (
  id uuid default gen_random_uuid() not null,
  domain text not null,
  name text,
  industry text,
  size_band text,
  hq_country text,
  hq_city text,
  founded_year integer,
  linkedin_url text,
  description text,
  technologies jsonb default '[]'::jsonb,
  compliance jsonb default '{}'::jsonb,
  threat_alerts jsonb default '[]'::jsonb,
  predictleads_data jsonb default '{}'::jsonb,
  apollo_data jsonb default '{}'::jsonb,
  deep_research_data jsonb default '{}'::jsonb,
  last_enriched_at timestamp with time zone,
  enrichment_version integer default 1,
  created_at timestamp with time zone default now(),
  updated_at timestamp with time zone default now(),
  ai_quality_pass jsonb,
  key_contacts jsonb,
  key_contacts_refreshed_at timestamp with time zone,
  person_intel_enabled text default 'off'::text not null,
  last_person_intel_at timestamp with time zone,
  why_this_matters jsonb,
  security_team_profile jsonb,
  profile_score jsonb,
  credential_exposure jsonb,
  job_board jsonb,
  employee_count integer,
  country text,
  tech_stack jsonb default '[]'::jsonb not null,
  has_security_team_growth boolean default false not null,
  has_recent_funding boolean default false not null,
  has_compliance_signal boolean default false not null,
  enrichment_count integer default 0 not null,
  watched_by_org_count integer default 0 not null
);

create table public.company_intel (
  id uuid default gen_random_uuid() not null,
  company_id uuid not null,
  signal_type text not null,
  signal_data jsonb default '{}'::jsonb not null,
  source text not null,
  first_observed_at timestamp with time zone default now() not null,
  latest_observed_at timestamp with time zone default now() not null,
  observed_count integer default 1 not null
);

create table public.company_job_postings (
  id uuid default gen_random_uuid() not null,
  company_id uuid not null,
  source text not null,
  external_id text not null,
  title text not null,
  location text,
  url text,
  first_published timestamp with time zone,
  first_seen timestamp with time zone default now() not null,
  last_seen timestamp with time zone default now() not null,
  closed_at timestamp with time zone,
  is_security boolean,
  role_category text,
  created_at timestamp with time zone default now() not null,
  is_grc boolean
);

create table public.company_threat_surface (
  id uuid default gen_random_uuid() not null,
  company_id uuid not null,
  threat_intel_id uuid not null,
  relevance_score numeric(3,2),
  matched_via text,
  first_observed_at timestamp with time zone default now() not null,
  org_alert_count integer default 0 not null
);

create table public.competitor_feed (
  id uuid default gen_random_uuid() not null,
  org_id uuid not null,
  competitor text not null,
  post_url text not null,
  post_text text,
  posted_at timestamp with time zone,
  engagement jsonb default '{}'::jsonb not null,
  security_engagers jsonb default '[]'::jsonb not null,
  engager_count integer default 0 not null,
  fetched_at timestamp with time zone default now() not null,
  competitor_domain text
);

create table public.content_suggestions (
  id uuid default gen_random_uuid() not null,
  org_id uuid,
  week_label text not null,
  suggestions jsonb default '[]'::jsonb not null,
  industry_context jsonb default '{}'::jsonb,
  pipeline_insights jsonb default '{}'::jsonb,
  generated_at timestamp with time zone default now(),
  status text default 'new'::text
);

create table public.credit_ledger (
  id uuid default gen_random_uuid() not null,
  org_id uuid not null,
  action text not null,
  units integer not null,
  related_id text,
  metadata jsonb default '{}'::jsonb not null,
  created_by uuid,
  created_at timestamp with time zone default now() not null
);

create table public.credit_topups (
  id uuid default gen_random_uuid() not null,
  org_id uuid not null,
  stripe_checkout_session_id text not null,
  stripe_payment_intent_id text,
  credits integer not null,
  amount_cents integer not null,
  currency text default 'usd'::text not null,
  status text default 'pending'::text not null,
  created_at timestamp with time zone default now(),
  completed_at timestamp with time zone
);

create table public.enrichment_cache (
  id uuid default gen_random_uuid() not null,
  domain text not null,
  enrichment_type text not null,
  enrichment_mode text,
  result_data jsonb default '{}'::jsonb,
  fetched_at timestamp with time zone default now(),
  expires_at timestamp with time zone default (now() + '7 days'::interval)
);

create table public.free_report_leads (
  email text not null,
  domain text not null,
  captured_at timestamp with time zone default now() not null,
  step integer default 0 not null,
  last_sent_at timestamp with time zone default now() not null,
  unsubscribed boolean default false not null,
  updated_at timestamp with time zone default now() not null,
  unsub_token uuid default gen_random_uuid() not null
);

create table public.free_report_requests (
  id uuid default gen_random_uuid() not null,
  created_at timestamp with time zone default now() not null,
  domain text not null,
  email text,
  ip text,
  user_agent text,
  emailed boolean default false not null
);

create table public.group_members (
  id uuid default gen_random_uuid() not null,
  group_id uuid not null,
  org_id uuid not null,
  entity_type text not null,
  entity_ref text not null,
  added_at timestamp with time zone default now() not null
);

create table public.groups (
  id uuid default gen_random_uuid() not null,
  org_id uuid not null,
  name text not null,
  entity_type text not null,
  mode text default 'manual'::text not null,
  rules jsonb default '{}'::jsonb not null,
  notify boolean default false not null,
  created_by uuid,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null,
  smart_snapshot jsonb
);

create table public.lead_outcomes (
  id uuid default gen_random_uuid() not null,
  org_id uuid not null,
  lead_id uuid not null,
  company_id uuid,
  scored_signals jsonb default '[]'::jsonb not null,
  score_at_time numeric(5,2),
  matched_play text,
  reply_classification text,
  final_status text,
  outcome text,
  created_at timestamp with time zone default now() not null
);

create table public.leads (
  id uuid default gen_random_uuid() not null,
  org_id uuid,
  email text not null,
  first_name text,
  last_name text,
  title text,
  company_name text,
  company_domain text,
  company_size text,
  company_industry text,
  company_location text,
  linkedin_url text,
  phone text,
  source text default 'apollo'::text,
  signals jsonb default '[]'::jsonb,
  signal_count integer default 0,
  signal_score numeric(5,2) default 0,
  icp_score numeric(5,2) default 0,
  timing_score numeric(5,2) default 0,
  total_score numeric(5,2) default 0,
  score_reasoning text,
  scored_at timestamp with time zone,
  enrichment_data jsonb default '{}'::jsonb,
  enriched_at timestamp with time zone,
  enrichment_sources_completed text[] default '{}'::text[],
  enrichment_version integer default 0,
  last_enrichment_error text,
  status text default 'new'::text,
  instantly_campaign_id text,
  instantly_lead_id text,
  apollo_id text,
  created_at timestamp with time zone default now(),
  updated_at timestamp with time zone default now(),
  data_status text default 'unverified'::text,
  data_status_note text,
  linkedin_verified_at timestamp with time zone,
  original_company_name text,
  original_title text,
  lead_rating text default 'unrated'::text,
  rating_reason text,
  recommended_action text,
  buying_signals jsonb default '[]'::jsonb,
  best_approach_angle text,
  source_type text default 'apollo'::text,
  batch_id uuid,
  enrichment_status text default 'enriched'::text,
  suggestion_reason text,
  rejection_reason text,
  pre_enrichment_data jsonb,
  discovery_batch_id text,
  discovery_date timestamp with time zone,
  times_discovered integer default 0,
  last_discovered_at timestamp with time zone,
  previous_pre_enrichment_data jsonb,
  email_confidence integer,
  company_id uuid,
  matched_persona_id uuid,
  first_hot_at timestamp with time zone,
  outreach_at timestamp with time zone,
  revenue_potential jsonb,
  signal_summary jsonb,
  company_revenue jsonb,
  photo_url text,
  person_monitoring boolean default false not null,
  last_person_monitor_at timestamp with time zone,
  email_verified boolean,
  email_domain_mismatch boolean,
  email_source text
);

create table public.market_snapshots (
  id uuid default gen_random_uuid() not null,
  org_id uuid not null,
  captured_at timestamp with time zone default now() not null,
  data jsonb not null,
  market_id uuid
);

create table public.markets (
  id uuid default gen_random_uuid() not null,
  org_id uuid not null,
  name text not null,
  is_primary boolean default false not null,
  active boolean default true not null,
  definition jsonb default '{}'::jsonb not null,
  avg_deal_size integer,
  currency text,
  created_at timestamp with time zone default now() not null
);

create table public.mcp_api_keys (
  id uuid default gen_random_uuid() not null,
  org_id uuid not null,
  name text not null,
  key_hash text not null,
  key_prefix text not null,
  created_at timestamp with time zone default now() not null,
  last_used_at timestamp with time zone,
  revoked_at timestamp with time zone,
  created_by uuid
);

create table public.org_credits (
  id uuid default gen_random_uuid() not null,
  org_id uuid not null,
  month text not null,
  included integer default 0 not null,
  purchased integer default 0 not null,
  used integer default 0 not null,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null
);

create table public.org_industry_cache (
  domain text not null,
  industry text,
  keywords jsonb,
  looked_up_at timestamp with time zone default now() not null
);

create table public.org_integrations (
  id uuid default gen_random_uuid() not null,
  org_id uuid not null,
  provider text not null,
  status text default 'connected'::text not null,
  account_label text,
  channel text,
  meta jsonb default '{}'::jsonb not null,
  secret text,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null
);

create table public.org_users (
  id uuid default gen_random_uuid() not null,
  user_id uuid,
  org_id uuid,
  role text default 'viewer'::text,
  email text not null,
  created_at timestamp with time zone default now()
);

create table public.organisations (
  id uuid default gen_random_uuid() not null,
  name text not null,
  domain text,
  industry text default 'cybersecurity'::text,
  icp_config jsonb default '{}'::jsonb,
  settings jsonb default '{}'::jsonb,
  created_at timestamp with time zone default now(),
  updated_at timestamp with time zone default now(),
  content_config jsonb default '{}'::jsonb,
  client_context jsonb default '{}'::jsonb,
  tier text default 'scout'::text,
  monthly_enrichment_limit integer default 100,
  billing_cycle_start integer default 1,
  icp_config_previous jsonb,
  stripe_customer_id text,
  stripe_subscription_id text,
  subscription_status text,
  subscription_tier text,
  subscription_cycle text,
  subscription_current_period_end timestamp with time zone,
  integration_fee_paid_at timestamp with time zone,
  persona_limit_override integer,
  seat_count integer default 1 not null,
  brain_mode text default 'auto'::text not null,
  recent_digest jsonb,
  recent_digest_at timestamp with time zone,
  capacity_packs integer default 0 not null,
  retention_discount_redeemed_at timestamp with time zone
);

create table public.rate_limit_events (
  id bigint default nextval('rate_limit_events_id_seq'::regclass) not null,
  org_id uuid not null,
  bucket_key text not null,
  event_at timestamp with time zone default now() not null
);

alter sequence public.rate_limit_events_id_seq owned by public.rate_limit_events.id;

create table public.raw_artifacts (
  id uuid default gen_random_uuid() not null,
  domain text not null,
  source text not null,
  source_endpoint text,
  content_hash text not null,
  storage_path text not null,
  size_bytes integer not null,
  content_type text default 'application/json'::text,
  org_id uuid,
  lead_id uuid,
  company_id uuid,
  created_at timestamp with time zone default now(),
  parsed_at timestamp with time zone,
  parsed_version integer
);

create table public.relevance_feedback (
  id uuid default gen_random_uuid() not null,
  org_id uuid not null,
  entity_type text not null,
  entity_ref text,
  company_domain text,
  label text,
  title text,
  surface text,
  reason text default 'not_relevant'::text not null,
  created_at timestamp with time zone default now() not null
);

create table public.replies (
  id uuid default gen_random_uuid() not null,
  org_id uuid,
  lead_id uuid,
  lead_email text not null,
  campaign_id text,
  campaign_name text,
  reply_text text not null,
  reply_subject text,
  reply_html text,
  classification text,
  confidence numeric(3,2),
  extracted_intent jsonb default '{}'::jsonb,
  forwarded_to text,
  forwarded_at timestamp with time zone,
  received_at timestamp with time zone default now(),
  classified_at timestamp with time zone,
  instantly_webhook_data jsonb default '{}'::jsonb
);

create table public.schema_migrations (
  filename text not null,
  applied_at timestamp with time zone default now() not null
);

create table public.signals (
  id uuid default gen_random_uuid() not null,
  org_id uuid,
  lead_id uuid,
  signal_type text not null,
  signal_source text,
  signal_data jsonb default '{}'::jsonb,
  signal_strength numeric(3,2) default 0.5,
  detected_at timestamp with time zone default now(),
  expires_at timestamp with time zone,
  signal_subject_type text default 'account'::text not null,
  inference_level text default 'observed'::text not null,
  source_quality text default 'third_party'::text not null,
  last_checked timestamp with time zone default now(),
  snippet text,
  company_id uuid,
  axis text
);

create table public.signup_attempts (
  id uuid default gen_random_uuid() not null,
  ip text,
  created_at timestamp with time zone default now() not null
);

create table public.strategy_memory (
  id uuid default gen_random_uuid() not null,
  org_id uuid,
  week_starting date not null,
  performance_summary jsonb,
  strategy_adjustments jsonb,
  scoring_weight_changes jsonb,
  best_performing_signals jsonb,
  worst_performing_signals jsonb,
  recommendations text,
  created_at timestamp with time zone default now()
);

create table public.system_heartbeat (
  key text not null,
  beat_at timestamp with time zone default now() not null,
  detail jsonb default '{}'::jsonb not null
);

create table public.tech_category (
  name text not null,
  display_name text,
  category text default 'other'::text not null,
  source text default 'pending'::text not null,
  confidence text,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null
);

create table public.threat_alerts (
  id uuid default gen_random_uuid() not null,
  org_id uuid,
  threat_intel_id uuid,
  relevance_reason text not null,
  affected_icp_industries jsonb default '[]'::jsonb,
  affected_watchlist_companies jsonb default '[]'::jsonb,
  sales_angle text,
  suggested_outreach text,
  content_hook text,
  urgency text default 'medium'::text,
  status text default 'new'::text,
  detected_at timestamp with time zone default now(),
  seen_at timestamp with time zone,
  actioned_at timestamp with time zone
);

create table public.threat_intel (
  id uuid default gen_random_uuid() not null,
  source text not null,
  source_id text,
  title text not null,
  description text,
  severity text,
  url text,
  published_at timestamp with time zone,
  affected_industries jsonb default '[]'::jsonb,
  affected_technologies jsonb default '[]'::jsonb,
  affected_vendors jsonb default '[]'::jsonb,
  ai_summary text,
  sales_relevance jsonb default '{}'::jsonb,
  fetched_at timestamp with time zone default now()
);

create table public.title_relevance (
  archetype text not null,
  title_norm text not null,
  score real not null,
  relevant boolean not null,
  reason text,
  source text default 'llm'::text,
  created_at timestamp with time zone default now() not null
);

create table public.upload_batches (
  id uuid default gen_random_uuid() not null,
  org_id uuid,
  uploaded_by uuid,
  filename text,
  total_leads integer default 0 not null,
  enriched_count integer default 0 not null,
  disqualified_count integer default 0 not null,
  failed_count integer default 0 not null,
  status text default 'processing'::text not null,
  created_at timestamp with time zone default now(),
  completed_at timestamp with time zone,
  lead_ids jsonb default '[]'::jsonb,
  leads_created integer default 0,
  duplicates integer default 0,
  errors integer default 0,
  error_details jsonb default '[]'::jsonb,
  leads_duplicate integer default 0,
  leads_failed integer default 0,
  leads_enriched integer default 0,
  leads_scored integer default 0,
  leads_pending integer default 0,
  leads_total integer default 0
);

create table public.usage_tracking (
  id uuid default gen_random_uuid() not null,
  org_id uuid not null,
  month_year text not null,
  enrichments_used integer default 0,
  enrichments_limit integer default 100 not null,
  discovery_scans_used integer default 0,
  csv_uploads_used integer default 0,
  last_enrichment_at timestamp with time zone,
  limit_hit_at timestamp with time zone,
  notified_at_80_pct boolean default false,
  notified_at_100_pct boolean default false,
  created_at timestamp with time zone default now(),
  updated_at timestamp with time zone default now()
);

create table public.waitlist (
  id uuid default gen_random_uuid() not null,
  email text not null,
  signed_up_at timestamp with time zone default now(),
  source text default 'landing_page'::text,
  notes text
);

create table public.watchlist (
  id uuid default gen_random_uuid() not null,
  org_id uuid,
  company_name text not null,
  company_domain text not null,
  target_contact_name text,
  target_contact_title text,
  target_contact_linkedin text,
  notes text,
  priority text default 'medium'::text,
  latest_snapshot jsonb default '{}'::jsonb,
  last_monitored_at timestamp with time zone,
  monitoring_active boolean default true,
  created_at timestamp with time zone default now(),
  updated_at timestamp with time zone default now(),
  lead_id uuid,
  next_monitor_at timestamp with time zone,
  status text default 'active'::text not null,
  dismissed_at timestamp with time zone,
  suggested_reasons jsonb,
  market_id uuid
);

create table public.watchlist_alerts (
  id uuid default gen_random_uuid() not null,
  org_id uuid,
  watchlist_id uuid,
  alert_type text not null,
  alert_title text not null,
  alert_summary text not null,
  alert_data jsonb default '{}'::jsonb,
  suggested_action text,
  approach_angle text,
  urgency text default 'medium'::text,
  status text default 'new'::text,
  detected_at timestamp with time zone default now(),
  seen_at timestamp with time zone,
  actioned_at timestamp with time zone,
  signal_subject_type text default 'account'::text not null,
  inference_level text default 'observed'::text not null,
  source_quality text default 'third_party'::text not null,
  last_checked timestamp with time zone default now(),
  snippet text
);

create table public.webhook_deliveries (
  id uuid default gen_random_uuid() not null,
  endpoint_id uuid not null,
  org_id uuid not null,
  event_type text not null,
  payload jsonb not null,
  status text default 'pending'::text not null,
  attempt_count integer default 0 not null,
  next_attempt_at timestamp with time zone default now() not null,
  response_status integer,
  response_body text,
  duration_ms integer,
  created_at timestamp with time zone default now() not null,
  delivered_at timestamp with time zone,
  last_attempt_at timestamp with time zone
);

create table public.webhook_endpoints (
  id uuid default gen_random_uuid() not null,
  org_id uuid not null,
  url text not null,
  secret text not null,
  events text[] default '{}'::text[] not null,
  name text default ''::text not null,
  last_delivered_at timestamp with time zone,
  last_failed_at timestamp with time zone,
  consecutive_failures integer default 0 not null,
  active boolean default true not null,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null
);

create table public.website_scan_cache (
  domain text not null,
  scan jsonb not null,
  scanned_at timestamp with time zone default now() not null
);

-- ---------------------------------------------------------------------------
-- Primary keys
-- ---------------------------------------------------------------------------

alter table public.action_outcomes add constraint action_outcomes_pkey PRIMARY KEY (id);
alter table public.action_plans add constraint action_plans_pkey PRIMARY KEY (id);
alter table public.activity_log add constraint activity_log_pkey PRIMARY KEY (id);
alter table public.api_keys add constraint api_keys_pkey PRIMARY KEY (id);
alter table public.api_request_log add constraint api_request_log_pkey PRIMARY KEY (id);
alter table public.apollo_people_cache add constraint apollo_people_cache_pkey PRIMARY KEY (id);
alter table public.campaigns add constraint campaigns_pkey PRIMARY KEY (id);
alter table public.cancellation_feedback add constraint cancellation_feedback_pkey PRIMARY KEY (id);
alter table public.chat_memories add constraint chat_memories_pkey PRIMARY KEY (id);
alter table public.chat_messages add constraint chat_messages_pkey PRIMARY KEY (id);
alter table public.chat_sessions add constraint chat_sessions_pkey PRIMARY KEY (id);
alter table public.companies add constraint companies_pkey PRIMARY KEY (id);
alter table public.company_intel add constraint company_intel_pkey PRIMARY KEY (id);
alter table public.company_job_postings add constraint company_job_postings_pkey PRIMARY KEY (id);
alter table public.company_threat_surface add constraint company_threat_surface_pkey PRIMARY KEY (id);
alter table public.competitor_feed add constraint competitor_feed_pkey PRIMARY KEY (id);
alter table public.content_suggestions add constraint content_suggestions_pkey PRIMARY KEY (id);
alter table public.credit_ledger add constraint credit_ledger_pkey PRIMARY KEY (id);
alter table public.credit_topups add constraint credit_topups_pkey PRIMARY KEY (id);
alter table public.enrichment_cache add constraint enrichment_cache_pkey PRIMARY KEY (id);
alter table public.free_report_leads add constraint free_report_leads_pkey PRIMARY KEY (email);
alter table public.free_report_requests add constraint free_report_requests_pkey PRIMARY KEY (id);
alter table public.group_members add constraint group_members_pkey PRIMARY KEY (id);
alter table public.groups add constraint groups_pkey PRIMARY KEY (id);
alter table public.lead_outcomes add constraint lead_outcomes_pkey PRIMARY KEY (id);
alter table public.leads add constraint leads_pkey PRIMARY KEY (id);
alter table public.market_snapshots add constraint market_snapshots_pkey PRIMARY KEY (id);
alter table public.markets add constraint markets_pkey PRIMARY KEY (id);
alter table public.mcp_api_keys add constraint mcp_api_keys_pkey PRIMARY KEY (id);
alter table public.org_credits add constraint org_credits_pkey PRIMARY KEY (id);
alter table public.org_industry_cache add constraint org_industry_cache_pkey PRIMARY KEY (domain);
alter table public.org_integrations add constraint org_integrations_pkey PRIMARY KEY (id);
alter table public.org_users add constraint org_users_pkey PRIMARY KEY (id);
alter table public.organisations add constraint organisations_pkey PRIMARY KEY (id);
alter table public.rate_limit_events add constraint rate_limit_events_pkey PRIMARY KEY (id);
alter table public.raw_artifacts add constraint raw_artifacts_pkey PRIMARY KEY (id);
alter table public.relevance_feedback add constraint relevance_feedback_pkey PRIMARY KEY (id);
alter table public.replies add constraint replies_pkey PRIMARY KEY (id);
alter table public.schema_migrations add constraint schema_migrations_pkey PRIMARY KEY (filename);
alter table public.signals add constraint signals_pkey PRIMARY KEY (id);
alter table public.signup_attempts add constraint signup_attempts_pkey PRIMARY KEY (id);
alter table public.strategy_memory add constraint strategy_memory_pkey PRIMARY KEY (id);
alter table public.system_heartbeat add constraint system_heartbeat_pkey PRIMARY KEY (key);
alter table public.tech_category add constraint tech_category_pkey PRIMARY KEY (name);
alter table public.threat_alerts add constraint threat_alerts_pkey PRIMARY KEY (id);
alter table public.threat_intel add constraint threat_intel_pkey PRIMARY KEY (id);
alter table public.title_relevance add constraint title_relevance_pkey PRIMARY KEY (archetype, title_norm);
alter table public.upload_batches add constraint upload_batches_pkey PRIMARY KEY (id);
alter table public.usage_tracking add constraint usage_tracking_pkey PRIMARY KEY (id);
alter table public.waitlist add constraint waitlist_pkey PRIMARY KEY (id);
alter table public.watchlist add constraint watchlist_pkey PRIMARY KEY (id);
alter table public.watchlist_alerts add constraint watchlist_alerts_pkey PRIMARY KEY (id);
alter table public.webhook_deliveries add constraint webhook_deliveries_pkey PRIMARY KEY (id);
alter table public.webhook_endpoints add constraint webhook_endpoints_pkey PRIMARY KEY (id);
alter table public.website_scan_cache add constraint website_scan_cache_pkey PRIMARY KEY (domain);

-- ---------------------------------------------------------------------------
-- Unique constraints
-- ---------------------------------------------------------------------------

alter table public.api_keys add constraint api_keys_key_hash_key UNIQUE (key_hash);
alter table public.apollo_people_cache add constraint apollo_people_cache_org_id_apollo_id_key UNIQUE (org_id, apollo_id);
alter table public.companies add constraint companies_domain_key UNIQUE (domain);
alter table public.company_intel add constraint company_intel_company_id_signal_type_key UNIQUE (company_id, signal_type);
alter table public.company_job_postings add constraint company_job_postings_company_id_source_external_id_key UNIQUE (company_id, source, external_id);
alter table public.company_threat_surface add constraint company_threat_surface_company_id_threat_intel_id_key UNIQUE (company_id, threat_intel_id);
alter table public.competitor_feed add constraint competitor_feed_org_id_post_url_key UNIQUE (org_id, post_url);
alter table public.enrichment_cache add constraint enrichment_cache_domain_enrichment_type_enrichment_mode_key UNIQUE (domain, enrichment_type, enrichment_mode);
alter table public.group_members add constraint group_members_group_id_entity_ref_key UNIQUE (group_id, entity_ref);
alter table public.mcp_api_keys add constraint mcp_api_keys_key_hash_key UNIQUE (key_hash);
alter table public.org_credits add constraint org_credits_org_id_month_key UNIQUE (org_id, month);
alter table public.org_integrations add constraint org_integrations_org_id_provider_key UNIQUE (org_id, provider);
alter table public.org_users add constraint org_users_user_id_org_id_key UNIQUE (user_id, org_id);
alter table public.threat_intel add constraint threat_intel_source_source_id_key UNIQUE (source, source_id);
alter table public.usage_tracking add constraint usage_tracking_org_id_month_year_key UNIQUE (org_id, month_year);
alter table public.waitlist add constraint waitlist_email_key UNIQUE (email);
alter table public.watchlist add constraint watchlist_org_id_company_domain_key UNIQUE (org_id, company_domain);

-- ---------------------------------------------------------------------------
-- Check constraints
-- ---------------------------------------------------------------------------

alter table public.chat_memories add constraint chat_memories_content_check CHECK ((char_length(content) <= 1000));
alter table public.chat_messages add constraint chat_messages_role_check CHECK ((role = ANY (ARRAY['user'::text, 'assistant'::text, 'tool_call'::text, 'tool_result'::text])));
alter table public.companies add constraint companies_person_intel_enabled_check CHECK ((person_intel_enabled = ANY (ARRAY['off'::text, 'top1'::text, 'deep'::text])));
alter table public.credit_topups add constraint credit_topups_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'completed'::text, 'failed'::text, 'refunded'::text])));
alter table public.group_members add constraint group_members_entity_type_check CHECK ((entity_type = ANY (ARRAY['account'::text, 'people'::text])));
alter table public.groups add constraint groups_entity_type_check CHECK ((entity_type = ANY (ARRAY['account'::text, 'people'::text])));
alter table public.groups add constraint groups_mode_check CHECK ((mode = ANY (ARRAY['manual'::text, 'smart'::text])));
alter table public.lead_outcomes add constraint lead_outcomes_outcome_check CHECK (((outcome IS NULL) OR (outcome = ANY (ARRAY['meeting_booked'::text, 'replied_positive'::text, 'replied_neutral'::text, 'no_reply'::text, 'not_contacted'::text, 'unqualified'::text, 'dead'::text]))));
alter table public.leads add constraint leads_status_check CHECK ((status = ANY (ARRAY['new'::text, 'qualified'::text, 'disqualified'::text, 'sequenced'::text, 'replied'::text, 'meeting'::text, 'closed'::text, 'dead'::text, 'parked'::text, 'bounced'::text, 'contacted'::text, 'deleted'::text])));
alter table public.organisations add constraint organisations_brain_mode_check CHECK ((brain_mode = ANY (ARRAY['auto'::text, 'surface_only'::text, 'off'::text])));
alter table public.replies add constraint replies_classification_check CHECK (((classification IS NULL) OR (classification = ANY (ARRAY['INTERESTED'::text, 'CURIOUS'::text, 'NOT_NOW'::text, 'OBJECTION'::text, 'REFERRAL'::text, 'UNSUBSCRIBE'::text, 'AUTO_REPLY'::text, 'DEAD'::text]))));
alter table public.signals add constraint signals_axis_check CHECK ((axis = ANY (ARRAY['risk'::text, 'posture'::text, 'maturity'::text, 'commercial'::text])));
alter table public.signals add constraint signals_inference_level_check CHECK ((inference_level = ANY (ARRAY['observed'::text, 'derived'::text, 'inferred'::text, 'speculative'::text])));
alter table public.signals add constraint signals_signal_type_check CHECK (((signal_type ~~ 'certification\_%'::text) OR (signal_type = ANY (ARRAY['security_leadership_hire'::text, 'security_team_expansion'::text, 'security_team_scaling'::text, 'cve_in_stack'::text, 'compliance_gap'::text, 'breach_proximity'::text, 'breach_mention'::text, 'credential_leak'::text, 'credential_leak_supply_chain'::text, 'cloud_security_scaling'::text, 'compliance_operationalisation'::text, 'customer_security_review_pressure'::text, 'enterprise_readiness_pressure'::text, 'post_funding_security_maturity_push'::text, 'public_proof_not_visible'::text, 'revenue_blocker_likely'::text, 'upmarket_security_friction'::text, 'hiring'::text, 'hiring_sales_bd'::text, 'hiring_security'::text, 'hiring_leadership'::text, 'hiring_grc'::text, 'new_partnership'::text, 'new_leadership'::text, 'expansion'::text, 'recent_funding'::text, 'recently_funded'::text, 'product_launch'::text, 'product_launch_or_partnership'::text, 'recent_acquisition'::text, 'growing_tech_stack'::text, 'person_security_leader'::text, 'person_grc_leader'::text, 'person_technical_leader'::text, 'person_commercial_leader'::text, 'person_active_linkedin'::text, 'person_changed_company'::text, 'person_new_in_role'::text, 'person_posted_relevant_topic'::text, 'person_promoted'::text, 'person_engaged_with_topic'::text, 'trust_center_present'::text, 'security_page_present'::text, 'privacy_page_present'::text, 'responsible_disclosure_present'::text, 'bug_bounty_present'::text, 'compliance_page_present'::text, 'dpa_present'::text, 'subprocessors_present'::text, 'enterprise_page_detected'::text, 'api_or_developer_platform'::text, 'security_contact_present'::text, 'status_page_present'::text, 'social_company_mention'::text, 'social_company_post'::text, 'social_vendor_relationship'::text, 'social_milestone'::text, 'social_funding_news'::text, 'social_complaint'::text, 'social_employee_chatter'::text, 'social_breach_mention'::text, 'social_hiring_buzz'::text, 'growth'::text, 'challenge'::text, 'vendor_review'::text, 'budget'::text, 'strategy_change'::text, 'event'::text, 'regulated_industry'::text, 'other'::text]))));
alter table public.signals add constraint signals_source_quality_check CHECK ((source_quality = ANY (ARRAY['first_party'::text, 'third_party'::text, 'inferred_from_multiple_sources'::text, 'weak_unverified'::text])));
alter table public.signals add constraint signals_subject_type_check CHECK ((signal_subject_type = ANY (ARRAY['account'::text, 'person'::text, 'job'::text, 'website'::text, 'tech'::text, 'news'::text])));
alter table public.threat_alerts add constraint threat_alerts_status_check CHECK ((status = ANY (ARRAY['new'::text, 'seen'::text, 'actioned'::text, 'dismissed'::text])));
alter table public.threat_alerts add constraint threat_alerts_urgency_check CHECK (((urgency IS NULL) OR (urgency = ANY (ARRAY['critical'::text, 'high'::text, 'medium'::text, 'low'::text]))));
alter table public.watchlist add constraint watchlist_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'active'::text, 'dismissed'::text, 'deleted'::text])));
alter table public.watchlist_alerts add constraint watchlist_alerts_inference_level_check CHECK ((inference_level = ANY (ARRAY['observed'::text, 'derived'::text, 'inferred'::text, 'speculative'::text])));
alter table public.watchlist_alerts add constraint watchlist_alerts_source_quality_check CHECK ((source_quality = ANY (ARRAY['first_party'::text, 'third_party'::text, 'inferred_from_multiple_sources'::text, 'weak_unverified'::text])));
alter table public.watchlist_alerts add constraint watchlist_alerts_status_check CHECK ((status = ANY (ARRAY['new'::text, 'seen'::text, 'actioned'::text, 'dismissed'::text])));
alter table public.watchlist_alerts add constraint watchlist_alerts_subject_type_check CHECK ((signal_subject_type = ANY (ARRAY['account'::text, 'person'::text, 'job'::text, 'website'::text, 'tech'::text, 'news'::text])));

-- ---------------------------------------------------------------------------
-- Foreign keys
-- ---------------------------------------------------------------------------

alter table public.action_outcomes add constraint action_outcomes_action_plan_id_fkey FOREIGN KEY (action_plan_id) REFERENCES public.action_plans(id);
alter table public.action_outcomes add constraint action_outcomes_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id);
alter table public.action_outcomes add constraint action_outcomes_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id);
alter table public.action_plans add constraint action_plans_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id);
alter table public.action_plans add constraint action_plans_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id);
alter table public.activity_log add constraint activity_log_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE SET NULL;
alter table public.activity_log add constraint activity_log_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.api_keys add constraint api_keys_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.api_request_log add constraint api_request_log_api_key_id_fkey FOREIGN KEY (api_key_id) REFERENCES public.api_keys(id) ON DELETE CASCADE;
alter table public.api_request_log add constraint api_request_log_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.apollo_people_cache add constraint apollo_people_cache_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.campaigns add constraint campaigns_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.cancellation_feedback add constraint cancellation_feedback_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE SET NULL;
alter table public.chat_memories add constraint chat_memories_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.chat_memories add constraint chat_memories_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
alter table public.chat_messages add constraint chat_messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.chat_sessions(id) ON DELETE CASCADE;
alter table public.company_intel add constraint company_intel_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;
alter table public.company_job_postings add constraint company_job_postings_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;
alter table public.company_threat_surface add constraint company_threat_surface_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;
alter table public.company_threat_surface add constraint company_threat_surface_threat_intel_id_fkey FOREIGN KEY (threat_intel_id) REFERENCES public.threat_intel(id) ON DELETE CASCADE;
alter table public.competitor_feed add constraint competitor_feed_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.content_suggestions add constraint content_suggestions_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.credit_ledger add constraint credit_ledger_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.credit_topups add constraint credit_topups_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.group_members add constraint group_members_group_id_fkey FOREIGN KEY (group_id) REFERENCES public.groups(id) ON DELETE CASCADE;
alter table public.group_members add constraint group_members_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.groups add constraint groups_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.lead_outcomes add constraint lead_outcomes_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id);
alter table public.leads add constraint leads_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.upload_batches(id);
alter table public.leads add constraint leads_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id);
alter table public.leads add constraint leads_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.market_snapshots add constraint market_snapshots_market_id_fkey FOREIGN KEY (market_id) REFERENCES public.markets(id) ON DELETE CASCADE;
alter table public.market_snapshots add constraint market_snapshots_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.markets add constraint markets_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.mcp_api_keys add constraint mcp_api_keys_created_by_fkey FOREIGN KEY (created_by) REFERENCES auth.users(id) ON DELETE SET NULL;
alter table public.mcp_api_keys add constraint mcp_api_keys_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.org_credits add constraint org_credits_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.org_integrations add constraint org_integrations_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.org_users add constraint org_users_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.org_users add constraint org_users_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
alter table public.raw_artifacts add constraint raw_artifacts_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE SET NULL;
alter table public.raw_artifacts add constraint raw_artifacts_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE SET NULL;
alter table public.relevance_feedback add constraint relevance_feedback_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.replies add constraint replies_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE SET NULL;
alter table public.replies add constraint replies_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.signals add constraint signals_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE SET NULL;
alter table public.signals add constraint signals_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE CASCADE;
alter table public.signals add constraint signals_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.strategy_memory add constraint strategy_memory_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id);
alter table public.threat_alerts add constraint threat_alerts_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.threat_alerts add constraint threat_alerts_threat_intel_id_fkey FOREIGN KEY (threat_intel_id) REFERENCES public.threat_intel(id) ON DELETE CASCADE;
alter table public.upload_batches add constraint upload_batches_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id);
alter table public.usage_tracking add constraint usage_tracking_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id);
alter table public.watchlist add constraint watchlist_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE SET NULL;
alter table public.watchlist add constraint watchlist_market_id_fkey FOREIGN KEY (market_id) REFERENCES public.markets(id) ON DELETE SET NULL;
alter table public.watchlist add constraint watchlist_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.watchlist_alerts add constraint watchlist_alerts_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.watchlist_alerts add constraint watchlist_alerts_watchlist_id_fkey FOREIGN KEY (watchlist_id) REFERENCES public.watchlist(id) ON DELETE CASCADE;
alter table public.webhook_deliveries add constraint webhook_deliveries_endpoint_id_fkey FOREIGN KEY (endpoint_id) REFERENCES public.webhook_endpoints(id) ON DELETE CASCADE;
alter table public.webhook_deliveries add constraint webhook_deliveries_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;
alter table public.webhook_endpoints add constraint webhook_endpoints_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

CREATE INDEX action_outcomes_lead_outcome_idx ON public.action_outcomes USING btree (lead_id, outcome);
CREATE INDEX action_outcomes_org_created_idx ON public.action_outcomes USING btree (org_id, created_at DESC);
CREATE INDEX action_outcomes_target_idx ON public.action_outcomes USING btree (target_type, target_id);
CREATE INDEX idx_action_outcomes_lead ON public.action_outcomes USING btree (lead_id);
CREATE INDEX idx_action_outcomes_org ON public.action_outcomes USING btree (org_id);
CREATE INDEX idx_action_outcomes_type ON public.action_outcomes USING btree (action_type, outcome);
CREATE INDEX idx_action_plans_lead ON public.action_plans USING btree (lead_id);
CREATE INDEX idx_action_plans_org ON public.action_plans USING btree (org_id);
CREATE INDEX idx_activity_created ON public.activity_log USING btree (created_at DESC);
CREATE INDEX idx_activity_org ON public.activity_log USING btree (org_id);
CREATE INDEX idx_activity_type ON public.activity_log USING btree (activity_type);
CREATE INDEX api_keys_org_id_active_idx ON public.api_keys USING btree (org_id) WHERE (revoked_at IS NULL);
CREATE INDEX api_request_log_key_created_idx ON public.api_request_log USING btree (api_key_id, created_at DESC);
CREATE INDEX api_request_log_org_created_idx ON public.api_request_log USING btree (org_id, created_at DESC);
CREATE INDEX apollo_people_cache_org_domain_idx ON public.apollo_people_cache USING btree (org_id, company_domain);
CREATE INDEX idx_campaigns_org ON public.campaigns USING btree (org_id);
CREATE INDEX chat_memories_user_org_idx ON public.chat_memories USING btree (user_id, org_id);
CREATE INDEX chat_messages_session_idx ON public.chat_messages USING btree (session_id, created_at);
CREATE INDEX chat_sessions_expires_idx ON public.chat_sessions USING btree (expires_at);
CREATE INDEX chat_sessions_org_user_idx ON public.chat_sessions USING btree (org_id, user_id, updated_at DESC);
CREATE INDEX companies_industry_idx ON public.companies USING btree (industry);
CREATE INDEX companies_last_enriched_idx ON public.companies USING btree (last_enriched_at);
CREATE INDEX idx_companies_country ON public.companies USING btree (country);
CREATE INDEX idx_companies_industry ON public.companies USING btree (industry);
CREATE INDEX idx_companies_person_intel ON public.companies USING btree (person_intel_enabled, last_person_intel_at) WHERE (person_intel_enabled <> 'off'::text);
CREATE INDEX idx_company_intel_company ON public.company_intel USING btree (company_id);
CREATE INDEX idx_company_intel_latest ON public.company_intel USING btree (latest_observed_at DESC);
CREATE INDEX idx_company_intel_type ON public.company_intel USING btree (signal_type);
CREATE INDEX idx_cjp_company ON public.company_job_postings USING btree (company_id);
CREATE INDEX idx_cjp_last_seen ON public.company_job_postings USING btree (last_seen);
CREATE INDEX idx_cjp_open_security ON public.company_job_postings USING btree (company_id, is_security) WHERE (closed_at IS NULL);
CREATE INDEX idx_threat_surface_company ON public.company_threat_surface USING btree (company_id);
CREATE INDEX idx_threat_surface_threat ON public.company_threat_surface USING btree (threat_intel_id);
CREATE INDEX competitor_feed_org_posted ON public.competitor_feed USING btree (org_id, posted_at DESC);
CREATE INDEX idx_content_suggestions_org ON public.content_suggestions USING btree (org_id);
CREATE INDEX idx_content_suggestions_week ON public.content_suggestions USING btree (week_label);
CREATE INDEX credit_ledger_org_action_idx ON public.credit_ledger USING btree (org_id, action);
CREATE INDEX credit_ledger_org_created_idx ON public.credit_ledger USING btree (org_id, created_at DESC);
CREATE INDEX idx_credit_topups_org ON public.credit_topups USING btree (org_id, created_at DESC);
CREATE UNIQUE INDEX idx_credit_topups_session ON public.credit_topups USING btree (stripe_checkout_session_id);
CREATE INDEX idx_enrichment_cache_domain ON public.enrichment_cache USING btree (domain);
CREATE INDEX idx_enrichment_cache_expires ON public.enrichment_cache USING btree (expires_at);
CREATE INDEX free_report_leads_due_idx ON public.free_report_leads USING btree (step, last_sent_at) WHERE (unsubscribed = false);
CREATE INDEX free_report_leads_unsub_token_idx ON public.free_report_leads USING btree (unsub_token);
CREATE INDEX idx_free_report_requests_created ON public.free_report_requests USING btree (created_at DESC);
CREATE INDEX idx_free_report_requests_ip_created ON public.free_report_requests USING btree (ip, created_at DESC);
CREATE INDEX group_members_group_idx ON public.group_members USING btree (group_id);
CREATE INDEX groups_org_idx ON public.groups USING btree (org_id);
CREATE INDEX idx_outcomes_lead ON public.lead_outcomes USING btree (lead_id);
CREATE INDEX idx_outcomes_org ON public.lead_outcomes USING btree (org_id);
CREATE INDEX idx_outcomes_org_created ON public.lead_outcomes USING btree (org_id, created_at DESC);
CREATE INDEX idx_outcomes_outcome ON public.lead_outcomes USING btree (outcome);
CREATE INDEX idx_leads_batch ON public.leads USING btree (batch_id);
CREATE INDEX idx_leads_company_revenue ON public.leads USING btree (org_id) WHERE (company_revenue IS NOT NULL);
CREATE INDEX idx_leads_data_status ON public.leads USING btree (data_status);
CREATE INDEX idx_leads_enrichment_status ON public.leads USING btree (enrichment_status);
CREATE INDEX idx_leads_first_hot_at ON public.leads USING btree (org_id, first_hot_at DESC) WHERE (first_hot_at IS NOT NULL);
CREATE INDEX idx_leads_matched_persona ON public.leads USING btree (matched_persona_id) WHERE (matched_persona_id IS NOT NULL);
CREATE INDEX idx_leads_org_id ON public.leads USING btree (org_id);
CREATE INDEX idx_leads_org_status ON public.leads USING btree (org_id, status);
CREATE INDEX idx_leads_outreach_at ON public.leads USING btree (org_id, outreach_at) WHERE (outreach_at IS NOT NULL);
CREATE INDEX idx_leads_person_monitoring ON public.leads USING btree (org_id) WHERE person_monitoring;
CREATE INDEX idx_leads_pool ON public.leads USING btree (org_id, enrichment_status) WHERE (enrichment_status = ANY (ARRAY['pool'::text, 'suggested'::text, 'filtered'::text]));
CREATE INDEX idx_leads_rating ON public.leads USING btree (lead_rating);
CREATE INDEX idx_leads_revenue_potential ON public.leads USING btree (org_id) WHERE (revenue_potential IS NOT NULL);
CREATE INDEX idx_leads_source_type ON public.leads USING btree (source_type);
CREATE INDEX idx_leads_status ON public.leads USING btree (status);
CREATE INDEX idx_leads_total_score ON public.leads USING btree (total_score DESC);
CREATE INDEX leads_company_id_idx ON public.leads USING btree (company_id);
CREATE UNIQUE INDEX leads_org_id_email_unique ON public.leads USING btree (org_id, email) WHERE ((email IS NOT NULL) AND (email <> ''::text));
CREATE UNIQUE INDEX leads_org_id_linkedin_unique ON public.leads USING btree (org_id, linkedin_url) WHERE ((linkedin_url IS NOT NULL) AND (linkedin_url <> ''::text));
CREATE INDEX market_snapshots_org_captured_idx ON public.market_snapshots USING btree (org_id, captured_at DESC);
CREATE UNIQUE INDEX markets_one_primary_per_org ON public.markets USING btree (org_id) WHERE is_primary;
CREATE INDEX markets_org_idx ON public.markets USING btree (org_id);
CREATE INDEX mcp_api_keys_active_idx ON public.mcp_api_keys USING btree (key_hash) WHERE (revoked_at IS NULL);
CREATE INDEX mcp_api_keys_org_id_idx ON public.mcp_api_keys USING btree (org_id);
CREATE INDEX org_credits_org_month_idx ON public.org_credits USING btree (org_id, month);
CREATE INDEX org_integrations_org_idx ON public.org_integrations USING btree (org_id);
CREATE INDEX idx_org_users_org ON public.org_users USING btree (org_id);
CREATE INDEX idx_org_users_user ON public.org_users USING btree (user_id);
CREATE INDEX idx_organisations_seat_count ON public.organisations USING btree (seat_count) WHERE (seat_count > 1);
CREATE UNIQUE INDEX idx_organisations_stripe_customer ON public.organisations USING btree (stripe_customer_id) WHERE (stripe_customer_id IS NOT NULL);
CREATE INDEX idx_organisations_stripe_subscription ON public.organisations USING btree (stripe_subscription_id) WHERE (stripe_subscription_id IS NOT NULL);
CREATE INDEX idx_rate_limit_events_age ON public.rate_limit_events USING btree (event_at);
CREATE INDEX idx_rate_limit_events_lookup ON public.rate_limit_events USING btree (org_id, bucket_key, event_at DESC);
CREATE INDEX raw_artifacts_company_idx ON public.raw_artifacts USING btree (company_id);
CREATE UNIQUE INDEX raw_artifacts_dedup_idx ON public.raw_artifacts USING btree (source, content_hash);
CREATE INDEX raw_artifacts_domain_source_idx ON public.raw_artifacts USING btree (domain, source);
CREATE INDEX relevance_feedback_org_idx ON public.relevance_feedback USING btree (org_id, entity_type);
CREATE UNIQUE INDEX relevance_feedback_uniq ON public.relevance_feedback USING btree (org_id, entity_type, entity_ref);
CREATE INDEX idx_replies_classification ON public.replies USING btree (classification);
CREATE INDEX idx_replies_lead ON public.replies USING btree (lead_id);
CREATE INDEX idx_replies_org ON public.replies USING btree (org_id);
CREATE INDEX idx_signals_company_axis ON public.signals USING btree (company_id, axis) WHERE (company_id IS NOT NULL);
CREATE INDEX idx_signals_lead ON public.signals USING btree (lead_id);
CREATE INDEX idx_signals_org ON public.signals USING btree (org_id);
CREATE INDEX idx_signals_org_detected ON public.signals USING btree (org_id, detected_at DESC);
CREATE INDEX idx_signals_type ON public.signals USING btree (signal_type);
CREATE INDEX signals_company_id_idx ON public.signals USING btree (company_id) WHERE (company_id IS NOT NULL);
CREATE INDEX signals_inference_level_idx ON public.signals USING btree (inference_level);
CREATE INDEX signals_lead_subject_idx ON public.signals USING btree (lead_id, signal_subject_type);
CREATE INDEX signals_subject_type_idx ON public.signals USING btree (signal_subject_type);
CREATE INDEX signup_attempts_created_idx ON public.signup_attempts USING btree (created_at);
CREATE INDEX signup_attempts_ip_created_idx ON public.signup_attempts USING btree (ip, created_at);
CREATE INDEX idx_strategy_memory_org ON public.strategy_memory USING btree (org_id, week_starting);
CREATE INDEX tech_category_source_idx ON public.tech_category USING btree (source);
CREATE INDEX idx_threat_alerts_org ON public.threat_alerts USING btree (org_id);
CREATE INDEX idx_threat_alerts_status ON public.threat_alerts USING btree (status);
CREATE INDEX idx_threat_alerts_urgency ON public.threat_alerts USING btree (urgency);
CREATE INDEX idx_threat_intel_published ON public.threat_intel USING btree (published_at DESC);
CREATE INDEX idx_threat_intel_severity ON public.threat_intel USING btree (severity);
CREATE INDEX idx_threat_intel_source ON public.threat_intel USING btree (source);
CREATE INDEX idx_upload_batches_org ON public.upload_batches USING btree (org_id);
CREATE INDEX idx_usage_tracking_org_month ON public.usage_tracking USING btree (org_id, month_year);
CREATE INDEX idx_waitlist_email ON public.waitlist USING btree (email);
CREATE INDEX idx_watchlist_active ON public.watchlist USING btree (monitoring_active);
CREATE INDEX idx_watchlist_lead ON public.watchlist USING btree (lead_id);
CREATE INDEX idx_watchlist_org ON public.watchlist USING btree (org_id);
CREATE INDEX idx_watchlist_org_status ON public.watchlist USING btree (org_id, status);
CREATE UNIQUE INDEX uq_watchlist_org_domain ON public.watchlist USING btree (org_id, lower(company_domain)) WHERE (status = ANY (ARRAY['pending'::text, 'active'::text]));
CREATE INDEX watchlist_next_monitor_at_idx ON public.watchlist USING btree (next_monitor_at) WHERE (monitoring_active = true);
CREATE INDEX idx_watchlist_alerts_org ON public.watchlist_alerts USING btree (org_id);
CREATE INDEX idx_watchlist_alerts_status ON public.watchlist_alerts USING btree (status);
CREATE INDEX idx_watchlist_alerts_type ON public.watchlist_alerts USING btree (alert_type);
CREATE INDEX idx_watchlist_alerts_watchlist ON public.watchlist_alerts USING btree (watchlist_id);
CREATE INDEX webhook_deliveries_endpoint_created_idx ON public.webhook_deliveries USING btree (endpoint_id, created_at DESC);
CREATE INDEX webhook_deliveries_org_created_idx ON public.webhook_deliveries USING btree (org_id, created_at DESC);
CREATE INDEX webhook_deliveries_pending_due_idx ON public.webhook_deliveries USING btree (next_attempt_at) WHERE (status = ANY (ARRAY['pending'::text, 'failed'::text]));
CREATE INDEX webhook_endpoints_org_active_idx ON public.webhook_endpoints USING btree (org_id) WHERE (active = true);

-- ---------------------------------------------------------------------------
-- Functions and triggers
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.chat_memories_set_updated_at()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $function$;

CREATE TRIGGER chat_memories_set_updated_at BEFORE UPDATE ON public.chat_memories FOR EACH ROW EXECUTE FUNCTION public.chat_memories_set_updated_at();

CREATE OR REPLACE FUNCTION public.cleanup_expired_chat_sessions()
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
BEGIN
  DELETE FROM public.chat_sessions WHERE expires_at < now();
END;
$function$;

CREATE OR REPLACE FUNCTION public.delete_org_account(p_org_id uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'auth'
AS $function$
DECLARE
  v_user_ids uuid[];
  v_orphans  uuid[];
  v_users_deleted int := 0;
BEGIN
  -- Capture members BEFORE we delete the org_users links.
  SELECT array_agg(user_id) INTO v_user_ids
  FROM public.org_users WHERE org_id = p_org_id;

  -- NO-ACTION FK tables — must go before the org row. action_outcomes
  -- references action_plans, so it goes first.
  DELETE FROM public.action_outcomes WHERE org_id = p_org_id;
  DELETE FROM public.action_plans    WHERE org_id = p_org_id;
  DELETE FROM public.strategy_memory WHERE org_id = p_org_id;
  DELETE FROM public.upload_batches  WHERE org_id = p_org_id;
  DELETE FROM public.usage_tracking  WHERE org_id = p_org_id;

  -- Chat (no FK cascade from organisations).
  DELETE FROM public.chat_messages
    WHERE session_id IN (SELECT id FROM public.chat_sessions WHERE org_id = p_org_id);
  DELETE FROM public.chat_sessions WHERE org_id = p_org_id;

  -- The org row — cascades org_users, watchlist, leads, signals, credit_*,
  -- webhooks, api_keys, etc.; SET NULLs cancellation_feedback + raw_artifacts.
  DELETE FROM public.organisations WHERE id = p_org_id;

  -- Auth users now belonging to no org at all → hard delete (cascades
  -- identities/sessions/mfa_factors within the auth schema).
  IF v_user_ids IS NOT NULL THEN
    SELECT array_agg(uid) INTO v_orphans
    FROM unnest(v_user_ids) AS uid
    WHERE NOT EXISTS (SELECT 1 FROM public.org_users ou WHERE ou.user_id = uid);

    IF v_orphans IS NOT NULL THEN
      DELETE FROM auth.users WHERE id = ANY(v_orphans);
      GET DIAGNOSTICS v_users_deleted = ROW_COUNT;
    END IF;
  END IF;

  RETURN jsonb_build_object(
    'org_deleted', true,
    'auth_users_deleted', v_users_deleted
  );
END;
$function$;

CREATE OR REPLACE FUNCTION public.vera_score_signal(p_signal_type text, p_signal_strength numeric, p_detected_at timestamp with time zone, p_account_fit numeric, p_priorities jsonb)
 RETURNS numeric
 LANGUAGE plpgsql
 STABLE
AS $function$
DECLARE
  v_weight   numeric;
  v_strength numeric;
  v_fit      numeric;
  v_decay    numeric;
  v_days     numeric;
BEGIN
  -- Per-client weight. Missing key = 0 (signal type isn't on this
  -- client's radar; effectively hidden from ranked surfaces).
  v_weight := COALESCE((p_priorities ->> p_signal_type)::numeric, 0);
  IF v_weight <= 0 THEN
    RETURN 0;
  END IF;

  v_strength := GREATEST(0, LEAST(1, COALESCE(p_signal_strength, 0.5)));
  v_fit      := GREATEST(0, LEAST(1, COALESCE(p_account_fit, 0.5)));

  v_days := GREATEST(0, EXTRACT(EPOCH FROM (NOW() - p_detected_at)) / 86400.0);
  -- 7-day half-life: 0d=1.0, 7d=0.5, 14d=0.25, 30d=~0.05.
  v_decay := EXP(-LN(2) * v_days / 7.0);

  RETURN v_weight * v_strength * v_decay * v_fit;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_account_scores(p_org_id uuid, p_since timestamp with time zone DEFAULT (now() - '30 days'::interval))
 RETURNS TABLE(domain text, account_score numeric, signal_count integer)
 LANGUAGE plpgsql
 STABLE
AS $function$
DECLARE
  v_priorities jsonb;
BEGIN
  SELECT o.icp_config -> 'signal_priorities'
    INTO v_priorities
  FROM organisations o
  WHERE o.id = p_org_id;

  IF v_priorities IS NULL OR v_priorities = '{}'::jsonb THEN
    RETURN;
  END IF;

  RETURN QUERY
  WITH scored AS (
    SELECT
      LOWER(COALESCE(c.domain, l.company_domain)) AS dom,
      public.vera_score_signal(
        s.signal_type,
        s.signal_strength,
        s.detected_at,
        COALESCE(l.icp_score / 100.0, 0.7),
        v_priorities
      ) AS score
    FROM signals s
    LEFT JOIN companies c ON c.id = s.company_id
    LEFT JOIN leads     l ON l.id = s.lead_id AND l.org_id = s.org_id
    WHERE s.org_id = p_org_id
      AND s.detected_at >= p_since
      AND COALESCE(c.domain, l.company_domain) IS NOT NULL
  )
  SELECT
    scored.dom AS domain,
    SUM(scored.score) AS account_score,
    COUNT(*)::int AS signal_count
  FROM scored
  WHERE scored.score > 0
  GROUP BY scored.dom
  ORDER BY SUM(scored.score) DESC;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_scored_signals(p_org_id uuid, p_since timestamp with time zone DEFAULT (now() - '30 days'::interval))
 RETURNS TABLE(id uuid, org_id uuid, lead_id uuid, company_id uuid, signal_type text, signal_source text, signal_data jsonb, signal_strength numeric, detected_at timestamp with time zone, signal_subject_type text, vera_score numeric)
 LANGUAGE plpgsql
 STABLE
AS $function$
DECLARE
  v_priorities jsonb;
BEGIN
  SELECT o.icp_config -> 'signal_priorities'
    INTO v_priorities
  FROM organisations o
  WHERE o.id = p_org_id;

  IF v_priorities IS NULL OR v_priorities = '{}'::jsonb THEN
    RETURN;
  END IF;

  RETURN QUERY
  SELECT
    s.id,
    s.org_id,
    s.lead_id,
    s.company_id,
    s.signal_type,
    s.signal_source,
    s.signal_data,
    s.signal_strength,
    s.detected_at,
    s.signal_subject_type,
    public.vera_score_signal(
      s.signal_type,
      s.signal_strength,
      s.detected_at,
      COALESCE(l.icp_score / 100.0, 0.7),
      v_priorities
    ) AS vera_score
  FROM signals s
  LEFT JOIN leads l ON l.id = s.lead_id AND l.org_id = s.org_id
  WHERE s.org_id = p_org_id
    AND s.detected_at >= p_since
  ORDER BY vera_score DESC NULLS LAST;
END;
$function$;

CREATE OR REPLACE FUNCTION public.org_last_seen()
 RETURNS TABLE(org_id uuid, last_seen timestamp with time zone)
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO 'public', 'auth'
AS $function$
  select ou.org_id, max(u.last_sign_in_at) as last_seen
  from public.org_users ou
  join auth.users u on u.id = ou.user_id
  group by ou.org_id;
$function$;

-- Security-definer functions are server-only (service role).
revoke execute on function public.cleanup_expired_chat_sessions() from public, anon, authenticated;
revoke execute on function public.delete_org_account(uuid) from public, anon, authenticated;
revoke execute on function public.org_last_seen() from public, anon, authenticated;

-- Safety net: any table created in public later gets RLS switched on.
CREATE OR REPLACE FUNCTION public.rls_auto_enable()
 RETURNS event_trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE
  cmd record;
BEGIN
  FOR cmd IN
    SELECT *
    FROM pg_event_trigger_ddl_commands()
    WHERE command_tag IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
      AND object_type IN ('table','partitioned table')
  LOOP
     IF cmd.schema_name IS NOT NULL AND cmd.schema_name IN ('public') AND cmd.schema_name NOT IN ('pg_catalog','information_schema') AND cmd.schema_name NOT LIKE 'pg_toast%' AND cmd.schema_name NOT LIKE 'pg_temp%' THEN
      BEGIN
        EXECUTE format('alter table if exists %s enable row level security', cmd.object_identity);
        RAISE LOG 'rls_auto_enable: enabled RLS on %', cmd.object_identity;
      EXCEPTION
        WHEN OTHERS THEN
          RAISE LOG 'rls_auto_enable: failed to enable RLS on %', cmd.object_identity;
      END;
     ELSE
        RAISE LOG 'rls_auto_enable: skip % (either system schema or not in enforced list: %.)', cmd.object_identity, cmd.schema_name;
     END IF;
  END LOOP;
END;
$function$;

revoke execute on function public.rls_auto_enable() from public, anon, authenticated;

do $$
begin
  if not exists (select 1 from pg_event_trigger where evtname = 'ensure_rls') then
    create event trigger ensure_rls on ddl_command_end
      when tag in ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
      execute function public.rls_auto_enable();
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- Row level security
--
-- The engine and dashboard server routes use the service role, which bypasses
-- RLS. These policies govern the browser (anon key + user session) only.
-- ---------------------------------------------------------------------------

alter table public.action_outcomes enable row level security;
alter table public.action_plans enable row level security;
alter table public.activity_log enable row level security;
alter table public.api_keys enable row level security;
alter table public.api_request_log enable row level security;
alter table public.apollo_people_cache enable row level security;
alter table public.campaigns enable row level security;
alter table public.cancellation_feedback enable row level security;
alter table public.chat_memories enable row level security;
alter table public.chat_messages enable row level security;
alter table public.chat_sessions enable row level security;
alter table public.companies enable row level security;
alter table public.company_intel enable row level security;
alter table public.company_job_postings enable row level security;
alter table public.company_threat_surface enable row level security;
alter table public.competitor_feed enable row level security;
alter table public.content_suggestions enable row level security;
alter table public.credit_ledger enable row level security;
alter table public.credit_topups enable row level security;
alter table public.enrichment_cache enable row level security;
alter table public.free_report_leads enable row level security;
alter table public.free_report_requests enable row level security;
alter table public.group_members enable row level security;
alter table public.groups enable row level security;
alter table public.lead_outcomes enable row level security;
alter table public.leads enable row level security;
alter table public.market_snapshots enable row level security;
alter table public.markets enable row level security;
alter table public.mcp_api_keys enable row level security;
alter table public.org_credits enable row level security;
alter table public.org_industry_cache enable row level security;
alter table public.org_integrations enable row level security;
alter table public.org_users enable row level security;
alter table public.organisations enable row level security;
alter table public.rate_limit_events enable row level security;
alter table public.raw_artifacts enable row level security;
alter table public.relevance_feedback enable row level security;
alter table public.replies enable row level security;
alter table public.schema_migrations enable row level security;
alter table public.signals enable row level security;
alter table public.signup_attempts enable row level security;
alter table public.strategy_memory enable row level security;
alter table public.system_heartbeat enable row level security;
alter table public.tech_category enable row level security;
alter table public.threat_alerts enable row level security;
alter table public.threat_intel enable row level security;
alter table public.title_relevance enable row level security;
alter table public.upload_batches enable row level security;
alter table public.usage_tracking enable row level security;
alter table public.waitlist enable row level security;
alter table public.watchlist enable row level security;
alter table public.watchlist_alerts enable row level security;
alter table public.webhook_deliveries enable row level security;
alter table public.webhook_endpoints enable row level security;
alter table public.website_scan_cache enable row level security;

create policy "Users read own org action_outcomes" on public.action_outcomes as PERMISSIVE for SELECT to authenticated using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy action_outcomes_authenticated_rw on public.action_outcomes as PERMISSIVE for ALL to authenticated using ((EXISTS ( SELECT 1 FROM org_users ou WHERE ((ou.org_id = action_outcomes.org_id) AND (ou.user_id = auth.uid()))))) with check ((EXISTS ( SELECT 1 FROM org_users ou WHERE ((ou.org_id = action_outcomes.org_id) AND (ou.user_id = auth.uid())))));
create policy action_outcomes_service_all on public.action_outcomes as PERMISSIVE for ALL to service_role using (true) with check (true);
create policy "Users read own org action_plans" on public.action_plans as PERMISSIVE for SELECT to authenticated using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy "Users read own org activity" on public.activity_log as PERMISSIVE for SELECT to authenticated using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy "org members can read unlocked people" on public.apollo_people_cache as PERMISSIVE for SELECT to public using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy "Users read own org campaigns" on public.campaigns as PERMISSIVE for SELECT to authenticated using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy chat_memories_delete_own on public.chat_memories as PERMISSIVE for DELETE to public using ((auth.uid() = user_id));
create policy chat_memories_insert_own on public.chat_memories as PERMISSIVE for INSERT to public with check ((auth.uid() = user_id));
create policy chat_memories_select_own on public.chat_memories as PERMISSIVE for SELECT to public using ((auth.uid() = user_id));
create policy chat_memories_update_own on public.chat_memories as PERMISSIVE for UPDATE to public using ((auth.uid() = user_id)) with check ((auth.uid() = user_id));
create policy chat_messages_owner_insert on public.chat_messages as PERMISSIVE for INSERT to public with check ((session_id IN ( SELECT chat_sessions.id FROM chat_sessions WHERE (chat_sessions.user_id = auth.uid()))));
create policy chat_messages_owner_select on public.chat_messages as PERMISSIVE for SELECT to public using ((session_id IN ( SELECT chat_sessions.id FROM chat_sessions WHERE ((chat_sessions.user_id = auth.uid()) AND (chat_sessions.expires_at > now())))));
create policy chat_sessions_owner_delete on public.chat_sessions as PERMISSIVE for DELETE to public using ((user_id = auth.uid()));
create policy chat_sessions_owner_insert on public.chat_sessions as PERMISSIVE for INSERT to public with check ((user_id = auth.uid()));
create policy chat_sessions_owner_select on public.chat_sessions as PERMISSIVE for SELECT to public using (((user_id = auth.uid()) AND (expires_at > now())));
create policy chat_sessions_owner_update on public.chat_sessions as PERMISSIVE for UPDATE to public using ((user_id = auth.uid()));
create policy companies_authenticated_read on public.companies as PERMISSIVE for SELECT to public using (((EXISTS ( SELECT 1 FROM (leads l JOIN org_users ou ON ((ou.org_id = l.org_id))) WHERE ((l.company_id = companies.id) AND (ou.user_id = auth.uid())))) OR (EXISTS ( SELECT 1 FROM (watchlist w JOIN org_users ou ON ((ou.org_id = w.org_id))) WHERE ((lower(w.company_domain) = lower(companies.domain)) AND (ou.user_id = auth.uid()))))));
create policy companies_service_all on public.companies as PERMISSIVE for ALL to service_role using (true) with check (true);
create policy company_intel_authenticated_read on public.company_intel as PERMISSIVE for SELECT to authenticated using (((EXISTS ( SELECT 1 FROM (leads l JOIN org_users ou ON ((ou.org_id = l.org_id))) WHERE ((l.company_id = company_intel.company_id) AND (ou.user_id = auth.uid())))) OR (EXISTS ( SELECT 1 FROM ((watchlist w JOIN org_users ou ON ((ou.org_id = w.org_id))) JOIN companies c ON ((lower(c.domain) = lower(w.company_domain)))) WHERE ((c.id = company_intel.company_id) AND (ou.user_id = auth.uid()))))));
create policy company_intel_service_all on public.company_intel as PERMISSIVE for ALL to service_role using (true) with check (true);
create policy company_job_postings_authenticated_read on public.company_job_postings as PERMISSIVE for SELECT to authenticated using (((EXISTS ( SELECT 1 FROM ((companies c JOIN watchlist w ON ((lower(w.company_domain) = lower(c.domain)))) JOIN org_users ou ON ((ou.org_id = w.org_id))) WHERE ((c.id = company_job_postings.company_id) AND (ou.user_id = auth.uid())))) OR (EXISTS ( SELECT 1 FROM ((companies c JOIN leads l ON ((l.company_id = c.id))) JOIN org_users ou ON ((ou.org_id = l.org_id))) WHERE ((c.id = company_job_postings.company_id) AND (ou.user_id = auth.uid()))))));
create policy company_job_postings_service_all on public.company_job_postings as PERMISSIVE for ALL to service_role using (true) with check (true);
create policy company_threat_surface_authenticated_read on public.company_threat_surface as PERMISSIVE for SELECT to authenticated using (((EXISTS ( SELECT 1 FROM (leads l JOIN org_users ou ON ((ou.org_id = l.org_id))) WHERE ((l.company_id = company_threat_surface.company_id) AND (ou.user_id = auth.uid())))) OR (EXISTS ( SELECT 1 FROM ((watchlist w JOIN org_users ou ON ((ou.org_id = w.org_id))) JOIN companies c ON ((lower(c.domain) = lower(w.company_domain)))) WHERE ((c.id = company_threat_surface.company_id) AND (ou.user_id = auth.uid()))))));
create policy company_threat_surface_service_all on public.company_threat_surface as PERMISSIVE for ALL to service_role using (true) with check (true);
create policy "Users read own org competitor feed" on public.competitor_feed as PERMISSIVE for SELECT to public using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy "Users read own org content" on public.content_suggestions as PERMISSIVE for SELECT to authenticated using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy "org members read their ledger" on public.credit_ledger as PERMISSIVE for SELECT to public using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy "Users read enrichment cache" on public.enrichment_cache as PERMISSIVE for SELECT to authenticated using (true);
create policy "Users manage own org group_members" on public.group_members as PERMISSIVE for ALL to public using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy "Users manage own org groups" on public.groups as PERMISSIVE for ALL to public using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy lead_outcomes_authenticated_read on public.lead_outcomes as PERMISSIVE for SELECT to authenticated using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy lead_outcomes_service_all on public.lead_outcomes as PERMISSIVE for ALL to service_role using (true) with check (true);
create policy "Users read own org leads" on public.leads as PERMISSIVE for SELECT to authenticated using (((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))) AND (enrichment_status = ANY (ARRAY['suggested'::text, 'enriched'::text]))));
create policy "org members read their keys" on public.mcp_api_keys as PERMISSIVE for SELECT to public using ((EXISTS ( SELECT 1 FROM org_users WHERE ((org_users.org_id = mcp_api_keys.org_id) AND (org_users.user_id = auth.uid())))));
create policy "org members update their keys" on public.mcp_api_keys as PERMISSIVE for UPDATE to public using ((EXISTS ( SELECT 1 FROM org_users WHERE ((org_users.org_id = mcp_api_keys.org_id) AND (org_users.user_id = auth.uid())))));
create policy "org members read their credits" on public.org_credits as PERMISSIVE for SELECT to public using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy "Users manage own org integrations" on public.org_integrations as PERMISSIVE for ALL to public using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid())))) with check ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy "Users read own org_users" on public.org_users as PERMISSIVE for SELECT to authenticated using ((user_id = auth.uid()));
create policy "Users read own org" on public.organisations as PERMISSIVE for SELECT to authenticated using ((id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy rate_limit_events_service_all on public.rate_limit_events as PERMISSIVE for ALL to service_role using (true) with check (true);
create policy raw_artifacts_service_all on public.raw_artifacts as PERMISSIVE for ALL to service_role using (true) with check (true);
create policy "Users manage own org relevance feedback" on public.relevance_feedback as PERMISSIVE for ALL to public using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid())))) with check ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy "Users read own org replies" on public.replies as PERMISSIVE for SELECT to authenticated using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy "Users read own org signals" on public.signals as PERMISSIVE for SELECT to authenticated using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy "Users read own org strategy_memory" on public.strategy_memory as PERMISSIVE for SELECT to authenticated using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy "Users read own org threat alerts" on public.threat_alerts as PERMISSIVE for SELECT to authenticated using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy "Allow anon read threat_intel" on public.threat_intel as PERMISSIVE for SELECT to public using (true);
create policy "Users read threat intel" on public.threat_intel as PERMISSIVE for SELECT to authenticated using (true);
create policy "Users read own org uploads" on public.upload_batches as PERMISSIVE for SELECT to authenticated using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy "Users read own org usage" on public.usage_tracking as PERMISSIVE for SELECT to authenticated using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy "Allow anonymous inserts" on public.waitlist as PERMISSIVE for INSERT to anon with check (true);
create policy "Users manage own org watchlist" on public.watchlist as PERMISSIVE for ALL to authenticated using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));
create policy "Users read own org watchlist alerts" on public.watchlist_alerts as PERMISSIVE for SELECT to authenticated using ((org_id IN ( SELECT org_users.org_id FROM org_users WHERE (org_users.user_id = auth.uid()))));

-- ---------------------------------------------------------------------------
-- Storage
-- ---------------------------------------------------------------------------

insert into storage.buckets (id, name, public)
values ('raw-artifacts', 'raw-artifacts', false)
on conflict (id) do nothing;

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('org-logos', 'org-logos', true, 2097152, array['image/png', 'image/jpeg', 'image/svg+xml', 'image/webp'])
on conflict (id) do nothing;

-- ---------------------------------------------------------------------------

insert into public.schema_migrations (filename) values ('001_baseline.sql');
