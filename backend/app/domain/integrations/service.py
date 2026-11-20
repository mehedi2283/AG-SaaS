"""Per-tenant provider credentials, envelope-encrypted. Plaintext exists only in memory, only here."""

from __future__ import annotations

import json
from typing import Any

import asyncpg
from pydantic import BaseModel, SecretStr, ValidationError

from app.core.auth import TenantContext
from app.core.crypto import Crypto, aad
from app.core.errors import Forbidden, NotFound, Unprocessable
from app.domain.audit.repo import record
from app.domain.integrations.schemas import ApiKeyConfig, CalendlyConfig, IntegrationOut, Provider

_SCHEMAS: dict[str, type[BaseModel]] = {
    "instantly": ApiKeyConfig,
    "heyreach": ApiKeyConfig,
    "apollo": ApiKeyConfig,
    "calendly": CalendlyConfig,
}
_COLUMNS = "provider, key_last4, status, last_verified_at, last_error, updated_at"


def parse_config(provider: Provider, body: dict[str, Any]) -> BaseModel:
    try:
        return _SCHEMAS[provider].model_validate(body)
    except ValidationError as exc:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors(include_input=False)]
        raise Unprocessable(f"The {provider} settings are incomplete or invalid.", extra={"errors": errors}) from exc


def _plaintext(config: BaseModel) -> tuple[bytes, str]:
    values = {k: (v.get_secret_value() if isinstance(v, SecretStr) else str(v)) for k, v in config}
    secret = values.get("api_key") or values.get("api_token") or ""
    return json.dumps(values).encode(), secret[-4:]


async def _wrapped_dek(conn: asyncpg.Connection) -> bytes:
    row = await conn.fetchrow("select wrapped_dek from app.current_tenant_dek()")
    if row is None or row["wrapped_dek"] is None:
        raise Forbidden("Only admins can change connected accounts.", code="insufficient_role")
    dek: bytes = row["wrapped_dek"]
    return dek


async def list_integrations(conn: asyncpg.Connection) -> list[IntegrationOut]:
    rows = await conn.fetch(f"select {_COLUMNS} from public.integrations order by provider")
    return [IntegrationOut(**dict(r)) for r in rows]


async def save_integration(
    conn: asyncpg.Connection, ctx: TenantContext, crypto: Crypto, provider: Provider, config: BaseModel
) -> IntegrationOut:
    plaintext, last4 = _plaintext(config)
    sealed = crypto.encrypt(await _wrapped_dek(conn), plaintext, aad(ctx.tenant_id, f"integration:{provider}"))
    # Update-then-insert rather than ON CONFLICT: an upsert would need read access to the secret columns,
    # which clients deliberately don't have. A concurrent first save hits the unique key and returns 409.
    row = await conn.fetchrow(
        f"""
        update public.integrations
           set config_ciphertext = $2, config_nonce = $3, dek_version = 1, key_last4 = $4,
               status = 'unverified', last_error = null, last_verified_at = null
         where provider = $1
        returning id, {_COLUMNS}
        """,
        provider,
        sealed.ciphertext,
        sealed.nonce,
        last4,
    )
    if row is None:
        row = await conn.fetchrow(
            f"""
            insert into public.integrations (provider, config_ciphertext, config_nonce, dek_version, key_last4)
            values ($1, $2, $3, 1, $4)
            returning id, {_COLUMNS}
            """,
            provider,
            sealed.ciphertext,
            sealed.nonce,
            last4,
        )
    assert row is not None
    await record(
        conn, ctx, "integration.saved", target_table="integrations", target_id=row["id"], diff={"provider": provider}
    )
    return IntegrationOut(**{k: row[k] for k in IntegrationOut.model_fields})


async def delete_integration(conn: asyncpg.Connection, ctx: TenantContext, provider: Provider) -> None:
    row = await conn.fetchrow("delete from public.integrations where provider = $1 returning id", provider)
    if row is None:
        raise NotFound(f"There's no {provider} account connected, or you need the owner role to remove it.")
    await record(
        conn, ctx, "integration.deleted", target_table="integrations", target_id=row["id"], diff={"provider": provider}
    )
