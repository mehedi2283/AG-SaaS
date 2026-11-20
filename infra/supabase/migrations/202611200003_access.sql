-- Access control: SECURITY DEFINER functions, the Supabase access-token hook, grants and RLS policies.
-- Shorthand in comments: T = row's tenant is the caller's active tenant; R(x) = caller holds role >= x
-- there (re-checked against memberships on every call, so a stale JWT loses access immediately).

-- Supabase grants ALL on new public objects to anon/authenticated/service_role by default. Stop that:
-- every grant in this repo is explicit.
alter default privileges in schema public revoke all on tables from public, anon, authenticated, service_role;
alter default privileges in schema public revoke all on sequences from public, anon, authenticated, service_role;
alter default privileges in schema public revoke all on functions from public, anon, authenticated, service_role;
alter default privileges in schema app revoke all on functions from public;

revoke all on all tables in schema public from public, anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- SECURITY DEFINER functions (owned by gazi_definer, search_path pinned to '')
-- ---------------------------------------------------------------------------

-- R(min_role) for the caller in the active tenant. The tenant must be active.
create or replace function app.has_role(p_min_role text) returns boolean
language sql stable security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.memberships m
    join public.tenants t on t.id = m.tenant_id
    where m.tenant_id = (select app.tenant_id())
      and m.user_id = (select app.user_id())
      and m.status = 'active'
      and t.status = 'active'
      and app.role_rank(m.role) >= app.role_rank(p_min_role)
  )
$$;

-- Tenants the caller belongs to (for the workspace switcher). Spans tenants on purpose; returns
-- only the caller's own memberships.
create or replace function app.my_tenants()
returns table (tenant_id uuid, name text, slug text, role text, membership_status text)
language sql stable security definer
set search_path = ''
as $$
  select t.id, t.name, t.slug, m.role, m.status
  from public.memberships m
  join public.tenants t on t.id = m.tenant_id
  where m.user_id = (select app.user_id()) and t.status = 'active'
  order by t.name
$$;

-- Self-serve workspace creation: the tenant plus the caller's owner membership, atomically.
-- p_wrapped_dek is generated and KEK-wrapped by the backend (app/core/crypto.py).
create or replace function app.create_tenant(p_name text, p_slug text, p_wrapped_dek bytea)
returns uuid
language plpgsql volatile security definer
set search_path = ''
as $$
declare
  v_user uuid := app.user_id();
  v_tenant uuid;
begin
  if v_user is null or not exists (select 1 from public.users u where u.id = v_user) then
    raise exception 'create_tenant requires an authenticated user' using errcode = 'insufficient_privilege';
  end if;
  if p_wrapped_dek is null or length(p_wrapped_dek) < 32 then
    raise exception 'create_tenant requires a wrapped data key' using errcode = 'invalid_parameter_value';
  end if;

  insert into public.tenants (name, slug, wrapped_dek) values (p_name, p_slug, p_wrapped_dek)
  returning id into v_tenant;

  insert into public.memberships (tenant_id, user_id, role, status)
  values (v_tenant, v_user, 'owner', 'active');

  return v_tenant;
end
$$;

-- The active tenant's wrapped DEK, for admins encrypting integration/sender secrets in the API.
-- Useless without the KEK, but still never part of a normal SELECT on tenants.
create or replace function app.current_tenant_dek()
returns table (wrapped_dek bytea, dek_version int)
language sql stable security definer
set search_path = ''
as $$
  select t.wrapped_dek, t.dek_version
  from public.tenants t
  where t.id = (select app.tenant_id()) and (select app.has_role('admin'))
$$;

