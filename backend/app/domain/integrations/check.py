"""check_integration job: after a key is saved, try it once against the provider and record the result."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from arq import Retry

from app.db.tenant_tx import as_worker
from app.domain.integrations.secrets import provider_config
from app.providers.apollo import ApolloClient
from app.providers.heyreach import HeyReachClient
from app.providers.http import ProviderError
from app.providers.instantly import InstantlyClient
from app.workers.context import tenant_job

log = structlog.get_logger()

_CLIENTS = {"instantly": InstantlyClient, "heyreach": HeyReachClient, "apollo": ApolloClient}


@tenant_job
async def check_integration(ctx: dict[str, Any], tenant_id: UUID, provider: str) -> dict[str, Any]:
    if provider not in _CLIENTS:
        return {"skipped": provider}  # Calendly's booking link needs no check
    async with as_worker(ctx["pool"], tenant_id) as conn:
        config = await provider_config(conn, ctx["crypto"], tenant_id, provider)
    try:
        async with ctx["limits"].slot(tenant_id, provider):
            await _CLIENTS[provider](ctx["http"], config["api_key"]).check_key()  # type: ignore[attr-defined]
        status, error = "active", None
    except ProviderError as exc:
        if exc.retryable:
            raise Retry(defer=60) from exc
        status, error = "invalid", exc.message
    async with as_worker(ctx["pool"], tenant_id) as conn:
        await conn.execute(
            "update public.integrations set status = $2, last_error = $3, last_verified_at = now() where provider = $1",
            provider,
            status,
            error,
        )
    log.info("integration.checked", provider=provider, status=status)
    return {"status": status}
