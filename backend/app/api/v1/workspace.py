from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends

from app.api.deps import tenant_db
from app.core.auth import TenantContext, current_tenant, require_role
from app.core.config import Settings, app_settings
from app.domain.tenants import service
from app.domain.tenants.schemas import AiSettingsOut, AiSettingsUpdate

router = APIRouter(prefix="/workspace", tags=["workspace"])


@router.get("/ai-settings")
async def get_ai_settings(
    _: TenantContext = Depends(current_tenant),
    conn: asyncpg.Connection = Depends(tenant_db),
    settings: Settings = Depends(app_settings),
) -> AiSettingsOut:
    return await service.get_ai_settings(conn, settings)


@router.patch("/ai-settings")
async def save_ai_settings(
    body: AiSettingsUpdate,
    ctx: TenantContext = Depends(require_role("admin")),
    conn: asyncpg.Connection = Depends(tenant_db),
    settings: Settings = Depends(app_settings),
) -> AiSettingsOut:
    """Turn auto-reply on or off and choose the AI models. Admins only."""
    return await service.save_ai_settings(conn, ctx, settings, body)
