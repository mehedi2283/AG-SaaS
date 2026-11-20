-- Background jobs (ARQ workers, role gazi_worker).
--
-- Cron jobs need to FIND work across tenants (the reconciler, the 7-day CSV purge) without being able to READ
-- across tenants. These SECURITY DEFINER functions return only (kind, tenant_id, id); each job then opens a
-- per-tenant transaction (app.tenant_id set), where RLS applies as usual. Later migrations extend
-- app.stuck_work() as more job types appear.

create or replace function app.stuck_work(p_older_than interval default interval '10 minutes')
returns table (kind text, tenant_id uuid, id uuid)
language sql stable security definer
set search_path = ''
as $$
  select 'webhook_event', e.tenant_id, e.id from public.webhook_events e
   where e.status in ('received', 'queued') and e.received_at < now() - p_older_than
  union all
  select 'lead_import', i.tenant_id, i.id from public.lead_imports i
   where (i.status = 'uploaded' and i.created_at < now() - p_older_than)
      or (i.status = 'processing' and i.updated_at < now() - greatest(p_older_than, interval '30 minutes'))
  limit 500
$$;

-- Uploaded CSVs past their 7 days (rule 8). The purge job deletes the file, then clears file_path per tenant.
create or replace function app.expired_imports()
returns table (tenant_id uuid, id uuid, file_path text)
language sql stable security definer
set search_path = ''
as $$
  select i.tenant_id, i.id, i.file_path from public.lead_imports i
   where i.file_path is not null and i.expires_at < now()
   limit 1000
$$;

alter function app.stuck_work(interval) owner to gazi_definer;
alter function app.expired_imports() owner to gazi_definer;
revoke all on function app.stuck_work(interval), app.expired_imports() from public;
grant execute on function app.stuck_work(interval), app.expired_imports() to gazi_worker;

grant select on public.webhook_events, public.lead_imports to gazi_definer;
create policy webhook_events_definer_select on public.webhook_events for select to gazi_definer using (true);
create policy lead_imports_definer_select on public.lead_imports for select to gazi_definer using (true);

-- Workers install the built-in prompt versions (app/llm/prompts.py) for a tenant the first time it needs them.
-- Rows stay immutable (0002 trigger); a new wording is a new version.
do $$
begin
  execute (
    select format('grant insert (%s) on public.prompt_templates to gazi_worker',
                  string_agg(quote_ident(column_name), ', '))
    from information_schema.columns
    where table_schema = 'public' and table_name = 'prompt_templates'
      and column_name not in ('id', 'tenant_id', 'created_at', 'updated_at')
  );
end
$$;
