"""Webhook endpoints: one active URL token per tenant and provider (rule 7)."""

from __future__ import annotations

import hashlib
import secrets
from uuid import UUID

import asyncpg

from app.core.auth import TenantContext
from app.core.crypto import Crypto, aad
from app.core.errors import Conflict, Forbidden, NotFound
from app.domain.audit.repo import record
from app.domain.webhooks.schemas import EndpointCreated, EndpointOut, WebhookProvider

_COLUMNS = "id, provider, token_prefix, dek_version is not null as has_secret, created_at, revoked_at, last_used_at"


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def secret_aad(tenant_id: UUID, token: str) -> bytes:
    """Binds the secret to its tenant and its URL token, so it can't be moved to another endpoint."""
    return aad(tenant_id, f"webhook_secret:{token_hash(token).hex()}")


async def list_endpoints(conn: asyncpg.Connection) -> list[EndpointOut]:
    rows = await conn.fetch(f"select {_COLUMNS} from public.webhook_endpoints order by created_at desc")
    return [EndpointOut(**dict(r)) for r in rows]


async def create_endpoint(
    conn: asyncpg.Connection, ctx: TenantContext, crypto: Crypto, provider: WebhookProvider, require_secret: bool
) -> EndpointCreated:
    token = f"wh_{secrets.token_urlsafe(32)}"
    secret: str | None = None
    ciphertext: bytes | None = None
    nonce: bytes | None = None
    if require_secret:
        dek = await conn.fetchval("select wrapped_dek from app.current_tenant_dek()")
        if dek is None:
            raise Forbidden("Only admins can manage webhooks.", code="insufficient_role")
        secret = secrets.token_urlsafe(24)
        sealed = crypto.encrypt(dek, secret.encode(), secret_aad(ctx.tenant_id, token))
        ciphertext, nonce = sealed.ciphertext, sealed.nonce
    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                insert into public.webhook_endpoints
                  (provider, token_hash, token_prefix, created_by, secret_ciphertext, secret_nonce, dek_version)
                values ($1, $2, $3, $4, $5, $6, case when $5::bytea is null then null else 1 end)
                returning {_COLUMNS}
                """,
                provider,
                token_hash(token),
                token[:10],
                ctx.principal.user_id,
                ciphertext,
                nonce,
            )
    except asyncpg.UniqueViolationError as exc:
        raise Conflict(
            f"There's already an active {provider} webhook. Rotate it to get a new URL.", code="endpoint_exists"
        ) from exc
    assert row is not None
    await record(
        conn,
        ctx,
        "webhook_endpoint.created",
        target_table="webhook_endpoints",
        target_id=row["id"],
        diff={"provider": provider, "require_secret": require_secret},
    )
    return EndpointCreated(**dict(row), url_path=f"/webhooks/{provider}/{token}", secret=secret)


async def revoke_endpoint(conn: asyncpg.Connection, ctx: TenantContext, endpoint_id: UUID) -> EndpointOut:
    row = await conn.fetchrow(
        f"update public.webhook_endpoints set revoked_at = now() where id = $1 and revoked_at is null"
        f" returning {_COLUMNS}",
        endpoint_id,
    )
    if row is None:
        raise NotFound("That webhook doesn't exist or is already revoked.")
    await record(conn, ctx, "webhook_endpoint.revoked", target_table="webhook_endpoints", target_id=endpoint_id)
    return EndpointOut(**dict(row))


async def rotate_endpoint(
    conn: asyncpg.Connection, ctx: TenantContext, crypto: Crypto, endpoint_id: UUID
) -> EndpointCreated:
    old = await revoke_endpoint(conn, ctx, endpoint_id)
    return await create_endpoint(conn, ctx, crypto, old.provider, old.has_secret)
