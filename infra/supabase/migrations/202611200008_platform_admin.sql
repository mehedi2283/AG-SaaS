-- Platform superusers (decided 2026-11-20). A superuser sees an ACCOUNT OVERVIEW of every workspace
-- (workspaces, users, roles, billing state, token usage, audit history) but never tenant content
-- (leads, campaigns, messages, replies). Actions: suspend/reactivate a workspace, disable/enable a login
-- (GoTrue ban, done by the API), change/remove members, free access until a date.
--
-- How it stays safe:
--   * Superusers are rows in platform_admins. NO application role can write that table; a person is made
--     superuser only by running SQL as the database owner (docs/runbooks/local-dev.md).
--   * Cross-tenant reads and writes happen ONLY inside the SECURITY DEFINER functions below. Each one
--     first checks app.is_platform_admin() for the user in the verified JWT and raises otherwise.
--     Plain table access (and RLS on every tenant table) is unchanged.
--   * Every superuser action is appended to platform_audit (append-only, superusers read it).
-- platform_admins and platform_audit are platform-level, so they have no tenant_id (documented exemption).

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------
create table public.platform_admins (
  user_id     uuid primary key references public.users (id) on delete cascade,
  note        text check (note is null or length(note) <= 200),
  created_at  timestamptz not null default now()
);

create table public.platform_audit (
  id                uuid primary key default gen_random_uuid(),
  actor_user_id     uuid references public.users (id) on delete set null,
  action            text not null check (length(action) between 1 and 100),
  target_tenant_id  uuid references public.tenants (id) on delete set null,
  target_user_id    uuid references public.users (id) on delete set null,
  diff              jsonb not null default '{}'::jsonb,
  request_id        text,
  created_at        timestamptz not null default now()
);
create index platform_audit_period_idx on public.platform_audit (created_at desc);
create trigger platform_audit_append_only before update or delete on public.platform_audit
  for each row execute function app.forbid_mutation();

-- Free access without Stripe charging (comp). Billing access treats the workspace as active until then.
alter table public.tenants add column comp_until timestamptz;

-- Login facts for the superuser overview, mirrored from auth.users by the existing sync trigger, so nothing
-- in Gazi needs access to the auth schema.
alter table public.users
  add column last_sign_in_at timestamptz,
  add column banned_until timestamptz;

create or replace function app.sync_auth_user() returns trigger
language plpgsql security definer
set search_path = ''
as $$
begin
  if tg_op = 'INSERT' then
    insert into public.users (id, email, last_sign_in_at, banned_until)
    values (new.id, lower(btrim(coalesce(new.email, ''))), new.last_sign_in_at, new.banned_until);
  elsif new.email is distinct from old.email
     or new.last_sign_in_at is distinct from old.last_sign_in_at
     or new.banned_until is distinct from old.banned_until then
    update public.users
       set email = lower(btrim(coalesce(new.email, ''))),
           last_sign_in_at = new.last_sign_in_at,
           banned_until = new.banned_until
     where id = new.id;
  end if;
  return new;
end
$$;
alter function app.sync_auth_user() owner to gazi_definer;
revoke all on function app.sync_auth_user() from public;

drop trigger if exists gazi_sync_auth_user on auth.users;
create trigger gazi_sync_auth_user after insert or update of email, last_sign_in_at, banned_until on auth.users
  for each row execute function app.sync_auth_user();

grant update (last_sign_in_at, banned_until) on public.users to gazi_definer;
grant insert (last_sign_in_at, banned_until) on public.users to gazi_definer;

-- Existing accounts (the migration role bypasses RLS).
update public.users u
   set last_sign_in_at = a.last_sign_in_at, banned_until = a.banned_until
  from auth.users a
 where a.id = u.id;

-- ---------------------------------------------------------------------------
-- Access state, shared by the workspace view and the superuser overview
-- ---------------------------------------------------------------------------
create or replace function app.access_state(p_status text, p_lapsed_at timestamptz, p_comp_until timestamptz)
returns text
language sql stable
set search_path = ''
as $$
  select case
    when p_comp_until > now() then 'active'
    when p_status in ('trialing', 'active') then 'active'
    when p_lapsed_at is null then 'none'
    when now() < p_lapsed_at + interval '7 days' then 'grace'
    else 'paused'
  end
