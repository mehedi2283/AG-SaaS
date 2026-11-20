from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, Request

from app.api.deps import get_billing, get_crypto, get_pool, get_queue
from app.core.config import Settings, app_settings
from app.core.crypto import Crypto
from app.core.errors import PayloadTooLarge
from app.domain.billing.schemas import StripeWebhookResult
from app.domain.billing.service import handle_stripe_webhook
from app.domain.webhooks.ingest import ingest
from app.domain.webhooks.schemas import IngestResult, WebhookProvider
from app.providers.stripe import BillingProvider
from app.queue import JobQueue

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# Declared before /{provider}/{token}. One endpoint for the platform's Stripe account: the tenant comes from
# the customer in the signed event, never from the URL.
@router.post("/stripe")
async def receive_stripe(
    request: Request,
    pool: asyncpg.Pool = Depends(get_pool),
    billing: BillingProvider | None = Depends(get_billing),
    settings: Settings = Depends(app_settings),
) -> StripeWebhookResult:
    body = await request.body()
    if len(body) > settings.max_webhook_bytes:
        raise PayloadTooLarge("The webhook body is too large.")
    return await handle_stripe_webhook(pool, billing, settings, body, request.headers.get("stripe-signature"))


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
