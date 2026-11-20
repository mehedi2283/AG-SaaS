from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.auth import Role


class TenantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")


class Membership(BaseModel):
    tenant_id: UUID
    name: str
    slug: str
    role: Role
    membership_status: Literal["invited", "active"]


class Me(BaseModel):
    user_id: UUID
    email: str | None
    active_tenant_id: UUID | None
    active_role: Role | None
    workspaces: list[Membership]


class ActiveTenantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # A selection only: checked against the caller's memberships. Data access still reads the tenant
    # from the verified JWT, which the auth hook re-issues after this call.
    tenant_id: UUID


class ActiveTenantResult(BaseModel):
    tenant_id: UUID
    refresh_session: bool = True