$$;

create or replace function app.billing_access()
returns table (access text, grace_ends_at timestamptz)
language sql stable
set search_path = ''
as $$
  select
    app.access_state(s.status, s.lapsed_at, t.comp_until),
    case
      when t.comp_until > now() or s.status in ('trialing', 'active') then null
      else s.lapsed_at + interval '7 days'
    end
  from (select null::int as one) d
  left join public.tenants t on t.id = (select app.tenant_id())
  left join public.subscriptions s on s.tenant_id = (select app.tenant_id())
$$;

-- ---------------------------------------------------------------------------
-- Superuser check and audit
-- ---------------------------------------------------------------------------
create or replace function app.is_platform_admin()
returns boolean
language sql stable security definer
set search_path = ''
as $$
  select exists (select 1 from public.platform_admins p where p.user_id = (select app.user_id()))
$$;

create or replace function app.require_platform_admin()
returns uuid
language plpgsql stable security definer
set search_path = ''
as $$
declare
  v_user uuid := app.user_id();
begin
  if v_user is null or not exists (select 1 from public.platform_admins p where p.user_id = v_user) then
    raise exception 'platform admin only' using errcode = 'insufficient_privilege';
  end if;
  return v_user;
end
$$;

-- Also used by the API for actions it performs outside the database (disabling a login in GoTrue).
create or replace function app.admin_log(p_action text, p_tenant uuid, p_user uuid, p_diff jsonb)
returns void
language plpgsql volatile security definer
set search_path = ''
as $$
begin
  insert into public.platform_audit (actor_user_id, action, target_tenant_id, target_user_id, diff, request_id)
  values (app.require_platform_admin(), p_action, p_tenant, p_user, coalesce(p_diff, '{}'::jsonb),
          nullif(current_setting('app.request_id', true), ''));
end
$$;

-- ---------------------------------------------------------------------------
-- Overview (read)
-- ---------------------------------------------------------------------------
create or replace function app.admin_tenants()
returns table (
  id uuid, name text, slug text, status text, created_at timestamptz, members int, owner_email text,
  subscription_status text, trial_end timestamptz, current_period_end timestamptz, cancel_at_period_end boolean,
  access text, comp_until timestamptz, addon_tokens bigint, tokens_this_month bigint,
  stripe_customer_id text, stripe_subscription_id text
)
language plpgsql stable security definer
set search_path = ''
as $$
#variable_conflict use_column
begin
  perform app.require_platform_admin();
  return query
  select t.id, t.name, t.slug, t.status, t.created_at,
         (select count(*)::int from public.memberships m where m.tenant_id = t.id and m.status = 'active'),
         (select u.email from public.memberships m join public.users u on u.id = m.user_id
           where m.tenant_id = t.id and m.role = 'owner' and m.status = 'active' order by m.created_at limit 1),
         coalesce(s.status, 'none'), s.trial_end, s.current_period_end, coalesce(s.cancel_at_period_end, false),
         app.access_state(s.status, s.lapsed_at, t.comp_until), t.comp_until, coalesce(s.addon_tokens, 0),
         (select coalesce(sum(l.total_tokens), 0)::bigint from public.usage_logs l
           where l.tenant_id = t.id
             and l.created_at >= date_trunc('month', now() at time zone 'utc') at time zone 'utc'),
         s.stripe_customer_id, s.stripe_subscription_id
  from public.tenants t
  left join public.subscriptions s on s.tenant_id = t.id
  where t.status <> 'deleted'
  order by t.created_at desc;
end
$$;

create or replace function app.admin_users()
returns table (
  id uuid, email text, created_at timestamptz, last_sign_in_at timestamptz, banned_until timestamptz,
  workspaces int, is_platform_admin boolean
)
language plpgsql stable security definer
set search_path = ''
as $$
#variable_conflict use_column
begin
  perform app.require_platform_admin();
  return query
  select u.id, u.email, u.created_at, u.last_sign_in_at, u.banned_until,
         (select count(*)::int from public.memberships m where m.user_id = u.id and m.status = 'active'),
         exists (select 1 from public.platform_admins p where p.user_id = u.id)
  from public.users u
  order by u.created_at desc;
