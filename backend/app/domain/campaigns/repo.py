from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

_COUNTS = """
  (select count(*) from public.leads l where l.campaign_id = c.id)::int as leads,
  (select count(*) from public.leads l where l.campaign_id = c.id and l.status = 'new')::int as new,
  (select count(*) from public.leads l where l.campaign_id = c.id and l.status = 'generating')::int as generating,
  (select count(*) from public.leads l where l.campaign_id = c.id and l.status = 'ready')::int as ready,
  (select count(*) from public.lead_messages m where m.campaign_id = c.id and m.status = 'failed')::int
    as failed_messages,
  (select count(*) from public.leads l where l.campaign_id = c.id and l.status in ('pushed', 'replied'))::int as pushed,
  (select count(*) from public.leads l where l.campaign_id = c.id and l.status = 'replied')::int as replied,
  (select count(*) from public.lead_duplicates d join public.lead_imports i on i.id = d.import_id
     where i.campaign_id = c.id and d.resolution = 'pending')::int as pending_duplicates,
  (select count(*) from public.bookings b where b.campaign_id = c.id and b.canceled_at is null)::int as bookings,
  (select count(*) from public.campaign_steps s where s.campaign_id = c.id)::int as sequence_count
"""
_FIELDS = (
    "c.id, c.name, c.channel, c.status, c.launch_status, c.launch_error, c.launched_at, c.offer_summary,"
    " c.offer_details, c.attachment_url, c.report_emails, c.timezone, c.sending_days, c.send_from, c.send_to,"
    " c.daily_limit, c.sender_identity_id, c.created_at, c.updated_at"
)
COUNT_KEYS = (
    "leads",
    "new",
    "generating",
    "ready",
    "failed_messages",
    "pushed",
    "replied",
    "pending_duplicates",
    "bookings",
)


async def list_campaigns(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    rows: list[asyncpg.Record] = await conn.fetch(
        f"select {_FIELDS}, {_COUNTS} from public.campaigns c where c.status <> 'archived' order by c.created_at desc"
    )
    return rows


async def get_campaign(conn: asyncpg.Connection, campaign_id: UUID) -> asyncpg.Record | None:
    row: asyncpg.Record | None = await conn.fetchrow(
        f"select {_FIELDS}, {_COUNTS} from public.campaigns c where c.id = $1", campaign_id
    )
    return row


async def insert_campaign(conn: asyncpg.Connection, values: dict[str, Any]) -> UUID:
    cols = ", ".join(values)
    params = ", ".join(f"${i + 1}" for i in range(len(values)))
    campaign_id: UUID = await conn.fetchval(
        f"insert into public.campaigns ({cols}) values ({params}) returning id", *values.values()
    )
    return campaign_id


async def update_campaign(conn: asyncpg.Connection, campaign_id: UUID, values: dict[str, Any]) -> bool:
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(values))
    result: str = await conn.execute(f"update public.campaigns set {sets} where id = $1", campaign_id, *values.values())
    return result != "UPDATE 0"


async def steps(conn: asyncpg.Connection, campaign_id: UUID) -> list[asyncpg.Record]:
    rows: list[asyncpg.Record] = await conn.fetch(
        "select step, delay_days, angle from public.campaign_steps where campaign_id = $1 order by step", campaign_id
    )
    return rows


async def replace_steps(conn: asyncpg.Connection, campaign_id: UUID, steps: list[tuple[int, int, str]]) -> None:
    await conn.execute("delete from public.campaign_steps where campaign_id = $1", campaign_id)
    await conn.executemany(
        "insert into public.campaign_steps (campaign_id, step, delay_days, angle) values ($1, $2, $3, $4)",
        [(campaign_id, *s) for s in steps],
    )


async def delete_campaign(conn: asyncpg.Connection, campaign_id: UUID) -> bool:
    row = await conn.fetchrow("delete from public.campaigns where id = $1 returning id", campaign_id)
    return row is not None
