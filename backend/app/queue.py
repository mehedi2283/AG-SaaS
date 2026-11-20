"""Job queue interface. Every job's first argument is the tenant_id (rule 3)."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from arq.connections import ArqRedis

QUEUE_REPLIES = "gazi:replies"
QUEUE_LLM = "gazi:llm"
QUEUE_SYNC = "gazi:sync"
QUEUE_IMPORTS = "gazi:imports"


class JobQueue(Protocol):
    async def enqueue(self, function: str, tenant_id: UUID, *args: str, job_id: str, queue: str) -> None: ...


class ArqQueue:
    def __init__(self, redis: ArqRedis) -> None:
        self._redis = redis

    async def enqueue(self, function: str, tenant_id: UUID, *args: str, job_id: str, queue: str) -> None:
        # A deterministic job_id makes enqueueing idempotent: ARQ ignores a second job with the same id.
        await self._redis.enqueue_job(function, str(tenant_id), *args, _job_id=job_id, _queue_name=queue)
