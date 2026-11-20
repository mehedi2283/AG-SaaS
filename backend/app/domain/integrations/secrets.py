"""Worker-side decryption of a tenant's provider settings and sender signatures. Plaintext lives only in memory
(rule 2) and is never logged."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import asyncpg

from app.core.crypto import Crypto, Sealed, aad
from app.domain.senders.service import signature_aad
from app.workers.context import JobFailed


async def provider_config(conn: asyncpg.Connection, crypto: Crypto, tenant_id: UUID, provider: str) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        select i.config_ciphertext, i.config_nonce, t.wrapped_dek
          from public.integrations i join public.tenants t on t.id = i.tenant_id
         where i.provider = $1
        """,
        provider,
    )
    if row is None:
        raise JobFailed(f"{provider}_not_connected", f"connect {provider} in Settings")
    plain = crypto.decrypt(
        row["wrapped_dek"],
        Sealed(row["config_ciphertext"], row["config_nonce"]),
        aad(tenant_id, f"integration:{provider}"),
    )
    config: dict[str, Any] = json.loads(plain)
    return config


async def sender_signature(conn: asyncpg.Connection, crypto: Crypto, tenant_id: UUID, sender_id: UUID) -> str | None:
    row = await conn.fetchrow(
        """
        select s.signature_ciphertext, s.signature_nonce, t.wrapped_dek
          from public.sender_identities s join public.tenants t on t.id = s.tenant_id
         where s.id = $1
        """,
        sender_id,
    )
    if row is None or row["signature_ciphertext"] is None:
        return None
    return crypto.decrypt(
        row["wrapped_dek"],
        Sealed(row["signature_ciphertext"], row["signature_nonce"]),
        signature_aad(tenant_id, sender_id),
    ).decode()
