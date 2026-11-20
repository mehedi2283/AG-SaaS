from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
from fastapi import Depends, Request

from app.core.auth import Principal, TenantContext, current_principal, current_tenant
from app.core.crypto import Crypto
from app.db.tenant_tx import as_user
from app.providers.gotrue import AuthAdmin
from app.queue import JobQueue


def get_pool(request: Request) -> asyncpg.Pool:
    pool: asyncpg.Pool = request.app.state.pool
    return pool


def get_crypto(request: Request) -> Crypto:
    crypto: Crypto = request.app.state.crypto
    return crypto


def get_queue(request: Request) -> JobQueue:
    queue: JobQueue = request.app.state.queue
    return queue


def get_auth_admin(request: Request) -> AuthAdmin:
    admin: AuthAdmin = request.app.state.auth_admin
    return admin


async def user_db(
    principal: Principal = Depends(current_principal), pool: asyncpg.Pool = Depends(get_pool)
) -> AsyncIterator[asyncpg.Connection]:
    """A transaction as the signed-in user, with or without an active workspace."""
    async with as_user(pool, principal) as conn:
        yield conn


async def tenant_db(
    ctx: TenantContext = Depends(current_tenant), pool: asyncpg.Pool = Depends(get_pool)
) -> AsyncIterator[asyncpg.Connection]:
    """A transaction as the signed-in user inside their active workspace."""
    async with as_user(pool, ctx.principal) as conn:
        yield conn
