from __future__ import annotations

from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Response, status

from app.api.deps import get_queue, tenant_db
from app.core.auth import TenantContext, current_tenant, require_role
from app.core.config import Settings, app_settings
from app.domain.campaigns import service
from app.domain.campaigns.schemas import CampaignIn, CampaignOut, CampaignUpdate, StepOut, StepsIn
from app.queue import JobQueue

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


@router.get("")
async def list_campaigns(
    _: TenantContext = Depends(current_tenant), conn: asyncpg.Connection = Depends(tenant_db)
) -> list[CampaignOut]:
    return await service.list_campaigns(conn)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_campaign(
    body: CampaignIn,
    ctx: TenantContext = Depends(require_role("member")),
    conn: asyncpg.Connection = Depends(tenant_db),
) -> CampaignOut:
    return await service.create_campaign(conn, ctx, body)


@router.get("/{campaign_id}")
async def get_campaign(
    campaign_id: UUID, _: TenantContext = Depends(current_tenant), conn: asyncpg.Connection = Depends(tenant_db)
) -> CampaignOut:
    return await service.get_campaign(conn, campaign_id)


@router.patch("/{campaign_id}")
async def update_campaign(
    campaign_id: UUID,
    body: CampaignUpdate,
    ctx: TenantContext = Depends(require_role("member")),
    conn: asyncpg.Connection = Depends(tenant_db),
) -> CampaignOut:
    return await service.update_campaign(conn, ctx, campaign_id, body)


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_campaign(
    campaign_id: UUID,
    ctx: TenantContext = Depends(require_role("admin")),
    conn: asyncpg.Connection = Depends(tenant_db),
) -> Response:
    await service.delete_campaign(conn, ctx, campaign_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{campaign_id}/steps")
async def get_steps(
    campaign_id: UUID, _: TenantContext = Depends(current_tenant), conn: asyncpg.Connection = Depends(tenant_db)
) -> list[StepOut]:
    return await service.get_steps(conn, campaign_id)


@router.put("/{campaign_id}/steps")
async def save_steps(
    campaign_id: UUID,
    body: StepsIn,
    ctx: TenantContext = Depends(require_role("member")),
    conn: asyncpg.Connection = Depends(tenant_db),
) -> list[StepOut]:
    return await service.save_steps(conn, ctx, campaign_id, body)


@router.post("/{campaign_id}/launch", status_code=status.HTTP_202_ACCEPTED)
async def launch(
    campaign_id: UUID,
    ctx: TenantContext = Depends(require_role("admin")),
    conn: asyncpg.Connection = Depends(tenant_db),
    settings: Settings = Depends(app_settings),
    queue: JobQueue = Depends(get_queue),
) -> CampaignOut:
    """Create the campaign in Instantly/HeyReach and push every ready buyer. Run again to add newly ready ones."""
    return await service.request_launch(conn, ctx, settings, queue, campaign_id)


@router.post("/{campaign_id}/pause", status_code=status.HTTP_202_ACCEPTED)
async def pause(
    campaign_id: UUID,
    ctx: TenantContext = Depends(require_role("member")),
    conn: asyncpg.Connection = Depends(tenant_db),
    queue: JobQueue = Depends(get_queue),
) -> Response:
    await service.request_running(conn, ctx, queue, campaign_id, running=False)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/{campaign_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume(
    campaign_id: UUID,
    ctx: TenantContext = Depends(require_role("admin")),
    conn: asyncpg.Connection = Depends(tenant_db),
    queue: JobQueue = Depends(get_queue),
) -> Response:
    await service.request_running(conn, ctx, queue, campaign_id, running=True)
    return Response(status_code=status.HTTP_202_ACCEPTED)
