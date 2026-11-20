-- Foundation: roles, the `app` schema and the request/job context helpers that every RLS policy uses.
--
-- Roles
--   authenticated  (Supabase) API requests. The backend runs `SET LOCAL ROLE authenticated` and
--                  sets `request.jwt.claims` from the VERIFIED JWT for every transaction.
--   gazi_worker    ARQ workers and webhook ingress. No BYPASSRLS. `app.tenant_id` is set per transaction
--                  by @tenant_job (or by webhook ingress after the token lookup).
--   gazi_definer   NOLOGIN owner of SECURITY DEFINER functions. Never granted to any login role except
--                  the migration role. It gets narrow RLS policies so definer functions keep working with
--                  FORCE ROW LEVEL SECURITY, whatever the migration role's superuser/BYPASSRLS status.
--
-- The backend's login role (created in infra with a secret password, not here) must be granted
-- `authenticated` and `gazi_worker` and must NOT be a superuser or BYPASSRLS:
--   create role gazi_api login noinherit password '…';  grant authenticated, gazi_worker to gazi_api;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'gazi_worker') then
    create role gazi_worker nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'gazi_definer') then
    create role gazi_definer nologin noinherit;
  end if;
end
$$;

-- Lets the migration role assign function ownership to gazi_definer.
grant gazi_definer to current_user;

create schema if not exists app;
revoke all on schema app from public;
grant usage on schema app to authenticated, gazi_worker, gazi_definer;
grant usage on schema public to gazi_worker, gazi_definer;
-- Required so SECURITY DEFINER functions in `app` can be owned by gazi_definer.
grant create on schema app to gazi_definer;

-- ---------------------------------------------------------------------------
-- Context helpers. SECURITY INVOKER on purpose: app.tenant_id() branches on current_user.
-- ---------------------------------------------------------------------------

create or replace function app.jwt_claims() returns jsonb
language sql stable
set search_path = ''
as $$
  select coalesce(nullif(current_setting('request.jwt.claims', true), '')::jsonb, '{}'::jsonb)
$$;

create or replace function app.user_id() returns uuid
language sql stable
set search_path = ''
as $$
  select case
    when (app.jwt_claims() ->> 'sub') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      then (app.jwt_claims() ->> 'sub')::uuid
  end
$$;

-- Active tenant:
--   gazi_worker            -> GUC app.tenant_id (set by @tenant_job / webhook ingress)
--   everyone else          -> verified JWT claim `tenant_id` (injected by custom_access_token_hook)
-- Never derived from request bodies. Returns NULL when absent or malformed, so policies deny.
create or replace function app.tenant_id() returns uuid
language sql stable
set search_path = ''
as $$
  select case
    when v ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then v::uuid
  end
  from (
    select case
      when current_user = 'gazi_worker' then current_setting('app.tenant_id', true)
      else app.jwt_claims() ->> 'tenant_id'
    end as v
  ) s
$$;

create or replace function app.role_rank(p_role text) returns int
language sql immutable
set search_path = ''
as $$
  select case p_role
    when 'viewer' then 1
    when 'member' then 2
    when 'admin'  then 3
    when 'owner'  then 4
    else 0
  end
$$;

create or replace function app.touch_updated_at() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end
$$;

create or replace function app.forbid_mutation() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception '% on %.% is not allowed (append-only)', tg_op, tg_table_schema, tg_table_name
    using errcode = 'insufficient_privilege';
end
$$;

revoke all on all functions in schema app from public;
grant execute on function app.jwt_claims(), app.user_id(), app.tenant_id(), app.role_rank(text)
  to authenticated, gazi_worker, gazi_definer;
