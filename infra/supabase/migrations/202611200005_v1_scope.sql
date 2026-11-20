-- v1 scope from the n8n review (2026-11-20):
--   * Sequences have a per-campaign number of steps, each with its own subject and body
--     (generated_emails with fixed personalization_1..5 is replaced by campaign_steps + lead_messages).
--   * Gazi creates Instantly/HeyReach campaigns itself, so campaigns carry schedule and launch state.
--   * Apollo API search with LLM buyer scoring, Calendly bookings, weekly reports.
-- Nothing has been deployed yet, so generated_emails is dropped rather than migrated.

-- Session-local helper. Drop 0003's version first in case the migrations share a session.
drop function if exists pg_temp.grant_cols(text, text, text, text[]);
create function pg_temp.grant_cols(p_priv text, p_table text, p_role text, p_only text[] default null,
                                   p_except text[] default '{}')
returns void language plpgsql as $$
declare
  v_cols text;
begin
  select string_agg(quote_ident(column_name), ', ' order by ordinal_position) into v_cols
  from information_schema.columns
  where table_schema = 'public' and table_name = p_table and is_generated = 'NEVER'
    and (p_only is null or column_name = any (p_only))
    and column_name <> all (p_except);
  execute format('grant %s (%s) on public.%I to %I', p_priv, v_cols, p_table, p_role);
end
$$;

-- ---------------------------------------------------------------------------
-- Allowed values
-- ---------------------------------------------------------------------------
alter table public.integrations drop constraint integrations_provider_check;
alter table public.integrations add constraint integrations_provider_check
  check (provider in ('instantly', 'heyreach', 'calendly', 'apollo'));

alter table public.webhook_endpoints drop constraint webhook_endpoints_provider_check;
alter table public.webhook_endpoints add constraint webhook_endpoints_provider_check
  check (provider in ('instantly', 'heyreach', 'calendly'));
alter table public.webhook_events drop constraint webhook_events_provider_check;
alter table public.webhook_events add constraint webhook_events_provider_check
  check (provider in ('instantly', 'heyreach', 'calendly'));

alter table public.prompt_templates drop constraint prompt_templates_kind_check;
alter table public.prompt_templates add constraint prompt_templates_kind_check
  check (kind in ('email_step', 'linkedin_step', 'lead_score', 'reply_classify', 'reply_draft', 'weekly_report'));

-- ---------------------------------------------------------------------------
-- campaigns: offer context, schedule, launch state
-- ---------------------------------------------------------------------------
alter table public.campaigns
  add column offer_summary      text,   -- what the sender sells, USPs, what never to say
  add column offer_details      text,   -- ICP, proof points, case studies, MOQ, certifications
  add column attachment_url     text check (attachment_url is null or attachment_url ~* '^https://'),
  add column report_emails      text[] not null default '{}',
  add column timezone           text not null default 'UTC',
  add column sending_days       text[] not null default '{mon,tue,wed,thu,fri}'
    check (sending_days <@ array['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'] and cardinality(sending_days) > 0),
  add column send_from          time not null default '09:00',
  add column send_to            time not null default '17:00',
  add column daily_limit        int not null default 50 check (daily_limit between 1 and 1000),
  add column heyreach_list_id   text,
  add column launch_status      text not null default 'not_launched'
    check (launch_status in ('not_launched', 'launching', 'launched', 'failed')),
  add column launch_error       text,
  add column launched_at        timestamptz,
  add constraint campaigns_send_window check (send_from < send_to);

-- ---------------------------------------------------------------------------
-- sender_identities: signature image (the only URL allowed in follow-ups)
-- ---------------------------------------------------------------------------
alter table public.sender_identities
  add column signature_image_url text check (signature_image_url is null or signature_image_url ~* '^https://');

-- ---------------------------------------------------------------------------
-- leads: Apollo fields and buyer scoring
-- ---------------------------------------------------------------------------
alter table public.leads
  add column source             text not null default 'csv' check (source in ('csv', 'apollo', 'manual')),
  add column apollo_person_id   text,
  add column industry           text,
  add column city               text,
  add column country            text,
  add column company_size       int check (company_size is null or company_size >= 0),
  add column buyer_score        smallint check (buyer_score between 1 and 10),
  add column buyer_confidence   smallint check (buyer_confidence between 1 and 100),
  add column buyer_reason       text,
  add column is_decision_maker  boolean,
  add column instantly_lead_id  text;
create unique index leads_apollo_person_uq on public.leads (tenant_id, campaign_id, apollo_person_id)
  where apollo_person_id is not null;

-- ---------------------------------------------------------------------------
-- campaign_steps: the sequence definition (one row per step)
-- ---------------------------------------------------------------------------
create table public.campaign_steps (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null default app.tenant_id() references public.tenants (id) on delete cascade,
  campaign_id  uuid not null,
  step         smallint not null check (step between 1 and 10),
  delay_days   smallint not null check (delay_days between 0 and 60),
  angle        text not null check (length(btrim(angle)) between 1 and 500),  -- e.g. "case study", "breakup"
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  unique (tenant_id, campaign_id, step),
  foreign key (tenant_id, campaign_id) references public.campaigns (tenant_id, id) on delete cascade,
  check ((step = 1) = (delay_days = 0))
);

