from __future__ import annotations

from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, status

from app.api.deps import get_crypto, tenant_db
from app.core.auth import TenantContext, require_role
from app.core.crypto import Crypto
from app.domain.webhooks import endpoints
from app.domain.webhooks.schemas import EndpointCreate, EndpointCreated, EndpointOut

router = APIRouter(prefix="/webhook-endpoints", tags=["webhooks"])


@router.get("")
async def list_endpoints(
    _: TenantContext = Depends(require_role("admin")), conn: asyncpg.Connection = Depends(tenant_db)
) -> list[EndpointOut]:
    return await endpoints.list_endpoints(conn)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    body: EndpointCreate,
    ctx: TenantContext = Depends(require_role("admin")),
    conn: asyncpg.Connection = Depends(tenant_db),
    crypto: Crypto = Depends(get_crypto),
) -> EndpointCreated:
    return await endpoints.create_endpoint(conn, ctx, crypto, body.provider, body.require_secret)


@router.post("/{endpoint_id}/rotate", status_code=status.HTTP_201_CREATED)
async def rotate_endpoint(
    endpoint_id: UUID,
    ctx: TenantContext = Depends(require_role("admin")),
    conn: asyncpg.Connection = Depends(tenant_db),
    crypto: Crypto = Depends(get_crypto),
) -> EndpointCreated:
    return await endpoints.rotate_endpoint(conn, ctx, crypto, endpoint_id)


@router.post("/{endpoint_id}/revoke")
async def revoke_endpoint(
    endpoint_id: UUID,
    ctx: TenantContext = Depends(require_role("admin")),
    conn: asyncpg.Connection = Depends(tenant_db),
) -> EndpointOut:
    return await endpoints.revoke_endpoint(conn, ctx, endpoint_id)