end
$$;

create or replace function app.admin_members(p_tenant uuid)
returns table (user_id uuid, email text, role text, status text, created_at timestamptz)
language plpgsql stable security definer
set search_path = ''
as $$
#variable_conflict use_column
begin
  perform app.require_platform_admin();
  return query
  select m.user_id, u.email, m.role, m.status, m.created_at
  from public.memberships m join public.users u on u.id = m.user_id
  where m.tenant_id = p_tenant
  order by app.role_rank(m.role) desc, u.email;
end
$$;

-- Recent history across the platform: superuser actions plus every workspace's audit_log (no secrets are
-- ever written to audit diffs).
create or replace function app.admin_activity(p_limit int default 200)
returns table (
  source text, created_at timestamptz, tenant_id uuid, tenant_name text, actor_email text, action text,
  target_user_email text, diff jsonb
)
language plpgsql stable security definer
set search_path = ''
as $$
#variable_conflict use_column
begin
  perform app.require_platform_admin();
  return query
  select * from (
    select 'platform'::text, p.created_at, p.target_tenant_id, t.name, a.email, p.action, tu.email, p.diff
    from public.platform_audit p
    left join public.tenants t on t.id = p.target_tenant_id
    left join public.users a on a.id = p.actor_user_id
    left join public.users tu on tu.id = p.target_user_id
    union all
    select 'workspace'::text, l.created_at, l.tenant_id, t.name, a.email, l.action, null::text, l.diff
    from public.audit_log l
    join public.tenants t on t.id = l.tenant_id
    left join public.users a on a.id = l.actor_user_id
  ) x
  order by 2 desc
  limit least(greatest(coalesce(p_limit, 200), 1), 1000);
end
$$;

-- ---------------------------------------------------------------------------
-- Actions (write). Each logs to platform_audit.
-- ---------------------------------------------------------------------------
create or replace function app.admin_set_tenant_status(p_tenant uuid, p_status text)
returns void
language plpgsql volatile security definer
set search_path = ''
as $$
declare
  v_old text;
begin
  perform app.require_platform_admin();
  if p_status not in ('active', 'suspended') then
    raise exception 'status must be active or suspended' using errcode = 'invalid_parameter_value';
  end if;
  select status into v_old from public.tenants where id = p_tenant and status <> 'deleted' for update;
  if v_old is null then
    raise exception 'no such workspace' using errcode = 'no_data_found';
  end if;
  update public.tenants set status = p_status where id = p_tenant;
  perform app.admin_log('workspace.status_changed', p_tenant, null,
                        jsonb_build_object('from', v_old, 'to', p_status));
end
$$;

create or replace function app.admin_set_comp(p_tenant uuid, p_until timestamptz)
returns void
language plpgsql volatile security definer
set search_path = ''
as $$
declare
  v_old timestamptz;
begin
  perform app.require_platform_admin();
  -- 730 days is also Stripe's limit for a trial end, which the API moves along with free access.
  if p_until is not null and (p_until <= now() or p_until > now() + interval '730 days') then
    raise exception 'free access must end in the future and within 730 days' using errcode = 'invalid_parameter_value';
  end if;
  select comp_until into v_old from public.tenants where id = p_tenant and status <> 'deleted' for update;
  if not found then
    raise exception 'no such workspace' using errcode = 'no_data_found';
  end if;
  update public.tenants set comp_until = p_until where id = p_tenant;
  perform app.admin_log('workspace.free_access_changed', p_tenant, null,
                        jsonb_build_object('from', v_old, 'to', p_until));
end
$$;

-- The owner-rules trigger still applies: a workspace always keeps at least one active owner.
create or replace function app.admin_set_member_role(p_tenant uuid, p_user uuid, p_role text)
returns void
language plpgsql volatile security definer
set search_path = ''
as $$
declare
  v_old text;
