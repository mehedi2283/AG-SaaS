from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

_GOOD = "('trialing', 'active')"


# ---- API side (role authenticated; RLS scopes everything to the active tenant) ----


async def subscription(conn: asyncpg.Connection) -> asyncpg.Record | None:
    row: asyncpg.Record | None = await conn.fetchrow(
        """
        select stripe_customer_id, stripe_subscription_id, status, price_id, trial_end, current_period_end,
               cancel_at_period_end, addon_tokens, addons
          from public.subscriptions
        """
    )
    return row


async def access(conn: asyncpg.Connection) -> tuple[str, datetime | None]:
    row = await conn.fetchrow("select access, grace_ends_at from app.billing_access()")
    assert row is not None  # the function always returns one row
    return row["access"], row["grace_ends_at"]


async def month_usage(conn: asyncpg.Connection, *, worker: bool) -> int:
    """Tokens used by the active tenant this calendar month (UTC)."""
    if worker:  # gazi_worker reads usage_logs directly (RLS scopes it to the job's tenant)
        query = (
            "select coalesce(sum(total_tokens), 0)::bigint from public.usage_logs"
            " where created_at >= date_trunc('month', now() at time zone 'utc') at time zone 'utc'"
        )
    else:  # usage_logs is admin-only for people; this function gives every member the total
        query = "select app.month_token_usage()"
    used: int = await conn.fetchval(query)
    return used


async def addon_tokens(conn: asyncpg.Connection) -> int:
    value: int | None = await conn.fetchval("select addon_tokens from public.subscriptions")
    return value or 0


async def tenant_name(conn: asyncpg.Connection) -> str | None:
    name: str | None = await conn.fetchval("select name from public.tenants")
    return name


async def save_customer(conn: asyncpg.Connection, customer_id: str) -> str:
    """Record the tenant's Stripe customer; if one was saved concurrently, keep and return that one."""
    await conn.execute(
        "insert into public.subscriptions (stripe_customer_id) values ($1) on conflict do nothing", customer_id
    )
    saved: str = await conn.fetchval("select stripe_customer_id from public.subscriptions")
    return saved


# ---- Stripe webhook side (role gazi_worker; app.tenant_id set from the resolved customer) ----


async def resolve_customer(conn: asyncpg.Connection, customer_id: str) -> UUID | None:
    tenant_id: UUID | None = await conn.fetchval("select app.resolve_stripe_customer($1)", customer_id)
    return tenant_id


async def store_event(
    conn: asyncpg.Connection, event_id: str, event_type: str, payload: dict[str, Any]
) -> tuple[UUID, str]:
    """Insert the raw event once; return its id and current status (a retried failure is processed again)."""
    await conn.execute(
        """
        insert into public.billing_events (stripe_event_id, type, payload) values ($1, $2, $3)
        on conflict (tenant_id, stripe_event_id) do nothing
        """,
        event_id,
        event_type,
        payload,
    )
    row = await conn.fetchrow("select id, status from public.billing_events where stripe_event_id = $1", event_id)
    assert row is not None
    return row["id"], row["status"]


async def mark_event(conn: asyncpg.Connection, event_row_id: UUID, status: str, error_code: str | None = None) -> None:
    await conn.execute(
        "update public.billing_events set status = $2, error_code = $3, processed_at = now() where id = $1",
        event_row_id,
        status,
        error_code,
    )


async def apply_subscription(
    conn: asyncpg.Connection,
    *,
    subscription_id: str,
    status: str,
    price_id: str | None,
    trial_end: datetime | None,
    current_period_end: datetime | None,
    cancel_at_period_end: bool,
    addon_tokens: int,
    addons: list[dict[str, Any]],
    event_created: datetime,
) -> bool:
    """Write Stripe's subscription state unless a newer event was already applied. False if stale.

    lapsed_at starts the 7-day grace when the subscription leaves trialing/active and clears on recovery.
    """
    result: str = await conn.execute(
        f"""
        update public.subscriptions
           set stripe_subscription_id = $1,
               status = $2,
               price_id = $3,
               trial_end = $4,
               current_period_end = $5,
               cancel_at_period_end = $6,
               lapsed_at = case
                 when $2 in {_GOOD} then null
                 else coalesce(lapsed_at, case when status in {_GOOD} then now() end)
               end,
               last_event_at = $7,
               addon_tokens = $8,
               addons = $9
         where last_event_at is null or last_event_at <= $7
        """,
        subscription_id,
        status,
        price_id,
        trial_end,
        current_period_end,
        cancel_at_period_end,
        event_created,
        addon_tokens,
        addons,
    )
    return result != "UPDATE 0"
