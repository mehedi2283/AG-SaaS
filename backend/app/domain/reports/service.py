from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

import asyncpg
from pydantic import BaseModel


class ReportOut(BaseModel):
    id: UUID
    campaign_id: UUID
    period_start: date
    period_end: date
    metrics: dict[str, Any]
    summary: str | None
    sent_to: list[str]
    status: str
    error_code: str | None
    sent_at: datetime | None


async def list_reports(conn: asyncpg.Connection, campaign_id: UUID | None) -> list[ReportOut]:
    rows = await conn.fetch(
        "select id, campaign_id, period_start, period_end, metrics, summary, sent_to, status, error_code, sent_at"
        " from public.campaign_reports where ($1::uuid is null or campaign_id = $1) order by period_start desc"
        " limit 52",
        campaign_id,
    )
    return [ReportOut(**dict(r)) for r in rows]
