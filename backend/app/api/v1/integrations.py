from __future__ import annotations

from typing import Any

import asyncpg
import structlog
from fastapi import APIRouter, Body, Depends, Response, status

from app.api.deps import get_crypto, get_queue, tenant_db
from app.core.auth import TenantContext, require_role
from app.core.crypto import Crypto
from app.domain.integrations import service
from app.domain.integrations.schemas import IntegrationOut, Provider
from app.queue import QUEUE_SYNC, JobQueue

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
    queue: JobQueue = Depends(get_queue),
) -> IntegrationOut:
    config = service.parse_config(provider, body)
    saved = await service.save_integration(conn, ctx, crypto, provider, config)
    try:  # the key check runs in a worker (rate-limited); the key is saved either way
        stamp = await conn.fetchval("select extract(epoch from now())::bigint")
        await queue.enqueue(
            "check_integration",
            ctx.tenant_id,
            provider,
            job_id=f"check:{ctx.tenant_id}:{provider}:{stamp}",
            queue=QUEUE_SYNC,
        )
    except Exception as exc:
        structlog.get_logger().warning("integration.check_not_queued", provider=provider, error_type=type(exc).__name__)
    return saved


@router.delete("/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_integration(
    provider: Provider,
    ctx: TenantContext = Depends(require_role("owner")),
    conn: asyncpg.Connection = Depends(tenant_db),
) -> Response:
    await service.delete_integration(conn, ctx, provider)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
