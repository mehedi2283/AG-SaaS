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

# Shown on the API docs page instead of a generic placeholder. Each provider's body is validated in the service.
_EXAMPLES: dict[str, Any] = {
    "api_key": {
        "summary": "Instantly, HeyReach or Apollo",
        "value": {"api_key": "paste-your-api-key"},
    },
    "calendly": {
        "summary": "Calendly",
        "value": {"booking_url": "https://calendly.com/your-name/intro", "api_token": "paste-your-token"},
    },
}


@router.get("")
async def list_integrations(
    _: TenantContext = Depends(require_role("admin")), conn: asyncpg.Connection = Depends(tenant_db)
) -> list[IntegrationOut]:
    return await service.list_integrations(conn)


@router.put("/{provider}")
async def save_integration(
    provider: Provider,
    body: dict[str, Any] = Body(..., openapi_examples=_EXAMPLES),
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
