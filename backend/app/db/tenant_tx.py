"""Per-transaction database identity. Every query runs inside one of these, so RLS always applies.

- as_user:   role `authenticated` + the VERIFIED JWT claims (API requests).
- as_worker: role `gazi_worker` + app.tenant_id from the job (workers, webhook ingress).
`SET LOCAL` ends with the transaction, so a pooled connection never leaks an identity.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from uuid import UUID

import asyncpg

from app.core.auth import Principal


@asynccontextmanager
async def as_user(pool: asyncpg.Pool, principal: Principal) -> AsyncIterator[asyncpg.Connection]:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("set local role authenticated")
        await conn.execute("select set_config('request.jwt.claims', $1, true)", json.dumps(principal.claims))
        yield cast(asyncpg.Connection, conn)  # a pool proxy with the Connection API


@asynccontextmanager
async def as_worker(pool: asyncpg.Pool, tenant_id: UUID | None) -> AsyncIterator[asyncpg.Connection]:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("set local role gazi_worker")
        if tenant_id is not None:
            await conn.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
        yield cast(asyncpg.Connection, conn)  # a pool proxy with the Connection API
