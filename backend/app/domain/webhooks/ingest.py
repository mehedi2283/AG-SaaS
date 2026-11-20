"""Webhook intake: resolve the URL token to a tenant, verify, store the raw event, enqueue, return fast.

No LLM or provider calls happen here (the n8n reply trigger timed out for exactly that reason).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from uuid import UUID

import asyncpg
import structlog

from app.core.crypto import Crypto, Sealed
from app.core.errors import NotFound, PayloadTooLarge, ServiceUnavailable, Unauthorized, Unprocessable
from app.db.tenant_tx import as_worker
from app.domain.webhooks.endpoints import secret_aad, token_hash
from app.domain.webhooks.schemas import IngestResult, WebhookProvider
from app.queue import QUEUE_REPLIES, QUEUE_SYNC, JobQueue

log = structlog.get_logger()

SECRET_HEADER = "x-gazi-secret"  # noqa: S105  (a header name, not a secret)
_QUEUE = {"instantly": QUEUE_REPLIES, "heyreach": QUEUE_REPLIES, "calendly": QUEUE_SYNC}
_CAMPAIGN_COLUMN = {"instantly": "instantly_campaign_id", "heyreach": "heyreach_campaign_id"}


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def campaign_ref(provider: WebhookProvider, payload: dict[str, Any]) -> str | None:
    """The provider's campaign id in the payload (Calendly has none; it is matched later by utm_campaign)."""
    value = {"instantly": payload.get("campaign_id"), "heyreach": _get(payload, "campaign", "id")}.get(provider)
    return str(value) if value not in (None, "") else None


def dedupe_key(provider: WebhookProvider, payload: dict[str, Any], body: bytes) -> str:
    key: Any = None
    if provider == "instantly":
        key = payload.get("id") or payload.get("email_id") or payload.get("message_id")
    elif provider == "heyreach":
        conversation = payload.get("conversation_id")
        messages = payload.get("recent_messages") or []
        latest = max((m.get("creation_time") or "" for m in messages if isinstance(m, dict)), default="")
        key = f"{conversation}:{latest}" if conversation and latest else None
    elif provider == "calendly":
        uri = _get(payload, "payload", "uri")
        key = f"{payload.get('event')}:{uri}" if uri else None
    return str(key)[:500] if key else "sha256:" + hashlib.sha256(body).hexdigest()


async def ingest(
    pool: asyncpg.Pool,
    crypto: Crypto,
    queue: JobQueue,
    *,
    provider: WebhookProvider,
    token: str,
    headers: dict[str, str],
    body: bytes,
    max_bytes: int,
) -> IngestResult:
    if len(body) > max_bytes:
        raise PayloadTooLarge("The webhook body is too large.")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise Unprocessable("The webhook body isn't valid JSON.", code="invalid_json") from exc
    if not isinstance(payload, dict):
        raise Unprocessable("The webhook body must be a JSON object.", code="invalid_json")

    async with as_worker(pool, None) as conn:
        endpoint = await conn.fetchrow(
            "select endpoint_id, tenant_id, provider, has_secret from app.resolve_webhook_token($1)", token_hash(token)
        )
    if endpoint is None or endpoint["provider"] != provider:
        log.warning("webhook.unknown_token", provider=provider, token_prefix=token[:10])
        raise NotFound("Unknown webhook.")

    tenant_id: UUID = endpoint["tenant_id"]
    structlog.contextvars.bind_contextvars(tenant_id=str(tenant_id), provider=provider)

    async with as_worker(pool, tenant_id) as conn:
        if endpoint["has_secret"]:
            await _verify_secret(conn, crypto, tenant_id, endpoint["endpoint_id"], token, headers.get(SECRET_HEADER))

        if provider in _CAMPAIGN_COLUMN:
            ref = campaign_ref(provider, payload)
            if ref is None:
                log.warning("webhook.missing_campaign")
                raise Unprocessable("The webhook has no campaign id.", code="missing_campaign")
            column = _CAMPAIGN_COLUMN[provider]
            if not await conn.fetchval(f"select exists(select 1 from public.campaigns where {column} = $1)", ref):
                log.warning("webhook.unknown_campaign", campaign_ref=ref)
                raise NotFound("Unknown webhook.")

        event_id: UUID | None = await conn.fetchval(
            """
            insert into public.webhook_events (provider, endpoint_id, dedupe_key, payload)
            values ($1, $2, $3, $4)
            on conflict (tenant_id, provider, dedupe_key) do nothing
            returning id
            """,
            provider,
            endpoint["endpoint_id"],
            dedupe_key(provider, payload, body),
            payload,
        )
        await conn.execute(
            "update public.webhook_endpoints set last_used_at = now() where id = $1", endpoint["endpoint_id"]
        )

    if event_id is None:
        log.info("webhook.duplicate")
        return IngestResult(duplicate=True)

    try:
        await queue.enqueue(
            "handle_webhook_event", tenant_id, str(event_id), job_id=f"webhook:{event_id}", queue=_QUEUE[provider]
        )
    except Exception as exc:
        # The event is stored as 'received'; the reconciler re-enqueues it. Tell the provider to retry too.
        log.error("webhook.enqueue_failed", event_id=str(event_id), error_type=type(exc).__name__)
        raise ServiceUnavailable(
            "Couldn't queue the event. It was saved and will be processed.", code="queue_unavailable"
        ) from exc

    async with as_worker(pool, tenant_id) as conn:
        await conn.execute(
            "update public.webhook_events set status = 'queued' where id = $1 and status = 'received'", event_id
        )
    log.info("webhook.queued", event_id=str(event_id))
    return IngestResult(duplicate=False)


async def _verify_secret(
    conn: asyncpg.Connection, crypto: Crypto, tenant_id: UUID, endpoint_id: UUID, token: str, given: str | None
) -> None:
    row = await conn.fetchrow(
        "select e.secret_ciphertext, e.secret_nonce, t.wrapped_dek from public.webhook_endpoints e"
        " join public.tenants t on t.id = e.tenant_id where e.id = $1",
        endpoint_id,
    )
    assert row is not None
    expected = crypto.decrypt(
        row["wrapped_dek"], Sealed(row["secret_ciphertext"], row["secret_nonce"]), secret_aad(tenant_id, token)
    )
    if not given or not hmac.compare_digest(given.encode(), expected):
        log.warning("webhook.bad_secret")
        raise Unauthorized("Missing or wrong webhook secret.", code="bad_webhook_secret")
