"""Cron jobs that look across tenants: re-enqueue stuck work, purge expired CSVs.

They only FIND work through SECURITY DEFINER functions that return ids; every read or write of a row happens
in a per-tenant worker transaction, so RLS still applies.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import anyio
import structlog

from app.db.tenant_tx import as_worker
from app.queue import QUEUE_IMPORTS, QUEUE_LLM, QUEUE_REPLIES, QUEUE_SYNC, JobQueue

log = structlog.get_logger()

# kind -> (job function, queue, job id prefix). Job ids match the original enqueue, so ARQ dedupes.
REQUEUE: dict[str, tuple[str, str, str]] = {
    "webhook_event": ("handle_webhook_event", QUEUE_REPLIES, "webhook"),
    "lead_import": ("import_leads", QUEUE_IMPORTS, "import"),
    "lead_generation": ("generate_lead_sequence", QUEUE_LLM, "gen-lead"),
}


async def reconcile_stuck(ctx: dict[str, Any]) -> dict[str, int]:
    """Postgres status is the source of truth: anything stuck too long is enqueued again (idempotent jobs)."""
    queue: JobQueue = ctx["queue"]
    async with as_worker(ctx["pool"], None) as conn:
        rows = await conn.fetch("select kind, tenant_id, id from app.stuck_work()")
    counts: dict[str, int] = {}
    for row in rows:
        route = REQUEUE.get(row["kind"])
        if route is None:
            continue
        function, queue_name, prefix = route
        # Calendly webhook events belong on the sync queue.
        if row["kind"] == "webhook_event":
            async with as_worker(ctx["pool"], row["tenant_id"]) as conn:
                provider = await conn.fetchval("select provider from public.webhook_events where id = $1", row["id"])
            queue_name = QUEUE_SYNC if provider == "calendly" else QUEUE_REPLIES
        # A fresh job id per attempt window: the original job finished or died, so its id may be taken.
        job_id = f"{prefix}:{row['id']}:retry"
        await queue.enqueue(function, row["tenant_id"], str(row["id"]), job_id=job_id, queue=queue_name)
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    if counts:
        log.info("reconcile.requeued", **counts)
    return counts


async def purge_imports(ctx: dict[str, Any]) -> int:
    """Delete uploaded CSVs after 7 days (rule 8). Lead data stays in Postgres."""
    root: str = ctx["settings"].imports_dir
    async with as_worker(ctx["pool"], None) as conn:
        rows = await conn.fetch("select tenant_id, id, file_path from app.expired_imports()")
    purged = 0
    for row in rows:
        if not await anyio.to_thread.run_sync(_delete_inside, root, row["file_path"]):
            log.error("purge.path_outside_volume", import_id=str(row["id"]), error_code="bad_import_path")
        async with as_worker(ctx["pool"], row["tenant_id"]) as conn:
            await conn.execute(
                "update public.lead_imports set file_path = null, file_deleted_at = now() where id = $1", row["id"]
            )
        purged += 1
    if purged:
        log.info("purge.imports_deleted", count=purged)
    return purged


def _delete_inside(root: str, file_path: str) -> bool:
    """Delete the file if it lies inside the imports volume. False (and nothing deleted) otherwise."""
    base, path = Path(root).resolve(), Path(file_path).resolve()
    if base not in path.parents:
        return False
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    return True
