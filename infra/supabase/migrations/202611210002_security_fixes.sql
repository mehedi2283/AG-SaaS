-- Security fixes ported from the unmerged audit branch (claude/audit-fixes-old-base), 2026-11-21.

-- 1. A NULL intent made the reply link check pass (a CHECK that evaluates to NULL counts as passed).
alter table public.replies drop constraint links_only_when_interested_or_question;
alter table public.replies add constraint links_only_when_interested_or_question check (
  coalesce(intent in ('interested', 'question'), false)
  or app.strip_img_tags(concat_ws(' ', draft_body, final_body)) !~* '(<a[[:space:]>]|https?://|www\.)'
);

-- 2. Clients approve/resolve only as themselves, and the timestamp is always set here.
--    tg_argv: [actor column, timestamp column]. Workers and definer code are not affected.
create or replace function app.stamp_actor() returns trigger
language plpgsql
set search_path = ''
as $$
declare
  v_by text := to_jsonb(new) ->> tg_argv[0];
  v_ts timestamptz := (to_jsonb(old) ->> tg_argv[1])::timestamptz;
begin
  if current_user = 'authenticated' then
    if v_by is distinct from (to_jsonb(old) ->> tg_argv[0]) then
      if v_by is not null and v_by is distinct from app.user_id()::text then
        raise exception '% must be the current user', tg_argv[0] using errcode = 'insufficient_privilege';
      end if;
      v_ts := case when v_by is not null then now() end;
    end if;
    new := jsonb_populate_record(new, jsonb_build_object(tg_argv[1], v_ts));
  end if;
  return new;
end
$$;

create trigger stamp_approval before update on public.lead_messages
  for each row execute function app.stamp_actor('approved_by', 'approved_at');
create trigger stamp_approval before update on public.replies
  for each row execute function app.stamp_actor('approved_by', 'approved_at');
create trigger stamp_resolution before update on public.lead_duplicates
  for each row execute function app.stamp_actor('resolved_by', 'resolved_at');

-- 3. The last-owner check is serialised per tenant, so two owners demoting each other at once
--    cannot both succeed. Otherwise unchanged from 202611200002_tables.sql.
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
    perform pg_advisory_xact_lock(hashtextextended(v_tenant::text, 0));
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
