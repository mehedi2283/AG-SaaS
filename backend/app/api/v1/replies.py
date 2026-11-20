from __future__ import annotations

from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import get_queue, tenant_db
from app.core.auth import TenantContext, current_tenant, require_role
from app.domain.replies import service
from app.domain.replies.schemas import ReplyEdit, ReplyOut, ReplyPage
from app.queue import JobQueue

router = APIRouter(prefix="/replies", tags=["replies"])


@router.get("")
async def list_replies(
    view: Literal["waiting", "scheduled", "sent", "all"] = "waiting",
    intent: str | None = Query(None, max_length=30),
    campaign_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: TenantContext = Depends(current_tenant),
    conn: asyncpg.Connection = Depends(tenant_db),
) -> ReplyPage:
    return await service.list_replies(
        conn, view=view, intent=intent, campaign_id=campaign_id, limit=limit, offset=offset
    )


@router.get("/{reply_id}")
async def get_reply(
    reply_id: UUID, _: TenantContext = Depends(current_tenant), conn: asyncpg.Connection = Depends(tenant_db)
) -> ReplyOut:
    return await service.get_reply(conn, reply_id)


@router.patch("/{reply_id}")
async def edit_reply(
    reply_id: UUID,
    body: ReplyEdit,
    ctx: TenantContext = Depends(require_role("member")),
    conn: asyncpg.Connection = Depends(tenant_db),
) -> ReplyOut:
    """Edit the draft or correct the intent. Links are added by Gazi when sending, so the draft must have none."""
    return await service.edit_reply(conn, ctx, reply_id, body)


@router.post("/{reply_id}/send", status_code=status.HTTP_202_ACCEPTED)
async def send(
    reply_id: UUID,
    ctx: TenantContext = Depends(require_role("member")),
    conn: asyncpg.Connection = Depends(tenant_db),
    queue: JobQueue = Depends(get_queue),
) -> ReplyOut:
    return await service.approve_and_send(conn, ctx, queue, reply_id)


@router.post("/{reply_id}/dismiss")
async def dismiss(
    reply_id: UUID, ctx: TenantContext = Depends(require_role("member")), conn: asyncpg.Connection = Depends(tenant_db)
) -> ReplyOut:
    return await service.dismiss(conn, ctx, reply_id)


@router.post("/{reply_id}/redraft", status_code=status.HTTP_202_ACCEPTED)
async def redraft(
    reply_id: UUID,
    ctx: TenantContext = Depends(require_role("member")),
    conn: asyncpg.Connection = Depends(tenant_db),
    queue: JobQueue = Depends(get_queue),
) -> Response:
    await service.redraft(conn, ctx, queue, reply_id)
    return Response(status_code=status.HTTP_202_ACCEPTED)
