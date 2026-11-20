from __future__ import annotations

from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends

from app.api.deps import tenant_db
from app.core.auth import TenantContext, current_tenant
from app.domain.reports.service import ReportOut, list_reports

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("")
async def reports(
    campaign_id: UUID | None = None,
    _: TenantContext = Depends(current_tenant),
    conn: asyncpg.Connection = Depends(tenant_db),
) -> list[ReportOut]:
    return await list_reports(conn, campaign_id)
