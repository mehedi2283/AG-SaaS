"""Superuser SQL. Every call goes through an app.admin_* SECURITY DEFINER function, which re-checks that the
user in the verified JWT is a platform admin and logs writes to platform_audit."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg
import structlog

from app.core.errors import NotFound, Unprocessable
from app.domain.admin.schemas import AdminActivity, AdminMember, AdminTenant, AdminUser


async def _set_request_id(conn: asyncpg.Connection) -> None:
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    await conn.execute("select set_config('app.request_id', $1, true)", request_id or "")


async def _call(conn: asyncpg.Connection, query: str, *args: Any) -> None:
    await _set_request_id(conn)
    try:
        async with conn.transaction():  # savepoint: a refused change leaves the request transaction usable
            await conn.execute(query, *args)
    except asyncpg.NoDataFoundError as exc:
        raise NotFound(str(exc.args[0]).capitalize() + ".", code="not_found") from exc
    except asyncpg.InvalidParameterValueError as exc:
        raise Unprocessable(str(exc.args[0]).capitalize() + ".", code="invalid_value") from exc


def _tenant(row: asyncpg.Record, included_tokens: int) -> AdminTenant:
    return AdminTenant(**dict(row), token_cap=included_tokens + row["addon_tokens"])


async def tenants(conn: asyncpg.Connection, included_tokens: int) -> list[AdminTenant]:
    return [_tenant(r, included_tokens) for r in await conn.fetch("select * from app.admin_tenants()")]


async def tenant(conn: asyncpg.Connection, tenant_id: UUID, included_tokens: int) -> AdminTenant:
    row = await conn.fetchrow("select * from app.admin_tenants() where id = $1", tenant_id)
    if row is None:
        raise NotFound("There's no such workspace.", code="not_found")
    return _tenant(row, included_tokens)


async def users(conn: asyncpg.Connection) -> list[AdminUser]:
    rows = await conn.fetch(
        "select *, (banned_until is null or banned_until <= now()) as login_enabled from app.admin_users()"
    )
    return [AdminUser(**dict(r)) for r in rows]


async def user(conn: asyncpg.Connection, user_id: UUID) -> AdminUser:
    row = await conn.fetchrow(
        "select *, (banned_until is null or banned_until <= now()) as login_enabled from app.admin_users()"
        " where id = $1",
        user_id,
    )
    if row is None:
        raise NotFound("There's no such user.", code="not_found")
    return AdminUser(**dict(row))


async def members(conn: asyncpg.Connection, tenant_id: UUID) -> list[AdminMember]:
    return [AdminMember(**dict(r)) for r in await conn.fetch("select * from app.admin_members($1)", tenant_id)]


async def activity(conn: asyncpg.Connection, limit: int) -> list[AdminActivity]:
    return [AdminActivity(**dict(r)) for r in await conn.fetch("select * from app.admin_activity($1)", limit)]


async def set_tenant_status(conn: asyncpg.Connection, tenant_id: UUID, status: str) -> None:
    await _call(conn, "select app.admin_set_tenant_status($1, $2)", tenant_id, status)


async def set_free_access(conn: asyncpg.Connection, tenant_id: UUID, until: datetime | None) -> None:
    await _call(conn, "select app.admin_set_comp($1, $2)", tenant_id, until)


async def set_member_role(conn: asyncpg.Connection, tenant_id: UUID, user_id: UUID, role: str) -> None:
    await _call(conn, "select app.admin_set_member_role($1, $2, $3)", tenant_id, user_id, role)


async def remove_member(conn: asyncpg.Connection, tenant_id: UUID, user_id: UUID) -> None:
    await _call(conn, "select app.admin_remove_member($1, $2)", tenant_id, user_id)


async def log(
    conn: asyncpg.Connection, action: str, tenant_id: UUID | None, user_id: UUID | None, diff: dict[str, Any]
) -> None:
    await _call(conn, "select app.admin_log($1, $2, $3, $4)", action, tenant_id, user_id, diff)
