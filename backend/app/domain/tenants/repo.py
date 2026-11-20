from __future__ import annotations

from uuid import UUID

import asyncpg

from app.core.errors import Conflict
from app.domain.tenants.schemas import Membership


async def my_workspaces(conn: asyncpg.Connection) -> list[Membership]:
    rows = await conn.fetch("select tenant_id, name, slug, role, membership_status from app.my_tenants()")
    return [Membership(**dict(r)) for r in rows]


async def create_workspace(conn: asyncpg.Connection, name: str, slug: str, wrapped_dek: bytes) -> UUID:
    try:
        async with conn.transaction():  # savepoint, so a taken slug leaves the outer transaction usable
            tenant_id: UUID = await conn.fetchval("select app.create_tenant($1, $2, $3)", name, slug, wrapped_dek)
    except asyncpg.UniqueViolationError as exc:
        raise Conflict("That workspace address is taken. Try another.", code="slug_taken") from exc
    return tenant_id
