"""Calendly bookings (invitee.created / invitee.canceled), matched to a campaign by utm_campaign (the booking link
Gazi adds carries ?utm_campaign=<campaign id>) and to a lead by email."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg
import structlog

log = structlog.get_logger()


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


def _time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


async def apply_booking(conn: asyncpg.Connection, event_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
    event = str(payload.get("event") or "")
    data = payload.get("payload") or {}
    uri = data.get("uri")
    email = str(data.get("email") or "").strip().lower()
    if event not in ("invitee.created", "invitee.canceled") or not uri or not email:
        return {"ignored": event or "unknown"}
    campaign_id = _uuid((data.get("tracking") or {}).get("utm_campaign"))
    if campaign_id and not await conn.fetchval(
        "select exists(select 1 from public.campaigns where id = $1)", campaign_id
    ):
        campaign_id = None  # someone else's id, or a deleted campaign: never trust it across tenants
    lead_id = await conn.fetchval(
        "select id from public.leads where email = $1 order by (campaign_id = $2) desc nulls last limit 1",
        email,
        campaign_id,
    )
    if event == "invitee.created":
        await conn.execute(
            """
            insert into public.bookings (campaign_id, lead_id, webhook_event_id, external_id, invitee_email,
                                         invitee_name, scheduled_for)
            values ($1, $2, $3, $4, $5, $6, $7)
            on conflict (tenant_id, external_id) do update
              set scheduled_for = excluded.scheduled_for, canceled_at = null
            """,
            campaign_id,
            lead_id,
            event_id,
            str(uri)[:500],
            email,
            (data.get("name") or None),
            _time((data.get("scheduled_event") or {}).get("start_time")),
        )
    else:
        await conn.execute("update public.bookings set canceled_at = now() where external_id = $1", str(uri)[:500])
    log.info("booking.applied", calendly_event=event, matched_campaign=bool(campaign_id), matched_lead=bool(lead_id))
    return {"booking": event, "campaign": str(campaign_id) if campaign_id else None}
