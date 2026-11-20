-- Tables, constraints, indexes and integrity triggers.
-- Conventions:
--   * Every tenant-scoped table: tenant_id uuid NOT NULL DEFAULT app.tenant_id(). Callers never pass it.
--   * References between tenant tables are COMPOSITE (tenant_id, id), so a row can never point at
--     another tenant's row, even through a bug in application code.
--   * Exempt from tenant_id (documented in CLAUDE.md): `tenants` (its id IS the tenant) and `users`
--     (one identity spans many tenants; visibility goes through memberships).
--   * Secrets are stored only as AES-256-GCM ciphertext + nonce + dek_version (see app/core/crypto.py).

-- ---------------------------------------------------------------------------
-- tenants
-- ---------------------------------------------------------------------------
create table public.tenants (
  id           uuid primary key default gen_random_uuid(),
  name         text not null check (length(btrim(name)) between 1 and 200),
  slug         text not null unique check (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
  status       text not null default 'active' check (status in ('active', 'suspended', 'deleted')),
  plan         text not null default 'free',
  settings     jsonb not null default '{}'::jsonb check (jsonb_typeof(settings) = 'object'),
  wrapped_dek  bytea,                     -- per-tenant data key, wrapped by the KEK; never exposed to clients
  dek_version  int not null default 1 check (dek_version > 0),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- users (profile mirror of auth.users)
-- ---------------------------------------------------------------------------
create table public.users (
  id          uuid primary key references auth.users (id) on delete cascade,
  email       text not null check (email = lower(btrim(email))),
  full_name   text check (full_name is null or length(full_name) <= 200),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- memberships
-- ---------------------------------------------------------------------------
create table public.memberships (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null default app.tenant_id() references public.tenants (id) on delete cascade,
  user_id     uuid not null references public.users (id) on delete cascade,
  role        text not null check (role in ('owner', 'admin', 'member', 'viewer')),
  status      text not null default 'invited' check (status in ('invited', 'active')),
  invited_by  uuid references public.users (id) on delete set null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (tenant_id, user_id)
);
create index memberships_user_idx on public.memberships (user_id);

-- ---------------------------------------------------------------------------
-- integrations (Instantly, HeyReach, Calendly). OpenAI uses the platform key, not stored here.
-- ---------------------------------------------------------------------------
create table public.integrations (
  id                 uuid primary key default gen_random_uuid(),
  tenant_id          uuid not null default app.tenant_id() references public.tenants (id) on delete cascade,
  provider           text not null check (provider in ('instantly', 'heyreach', 'calendly')),
  config_ciphertext  bytea not null,
  config_nonce       bytea not null check (length(config_nonce) = 12),
  dek_version        int not null check (dek_version > 0),
  key_last4          text check (key_last4 is null or length(key_last4) <= 4),
  status             text not null default 'unverified' check (status in ('unverified', 'active', 'invalid')),
  last_verified_at   timestamptz,
  last_error         text,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  unique (tenant_id, provider)
);

-- ---------------------------------------------------------------------------
-- sender_identities
-- ---------------------------------------------------------------------------
create table public.sender_identities (
  id                    uuid primary key default gen_random_uuid(),
  tenant_id             uuid not null default app.tenant_id() references public.tenants (id) on delete cascade,
  display_name          text not null check (length(btrim(display_name)) between 1 and 200),
  title                 text,
  company               text,
  signature_ciphertext  bytea,
  signature_nonce       bytea check (signature_nonce is null or length(signature_nonce) = 12),
  dek_version           int check (dek_version is null or dek_version > 0),
  instantly_eaccount    text check (instantly_eaccount is null or instantly_eaccount = lower(btrim(instantly_eaccount))),
  heyreach_account_id   text,
  is_default            boolean not null default false,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  unique (tenant_id, id),
  check ((signature_ciphertext is null) = (signature_nonce is null))
);
create unique index sender_identities_one_default on public.sender_identities (tenant_id) where is_default;

-- ---------------------------------------------------------------------------
-- prompt_templates (immutable versions)
-- ---------------------------------------------------------------------------
create table public.prompt_templates (
  id                     uuid primary key default gen_random_uuid(),
  tenant_id              uuid not null default app.tenant_id() references public.tenants (id) on delete cascade,
  kind                   text not null check (kind in ('email_gen', 'reply_classify', 'reply_draft')),
  name                   text not null check (length(btrim(name)) between 1 and 200),
  version                int not null check (version > 0),
  system_prompt          text not null check (system_prompt ~* 'must not'),  -- rule 5: explicit prohibitions
  user_template          text not null,
  model                  text not null,
  temperature            numeric(3, 2) not null default 0.7 check (temperature between 0 and 2),
  output_schema_version  int not null default 1 check (output_schema_version > 0),
  is_active              boolean not null default false,
  created_by             uuid references public.users (id) on delete set null,
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now(),
  unique (tenant_id, kind, name, version),
  unique (tenant_id, id)
);
create unique index prompt_templates_one_active on public.prompt_templates (tenant_id, kind, name) where is_active;

-- ---------------------------------------------------------------------------
-- campaigns
-- ---------------------------------------------------------------------------
create table public.campaigns (
  id                     uuid primary key default gen_random_uuid(),
  tenant_id              uuid not null default app.tenant_id() references public.tenants (id) on delete cascade,
  name                   text not null check (length(btrim(name)) between 1 and 200),
  channel                text not null check (channel in ('email', 'linkedin')),
  status                 text not null default 'draft'
                           check (status in ('draft', 'active', 'paused', 'completed', 'archived')),
  instantly_campaign_id  text,
  heyreach_campaign_id   text,
  prompt_template_id     uuid,
  sender_identity_id     uuid,
  created_by             uuid references public.users (id) on delete set null,
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now(),
  unique (tenant_id, id),
  foreign key (tenant_id, prompt_template_id) references public.prompt_templates (tenant_id, id),
  foreign key (tenant_id, sender_identity_id) references public.sender_identities (tenant_id, id)
);
-- Webhook payloads name provider campaign ids; these resolve them within the tenant.
create unique index campaigns_instantly_uq on public.campaigns (tenant_id, instantly_campaign_id)
  where instantly_campaign_id is not null;
create unique index campaigns_heyreach_uq on public.campaigns (tenant_id, heyreach_campaign_id)
  where heyreach_campaign_id is not null;

-- ---------------------------------------------------------------------------
-- lead_imports (CSV kept 7 days on a local volume, then purged; data lives in leads)
-- ---------------------------------------------------------------------------
create table public.lead_imports (
  id                 uuid primary key default gen_random_uuid(),
  tenant_id          uuid not null default app.tenant_id() references public.tenants (id) on delete cascade,
  campaign_id        uuid not null,
  original_filename  text not null,
  file_path          text,                -- NULL once purged
  file_deleted_at    timestamptz,
  expires_at         timestamptz not null default now() + interval '7 days',
  status             text not null default 'uploaded'
                       check (status in ('uploaded', 'processing', 'completed', 'completed_with_errors', 'failed')),
  total_rows         int not null default 0 check (total_rows >= 0),
  imported_rows      int not null default 0 check (imported_rows >= 0),
  failed_rows        int not null default 0 check (failed_rows >= 0),
  duplicate_rows     int not null default 0 check (duplicate_rows >= 0),
  header_map         jsonb not null default '{}'::jsonb,
  errors             jsonb not null default '[]'::jsonb check (jsonb_typeof(errors) = 'array'),
  error_code         text,
  created_by         uuid references public.users (id) on delete set null,
  started_at         timestamptz,
  finished_at        timestamptz,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  unique (tenant_id, id),
  foreign key (tenant_id, campaign_id) references public.campaigns (tenant_id, id) on delete cascade
);
create index lead_imports_expiry_idx on public.lead_imports (expires_at) where file_path is not null;

-- ---------------------------------------------------------------------------
-- leads
-- ---------------------------------------------------------------------------
create table public.leads (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null default app.tenant_id() references public.tenants (id) on delete cascade,
  campaign_id       uuid not null,
  email             text not null check (email = lower(btrim(email)) and email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
  first_name        text,
  last_name         text,
  title             text,
  company           text,
  linkedin_url      text,
  website           text,
  raw               jsonb not null default '{}'::jsonb,
  status            text not null default 'new'
                      check (status in ('new', 'generating', 'ready', 'pushed', 'replied',
                                        'unsubscribed', 'bounced', 'do_not_contact')),
  source_import_id  uuid,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  unique (tenant_id, campaign_id, email),
  unique (tenant_id, id),
  foreign key (tenant_id, campaign_id) references public.campaigns (tenant_id, id) on delete cascade,
  foreign key (tenant_id, source_import_id) references public.lead_imports (tenant_id, id)
);
create index leads_tenant_email_idx on public.leads (tenant_id, email);  -- tenant-wide duplicate checks

-- ---------------------------------------------------------------------------
-- lead_duplicates (review queue; unresolved rows block nothing but are never generated/pushed)
-- ---------------------------------------------------------------------------
create table public.lead_duplicates (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null default app.tenant_id() references public.tenants (id) on delete cascade,
  import_id         uuid not null,
  existing_lead_id  uuid,
  incoming_email    text not null check (incoming_email = lower(btrim(incoming_email))),
  incoming          jsonb not null,
  reason            text not null check (reason in ('exists_in_tenant', 'repeated_in_file')),
  resolution        text not null default 'pending'
                      check (resolution in ('pending', 'skip', 'update_existing', 'add_anyway')),
  resolved_by       uuid references public.users (id) on delete set null,
  resolved_at       timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  foreign key (tenant_id, import_id) references public.lead_imports (tenant_id, id) on delete cascade,
  foreign key (tenant_id, existing_lead_id) references public.leads (tenant_id, id) on delete cascade,
  check ((resolution = 'pending') = (resolved_at is null))
);
create index lead_duplicates_pending_idx on public.lead_duplicates (tenant_id, import_id) where resolution = 'pending';

-- ---------------------------------------------------------------------------
-- generated_emails
-- ---------------------------------------------------------------------------
create table public.generated_emails (
  id                  uuid primary key default gen_random_uuid(),
  tenant_id           uuid not null default app.tenant_id() references public.tenants (id) on delete cascade,
  lead_id             uuid not null,
  campaign_id         uuid not null,
  subject             text,
  body                text,
  personalization_1   text,
  personalization_2   text,
  personalization_3   text,
  personalization_4   text,
  personalization_5   text,
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
  foreign key (tenant_id, lead_id) references public.leads (tenant_id, id) on delete cascade,
  foreign key (tenant_id, campaign_id) references public.campaigns (tenant_id, id) on delete cascade,
  foreign key (tenant_id, prompt_template_id) references public.prompt_templates (tenant_id, id),
  -- Rule 6 backstop (app/llm/guards.py does the full check): follow-ups never carry a link.
  constraint follow_ups_have_no_links check (
    concat_ws(' ', personalization_2, personalization_3, personalization_4, personalization_5)
      !~* '(https?://|www\.)'
  )
);
create index generated_emails_lead_idx on public.generated_emails (tenant_id, lead_id);
create index generated_emails_campaign_status_idx on public.generated_emails (tenant_id, campaign_id, status);

-- ---------------------------------------------------------------------------
-- webhook_endpoints (one active token per tenant + provider)
-- ---------------------------------------------------------------------------
create table public.webhook_endpoints (
  id                 uuid primary key default gen_random_uuid(),
  tenant_id          uuid not null default app.tenant_id() references public.tenants (id) on delete cascade,
  provider           text not null check (provider in ('instantly', 'heyreach')),
  token_hash         bytea not null unique check (length(token_hash) = 32),   -- sha256 of the URL token
  token_prefix       text not null check (length(token_prefix) between 4 and 12),
  secret_ciphertext  bytea,               -- optional shared-secret header value
  secret_nonce       bytea check (secret_nonce is null or length(secret_nonce) = 12),
  dek_version        int check (dek_version is null or dek_version > 0),
  created_by         uuid references public.users (id) on delete set null,
  revoked_at         timestamptz,
  last_used_at       timestamptz,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  unique (tenant_id, id),
  check ((secret_ciphertext is null) = (secret_nonce is null))
);
create unique index webhook_endpoints_one_active on public.webhook_endpoints (tenant_id, provider)
  where revoked_at is null;

-- ---------------------------------------------------------------------------
-- webhook_events (raw inbox: dedupe + replay)
-- ---------------------------------------------------------------------------
create table public.webhook_events (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null default app.tenant_id() references public.tenants (id) on delete cascade,
  provider      text not null check (provider in ('instantly', 'heyreach')),
  endpoint_id   uuid not null,
  dedupe_key    text not null check (length(dedupe_key) between 1 and 500),
  payload       jsonb not null,
  status        text not null default 'received'
                  check (status in ('received', 'queued', 'processed', 'failed', 'ignored')),
  attempts      int not null default 0 check (attempts >= 0),
  error_code    text,
  error         text,
  received_at   timestamptz not null default now(),
  processed_at  timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  unique (tenant_id, provider, dedupe_key),
  unique (tenant_id, id),
  foreign key (tenant_id, endpoint_id) references public.webhook_endpoints (tenant_id, id)
);
create index webhook_events_status_idx on public.webhook_events (status, received_at)
  where status in ('received', 'queued');

-- ---------------------------------------------------------------------------
-- replies (also the intent log)
-- ---------------------------------------------------------------------------
create table public.replies (
  id                   uuid primary key default gen_random_uuid(),
  tenant_id            uuid not null default app.tenant_id() references public.tenants (id) on delete cascade,
  lead_id              uuid,             -- NULL if the sender could not be matched to a lead
  campaign_id          uuid,
  webhook_event_id     uuid,
  channel              text not null check (channel in ('email', 'linkedin')),
  provider_message_id  text not null,
  thread_ref           jsonb not null default '{}'::jsonb,  -- Instantly email id / HeyReach account_id + conversation_id
  inbound_body         text,
  received_at          timestamptz not null default now(),
  intent               text check (intent in ('interested', 'not_interested', 'question', 'referral',
                                              'out_of_office', 'unsubscribe', 'bounce', 'other')),
  confidence           numeric(4, 3) check (confidence between 0 and 1),
  rationale            text,
  draft_body           text,
  final_body           text,
  status               text not null default 'received'
                         check (status in ('received', 'classified', 'drafted', 'awaiting_approval',
                                           'sent', 'failed', 'ignored')),
  prompt_template_id   uuid,
  prompt_version       int check (prompt_version is null or prompt_version > 0),
  error_code           text,
  approved_by          uuid references public.users (id) on delete set null,
  approved_at          timestamptz,
  sent_at              timestamptz,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  unique (tenant_id, channel, provider_message_id),
  foreign key (tenant_id, lead_id) references public.leads (tenant_id, id) on delete set null (lead_id),
  foreign key (tenant_id, campaign_id) references public.campaigns (tenant_id, id) on delete set null (campaign_id),
  foreign key (tenant_id, webhook_event_id) references public.webhook_events (tenant_id, id),
  foreign key (tenant_id, prompt_template_id) references public.prompt_templates (tenant_id, id),
  -- Rule 6 backstop: only an "interested" reply may contain a link (the calendar link, added by code).
  constraint links_only_when_interested check (
    intent = 'interested'
    or concat_ws(' ', draft_body, final_body) !~* '(https?://|www\.)'
  ),
  constraint drafted_reply_has_prompt_version check (
    draft_body is null or (prompt_template_id is not null and prompt_version is not null)
  )
);
create index replies_inbox_idx on public.replies (tenant_id, status, received_at desc);

-- ---------------------------------------------------------------------------
-- usage_logs (append-only)
-- ---------------------------------------------------------------------------
create table public.usage_logs (
  id                 uuid primary key default gen_random_uuid(),
  tenant_id          uuid not null default app.tenant_id() references public.tenants (id) on delete restrict,
  kind               text not null check (kind in ('llm', 'api')),
  provider           text not null,
  model              text,
  prompt_tokens      int not null default 0 check (prompt_tokens >= 0),
  completion_tokens  int not null default 0 check (completion_tokens >= 0),
  total_tokens       int generated always as (prompt_tokens + completion_tokens) stored,
  cost_usd_est       numeric(12, 6) not null default 0 check (cost_usd_est >= 0),
  ref_table          text,
  ref_id             uuid,
  job_id             text,
  created_at         timestamptz not null default now()
);
create index usage_logs_period_idx on public.usage_logs (tenant_id, created_at);

-- ---------------------------------------------------------------------------
-- audit_log (append-only)
-- ---------------------------------------------------------------------------
create table public.audit_log (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null default app.tenant_id() references public.tenants (id) on delete restrict,
  actor_kind     text not null check (actor_kind in ('user', 'worker', 'system')),
  actor_user_id  uuid references public.users (id) on delete set null,
  action         text not null,
  target_table   text,
  target_id      uuid,
  diff           jsonb not null default '{}'::jsonb,
  ip             inet,
  request_id     text,
  created_at     timestamptz not null default now(),
  check ((actor_kind = 'user') = (actor_user_id is not null))
);
create index audit_log_period_idx on public.audit_log (tenant_id, created_at desc);

-- ---------------------------------------------------------------------------
-- Triggers
-- ---------------------------------------------------------------------------
do $$
declare
  t text;
begin
  foreach t in array array[
    'tenants', 'users', 'memberships', 'integrations', 'sender_identities', 'prompt_templates',
    'campaigns', 'lead_imports', 'leads', 'lead_duplicates', 'generated_emails', 'webhook_endpoints',
    'webhook_events', 'replies'
  ] loop
    execute format(
      'create trigger touch_updated_at before update on public.%I
         for each row execute function app.touch_updated_at()', t);
  end loop;
end
$$;

create trigger usage_logs_append_only before update or delete on public.usage_logs
  for each row execute function app.forbid_mutation();
create trigger audit_log_append_only before update or delete on public.audit_log
  for each row execute function app.forbid_mutation();

-- Prompt versions are immutable: only is_active may change. A new wording is a new version.
create or replace function app.prompt_templates_immutable() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if (to_jsonb(new) - 'is_active' - 'updated_at') is distinct from (to_jsonb(old) - 'is_active' - 'updated_at') then
    raise exception 'prompt_templates rows are immutable; insert a new version instead'
      using errcode = 'insufficient_privilege';
  end if;
  return new;
end
$$;
create trigger prompt_templates_immutable before update on public.prompt_templates
  for each row execute function app.prompt_templates_immutable();

-- Owner rules: only an owner may grant, change or remove the owner role, and a tenant always keeps
-- at least one active owner. SECURITY DEFINER code (create_tenant) runs as gazi_definer and is exempt
-- from the "caller must be owner" check but not from the last-owner check.
create or replace function app.memberships_owner_rules() returns trigger
language plpgsql
set search_path = ''
as $$
declare
  v_tenant uuid := coalesce(new.tenant_id, old.tenant_id);
  v_caller_is_owner boolean;
begin
  if current_user <> 'gazi_definer'
     and ((tg_op in ('INSERT', 'UPDATE') and new.role = 'owner')
          or (tg_op in ('UPDATE', 'DELETE') and old.role = 'owner')) then
    select exists (
      select 1 from public.memberships m
      where m.tenant_id = v_tenant and m.user_id = app.user_id() and m.role = 'owner' and m.status = 'active'
    ) into v_caller_is_owner;
    if not v_caller_is_owner then
      raise exception 'only an owner can grant, change or remove the owner role'
        using errcode = 'insufficient_privilege';
    end if;
  end if;

  if tg_op in ('UPDATE', 'DELETE') and old.role = 'owner' and old.status = 'active'
     and (tg_op = 'DELETE' or new.role <> 'owner' or new.status <> 'active') then
    if not exists (
      select 1 from public.memberships m
      where m.tenant_id = v_tenant and m.role = 'owner' and m.status = 'active' and m.id <> old.id
    ) then
      raise exception 'a tenant must keep at least one active owner'
        using errcode = 'check_violation';
    end if;
  end if;

  if tg_op = 'UPDATE' and (new.tenant_id <> old.tenant_id or new.user_id <> old.user_id) then
    raise exception 'membership tenant_id/user_id cannot change' using errcode = 'insufficient_privilege';
  end if;

  return coalesce(new, old);
end
$$;
create trigger memberships_owner_rules before insert or update or delete on public.memberships
  for each row execute function app.memberships_owner_rules();

-- tenant_id never changes after insert, on any tenant table.
create or replace function app.tenant_id_immutable() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.tenant_id is distinct from old.tenant_id then
    raise exception 'tenant_id cannot change' using errcode = 'insufficient_privilege';
  end if;
  return new;
end
$$;
do $$
declare
  t text;
begin
  foreach t in array array[
    'integrations', 'sender_identities', 'prompt_templates', 'campaigns', 'lead_imports', 'leads',
    'lead_duplicates', 'generated_emails', 'webhook_endpoints', 'webhook_events', 'replies'
  ] loop
    execute format(
      'create trigger tenant_id_immutable before update on public.%I
         for each row execute function app.tenant_id_immutable()', t);
  end loop;
end
$$;