begin
  perform app.require_platform_admin();
  if p_role not in ('owner', 'admin', 'member', 'viewer') then
    raise exception 'unknown role' using errcode = 'invalid_parameter_value';
  end if;
  select role into v_old from public.memberships where tenant_id = p_tenant and user_id = p_user for update;
  if v_old is null then
    raise exception 'not a member of that workspace' using errcode = 'no_data_found';
  end if;
  update public.memberships set role = p_role where tenant_id = p_tenant and user_id = p_user;
  perform app.admin_log('member.role_changed', p_tenant, p_user, jsonb_build_object('from', v_old, 'to', p_role));
end
$$;

create or replace function app.admin_remove_member(p_tenant uuid, p_user uuid)
returns void
language plpgsql volatile security definer
set search_path = ''
as $$
declare
  v_old text;
begin
  perform app.require_platform_admin();
  delete from public.memberships where tenant_id = p_tenant and user_id = p_user returning role into v_old;
  if v_old is null then
    raise exception 'not a member of that workspace' using errcode = 'no_data_found';
  end if;
  perform app.admin_log('member.removed', p_tenant, p_user, jsonb_build_object('role', v_old));
end
$$;

-- ---------------------------------------------------------------------------
-- Ownership, grants, RLS
-- ---------------------------------------------------------------------------
do $$
declare
  f text;
begin
  foreach f in array array[
    'app.is_platform_admin()', 'app.require_platform_admin()', 'app.admin_log(text, uuid, uuid, jsonb)',
    'app.admin_tenants()', 'app.admin_users()', 'app.admin_members(uuid)', 'app.admin_activity(int)',
    'app.admin_set_tenant_status(uuid, text)', 'app.admin_set_comp(uuid, timestamptz)',
    'app.admin_set_member_role(uuid, uuid, text)', 'app.admin_remove_member(uuid, uuid)'
  ] loop
    execute format('alter function %s owner to gazi_definer', f);
    execute format('revoke all on function %s from public', f);
    execute format('grant execute on function %s to authenticated', f);
  end loop;
end
$$;
revoke all on function app.access_state(text, timestamptz, timestamptz) from public;
grant execute on function app.access_state(text, timestamptz, timestamptz) to authenticated, gazi_worker, gazi_definer;
-- The definer functions call each other; gazi_definer must be able to execute them.
grant execute on function app.require_platform_admin(), app.admin_log(text, uuid, uuid, jsonb) to gazi_definer;

revoke all on public.platform_admins, public.platform_audit from public, anon, authenticated, service_role;

-- Clients may only check their own row (the app shows the Admin link from /v1/me, which uses the function).
grant select on public.platform_admins to authenticated;
grant select on public.platform_admins, public.platform_audit to gazi_definer;
grant insert (actor_user_id, action, target_tenant_id, target_user_id, diff, request_id)
  on public.platform_audit to gazi_definer;

-- What the definer functions need beyond 0003/0007.
grant update (status, comp_until) on public.tenants to gazi_definer;
grant update (role) on public.memberships to gazi_definer;
grant delete on public.memberships to gazi_definer;
grant select on public.subscriptions, public.audit_log to gazi_definer;
grant select (comp_until) on public.tenants to authenticated;

alter table public.platform_admins enable row level security;
alter table public.platform_admins force row level security;
alter table public.platform_audit enable row level security;
alter table public.platform_audit force row level security;

create policy platform_admins_self on public.platform_admins for select to authenticated
  using (user_id = (select app.user_id()));
create policy platform_admins_definer on public.platform_admins for select to gazi_definer using (true);
create policy platform_audit_definer_select on public.platform_audit for select to gazi_definer using (true);
create policy platform_audit_definer_insert on public.platform_audit for insert to gazi_definer with check (true);

create policy tenants_definer_update on public.tenants for update to gazi_definer using (true) with check (true);
create policy memberships_definer_update on public.memberships for update to gazi_definer
  using (true) with check (true);
create policy memberships_definer_delete on public.memberships for delete to gazi_definer using (true);
create policy audit_log_definer_select on public.audit_log for select to gazi_definer using (true);
-- subscriptions_definer_select exists since 0006.
