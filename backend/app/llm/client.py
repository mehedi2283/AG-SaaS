"""OpenAI client (one platform key, rule 2) with structured output (rule 5) and usage logging (rule 4).

Every call:
  1. checks the workspace may use AI (billing not paused) and is under its monthly token cap
     (over the cap -> llm_quota_exceeded),
  2. takes a per-tenant rate-limit slot for "openai" (deferring the job when busy),
  3. asks for strict JSON matching a Pydantic model and validates the answer,
  4. writes a usage_logs row (tenant, model, tokens, estimated cost, ref).
Errors are classified: 429/5xx/timeouts retry later (arq.Retry); other 4xx are permanent (JobFailed).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, TypeVar
from uuid import UUID

import asyncpg
import httpx
import structlog
from arq import Retry
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.errors import PaymentRequired
from app.db.tenant_tx import as_worker
from app.domain.billing.service import ensure_access, ensure_token_budget
from app.llm.outputs import strict_schema
from app.workers.context import JobFailed
from app.workers.limits import RateLimiter

log = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
# Estimated USD per 1M tokens (input, output), for usage_logs.cost_usd_est. Update from OpenAI's price list.
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-5.5": (Decimal("1.25"), Decimal("10.00")),
    "gpt-5.4-mini": (Decimal("0.25"), Decimal("2.00")),
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
}
_DEFAULT_PRICE = (Decimal("1.25"), Decimal("10.00"))


@dataclass(frozen=True)
class LlmResult:
    output: Any
    model: str
    prompt_tokens: int
    completion_tokens: int


class LlmClient:
    def __init__(self, http: httpx.AsyncClient, settings: Settings, limits: RateLimiter) -> None:
        self._http = http
        self._settings = settings
        self._limits = limits

    async def complete(
        self,
        pool: asyncpg.Pool,
        tenant_id: UUID,
        *,
        model: str,
        system: str,
        user: str,
        output: type[T],
        temperature: float,
        ref_table: str,
        ref_id: UUID | None,
        job_id: str | None,
    ) -> LlmResult:
        key = self._settings.openai_api_key
        if key is None:
            raise JobFailed("llm_not_configured", "GAZI_OPENAI_API_KEY is not set")
        # Short transactions only: no DB transaction stays open during the (slow) model call, and the usage row is
        # committed even if the caller's own work fails afterwards.
        try:
            async with as_worker(pool, tenant_id) as conn:
                await ensure_access(conn, self._settings)
                await ensure_token_budget(conn, self._settings, worker=True)
        except PaymentRequired as exc:
            raise JobFailed(exc.error_code) from exc

        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": output.__name__, "schema": strict_schema(output), "strict": True},
            },
        }
        if not model.startswith(("gpt-5", "o")):  # reasoning models only accept their default temperature
            body["temperature"] = temperature

        async with self._limits.slot(tenant_id, "openai"):
            try:
                response = await self._http.post(
                    OPENAI_URL,
                    json=body,
                    headers={"Authorization": f"Bearer {key.get_secret_value()}"},
                    timeout=httpx.Timeout(120, connect=10),
                )
            except httpx.TimeoutException as exc:
                log.warning("llm.timeout", model=model)
                raise Retry(defer=20) from exc
            except httpx.HTTPError as exc:
                log.warning("llm.unreachable", model=model, error_type=type(exc).__name__)
                raise Retry(defer=30) from exc

        if response.status_code == 429 or response.status_code >= 500:
            wait = float(response.headers.get("retry-after", "20") or 20)
            log.warning("llm.retryable", status=response.status_code, model=model)
            raise Retry(defer=min(max(wait, 5), 300))
        if response.status_code >= 400:
            log.error("llm.rejected", status=response.status_code, model=model, error_code="llm_rejected")
            raise JobFailed("llm_rejected", f"OpenAI returned {response.status_code}")

        data = response.json()
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        async with as_worker(pool, tenant_id) as conn:
            await self._log_usage(
                conn, data.get("model") or model, prompt_tokens, completion_tokens, ref_table, ref_id, job_id
            )

        try:
            content = data["choices"][0]["message"]["content"]
            parsed = output.model_validate(json.loads(content))
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            log.warning("llm.bad_output", model=model, error_code="llm_bad_output")
            raise JobFailed("llm_bad_output", type(exc).__name__) from exc
        return LlmResult(parsed, data.get("model") or model, prompt_tokens, completion_tokens)

    async def _log_usage(
        self,
        conn: asyncpg.Connection,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        ref_table: str,
        ref_id: UUID | None,
        job_id: str | None,
    ) -> None:
        base = next((m for m in PRICES if model.startswith(m)), None)
        price_in, price_out = PRICES[base] if base else _DEFAULT_PRICE
        cost = (price_in * prompt_tokens + price_out * completion_tokens) / Decimal(1_000_000)
        await conn.execute(
            """
            insert into public.usage_logs (kind, provider, model, prompt_tokens, completion_tokens, cost_usd_est,
                                           ref_table, ref_id, job_id)
            values ('llm', 'openai', $1, $2, $3, $4, $5, $6, $7)
            """,
            model,
            prompt_tokens,
            completion_tokens,
            cost,
            ref_table,
            ref_id,
            job_id,
        )
