from __future__ import annotations

from typing import Any

import asyncpg
from fastapi import APIRouter, Body, Depends, Response, status

from app.api.deps import get_crypto, tenant_db
from app.core.auth import TenantContext, require_role
from app.core.crypto import Crypto
from app.domain.integrations import service
from app.domain.integrations.schemas import IntegrationOut, Provider

router = APIRouter(prefix="/integrations", tags=["integrations"])


@router.get("")
async def list_integrations(
    _: TenantContext = Depends(require_role("admin")), conn: asyncpg.Connection = Depends(tenant_db)
) -> list[IntegrationOut]:
    return await service.list_integrations(conn)


@router.put("/{provider}")
async def save_integration(
    provider: Provider,
    body: dict[str, Any] = Body(...),
    ctx: TenantContext = Depends(require_role("admin")),
    conn: asyncpg.Connection = Depends(tenant_db),
    crypto: Crypto = Depends(get_crypto),
) -> IntegrationOut:
    config = service.parse_config(provider, body)
    return await service.save_integration(conn, ctx, crypto, provider, config)


@router.delete("/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_integration(
    provider: Provider,
    ctx: TenantContext = Depends(require_role("owner")),
    conn: asyncpg.Connection = Depends(tenant_db),
) -> Response:
    await service.delete_integration(conn, ctx, provider)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
