from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, Request

from app.api.deps import get_crypto, get_pool, get_queue
from app.core.config import Settings, app_settings
from app.core.crypto import Crypto
from app.domain.webhooks.ingest import ingest
from app.domain.webhooks.schemas import IngestResult, WebhookProvider
from app.queue import JobQueue

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/{provider}/{token}")
async def receive(
    provider: WebhookProvider,
    token: str,
    request: Request,
    pool: asyncpg.Pool = Depends(get_pool),
    crypto: Crypto = Depends(get_crypto),
    queue: JobQueue = Depends(get_queue),
    settings: Settings = Depends(app_settings),
) -> IngestResult:
    body = await request.body()
    return await ingest(
        pool,
        crypto,
        queue,
        provider=provider,
        token=token,
        headers=dict(request.headers),
        body=body,
        max_bytes=settings.max_webhook_bytes,
    )
