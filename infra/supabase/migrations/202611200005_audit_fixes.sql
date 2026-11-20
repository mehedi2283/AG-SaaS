-- Audit fixes (2026-11-20).

-- 1. A NULL intent made the reply link check pass (CHECK treats NULL as true).
alter table public.replies drop constraint links_only_when_interested_or_question;
alter table public.replies add constraint links_only_when_interested_or_question check (
  coalesce(intent in ('interested', 'question'), false)
  or app.strip_img_tags(concat_ws(' ', draft_body, final_body)) !~* '(<a[[:space:]>]|https?://|www\.)'
);

-- 2. Clients may approve/resolve only as themselves; the timestamp is set here, never by the client.
--    tg_argv: [actor column, timestamp column].
create or replace function app.stamp_actor() returns trigger
language plpgsql
set search_path = ''
as $$
declare
  v_by text := to_jsonb(new) ->> tg_argv[0];
begin
  if current_user = 'authenticated' and v_by is distinct from (to_jsonb(old) ->> tg_argv[0]) then
    if v_by is not null and v_by is distinct from app.user_id()::text then
      raise exception '% must be the current user', tg_argv[0] using errcode = 'insufficient_privilege';
    end if;
    new := jsonb_populate_record(new, jsonb_build_object(tg_argv[1], case when v_by is not null then now() end));
  end if;
  return new;
end
$$;

revoke update (approved_at) on public.generated_emails, public.replies from authenticated;
revoke update (resolved_at) on public.lead_duplicates from authenticated;

create trigger stamp_approval before update on public.generated_emails
  for each row execute function app.stamp_actor('approved_by', 'approved_at');
create trigger stamp_approval before update on public.replies
  for each row execute function app.stamp_actor('approved_by', 'approved_at');
create trigger stamp_resolution before update on public.lead_duplicates
  for each row execute function app.stamp_actor('resolved_by', 'resolved_at');

-- 3. Deleting a user must not be blocked by history rows.
--    audit_log is append-only, so ON DELETE SET NULL could never run; keep the id without a FK.
alter table public.audit_log drop constraint audit_log_actor_user_id_fkey;

--    created_by is set to NULL when its user is deleted; that is not a new prompt wording.
create or replace function app.prompt_templates_immutable() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if (to_jsonb(new) - 'is_active' - 'updated_at' - 'created_by')
     is distinct from (to_jsonb(old) - 'is_active' - 'updated_at' - 'created_by')
     or (new.created_by is not null and new.created_by is distinct from old.created_by) then
    raise exception 'prompt_templates rows are immutable; insert a new version instead'
      using errcode = 'insufficient_privilege';
  end if;
  return new;
end
$$;

-- 3 + 4. The "caller must be owner" check applies to API requests only (cascades from a user deletion
--        run as the table owner). The last-owner check is serialised per tenant, so two owners demoting
--        each other at once cannot both succeed.
create or replace function app.memberships_owner_rules() returns trigger
language plpgsql
set search_path = ''
as $$
declare
  v_tenant uuid := coalesce(new.tenant_id, old.tenant_id);
  v_caller_is_owner boolean;
begin
  if current_user = 'authenticated'
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

-- 5. One generated email per lead, so a retried job upserts instead of adding a second row.
drop index public.generated_emails_lead_idx;
create unique index generated_emails_one_per_lead on public.generated_emails (tenant_id, lead_id);

-- 6. Deleting the existing lead keeps the pending duplicate (and its incoming data) for review.
alter table public.lead_duplicates drop constraint lead_duplicates_tenant_id_existing_lead_id_fkey;
alter table public.lead_duplicates add foreign key (tenant_id, existing_lead_id)
  references public.leads (tenant_id, id) on delete set null (existing_lead_id);