-- Webhook ingress: URL token hash -> endpoint + tenant. Revoked tokens and inactive tenants resolve
-- to nothing (the handler answers 404). Callable by gazi_worker only.
create or replace function app.resolve_webhook_token(p_token_hash bytea)
returns table (endpoint_id uuid, tenant_id uuid, provider text, has_secret boolean)
language sql stable security definer
set search_path = ''
as $$
  select e.id, e.tenant_id, e.provider, e.secret_ciphertext is not null
  from public.webhook_endpoints e
  join public.tenants t on t.id = e.tenant_id
  where e.token_hash = p_token_hash and e.revoked_at is null and t.status = 'active'
$$;

-- Mirror auth.users into public.users.
create or replace function app.sync_auth_user() returns trigger
language plpgsql security definer
set search_path = ''
as $$
begin
  if tg_op = 'INSERT' then
    insert into public.users (id, email) values (new.id, lower(btrim(coalesce(new.email, ''))));
  elsif new.email is distinct from old.email then
    update public.users set email = lower(btrim(coalesce(new.email, ''))) where id = new.id;
  end if;
  return new;
end
$$;

drop trigger if exists gazi_sync_auth_user on auth.users;
create trigger gazi_sync_auth_user after insert or update of email on auth.users
  for each row execute function app.sync_auth_user();