-- ---------------------------------------------------------------------------
-- lead_messages: generated copy per lead and step (replaces generated_emails)
-- ---------------------------------------------------------------------------
drop table public.generated_emails;

create table public.lead_messages (
  id                  uuid primary key default gen_random_uuid(),
  tenant_id           uuid not null default app.tenant_id() references public.tenants (id) on delete cascade,
  lead_id             uuid not null,
  campaign_id         uuid not null,
  step                smallint not null check (step between 1 and 10),
  channel             text not null check (channel in ('email', 'linkedin')),
  subject             text,
  body                text,
  prompt_template_id  uuid not null,
  prompt_version      int not null check (prompt_version > 0),
  model               text not null,
  status              text not null default 'pending'
                        check (status in ('pending', 'ok', 'failed', 'approved', 'pushed')),
  validation_errors   jsonb not null default '[]'::jsonb,
  attempt             int not null default 0 check (attempt >= 0),
  error_code          text,
  approved_by         uuid references public.users (id) on delete set null,
  approved_at         timestamptz,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  unique (tenant_id, lead_id, step),
  foreign key (tenant_id, lead_id) references public.leads (tenant_id, id) on delete cascade,
  foreign key (tenant_id, campaign_id) references public.campaigns (tenant_id, id) on delete cascade,
  foreign key (tenant_id, prompt_template_id) references public.prompt_templates (tenant_id, id),
  -- Rule 6 backstop: follow-ups carry no clickable link; the signature <img> is allowed.
  constraint follow_ups_have_no_links check (
    step = 1 or app.strip_img_tags(concat_ws(' ', subject, body)) !~* '(<a[[:space:]>]|https?://|www\.)'
  ),
  -- LinkedIn: no subjects; connection requests fit LinkedIn's 200-character limit.
  constraint linkedin_shape check (
    channel = 'email' or (subject is null and (step <> 1 or length(coalesce(body, '')) <= 200))
  ),
  constraint ok_messages_have_body check (status not in ('ok', 'approved', 'pushed') or body is not null)
);
create index lead_messages_campaign_status_idx on public.lead_messages (tenant_id, campaign_id, status);

-- ---------------------------------------------------------------------------
-- bookings (Calendly invitee.created / invitee.canceled)
-- ---------------------------------------------------------------------------
create table public.bookings (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null default app.tenant_id() references public.tenants (id) on delete cascade,
  campaign_id       uuid,
  lead_id           uuid,
  webhook_event_id  uuid,
  external_id       text not null,          -- Calendly invitee URI
  invitee_email     text not null check (invitee_email = lower(btrim(invitee_email))),
  invitee_name      text,
  scheduled_for     timestamptz,
  canceled_at       timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  unique (tenant_id, external_id),
  foreign key (tenant_id, campaign_id) references public.campaigns (tenant_id, id) on delete set null (campaign_id),
  foreign key (tenant_id, lead_id) references public.leads (tenant_id, id) on delete set null (lead_id),
  foreign key (tenant_id, webhook_event_id) references public.webhook_events (tenant_id, id)
);
create index bookings_campaign_idx on public.bookings (tenant_id, campaign_id);

-- ---------------------------------------------------------------------------
-- campaign_reports (weekly)
-- ---------------------------------------------------------------------------
create table public.campaign_reports (
  id                  uuid primary key default gen_random_uuid(),
  tenant_id           uuid not null default app.tenant_id() references public.tenants (id) on delete cascade,
  campaign_id         uuid not null,
  period_start        date not null,
  period_end          date not null,
  metrics             jsonb not null default '{}'::jsonb,
  summary             text,
  sent_to             text[] not null default '{}',
  status              text not null default 'pending' check (status in ('pending', 'sent', 'failed', 'skipped')),
  error_code          text,
  prompt_template_id  uuid,
  prompt_version      int check (prompt_version is null or prompt_version > 0),
  sent_at             timestamptz,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  unique (tenant_id, campaign_id, period_start),
  foreign key (tenant_id, campaign_id) references public.campaigns (tenant_id, id) on delete cascade,
  foreign key (tenant_id, prompt_template_id) references public.prompt_templates (tenant_id, id),
  check (period_start <= period_end)
);

-- ---------------------------------------------------------------------------
-- Triggers
-- ---------------------------------------------------------------------------
do $$
declare
  t text;
