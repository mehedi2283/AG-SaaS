from __future__ import annotations

from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import get_queue, tenant_db
from app.core.auth import TenantContext, current_tenant, require_role
from app.core.config import Settings, app_settings
from app.domain.generation import service
from app.domain.generation.schemas import GenerateOut, MessageEdit, MessageOut, MessagePage
from app.queue import JobQueue

router = APIRouter(tags=["generation"])


@router.post("/campaigns/{campaign_id}/generate", status_code=status.HTTP_202_ACCEPTED)
async def generate(
    campaign_id: UUID,
    ctx: TenantContext = Depends(require_role("member")),
    conn: asyncpg.Connection = Depends(tenant_db),
    settings: Settings = Depends(app_settings),
    queue: JobQueue = Depends(get_queue),
) -> GenerateOut:
    """Write the sequence for every new buyer in the campaign (in the background)."""
    return await service.start(conn, ctx, settings, queue, campaign_id)


@router.get("/campaigns/{campaign_id}/messages")
async def list_messages(
    campaign_id: UUID,
    lead_id: UUID | None = None,
    status_: str | None = Query(None, alias="status", max_length=20),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: TenantContext = Depends(current_tenant),
    conn: asyncpg.Connection = Depends(tenant_db),
) -> MessagePage:
    return await service.list_messages(conn, campaign_id, lead_id=lead_id, status=status_, limit=limit, offset=offset)


@router.patch("/lead-messages/{message_id}")
async def edit_message(
    message_id: UUID,
    body: MessageEdit,
    ctx: TenantContext = Depends(require_role("member")),
    conn: asyncpg.Connection = Depends(tenant_db),
) -> MessageOut:
    """Edit a written message. The same rules as for the AI apply (no links, no sign-off, no placeholders)."""
    return await service.edit_message(conn, ctx, message_id, body)


@router.post("/leads/{lead_id}/regenerate", status_code=status.HTTP_202_ACCEPTED)
async def regenerate(
    lead_id: UUID,
    ctx: TenantContext = Depends(require_role("member")),
    conn: asyncpg.Connection = Depends(tenant_db),
    settings: Settings = Depends(app_settings),
    queue: JobQueue = Depends(get_queue),
) -> Response:
    await service.regenerate_lead(conn, ctx, settings, queue, lead_id)
    return Response(status_code=status.HTTP_202_ACCEPTED)