-- Supabase Custom Access Token Hook (enable in GoTrue: GOTRUE_HOOK_CUSTOM_ACCESS_TOKEN_ENABLED=true,
-- GOTRUE_HOOK_CUSTOM_ACCESS_TOKEN_URI=pg-functions://postgres/app/custom_access_token_hook).
-- Puts tenant_id + tenant_role into the JWT ONLY for a live, active membership in an active tenant.
-- Selection: app_metadata.active_tenant_id (set by the backend after its own membership check),
-- falling back to the user's earliest active membership. No membership -> no tenant claims.
create or replace function app.custom_access_token_hook(event jsonb) returns jsonb
language plpgsql stable security definer
set search_path = ''
as $$
declare
  v_claims jsonb := coalesce(event -> 'claims', '{}'::jsonb) - 'tenant_id' - 'tenant_role';
  v_user_text text := event ->> 'user_id';
  v_wanted_text text := v_claims -> 'app_metadata' ->> 'active_tenant_id';
  v_uuid_re constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
  v_user uuid;
  v_wanted uuid;
  v_tenant uuid;
  v_role text;
begin
  if v_user_text is null or v_user_text !~* v_uuid_re then
    return jsonb_build_object('claims', v_claims);
  end if;
  v_user := v_user_text::uuid;
  if v_wanted_text ~* v_uuid_re then
    v_wanted := v_wanted_text::uuid;
  end if;

  select m.tenant_id, m.role into v_tenant, v_role
  from public.memberships m
  join public.tenants t on t.id = m.tenant_id
  where m.user_id = v_user and m.status = 'active' and t.status = 'active'
  order by (m.tenant_id = v_wanted) desc nulls last, m.created_at
  limit 1;

  if v_tenant is not null then
    v_claims := v_claims || jsonb_build_object('tenant_id', v_tenant, 'tenant_role', v_role);
  end if;
  return jsonb_build_object('claims', v_claims);
end
$$;

alter function app.has_role(text) owner to gazi_definer;
alter function app.my_tenants() owner to gazi_definer;
alter function app.create_tenant(text, text, bytea) owner to gazi_definer;
alter function app.current_tenant_dek() owner to gazi_definer;
alter function app.resolve_webhook_token(bytea) owner to gazi_definer;
alter function app.sync_auth_user() owner to gazi_definer;
alter function app.custom_access_token_hook(jsonb) owner to gazi_definer;

revoke all on all functions in schema app from public;
revoke all on function app.custom_access_token_hook(jsonb) from public, anon, authenticated, service_role;
grant usage on schema app to supabase_auth_admin;

grant execute on function app.jwt_claims(), app.user_id(), app.tenant_id(), app.role_rank(text)
  to authenticated, gazi_worker, gazi_definer;
grant execute on function app.has_role(text) to authenticated, gazi_definer;
grant execute on function app.my_tenants(), app.create_tenant(text, text, bytea), app.current_tenant_dek()
  to authenticated;
grant execute on function app.resolve_webhook_token(bytea) to gazi_worker;
grant execute on function app.custom_access_token_hook(jsonb) to supabase_auth_admin;

-- ---------------------------------------------------------------------------
-- Grants. Column lists are generated so that NO role can write tenant_id (it always comes from the
-- column default app.tenant_id()), id or timestamps. Secret columns are never readable by clients.
-- ---------------------------------------------------------------------------
create function pg_temp.grant_cols(p_priv text, p_table text, p_role text, p_except text[] default '{}')
returns void language plpgsql as $$
declare
  v_cols text;
begin
  select string_agg(quote_ident(column_name), ', ' order by ordinal_position) into v_cols
  from information_schema.columns
  where table_schema = 'public' and table_name = p_table
    and column_name <> all (p_except)
    and is_generated = 'NEVER';
  execute format('grant %s (%s) on public.%I to %I', p_priv, v_cols, p_table, p_role);
end
$$;

do $$
declare
  w constant text[] := array['id', 'tenant_id', 'created_at', 'updated_at'];  -- never written by clients
  t text;
begin
  -- authenticated (API requests; RLS narrows rows, grants narrow columns/verbs)
  perform pg_temp.grant_cols('select', 'tenants', 'authenticated', array['wrapped_dek']);
  grant update (name, settings) on public.tenants to authenticated;

  grant select on public.users to authenticated;
  grant update (full_name) on public.users to authenticated;

  grant select, delete on public.memberships to authenticated;
  perform pg_temp.grant_cols('insert', 'memberships', 'authenticated', w);
  grant update (role, status) on public.memberships to authenticated;

  perform pg_temp.grant_cols('select', 'integrations', 'authenticated', array['config_ciphertext', 'config_nonce']);
  perform pg_temp.grant_cols('insert', 'integrations', 'authenticated', w);
  perform pg_temp.grant_cols('update', 'integrations', 'authenticated', w || array['provider']);
  grant delete on public.integrations to authenticated;

  perform pg_temp.grant_cols('select', 'sender_identities', 'authenticated',
                             array['signature_ciphertext', 'signature_nonce']);
  perform pg_temp.grant_cols('insert', 'sender_identities', 'authenticated', w);
  perform pg_temp.grant_cols('update', 'sender_identities', 'authenticated', w);
  grant delete on public.sender_identities to authenticated;

  grant select on public.prompt_templates to authenticated;
  perform pg_temp.grant_cols('insert', 'prompt_templates', 'authenticated', w);
  grant update (is_active) on public.prompt_templates to authenticated;

  foreach t in array array['campaigns', 'leads'] loop
    execute format('grant select, delete on public.%I to authenticated', t);
    perform pg_temp.grant_cols('insert', t, 'authenticated', w);
    perform pg_temp.grant_cols('update', t, 'authenticated', w);
  end loop;

  grant select on public.lead_imports to authenticated;
  perform pg_temp.grant_cols('insert', 'lead_imports', 'authenticated', w);

  grant select on public.lead_duplicates to authenticated;
  grant update (resolution, resolved_by, resolved_at) on public.lead_duplicates to authenticated;

  grant select on public.generated_emails to authenticated;
  grant update (subject, body, personalization_1, personalization_2, personalization_3, personalization_4,
                personalization_5, status, approved_by, approved_at) on public.generated_emails to authenticated;

  grant select on public.replies to authenticated;
  grant update (intent, draft_body, final_body, status, approved_by, approved_at) on public.replies to authenticated;

  grant select on public.usage_logs to authenticated;

  grant select on public.audit_log to authenticated;
  perform pg_temp.grant_cols('insert', 'audit_log', 'authenticated', array['id', 'tenant_id', 'created_at']);

  perform pg_temp.grant_cols('select', 'webhook_endpoints', 'authenticated',
                             array['token_hash', 'secret_ciphertext', 'secret_nonce']);
  perform pg_temp.grant_cols('insert', 'webhook_endpoints', 'authenticated', w || array['revoked_at', 'last_used_at']);
  grant update (revoked_at) on public.webhook_endpoints to authenticated;

  grant select on public.webhook_events to authenticated;

  -- gazi_worker (jobs + webhook ingress; tenant from app.tenant_id GUC)
  grant select on public.tenants, public.integrations, public.sender_identities, public.prompt_templates,
                  public.campaigns, public.webhook_endpoints
    to gazi_worker;
  grant update (status, last_verified_at, last_error) on public.integrations to gazi_worker;
  grant update (status) on public.campaigns to gazi_worker;
  grant update (last_used_at) on public.webhook_endpoints to gazi_worker;
  foreach t in array array['leads', 'lead_imports', 'lead_duplicates', 'generated_emails', 'webhook_events', 'replies'] loop
    execute format('grant select on public.%I to gazi_worker', t);
    perform pg_temp.grant_cols('insert', t, 'gazi_worker', w);
    perform pg_temp.grant_cols('update', t, 'gazi_worker', w);
  end loop;
  grant select on public.usage_logs to gazi_worker;
  perform pg_temp.grant_cols('insert', 'usage_logs', 'gazi_worker', array['id', 'tenant_id', 'created_at']);
  perform pg_temp.grant_cols('insert', 'audit_log', 'gazi_worker', array['id', 'tenant_id', 'created_at']);

  -- gazi_definer (only what the SECURITY DEFINER functions above need)
  grant select, insert on public.tenants, public.memberships to gazi_definer;
  grant select, insert on public.users to gazi_definer;
  grant update (email) on public.users to gazi_definer;
  grant select on public.webhook_endpoints to gazi_definer;
end
$$;

-- ---------------------------------------------------------------------------
-- Row Level Security: enabled AND forced on every table.
-- ---------------------------------------------------------------------------
do $$
declare
  t text;
begin
  for t in select tablename from pg_tables where schemaname = 'public' loop
    execute format('alter table public.%I enable row level security', t);
    execute format('alter table public.%I force row level security', t);
  end loop;
end
$$;

-- tenants
create policy tenants_select on public.tenants for select to authenticated
  using (id = (select app.tenant_id()) and (select app.has_role('viewer')));
create policy tenants_update on public.tenants for update to authenticated
  using (id = (select app.tenant_id()) and (select app.has_role('admin')))
  with check (id = (select app.tenant_id()) and (select app.has_role('admin')));
create policy tenants_worker on public.tenants for select to gazi_worker
  using (id = (select app.tenant_id()));
create policy tenants_definer_select on public.tenants for select to gazi_definer using (true);
create policy tenants_definer_insert on public.tenants for insert to gazi_definer with check (true);

-- users: yourself, or people who share your active tenant
create policy users_select on public.users for select to authenticated
  using (
    id = (select app.user_id())
    or ((select app.has_role('viewer')) and exists (
          select 1 from public.memberships m where m.user_id = users.id and m.tenant_id = (select app.tenant_id())))
  );
create policy users_update_self on public.users for update to authenticated
  using (id = (select app.user_id())) with check (id = (select app.user_id()));
create policy users_definer_select on public.users for select to gazi_definer using (true);
create policy users_definer_insert on public.users for insert to gazi_definer with check (true);
create policy users_definer_update on public.users for update to gazi_definer using (true) with check (true);

-- memberships: T∧R(viewer) read, T∧R(admin) write; owner rules enforced by trigger
create policy memberships_select on public.memberships for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('viewer')));
create policy memberships_insert on public.memberships for insert to authenticated
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));
create policy memberships_update on public.memberships for update to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')))
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));
create policy memberships_delete on public.memberships for delete to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));
create policy memberships_definer_select on public.memberships for select to gazi_definer using (true);
create policy memberships_definer_insert on public.memberships for insert to gazi_definer with check (true);