begin
  foreach t in array array['campaign_steps', 'lead_messages', 'bookings', 'campaign_reports'] loop
    execute format('create trigger touch_updated_at before update on public.%I
                      for each row execute function app.touch_updated_at()', t);
    execute format('create trigger tenant_id_immutable before update on public.%I
                      for each row execute function app.tenant_id_immutable()', t);
  end loop;
end
$$;

-- ---------------------------------------------------------------------------
-- Grants (tenant_id, id and timestamps are never client-writable)
-- ---------------------------------------------------------------------------
revoke all on public.campaign_steps, public.lead_messages, public.bookings, public.campaign_reports
  from public, anon, authenticated, service_role;

do $$
declare
  w constant text[] := array['id', 'tenant_id', 'created_at', 'updated_at'];
begin
  -- new columns on existing tables
  perform pg_temp.grant_cols('select', 'campaigns', 'authenticated');
  perform pg_temp.grant_cols('insert', 'campaigns', 'authenticated', null,
                             w || array['launch_status', 'launch_error', 'launched_at', 'heyreach_list_id']);
  perform pg_temp.grant_cols('update', 'campaigns', 'authenticated', null,
                             w || array['launch_status', 'launch_error', 'launched_at', 'heyreach_list_id']);
  perform pg_temp.grant_cols('update', 'campaigns', 'gazi_worker',
                             array['status', 'launch_status', 'launch_error', 'launched_at', 'heyreach_list_id',
                                   'instantly_campaign_id', 'heyreach_campaign_id']);

  perform pg_temp.grant_cols('select', 'sender_identities', 'authenticated', null,
                             array['signature_ciphertext', 'signature_nonce']);
  perform pg_temp.grant_cols('insert', 'sender_identities', 'authenticated', null, w);
  perform pg_temp.grant_cols('update', 'sender_identities', 'authenticated', null, w);

  perform pg_temp.grant_cols('select', 'leads', 'authenticated');
  perform pg_temp.grant_cols('insert', 'leads', 'authenticated', null, w);
  perform pg_temp.grant_cols('update', 'leads', 'authenticated', null, w);
  perform pg_temp.grant_cols('insert', 'leads', 'gazi_worker', null, w);
  perform pg_temp.grant_cols('update', 'leads', 'gazi_worker', null, w);

  -- campaign_steps: everyone reads, member edits
  grant select, delete on public.campaign_steps to authenticated;
  perform pg_temp.grant_cols('insert', 'campaign_steps', 'authenticated', null, w);
  perform pg_temp.grant_cols('update', 'campaign_steps', 'authenticated', null, w || array['campaign_id']);
  grant select on public.campaign_steps to gazi_worker;

  -- lead_messages: worker writes, member edits copy and approves
  grant select on public.lead_messages to authenticated;
  grant update (subject, body, status, approved_by, approved_at) on public.lead_messages to authenticated;
  grant select on public.lead_messages to gazi_worker;
  perform pg_temp.grant_cols('insert', 'lead_messages', 'gazi_worker', null, w);
  perform pg_temp.grant_cols('update', 'lead_messages', 'gazi_worker', null, w);

  -- bookings, campaign_reports: read-only for people, written by workers
  grant select on public.bookings, public.campaign_reports to authenticated;
  grant select on public.bookings, public.campaign_reports to gazi_worker;
  perform pg_temp.grant_cols('insert', 'bookings', 'gazi_worker', null, w);
  perform pg_temp.grant_cols('update', 'bookings', 'gazi_worker', null, w);
  perform pg_temp.grant_cols('insert', 'campaign_reports', 'gazi_worker', null, w);
  perform pg_temp.grant_cols('update', 'campaign_reports', 'gazi_worker', null, w);
end
$$;

-- ---------------------------------------------------------------------------
-- RLS
-- ---------------------------------------------------------------------------
alter table public.campaign_steps enable row level security;
alter table public.campaign_steps force row level security;
alter table public.lead_messages enable row level security;
alter table public.lead_messages force row level security;
alter table public.bookings enable row level security;
alter table public.bookings force row level security;
alter table public.campaign_reports enable row level security;
alter table public.campaign_reports force row level security;

create policy campaign_steps_select on public.campaign_steps for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('viewer')));
create policy campaign_steps_insert on public.campaign_steps for insert to authenticated
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('member')));
create policy campaign_steps_update on public.campaign_steps for update to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('member')))
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('member')));
create policy campaign_steps_delete on public.campaign_steps for delete to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('member')));

create policy lead_messages_select on public.lead_messages for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('viewer')));
create policy lead_messages_update on public.lead_messages for update to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('member')))
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('member')));

create policy bookings_select on public.bookings for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('viewer')));
create policy campaign_reports_select on public.campaign_reports for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('viewer')));

do $$
declare
  t text;
begin
  foreach t in array array['campaign_steps', 'lead_messages', 'bookings', 'campaign_reports'] loop
    execute format(
      'create policy %I on public.%I for all to gazi_worker
         using (tenant_id = (select app.tenant_id())) with check (tenant_id = (select app.tenant_id()))',
      t || '_worker', t);
  end loop;
end
$$;

drop function pg_temp.grant_cols(text, text, text, text[], text[]);
