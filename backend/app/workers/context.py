"""@tenant_job: every ARQ job runs for exactly one tenant (rule 3).

The wrapped job is called as job(ctx, tenant_id: UUID, *args). The decorator binds structured-log context,
refuses to run for a tenant that isn't active (a suspended workspace's queued work stops), and turns
unexpected exceptions into a logged, typed failure instead of a silent one. Database work inside a job uses
`as_worker(ctx["pool"], tenant_id)`, which sets role gazi_worker and app.tenant_id, so RLS scopes it.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar
from uuid import UUID

import structlog
from arq import Retry

from app.db.tenant_tx import as_worker

log = structlog.get_logger()

P = ParamSpec("P")
R = TypeVar("R")

Job = Callable[..., Awaitable[Any]]


class JobFailed(Exception):
    """A permanent, expected failure: already recorded on the row; the job must not retry."""

    def __init__(self, error_code: str, detail: str = "") -> None:
        super().__init__(f"{error_code}: {detail}" if detail else error_code)
        self.error_code = error_code


def tenant_job(fn: Job) -> Job:
    @functools.wraps(fn)
    async def wrapper(ctx: dict[str, Any], tenant_id: str, *args: Any, **kwargs: Any) -> Any:
        tid = UUID(str(tenant_id))
        job_id = ctx.get("job_id")
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(tenant_id=str(tid), job_id=job_id, job=fn.__name__)
        async with as_worker(ctx["pool"], tid) as conn:
            status = await conn.fetchval("select status from public.tenants")
        if status != "active":
            log.info("job.skipped_inactive_tenant", tenant_status=status)
            return {"skipped": "tenant_not_active"}
        try:
            return await fn(ctx, tid, *args, **kwargs)
        except Retry:
            raise  # rate limited or a retryable provider error: ARQ runs it again later
        except JobFailed as exc:
            log.warning("job.failed", error_code=exc.error_code)
            from app.domain.alerts import alert_admins  # late import: alerts uses the worker's providers

            try:
                await alert_admins(ctx, tid, exc.error_code)
            except Exception as alert_exc:  # an alert must never turn a recorded failure into a crash
                log.error("alert.crashed", error_type=type(alert_exc).__name__, error_code="alert_failed")
            return {"failed": exc.error_code}
        except Exception as exc:
            log.exception("job.crashed", error_type=type(exc).__name__, error_code="job_crashed")
            raise

    wrapper.__qualname__ = fn.__name__
    return wrapper
