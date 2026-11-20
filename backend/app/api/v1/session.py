from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, status

from app.api.deps import get_auth_admin, get_crypto, user_db
from app.core.auth import Principal, current_principal
from app.core.crypto import Crypto
from app.core.errors import Forbidden
from app.domain.tenants import repo
from app.domain.tenants.schemas import ActiveTenantRequest, ActiveTenantResult, Me, Membership, TenantCreate
from app.providers.gotrue import AuthAdmin

router = APIRouter(tags=["session"])


@router.get("/me")
async def me(principal: Principal = Depends(current_principal), conn: asyncpg.Connection = Depends(user_db)) -> Me:
    return Me(
        user_id=principal.user_id,
        email=principal.email,
        active_tenant_id=principal.tenant_id,
        active_role=principal.tenant_role,
        workspaces=await repo.my_workspaces(conn),
    )


@router.get("/tenants")
async def list_workspaces(conn: asyncpg.Connection = Depends(user_db)) -> list[Membership]:
    return await repo.my_workspaces(conn)


@router.post("/tenants", status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: TenantCreate,
    principal: Principal = Depends(current_principal),
    conn: asyncpg.Connection = Depends(user_db),
    crypto: Crypto = Depends(get_crypto),
) -> Membership:
    tenant_id = await repo.create_workspace(conn, body.name, body.slug, crypto.new_wrapped_dek())
    return next(m for m in await repo.my_workspaces(conn) if m.tenant_id == tenant_id)


@router.post("/session/active-tenant")
async def switch_workspace(
    body: ActiveTenantRequest,
    principal: Principal = Depends(current_principal),
    conn: asyncpg.Connection = Depends(user_db),
    auth_admin: AuthAdmin = Depends(get_auth_admin),
) -> ActiveTenantResult:
    """Record the user's choice; the access-token hook re-checks membership when the session refreshes."""
    workspaces = {m.tenant_id: m for m in await repo.my_workspaces(conn)}
    chosen = workspaces.get(body.tenant_id)
    if chosen is None or chosen.membership_status != "active":
        raise Forbidden("You're not a member of that workspace.", code="not_a_member")
    await auth_admin.set_active_tenant(principal.user_id, body.tenant_id)
    return ActiveTenantResult(tenant_id=body.tenant_id)
