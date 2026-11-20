"""JWT verification. The ONLY place tokens are verified, so switching to asymmetric/JWKS is one change.

The tenant comes only from the verified `tenant_id` claim (set by app.custom_access_token_hook).
Role checks here are a fast path; RLS re-checks the live membership on every query.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

import jwt
import structlog
from fastapi import Depends, Header

from app.core.config import Settings, app_settings
from app.core.errors import Forbidden, Unauthorized

Role = Literal["owner", "admin", "member", "viewer"]
ROLE_RANK: dict[str, int] = {"viewer": 1, "member": 2, "admin": 3, "owner": 4}


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    email: str | None
    tenant_id: UUID | None
    tenant_role: Role | None
    claims: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class TenantContext:
    tenant_id: UUID
    role: Role
    principal: Principal


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


def verify_token(token: str, settings: Settings) -> Principal:
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            audience=settings.jwt_audience,
            options={"require": ["exp", "sub", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("Your session has expired. Sign in again.", code="token_expired") from exc
    except jwt.PyJWTError as exc:
        raise Unauthorized("The access token is invalid. Sign in again.", code="invalid_token") from exc

    user_id = _uuid(payload.get("sub"))
    if user_id is None:
        raise Unauthorized("The access token has no valid user.", code="invalid_token")
    tenant_id = _uuid(payload.get("tenant_id"))
    role = payload.get("tenant_role")
    tenant_role: Role | None = role if role in ROLE_RANK else None
    if (tenant_id is None) != (tenant_role is None):
        raise Unauthorized("The access token has an incomplete workspace claim.", code="invalid_token")
    return Principal(user_id, payload.get("email"), tenant_id, tenant_role, payload)


async def current_principal(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(app_settings),
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("Sign in to continue.", code="missing_token")
    principal = verify_token(authorization[7:].strip(), settings)
    structlog.contextvars.bind_contextvars(user_id=str(principal.user_id))
    return principal


async def current_tenant(principal: Principal = Depends(current_principal)) -> TenantContext:
    if principal.tenant_id is None or principal.tenant_role is None:
        raise Forbidden("Choose a workspace first.", code="no_active_tenant")
    structlog.contextvars.bind_contextvars(tenant_id=str(principal.tenant_id))
    return TenantContext(principal.tenant_id, principal.tenant_role, principal)


def require_role(min_role: Role) -> Callable[..., Awaitable[TenantContext]]:
    async def _check(ctx: TenantContext = Depends(current_tenant)) -> TenantContext:
        if ROLE_RANK[ctx.role] < ROLE_RANK[min_role]:
            raise Forbidden(f"This needs the {min_role} role or higher.", code="insufficient_role")
        return ctx

    return _check
