-- Weekly reports, error alerts and Apollo searches.
--
-- * app.report_targets(): launched campaigns that have report recipients, across tenants, for the Saturday
--   report cron (ids only; the report job reads data per tenant under RLS).
-- * app.tenant_admin_emails(): the owner/admin emails of the job's own tenant, so a worker can send an alert.
--   Workers can't read memberships or users directly; this returns only addresses, only for app.tenant_id().
-- * Apollo searches reuse lead_imports (no file; the search parameters live in header_map), so they appear in
--   the same import history and follow the same duplicate review.

create or replace function app.report_targets()
returns table (tenant_id uuid, campaign_id uuid)
language sql stable security definer
set search_path = ''
as $$
  select c.tenant_id, c.id from public.campaigns c
  join public.tenants t on t.id = c.tenant_id and t.status = 'active'
  where c.launch_status = 'launched' and cardinality(c.report_emails) > 0 and c.status <> 'archived'
$$;

create or replace function app.tenant_admin_emails()
returns table (email text)
language sql stable security definer
set search_path = ''
as $$
  select u.email from public.memberships m join public.users u on u.id = m.user_id
  -- Inside SECURITY DEFINER, current_user is gazi_definer, so app.tenant_id() would read JWT claims; the worker's
  -- tenant is the app.tenant_id setting (set by @tenant_job). Only gazi_worker may call this function.
  where m.tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
    and m.status = 'active' and m.role in ('owner', 'admin')
    and u.email <> ''
$$;

alter function app.report_targets() owner to gazi_definer;
alter function app.tenant_admin_emails() owner to gazi_definer;
revoke all on function app.report_targets(), app.tenant_admin_emails() from public;
grant execute on function app.report_targets(), app.tenant_admin_emails() to gazi_worker;

grant select on public.campaigns to gazi_definer;
create policy campaigns_definer_select on public.campaigns for select to gazi_definer using (true);

-- Apollo searches have no file; their parameters are kept in header_map and their source shows in the UI.
alter table public.lead_imports
  add column source text not null default 'csv' check (source in ('csv', 'apollo'));
grant select (source) on public.lead_imports to authenticated;
grant insert (source) on public.lead_imports to authenticated;

-- The reconciler must send a stuck Apollo search to apollo_search, not to the CSV importer.
create or replace function app.stuck_work(p_older_than interval default interval '10 minutes')
returns table (kind text, tenant_id uuid, id uuid)
language sql stable security definer
set search_path = ''
as $$
  select 'webhook_event', e.tenant_id, e.id from public.webhook_events e
   where e.status in ('received', 'queued') and e.received_at < now() - p_older_than
  union all
  select case when i.source = 'apollo' then 'apollo_search' else 'lead_import' end, i.tenant_id, i.id
    from public.lead_imports i
   where (i.status = 'uploaded' and i.created_at < now() - p_older_than)
      or (i.status = 'processing' and i.updated_at < now() - greatest(p_older_than, interval '30 minutes'))
  union all
  select 'lead_generation', l.tenant_id, l.id from public.leads l
   where l.status = 'generating' and l.updated_at < now() - greatest(p_older_than, interval '20 minutes')
  limit 500
$$;
alter function app.stuck_work(interval) owner to gazi_definer;
revoke all on function app.stuck_work(interval) from public;
grant execute on function app.stuck_work(interval) to gazi_worker;
