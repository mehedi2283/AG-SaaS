from __future__ import annotations

from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.deps import get_queue, tenant_db
from app.core.auth import TenantContext, current_tenant, require_role
from app.core.config import Settings, app_settings
from app.domain.leads import service
from app.domain.leads.schemas import (
    DuplicateOut,
    ImportOut,
    LeadOut,
    LeadPage,
    LeadStatus,
    LeadUpdate,
    ResolveIn,
    ResolveOut,
)
from app.queue import JobQueue

router = APIRouter(tags=["leads"])


@router.post(
    "/lead-imports",
    status_code=status.HTTP_202_ACCEPTED,
    openapi_extra={"requestBody": {"content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}}}},
)
async def upload_import(
    request: Request,
    campaign_id: UUID = Query(...),
    filename: str = Query("import.csv", max_length=255),
    ctx: TenantContext = Depends(require_role("member")),
    conn: asyncpg.Connection = Depends(tenant_db),
    settings: Settings = Depends(app_settings),
    queue: JobQueue = Depends(get_queue),
) -> ImportOut:
    """Upload a CSV as the raw request body. The import runs in the background: poll GET /lead-imports/{id}."""
    return await service.upload_import(
        conn, ctx, settings, queue, campaign_id=campaign_id, filename=filename, body=request.stream()
    )


@router.get("/lead-imports")
async def list_imports(
    campaign_id: UUID | None = None,
    _: TenantContext = Depends(current_tenant),
    conn: asyncpg.Connection = Depends(tenant_db),
) -> list[ImportOut]:
    return await service.list_imports(conn, campaign_id)


@router.get("/lead-imports/{import_id}")
async def get_import(
    import_id: UUID, _: TenantContext = Depends(current_tenant), conn: asyncpg.Connection = Depends(tenant_db)
) -> ImportOut:
    return await service.get_import(conn, import_id)


@router.get("/lead-imports/{import_id}/duplicates")
async def list_duplicates(
    import_id: UUID, _: TenantContext = Depends(current_tenant), conn: asyncpg.Connection = Depends(tenant_db)
) -> list[DuplicateOut]:
    return await service.list_duplicates(conn, import_id)


@router.post("/lead-imports/{import_id}/duplicates/resolve")
async def resolve_duplicates(
    import_id: UUID,
    body: ResolveIn,
    ctx: TenantContext = Depends(require_role("member")),
    conn: asyncpg.Connection = Depends(tenant_db),
) -> ResolveOut:
    return await service.resolve_duplicates(conn, ctx, import_id, body)


@router.get("/leads")
async def list_leads(
    campaign_id: UUID | None = None,
    status_: LeadStatus | None = Query(None, alias="status"),
    q: str | None = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: TenantContext = Depends(current_tenant),
    conn: asyncpg.Connection = Depends(tenant_db),
) -> LeadPage:
    return await service.list_leads(conn, campaign_id=campaign_id, status=status_, q=q, limit=limit, offset=offset)


@router.patch("/leads/{lead_id}")
async def update_lead(
    lead_id: UUID,
    body: LeadUpdate,
    ctx: TenantContext = Depends(require_role("member")),
    conn: asyncpg.Connection = Depends(tenant_db),
) -> LeadOut:
    return await service.update_lead(conn, ctx, lead_id, body)


@router.delete("/leads/{lead_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_lead(
    lead_id: UUID, ctx: TenantContext = Depends(require_role("admin")), conn: asyncpg.Connection = Depends(tenant_db)
) -> Response:
    await service.delete_lead(conn, ctx, lead_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
