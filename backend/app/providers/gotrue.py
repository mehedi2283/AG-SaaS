"""Supabase Auth (GoTrue) admin client. Used only to store the user's selected workspace."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

import httpx
import structlog

from app.core.errors import UpstreamError

log = structlog.get_logger()


class AuthAdmin(Protocol):
    async def set_active_tenant(self, user_id: UUID, tenant_id: UUID) -> None: ...


class GoTrueAdmin:
    def __init__(self, base_url: str, service_key: str, client: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}
        self._client = client

    async def set_active_tenant(self, user_id: UUID, tenant_id: UUID) -> None:
        try:
            response = await self._client.put(
                f"{self._base}/admin/users/{user_id}",
                json={"app_metadata": {"active_tenant_id": str(tenant_id)}},
                headers=self._headers,
                timeout=10,
            )
        except httpx.HTTPError as exc:
            log.error("gotrue.unreachable", error_type=type(exc).__name__)
            raise UpstreamError("The sign-in service didn't respond. Try again.", code="auth_unavailable") from exc
        if response.status_code >= 400:
            log.error("gotrue.update_failed", status=response.status_code)
            raise UpstreamError("Couldn't switch workspace. Try again.", code="auth_update_failed")
