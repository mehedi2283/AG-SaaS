-- Sequence generation: leads in 'generating' whose messages are still pending get re-enqueued by the reconciler.

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
  union all
  select 'lead_generation', l.tenant_id, l.id from public.leads l
   where l.status = 'generating' and l.updated_at < now() - greatest(p_older_than, interval '20 minutes')
  limit 500
$$;
alter function app.stuck_work(interval) owner to gazi_definer;
revoke all on function app.stuck_work(interval) from public;
grant execute on function app.stuck_work(interval) to gazi_worker;

grant select on public.leads to gazi_definer;
create policy leads_definer_select on public.leads for select to gazi_definer using (true);

-- "Rewrite" in the app resets a lead's failed/written messages so the worker writes them again.
grant update (attempt, error_code, validation_errors) on public.lead_messages to authenticated;
