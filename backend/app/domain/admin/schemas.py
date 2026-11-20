from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.core.auth import Role


class AdminTenant(BaseModel):
    """Account overview of one workspace. Never includes tenant content (leads, messages, replies)."""

    id: UUID
    name: str
    slug: str
    status: str
    created_at: datetime
    members: int
    owner_email: str | None
    subscription_status: str
    trial_end: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    access: str
    comp_until: datetime | None
    addon_tokens: int
    tokens_this_month: int
    token_cap: int = 0  # filled in from GAZI_INCLUDED_TOKENS + addon_tokens
    stripe_customer_id: str | None
    stripe_subscription_id: str | None


class AdminUser(BaseModel):
    id: UUID
    email: str
    created_at: datetime
    last_sign_in_at: datetime | None
    banned_until: datetime | None
    workspaces: int
    is_platform_admin: bool
    login_enabled: bool


class AdminMember(BaseModel):
    user_id: UUID
    email: str
    role: Role
    status: str
    created_at: datetime


class AdminActivity(BaseModel):
    source: Literal["platform", "workspace"]
    created_at: datetime
    tenant_id: UUID | None
    tenant_name: str | None
    actor_email: str | None
    action: str
    target_user_email: str | None
    diff: dict[str, Any]


class TenantStatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["active", "suspended"]


class FreeAccessChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    until: datetime | None  # null ends free access


class MemberRoleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role


class LoginChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
