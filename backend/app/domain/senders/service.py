"""Sender identities: who a campaign sends as. The signature is envelope-encrypted (rule 2)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from app.core.auth import TenantContext
from app.core.crypto import Crypto, aad
from app.core.errors import Conflict, Forbidden, NotFound
from app.domain.audit.repo import record
from app.domain.senders.schemas import SenderIn, SenderOut, SenderUpdate

_COLS = (
    "id, display_name, title, company, dek_version is not null as has_signature, signature_image_url,"
    " instantly_eaccount, heyreach_account_id, is_default, updated_at"
)


def signature_aad(tenant_id: UUID, sender_id: UUID) -> bytes:
    return aad(tenant_id, f"sender_signature:{sender_id}")


async def _dek(conn: asyncpg.Connection) -> bytes:
    dek: bytes | None = await conn.fetchval("select wrapped_dek from app.current_tenant_dek()")
    if dek is None:
        raise Forbidden("Only admins can change senders.", code="insufficient_role")
    return dek


async def list_senders(conn: asyncpg.Connection) -> list[SenderOut]:
    rows = await conn.fetch(f"select {_COLS} from public.sender_identities order by is_default desc, display_name")
    return [SenderOut(**dict(r)) for r in rows]


def _plain(values: dict[str, Any]) -> dict[str, Any]:
    if values.get("signature_image_url") is not None:
        values["signature_image_url"] = str(values["signature_image_url"])
    return values


async def create_sender(conn: asyncpg.Connection, ctx: TenantContext, crypto: Crypto, body: SenderIn) -> SenderOut:
    values = _plain(body.model_dump(exclude={"signature"}))
    async with conn.transaction():
        if values["is_default"]:
            await conn.execute("update public.sender_identities set is_default = false where is_default")
        sender_id: UUID = await conn.fetchval(
            """
            insert into public.sender_identities
              (display_name, title, company, signature_image_url, instantly_eaccount, heyreach_account_id, is_default)
            values ($1, $2, $3, $4, $5, $6, $7) returning id
            """,
            *(
                values[k]
                for k in (
                    "display_name",
                    "title",
                    "company",
                    "signature_image_url",
                    "instantly_eaccount",
                    "heyreach_account_id",
                    "is_default",
                )
            ),
        )
        if body.signature:
            await _save_signature(conn, ctx, crypto, sender_id, body.signature)
    await record(conn, ctx, "sender.created", target_table="sender_identities", target_id=sender_id)
    return await get_sender(conn, sender_id)


async def _save_signature(
    conn: asyncpg.Connection, ctx: TenantContext, crypto: Crypto, sender_id: UUID, signature: str | None
) -> None:
    if not signature:
        await conn.execute(
            "update public.sender_identities set signature_ciphertext = null, signature_nonce = null,"
            " dek_version = null where id = $1",
            sender_id,
        )
        return
    sealed = crypto.encrypt(await _dek(conn), signature.encode(), signature_aad(ctx.tenant_id, sender_id))
    await conn.execute(
        "update public.sender_identities set signature_ciphertext = $2, signature_nonce = $3, dek_version = 1"
        " where id = $1",
        sender_id,
        sealed.ciphertext,
        sealed.nonce,
    )


async def get_sender(conn: asyncpg.Connection, sender_id: UUID) -> SenderOut:
    row = await conn.fetchrow(f"select {_COLS} from public.sender_identities where id = $1", sender_id)
    if row is None:
        raise NotFound("There's no such sender.")
    return SenderOut(**dict(row))


async def update_sender(
    conn: asyncpg.Connection, ctx: TenantContext, crypto: Crypto, sender_id: UUID, body: SenderUpdate
) -> SenderOut:
    await get_sender(conn, sender_id)
    changes = _plain(body.model_dump(exclude_unset=True, exclude={"signature"}))
    async with conn.transaction():
        if changes.get("is_default"):
            await conn.execute(
                "update public.sender_identities set is_default = false where is_default and id <> $1", sender_id
            )
        if changes:
            sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(changes))
            result = await conn.execute(
                f"update public.sender_identities set {sets} where id = $1", sender_id, *changes.values()
            )
            if result == "UPDATE 0":
                raise Forbidden("Only admins can change senders.", code="insufficient_role")
        if "signature" in body.model_fields_set:
            await _save_signature(conn, ctx, crypto, sender_id, body.signature)
    await record(
        conn,
        ctx,
        "sender.updated",
        target_table="sender_identities",
        target_id=sender_id,
        diff={"fields": sorted(body.model_fields_set)},
    )
    return await get_sender(conn, sender_id)


async def delete_sender(conn: asyncpg.Connection, ctx: TenantContext, sender_id: UUID) -> None:
    in_use = await conn.fetchval(
        "select exists(select 1 from public.campaigns where sender_identity_id = $1)", sender_id
    )
    if in_use:
        raise Conflict("A campaign uses this sender. Pick another sender for it first.", code="sender_in_use")
    row = await conn.fetchrow("delete from public.sender_identities where id = $1 returning id", sender_id)
    if row is None:
        raise NotFound("There's no such sender, or you need the admin role to remove it.")
    await record(conn, ctx, "sender.deleted", target_table="sender_identities", target_id=sender_id)
