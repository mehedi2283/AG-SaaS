"""API test harness: the real app and the real database (migrations + RLS), connecting as a non-superuser
login role exactly like production. Only the job queue and the GoTrue admin client are faked."""

from __future__ import annotations

import base64
import os
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx
import jwt
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr

from app.core.config import Settings
from app.core.crypto import Crypto
from app.db.pool import create_pool
from app.main import create_app
from tests.rls.conftest import Seed, dsn, seed  # noqa: F401  (session fixtures reused here)

JWT_SECRET = "test-jwt-secret-that-is-long-enough-for-hs256"
API_ROLE, API_PASSWORD = "gazi_api_test", "gazi-api-test-password"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass
class FakeQueue:
    jobs: list[dict[str, Any]] = field(default_factory=list)
    fail: bool = False

    async def enqueue(self, function: str, tenant_id: UUID, *args: str, job_id: str, queue: str) -> None:
        if self.fail:
            raise ConnectionError("redis down")
        self.jobs.append({"function": function, "tenant_id": tenant_id, "args": args, "job_id": job_id, "queue": queue})


@dataclass
class FakeAuthAdmin:
    calls: list[tuple[UUID, UUID]] = field(default_factory=list)
    logins: list[tuple[UUID, bool]] = field(default_factory=list)

    async def set_active_tenant(self, user_id: UUID, tenant_id: UUID) -> None:
        self.calls.append((user_id, tenant_id))

    async def set_login_enabled(self, user_id: UUID, enabled: bool) -> None:
        self.logins.append((user_id, enabled))


@pytest.fixture(scope="session")
def crypto() -> Crypto:
    return Crypto.from_base64(base64.b64encode(os.urandom(32)).decode(), 1)


@pytest.fixture(scope="session")
def api_dsn(dsn: str, seed: Seed, crypto: Crypto) -> str:  # noqa: F811
    """A login role like production's: member of authenticated + gazi_worker, not superuser, no BYPASSRLS."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            f"""
            do $$ begin
              if not exists (select 1 from pg_roles where rolname = '{API_ROLE}') then
                create role {API_ROLE} login noinherit password '{API_PASSWORD}';
              end if;
            end $$;
            grant authenticated, gazi_worker to {API_ROLE};
            """
        )
        # Seeded tenants get real wrapped data keys so the API can encrypt for them.
        for tenant in (seed.a.id, seed.b.id):
            conn.execute("update public.tenants set wrapped_dek = %s where id = %s", [crypto.new_wrapped_dek(), tenant])
    params = conninfo_to_dict(dsn)
    host, port = params.get("host", "127.0.0.1"), params.get("port", "5432")
    return f"postgresql://{API_ROLE}:{API_PASSWORD}@{host}:{port}/{params['dbname']}"


@pytest.fixture
def settings(api_dsn: str) -> Settings:
    return Settings(
        database_url=SecretStr(api_dsn),
        jwt_secret=SecretStr(JWT_SECRET),
        gotrue_service_key=SecretStr("unused"),
        kek=SecretStr("unused"),
        log_level="WARNING",
    )


@dataclass
class Harness:
    client: httpx.AsyncClient
    queue: FakeQueue
    auth_admin: FakeAuthAdmin
    crypto: Crypto
    dsn: str


@pytest.fixture
async def api(settings: Settings, crypto: Crypto, dsn: str) -> AsyncIterator[Harness]:  # noqa: F811
    pool = await create_pool(settings.database_url.get_secret_value(), max_size=4)
    queue, auth_admin = FakeQueue(), FakeAuthAdmin()
    app = create_app(settings, pool=pool, queue=queue, auth_admin=auth_admin, crypto=crypto)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield Harness(client, queue, auth_admin, crypto, dsn)
    await pool.close()


def token(
    user_id: UUID,
    tenant_id: UUID | None = None,
    role: str | None = None,
    *,
    expires_in: int = 300,
    secret: str = JWT_SECRET,
) -> str:
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "aud": "authenticated",
        "role": "authenticated",
        "exp": int(time.time()) + expires_in,
        "email": f"{user_id}@example.com",
    }
    if tenant_id is not None:
        claims |= {"tenant_id": str(tenant_id), "tenant_role": role}
    return jwt.encode(claims, secret, algorithm="HS256")


def auth(user_id: UUID, tenant_id: UUID | None = None, role: str | None = None, **kw: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(user_id, tenant_id, role, **kw)}"}


@pytest.fixture
def superuser(dsn: str) -> Iterator[psycopg.Connection]:  # noqa: F811
    with psycopg.connect(dsn, autocommit=True) as conn:
        yield conn


def unique_slug(prefix: str = "ws") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"
