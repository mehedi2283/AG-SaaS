"""Superuser (platform admin) routes. The account overview only: no tenant content.

Exception to "never accept tenant_id in a path" (CLAUDE.md rule 1): these routes act on a TARGET workspace
named in the path, never on the caller's own tenant context. Authorisation is the platform_admins check
inside each app.admin_* database function, not the tenant claim.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import get_auth_admin, get_billing, user_db
from app.core.auth import Principal, current_principal
from app.core.config import Settings, app_settings
from app.core.errors import Forbidden
from app.domain.admin import repo, service
from app.domain.admin.schemas import (
    AdminActivity,
    AdminMember,
    AdminTenant,
    AdminUser,
    FreeAccessChange,
    LoginChange,
    MemberRoleChange,
    TenantStatusChange,
)
from app.domain.tenants.repo import is_platform_admin
from app.providers.gotrue import AuthAdmin
from app.providers.stripe import BillingProvider


async def admin_db(conn: asyncpg.Connection = Depends(user_db)) -> asyncpg.Connection:
    """Fast, friendly 403. The database functions check again on every call."""
    if not await is_platform_admin(conn):
        raise Forbidden("This is for Gazi superusers only.", code="not_platform_admin")
    return conn


router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/tenants")
async def list_tenants(
    conn: asyncpg.Connection = Depends(admin_db), settings: Settings = Depends(app_settings)
) -> list[AdminTenant]:
    return await repo.tenants(conn, settings.included_tokens)


@router.get("/tenants/{tenant_id}/members")
async def list_members(tenant_id: UUID, conn: asyncpg.Connection = Depends(admin_db)) -> list[AdminMember]:
    return await repo.members(conn, tenant_id)


@router.get("/users")
async def list_users(conn: asyncpg.Connection = Depends(admin_db)) -> list[AdminUser]:
    return await repo.users(conn)


@router.get("/activity")
async def list_activity(
    limit: int = Query(200, ge=1, le=1000), conn: asyncpg.Connection = Depends(admin_db)
) -> list[AdminActivity]:
    return await repo.activity(conn, limit)


@router.post("/tenants/{tenant_id}/status")
async def set_tenant_status(
    tenant_id: UUID,
    body: TenantStatusChange,
    conn: asyncpg.Connection = Depends(admin_db),
    settings: Settings = Depends(app_settings),
) -> AdminTenant:
    """Suspend (everyone in it loses access at once; data is kept) or reactivate a workspace."""
    await repo.set_tenant_status(conn, tenant_id, body.status)
    return await repo.tenant(conn, tenant_id, settings.included_tokens)


@router.post("/tenants/{tenant_id}/free-access")
async def set_free_access(
    tenant_id: UUID,
    body: FreeAccessChange,
    conn: asyncpg.Connection = Depends(admin_db),
    settings: Settings = Depends(app_settings),
    billing: BillingProvider | None = Depends(get_billing),
) -> AdminTenant:
    return await service.set_free_access(conn, settings, billing, tenant_id, body.until)


@router.patch("/tenants/{tenant_id}/members/{user_id}")
async def set_member_role(
    tenant_id: UUID, user_id: UUID, body: MemberRoleChange, conn: asyncpg.Connection = Depends(admin_db)
) -> list[AdminMember]:
    await repo.set_member_role(conn, tenant_id, user_id, body.role)
    return await repo.members(conn, tenant_id)


@router.delete("/tenants/{tenant_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(tenant_id: UUID, user_id: UUID, conn: asyncpg.Connection = Depends(admin_db)) -> Response:
    await repo.remove_member(conn, tenant_id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/users/{user_id}/login")
async def set_login(
    user_id: UUID,
    body: LoginChange,
    conn: asyncpg.Connection = Depends(admin_db),
    principal: Principal = Depends(current_principal),
    auth_admin: AuthAdmin = Depends(get_auth_admin),
) -> AdminUser:
    """Disable (ban) or re-enable someone's login across every workspace."""
    return await service.set_login_enabled(conn, principal, auth_admin, user_id, body.enabled)
