from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
import structlog

from app.core.auth import TenantContext


async def record(
    conn: asyncpg.Connection,
    ctx: TenantContext,
    action: str,
    *,
    target_table: str | None = None,
    target_id: UUID | None = None,
    diff: dict[str, Any] | None = None,
) -> None:
    """Append to audit_log as the acting user. Never put secrets in `diff`."""
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    await conn.execute(
        """
        insert into public.audit_log (actor_kind, actor_user_id, action, target_table, target_id, diff, request_id)
        values ('user', $1, $2, $3, $4, $5, $6)
        """,
        ctx.principal.user_id,
        action,
        target_table,
        target_id,
        diff or {},
        request_id,
    )