-- integrations: admin only; delete owner only
create policy integrations_select on public.integrations for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));
create policy integrations_insert on public.integrations for insert to authenticated
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));
create policy integrations_update on public.integrations for update to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')))
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));
create policy integrations_delete on public.integrations for delete to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('owner')));

-- sender_identities, prompt_templates: everyone reads, admin writes
create policy sender_identities_select on public.sender_identities for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('viewer')));
create policy sender_identities_insert on public.sender_identities for insert to authenticated
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));
create policy sender_identities_update on public.sender_identities for update to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')))
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));
create policy sender_identities_delete on public.sender_identities for delete to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));

create policy prompt_templates_select on public.prompt_templates for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('viewer')));
create policy prompt_templates_insert on public.prompt_templates for insert to authenticated
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));
create policy prompt_templates_update on public.prompt_templates for update to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')))
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));

-- campaigns, leads: everyone reads, member writes, admin deletes
create policy campaigns_select on public.campaigns for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('viewer')));
create policy campaigns_insert on public.campaigns for insert to authenticated
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('member')));
create policy campaigns_update on public.campaigns for update to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('member')))
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('member')));
create policy campaigns_delete on public.campaigns for delete to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));

create policy leads_select on public.leads for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('viewer')));
create policy leads_insert on public.leads for insert to authenticated
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('member')));
create policy leads_update on public.leads for update to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('member')))
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('member')));
create policy leads_delete on public.leads for delete to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));

