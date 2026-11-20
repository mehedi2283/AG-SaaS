-- AI token allowance (decided 2026-11-20): the $99 base plan includes 2M tokens a month (GAZI_INCLUDED_TOKENS);
-- workspaces can add monthly packs (+2M $49, +5M $99, +12M $199) as extra items on their Stripe subscription.
-- Over the allowance, AI writing and drafting pause (error_code llm_quota_exceeded); nothing else does.
-- The month is the calendar month in UTC.
--
-- Pack state, like the rest of the subscription, is written only from signed Stripe webhooks.

alter table public.subscriptions
  add column addon_tokens bigint not null default 0 check (addon_tokens >= 0),
  -- [{"price_id": "price_...", "quantity": 1}] for the pack items currently on the subscription.
  add column addons jsonb not null default '[]'::jsonb check (jsonb_typeof(addons) = 'array');

grant update (addon_tokens, addons) on public.subscriptions to gazi_worker;

-- Tokens the active tenant used this calendar month (UTC). usage_logs itself stays admin-only; this lets every
-- member see the total, which the app needs for the "AI paused" banner. Workers sum usage_logs directly.
create or replace function app.month_token_usage()
returns bigint
language sql stable security definer
set search_path = ''
as $$
  select coalesce(sum(u.total_tokens), 0)::bigint
  from public.usage_logs u
  where u.tenant_id = (select app.tenant_id())
    and (select app.has_role('viewer'))
    and u.created_at >= date_trunc('month', now() at time zone 'utc') at time zone 'utc'
$$;
alter function app.month_token_usage() owner to gazi_definer;
revoke all on function app.month_token_usage() from public;
grant execute on function app.month_token_usage() to authenticated;

grant select on public.usage_logs to gazi_definer;
create policy usage_logs_definer_select on public.usage_logs for select to gazi_definer using (true);
