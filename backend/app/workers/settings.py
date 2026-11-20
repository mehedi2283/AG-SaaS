"""ARQ worker classes, one per queue (rule 3: replies have their own worker so imports/generation never starve them).

Run, for example:  uv run arq app.workers.settings.ImportsWorker
Every class knows every job; the queue decides which process runs it.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog
from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.crypto import Crypto
from app.core.logging import configure_logging
from app.db.pool import create_pool
from app.llm.client import LlmClient
from app.queue import QUEUE_IMPORTS, QUEUE_LLM, QUEUE_REPLIES, QUEUE_SYNC, ArqQueue
from app.workers import jobs
from app.workers.limits import RateLimiter
from app.workers.maintenance import purge_imports, reconcile_stuck

log = structlog.get_logger()


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    ctx["settings"] = settings
    ctx["pool"] = await create_pool(settings.database_url.get_secret_value(), max_size=5)
    ctx["crypto"] = Crypto.from_base64(settings.kek.get_secret_value(), settings.kek_version)
    ctx["http"] = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
    ctx["queue"] = ArqQueue(ctx["redis"])
    ctx["limits"] = RateLimiter(ctx["redis"])
    ctx["llm"] = LlmClient(ctx["http"], settings, ctx["limits"])
    log.info("worker.started")


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["http"].aclose()
    await ctx["pool"].close()


def _worker(name: str, queue: str, **extra: Any) -> type:
    """ARQ reads settings from the class's own __dict__ (inherited attributes are ignored), so each worker class
    gets every setting directly."""
    return type(
        name,
        (),
        {
            "functions": jobs.ALL,
            "on_startup": startup,
            "on_shutdown": shutdown,
            "queue_name": queue,
            "redis_settings": RedisSettings.from_dsn(get_settings().redis_url),
            "max_tries": 8,
            "job_timeout": 600,
            "keep_result": 3600,
            "retry_jobs": True,
            **extra,
        },
    )


RepliesWorker = _worker("RepliesWorker", QUEUE_REPLIES, max_jobs=10)
LlmWorker = _worker("LlmWorker", QUEUE_LLM, max_jobs=8)
ImportsWorker = _worker("ImportsWorker", QUEUE_IMPORTS, max_jobs=2, job_timeout=1800)  # a 50 MB CSV
# Provider sync, launches, Calendly, reports, and the platform crons.
SyncWorker = _worker(
    "SyncWorker",
    QUEUE_SYNC,
    max_jobs=6,
    cron_jobs=[
        cron(reconcile_stuck, minute=set(range(0, 60, 5)), run_at_startup=True),
        cron(purge_imports, minute=17),
    ],
)

# Kept for the CLAUDE.md command. A worker serves ONE queue; docker-compose runs all four.
DefaultWorker = SyncWorker