-- lead_imports: everyone reads, member uploads; processing is worker-only
create policy lead_imports_select on public.lead_imports for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('viewer')));
create policy lead_imports_insert on public.lead_imports for insert to authenticated
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('member')));

-- lead_duplicates, generated_emails, replies: everyone reads, member reviews/edits; worker creates
create policy lead_duplicates_select on public.lead_duplicates for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('viewer')));
create policy lead_duplicates_update on public.lead_duplicates for update to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('member')))
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('member')));

create policy generated_emails_select on public.generated_emails for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('viewer')));
create policy generated_emails_update on public.generated_emails for update to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('member')))
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('member')));

create policy replies_select on public.replies for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('viewer')));
create policy replies_update on public.replies for update to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('member')))
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('member')));

-- usage_logs, audit_log: admin reads; append-only (triggers block UPDATE/DELETE for everyone)
create policy usage_logs_select on public.usage_logs for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));

create policy audit_log_select on public.audit_log for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));
create policy audit_log_insert on public.audit_log for insert to authenticated
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('viewer'))
              and actor_kind = 'user' and actor_user_id = (select app.user_id()));

-- webhook_endpoints, webhook_events: admin only
create policy webhook_endpoints_select on public.webhook_endpoints for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));
create policy webhook_endpoints_insert on public.webhook_endpoints for insert to authenticated
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));
create policy webhook_endpoints_update on public.webhook_endpoints for update to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')))
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));
create policy webhook_endpoints_definer on public.webhook_endpoints for select to gazi_definer using (true);

create policy webhook_events_select on public.webhook_events for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));

-- Workers: one policy per tenant table, scoped to app.tenant_id(). Grants above limit the verbs.
do $$
declare
  t text;
begin
  foreach t in array array[
    'integrations', 'sender_identities', 'prompt_templates', 'campaigns', 'lead_imports', 'leads',
    'lead_duplicates', 'generated_emails', 'webhook_endpoints', 'webhook_events', 'replies',
    'usage_logs', 'audit_log'
  ] loop
    execute format(
      'create policy %I on public.%I for all to gazi_worker
         using (tenant_id = (select app.tenant_id())) with check (tenant_id = (select app.tenant_id()))',
      t || '_worker', t);
  end loop;
end
$$;
