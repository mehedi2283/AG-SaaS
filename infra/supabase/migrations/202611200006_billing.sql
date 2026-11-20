-- Billing (Stripe), per workspace. Decisions 2026-11-20: one plan, 14-day trial with the card taken
-- up front in Stripe Checkout; when a payment fails or the subscription ends, 7 days of grace, then
-- launches and AI use pause while data stays readable.
--
-- Who writes what:
--   * The API (role authenticated, admin+) only records the Stripe customer it created for the tenant.
--   * Subscription state comes ONLY from signature-verified Stripe webhooks, written as gazi_worker after
--     app.resolve_stripe_customer() maps the customer to its tenant. No client can mark itself paid.
-- tenants.plan (from 0002) is unused; the subscription row is the source of truth.

-- ---------------------------------------------------------------------------
-- subscriptions (one per tenant)
-- ---------------------------------------------------------------------------
create table public.subscriptions (
  id                      uuid primary key default gen_random_uuid(),
  tenant_id               uuid not null default app.tenant_id() references public.tenants (id) on delete cascade,
  stripe_customer_id      text not null unique check (stripe_customer_id ~ '^cus_[A-Za-z0-9]+$'),
  stripe_subscription_id  text unique check (stripe_subscription_id is null or stripe_subscription_id ~ '^sub_[A-Za-z0-9]+$'),
  -- 'none' until Stripe reports a subscription; otherwise Stripe's own status values.
  status                  text not null default 'none' check (status in (
                            'none', 'incomplete', 'incomplete_expired', 'trialing', 'active',
                            'past_due', 'canceled', 'unpaid', 'paused')),
  price_id                text,
  trial_end               timestamptz,
  current_period_end      timestamptz,
  cancel_at_period_end    boolean not null default false,
  -- When the workspace left trialing/active; the 7-day grace runs from here. Null while in good standing.
  lapsed_at               timestamptz,
  -- Stripe event.created of the last applied event, so a late, older event never overwrites a newer state.
  last_event_at           timestamptz,
  created_at              timestamptz not null default now(),
  updated_at              timestamptz not null default now(),
  unique (tenant_id),
  unique (tenant_id, id)
);

-- ---------------------------------------------------------------------------
-- billing_events (raw Stripe events: dedupe + audit trail)
-- ---------------------------------------------------------------------------
create table public.billing_events (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null default app.tenant_id() references public.tenants (id) on delete cascade,
  stripe_event_id  text not null check (stripe_event_id ~ '^evt_[A-Za-z0-9]+$'),
  type             text not null check (length(type) between 1 and 200),
  payload          jsonb not null,
  status           text not null default 'received' check (status in ('received', 'processed', 'ignored', 'failed')),
  error_code       text,
  received_at      timestamptz not null default now(),
  processed_at     timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (tenant_id, stripe_event_id),
  unique (tenant_id, id)
);

do $$
declare
  t text;
begin
  foreach t in array array['subscriptions', 'billing_events'] loop
    execute format('create trigger touch_updated_at before update on public.%I
                      for each row execute function app.touch_updated_at()', t);
    execute format('create trigger tenant_id_immutable before update on public.%I
                      for each row execute function app.tenant_id_immutable()', t);
  end loop;
end
$$;

-- ---------------------------------------------------------------------------
-- Functions
-- ---------------------------------------------------------------------------

-- Stripe webhook: customer id -> tenant. Callable by gazi_worker only (like resolve_webhook_token).
create or replace function app.resolve_stripe_customer(p_customer_id text)
returns uuid
language sql stable security definer
set search_path = ''
as $$
  select s.tenant_id from public.subscriptions s where s.stripe_customer_id = p_customer_id
$$;
alter function app.resolve_stripe_customer(text) owner to gazi_definer;

-- What the active tenant may do, for the API and workers alike (SECURITY INVOKER: RLS applies).
--   active  trialing or paid
--   grace   lapsed less than 7 days ago: everything works, the app shows a banner
--   paused  lapsed 7+ days ago: no launches, no AI use; data stays readable
--   none    never subscribed (no checkout finished yet)
create or replace function app.billing_access()
returns table (access text, grace_ends_at timestamptz)
language sql stable
set search_path = ''
as $$
  select
    case
      when s.status in ('trialing', 'active') then 'active'
      when s.lapsed_at is null then 'none'
      when now() < s.lapsed_at + interval '7 days' then 'grace'
      else 'paused'
    end,
    case when s.status not in ('trialing', 'active') then s.lapsed_at + interval '7 days' end
  from (select null::int as one) d
  left join public.subscriptions s on s.tenant_id = (select app.tenant_id())
$$;

revoke all on function app.resolve_stripe_customer(text), app.billing_access() from public;
grant execute on function app.resolve_stripe_customer(text) to gazi_worker;
grant execute on function app.billing_access() to authenticated, gazi_worker;

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------
revoke all on public.subscriptions, public.billing_events from public, anon, authenticated, service_role;

grant select on public.subscriptions to authenticated;
grant insert (stripe_customer_id) on public.subscriptions to authenticated;

grant select on public.subscriptions to gazi_worker;
grant update (stripe_subscription_id, status, price_id, trial_end, current_period_end, cancel_at_period_end,
              lapsed_at, last_event_at)
  on public.subscriptions to gazi_worker;
grant select on public.subscriptions to gazi_definer;

grant select on public.billing_events to authenticated;
grant select on public.billing_events to gazi_worker;
grant insert (stripe_event_id, type, payload) on public.billing_events to gazi_worker;
grant update (status, error_code, processed_at) on public.billing_events to gazi_worker;

-- ---------------------------------------------------------------------------
-- RLS
-- ---------------------------------------------------------------------------
alter table public.subscriptions enable row level security;
alter table public.subscriptions force row level security;
alter table public.billing_events enable row level security;
alter table public.billing_events force row level security;

-- Everyone in the workspace sees its billing state (for the banner); admins start checkout.
create policy subscriptions_select on public.subscriptions for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('viewer')));
create policy subscriptions_insert on public.subscriptions for insert to authenticated
  with check (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));
create policy subscriptions_worker on public.subscriptions for all to gazi_worker
  using (tenant_id = (select app.tenant_id())) with check (tenant_id = (select app.tenant_id()));
create policy subscriptions_definer_select on public.subscriptions for select to gazi_definer using (true);

create policy billing_events_select on public.billing_events for select to authenticated
  using (tenant_id = (select app.tenant_id()) and (select app.has_role('admin')));
create policy billing_events_worker on public.billing_events for all to gazi_worker
  using (tenant_id = (select app.tenant_id())) with check (tenant_id = (select app.tenant_id()));
