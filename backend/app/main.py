"""FastAPI app factory. Dependencies can be injected for tests; otherwise they are built from settings."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
import structlog
from arq import create_pool as create_redis
from arq.connections import RedisSettings
from fastapi import FastAPI, Request, Response

from app.api.v1 import (
    admin,
    billing,
    campaigns,
    generation,
    integrations,
    leads,
    senders,
    session,
    webhook_endpoints,
    workspace,
)
from app.core.config import Settings, get_settings
from app.core.crypto import Crypto
from app.core.errors import ServiceUnavailable, install_error_handlers
from app.core.logging import configure_logging
from app.db.pool import create_pool
from app.providers.gotrue import AuthAdmin, GoTrueAdmin
from app.providers.stripe import BillingProvider, StripeBilling
from app.queue import ArqQueue, JobQueue
from app.webhooks.router import router as webhooks_router

log = structlog.get_logger()


def create_app(
    settings: Settings | None = None,
    *,
    pool: object | None = None,
    queue: JobQueue | None = None,
    auth_admin: AuthAdmin | None = None,
    crypto: Crypto | None = None,
    billing_provider: BillingProvider | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned: list[Callable[[], Awaitable[object]]] = []
        app.state.settings = settings
        app.state.crypto = crypto or Crypto.from_base64(settings.kek.get_secret_value(), settings.kek_version)
        if pool is None:
            app.state.pool = await create_pool(settings.database_url.get_secret_value())
            owned.append(app.state.pool.close)
        else:
            app.state.pool = pool
        if queue is None:
            redis = await create_redis(RedisSettings.from_dsn(settings.redis_url))
            app.state.redis = redis
            app.state.queue = ArqQueue(redis)
            owned.append(redis.aclose)
        else:
            app.state.redis, app.state.queue = None, queue
        if auth_admin is None:
            client = httpx.AsyncClient()
            app.state.auth_admin = GoTrueAdmin(
                settings.gotrue_url, settings.gotrue_service_key.get_secret_value(), client
            )
            owned.append(client.aclose)
        else:
            app.state.auth_admin = auth_admin
        if billing_provider is not None:
            app.state.billing = billing_provider
        elif settings.billing_enabled and settings.stripe_secret_key is not None:
            secret = settings.stripe_webhook_secret
            app.state.billing = StripeBilling(
                settings.stripe_secret_key.get_secret_value(), secret.get_secret_value() if secret else None
            )
        else:
            app.state.billing = None
        yield
        for close in reversed(owned):
            await close()

    app = FastAPI(title="Gazi API", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id, path=request.url.path)
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    async def readyz(request: Request) -> dict[str, str]:
        try:
            await request.app.state.pool.fetchval("select 1")
            if request.app.state.redis is not None:
                await request.app.state.redis.ping()
        except Exception as exc:
            log.error("readyz.failed", error_type=type(exc).__name__)
            raise ServiceUnavailable("A dependency isn't reachable.", code="not_ready") from exc
        return {"status": "ready"}

    for router in (
        session.router,
        integrations.router,
        webhook_endpoints.router,
        billing.router,
        workspace.router,
        admin.router,
        senders.router,
        campaigns.router,
        leads.router,
        generation.router,
    ):
        app.include_router(router, prefix="/v1")
    app.include_router(webhooks_router)
    return app
