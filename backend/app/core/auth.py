"""JWT verification. The ONLY place tokens are verified.

Asymmetric tokens (ES256/RS256, current Supabase) are checked against the public keys at GAZI_JWT_JWKS_URL;
legacy HS256 tokens against GAZI_JWT_SECRET. Only algorithms with a configured key are accepted.

The tenant comes only from the verified `tenant_id` claim (set by app.custom_access_token_hook).
Role checks here are a fast path; RLS re-checks the live membership on every query.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal
from uuid import UUID

import anyio
import jwt
import structlog
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, app_settings
from app.core.errors import Forbidden, ServiceUnavailable, Unauthorized

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


ASYMMETRIC_ALGORITHMS = ("ES256", "RS256")
log = structlog.get_logger()


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> jwt.PyJWKClient:
    # Caches the key set for 5 minutes and refetches once when it meets an unknown key id (key rotation).
    return jwt.PyJWKClient(url, cache_jwk_set=True, lifespan=300, timeout=5)


async def signing_key(token: str, settings: Settings) -> tuple[Any, list[str]]:
    """The key and the single algorithm allowed for this token, based on configuration, never on trust."""
    try:
        alg = jwt.get_unverified_header(token).get("alg")
    except jwt.PyJWTError as exc:
        raise Unauthorized("The access token is invalid. Sign in again.", code="invalid_token") from exc
    if alg in ASYMMETRIC_ALGORITHMS and settings.jwt_jwks_url:
        client = _jwks_client(settings.jwt_jwks_url)
        try:
            key = await anyio.to_thread.run_sync(client.get_signing_key_from_jwt, token)
        except jwt.PyJWKClientConnectionError as exc:
            log.error("auth.jwks_unreachable", error_type=type(exc).__name__)
            raise ServiceUnavailable(
                "Sign-in keys couldn't be loaded. Try again.", code="auth_keys_unavailable"
            ) from exc
        except jwt.PyJWKClientError as exc:
            raise Unauthorized("The access token is invalid. Sign in again.", code="invalid_token") from exc
        return key.key, [alg]
    if alg == "HS256" and settings.jwt_secret:
        return settings.jwt_secret.get_secret_value(), ["HS256"]
    raise Unauthorized("The access token is invalid. Sign in again.", code="invalid_token")


def verify_token(token: str, key: Any, algorithms: list[str], settings: Settings) -> Principal:
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=algorithms,
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


# Declared as HTTP bearer so the API docs page offers an "Authorize" button.
_bearer = HTTPBearer(auto_error=False, description="Supabase access token")


async def current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(app_settings),
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise Unauthorized("Sign in to continue.", code="missing_token")
    token = credentials.credentials.strip()
    key, algorithms = await signing_key(token, settings)
    principal = verify_token(token, key, algorithms, settings)
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
