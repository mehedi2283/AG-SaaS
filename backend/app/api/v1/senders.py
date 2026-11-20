from __future__ import annotations

from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Response, status

from app.api.deps import get_crypto, tenant_db
from app.core.auth import TenantContext, current_tenant, require_role
from app.core.crypto import Crypto
from app.domain.senders import service
from app.domain.senders.schemas import SenderIn, SenderOut, SenderUpdate

router = APIRouter(prefix="/sender-identities", tags=["senders"])


@router.get("")
async def list_senders(
    _: TenantContext = Depends(current_tenant), conn: asyncpg.Connection = Depends(tenant_db)
) -> list[SenderOut]:
    return await service.list_senders(conn)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_sender(
    body: SenderIn,
    ctx: TenantContext = Depends(require_role("admin")),
    conn: asyncpg.Connection = Depends(tenant_db),
    crypto: Crypto = Depends(get_crypto),
) -> SenderOut:
    return await service.create_sender(conn, ctx, crypto, body)


@router.patch("/{sender_id}")
async def update_sender(
    sender_id: UUID,
    body: SenderUpdate,
    ctx: TenantContext = Depends(require_role("admin")),
    conn: asyncpg.Connection = Depends(tenant_db),
    crypto: Crypto = Depends(get_crypto),
) -> SenderOut:
    return await service.update_sender(conn, ctx, crypto, sender_id, body)


@router.delete("/{sender_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sender(
    sender_id: UUID, ctx: TenantContext = Depends(require_role("admin")), conn: asyncpg.Connection = Depends(tenant_db)
) -> Response:
    await service.delete_sender(conn, ctx, sender_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
