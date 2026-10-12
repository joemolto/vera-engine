-- Two-factor at the data layer.
--
-- The dashboard asks for the authenticator code at login, but a session
-- created with only a password (aal1) could still query these tables
-- directly. These restrictive policies hide org data from that session for
-- any user who has a verified authenticator. org_users and organisations
-- stay readable: the dashboard reads them before the code is entered to
-- decide where to send the user.

-- auth.mfa_factors isn't readable by signed-in users, so this check runs as
-- its owner; it only ever looks at the caller's own factors.
create or replace function public.mfa_satisfied()
 returns boolean
 language sql
 stable
 security definer
 set search_path to ''
as $function$
  select coalesce(auth.jwt() ->> 'aal', '') = 'aal2'
      or not exists (
        select 1 from auth.mfa_factors f
        where f.user_id = auth.uid() and f.status = 'verified'
      );
$function$;

revoke execute on function public.mfa_satisfied() from public, anon;
grant execute on function public.mfa_satisfied() to authenticated;

create policy require_mfa on public.action_outcomes as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.action_plans as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.activity_log as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.apollo_people_cache as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.campaigns as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.chat_memories as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.chat_messages as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.chat_sessions as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.companies as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.company_intel as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.company_job_postings as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.company_threat_surface as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.competitor_feed as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.content_suggestions as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.credit_ledger as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.enrichment_cache as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.group_members as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.groups as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.lead_outcomes as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.leads as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.mcp_api_keys as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.org_credits as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.org_integrations as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.relevance_feedback as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.replies as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.signals as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.strategy_memory as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.threat_alerts as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.threat_intel as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.upload_batches as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.usage_tracking as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.watchlist as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());
create policy require_mfa on public.watchlist_alerts as restrictive for all to authenticated
  using (public.mfa_satisfied()) with check (public.mfa_satisfied());

insert into public.schema_migrations (filename) values ('002_require_mfa.sql');
