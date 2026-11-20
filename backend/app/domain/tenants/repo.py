from __future__ import annotations

from typing import Any
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


async def tenant_settings(conn: asyncpg.Connection) -> dict[str, Any]:
    stored: dict[str, Any] | None = await conn.fetchval("select settings from public.tenants")
    return stored or {}


async def merge_tenant_settings(conn: asyncpg.Connection, changes: dict[str, Any]) -> bool:
    """Merge keys into the active tenant's settings. False if RLS let nothing through (not an admin)."""
    result: str = await conn.execute("update public.tenants set settings = settings || $1::jsonb", changes)
    return result != "UPDATE 0"


async def is_platform_admin(conn: asyncpg.Connection) -> bool:
    value: bool = await conn.fetchval("select app.is_platform_admin()")
    return value
