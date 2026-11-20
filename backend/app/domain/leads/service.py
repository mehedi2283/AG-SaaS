"""Lead imports (CSV upload -> background import), duplicate review, and the lead list."""

from __future__ import annotations

import csv
import io
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import anyio
import asyncpg
import structlog

from app.core.auth import TenantContext
from app.core.config import Settings
from app.core.errors import Conflict, NotFound, PayloadTooLarge, Unprocessable
from app.domain.audit.repo import record
from app.domain.billing.service import ensure_access, ensure_token_budget
from app.domain.leads.apollo import ApolloSearchIn
from app.domain.leads.normalize import LEAD_COLUMNS, map_headers
from app.domain.leads.schemas import (
    DuplicateOut,
    ImportOut,
    LeadOut,
    LeadPage,
    LeadUpdate,
    ResolveIn,
    ResolveOut,
)
from app.queue import QUEUE_IMPORTS, JobQueue

log = structlog.get_logger()

_IMPORT_COLS = """
  i.id, i.campaign_id, i.original_filename, i.source, i.status, i.total_rows, i.imported_rows, i.failed_rows,
  i.duplicate_rows, i.errors, i.error_code, i.created_at, i.finished_at,
  case when i.file_path is not null then i.expires_at end as file_kept_until,
  (select count(*) from public.lead_duplicates d where d.import_id = i.id and d.resolution = 'pending')::int
    as pending_duplicates
"""
_LEAD_COLS = (
    "id, campaign_id, email, first_name, last_name, title, company, linkedin_url, website, industry, city, country,"
    " company_size, status, source, buyer_score, created_at"
)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def _check_header(first_chunk: bytes) -> None:
    """Fail fast, before anything is stored, when the file can't be a lead list."""
    text = first_chunk.decode("utf-8-sig", errors="replace")
    try:
        header = next(csv.reader(io.StringIO(text.splitlines()[0] if text else "")))
    except (StopIteration, csv.Error, IndexError) as exc:
        raise Unprocessable("That file isn't a CSV with a header row.", code="not_a_csv") from exc
    if "email" not in map_headers(header).values():
        raise Unprocessable(
            "The file has no email column. Export it from Apollo with the Email field included.",
            code="no_email_column",
        )


async def upload_import(
    conn: asyncpg.Connection,
    ctx: TenantContext,
    settings: Settings,
    queue: JobQueue,
    *,
    campaign_id: UUID,
    filename: str,
    body: AsyncIterator[bytes],
) -> ImportOut:
    campaign = await conn.fetchrow("select status from public.campaigns where id = $1", campaign_id)
    if campaign is None:
        raise NotFound("There's no such campaign.", code="campaign_not_found")
    if campaign["status"] == "archived":
        raise Conflict("That campaign is archived.", code="campaign_archived")

    folder = Path(settings.imports_dir) / str(ctx.tenant_id)
    path = folder / f"{uuid.uuid4().hex}.csv"  # never the user's file name on disk
    size, checked = 0, False
    await anyio.to_thread.run_sync(lambda: folder.mkdir(parents=True, exist_ok=True))
    try:
        async with await anyio.open_file(path, "wb") as out:
            pending = b""
            async for chunk in body:
                size += len(chunk)
                if size > settings.max_import_bytes:
                    raise PayloadTooLarge("The file is over 50 MB. Split it into smaller files.", code="file_too_large")
                if not checked:
                    pending += chunk
                    if b"\n" in pending or len(pending) > 65_536:
                        _check_header(pending)
                        checked = True
                await out.write(chunk)
        if not checked:
            _check_header(pending)
        if size == 0:
            raise Unprocessable("The file is empty.", code="empty_file")
        import_id: UUID = await conn.fetchval(
            """
            insert into public.lead_imports (campaign_id, original_filename, file_path, created_by)
            values ($1, $2, $3, $4) returning id
            """,
            campaign_id,
            os.path.basename(filename)[:200] or "import.csv",
            str(path),
            ctx.principal.user_id,
        )
    except BaseException:
        await anyio.to_thread.run_sync(lambda: path.unlink(missing_ok=True))
        raise
    await record(
        conn, ctx, "leads.import_uploaded", target_table="lead_imports", target_id=import_id, diff={"bytes": size}
    )
    try:
        await queue.enqueue(
            "import_leads", ctx.tenant_id, str(import_id), job_id=f"import:{import_id}", queue=QUEUE_IMPORTS
        )
    except Exception as exc:  # stored as 'uploaded'; the reconciler enqueues it again
        log.error("leads.enqueue_failed", import_id=str(import_id), error_type=type(exc).__name__)
    return await get_import(conn, import_id)


async def list_imports(conn: asyncpg.Connection, campaign_id: UUID | None) -> list[ImportOut]:
    rows = await conn.fetch(
        f"select {_IMPORT_COLS} from public.lead_imports i where ($1::uuid is null or i.campaign_id = $1)"
        " order by i.created_at desc limit 100",
        campaign_id,
    )
    return [ImportOut(**dict(r)) for r in rows]


async def get_import(conn: asyncpg.Connection, import_id: UUID) -> ImportOut:
    row = await conn.fetchrow(f"select {_IMPORT_COLS} from public.lead_imports i where i.id = $1", import_id)
    if row is None:
        raise NotFound("There's no such import.", code="import_not_found")
    return ImportOut(**dict(row))


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


async def list_duplicates(conn: asyncpg.Connection, import_id: UUID) -> list[DuplicateOut]:
    await get_import(conn, import_id)
    rows = await conn.fetch(
        """
        select d.id, d.incoming_email, d.incoming, d.reason, d.resolution, d.existing_lead_id, d.resolved_at,
               c.name as existing_campaign,
               nullif(btrim(concat_ws(' ', l.first_name, l.last_name)), '') as existing_name
          from public.lead_duplicates d
          left join public.leads l on l.id = d.existing_lead_id
          left join public.campaigns c on c.id = l.campaign_id
         where d.import_id = $1
         order by (d.resolution = 'pending') desc, d.incoming_email
         limit 2000
        """,
        import_id,
    )
    return [DuplicateOut(**dict(r)) for r in rows]


def _incoming_values(incoming: dict[str, Any]) -> dict[str, Any]:
    return {k: incoming.get(k) for k in LEAD_COLUMNS if k != "email" and incoming.get(k) not in (None, "")}


async def resolve_duplicates(
    conn: asyncpg.Connection, ctx: TenantContext, import_id: UUID, body: ResolveIn
) -> ResolveOut:
    imp = await get_import(conn, import_id)
    rows = await conn.fetch(
        """
        select d.id, d.incoming_email, d.incoming, d.reason, d.existing_lead_id, l.campaign_id as existing_campaign_id
          from public.lead_duplicates d
          left join public.leads l on l.id = d.existing_lead_id
         where d.import_id = $1 and d.resolution = 'pending' and ($2 or d.id = any($3::uuid[]))
         for update of d
        """,
        import_id,
        body.all_pending,
        body.ids or [],
    )
    out = ResolveOut(resolved=0, added=0, updated=0, skipped=0, not_possible=0)
    for row in rows:
        incoming = row["incoming"] or {}
        values = _incoming_values(incoming)
        applied = body.resolution
        if body.resolution == "update_existing":
            if row["existing_lead_id"] and values:
                sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(values))
                await conn.execute(
                    f"update public.leads set {sets} where id = $1", row["existing_lead_id"], *values.values()
                )
                out.updated += 1
            else:
                applied = "skip"  # nothing to copy over
                out.skipped += 1
        elif body.resolution == "add_anyway":
            cols = ["campaign_id", "email", "source_import_id", "raw", *values]
            params = ", ".join(f"${i + 1}" for i in range(len(cols)))
            added = await conn.fetchval(
                f"insert into public.leads ({', '.join(cols)}) values ({params})"
                " on conflict (tenant_id, campaign_id, email) do nothing returning id",
                imp.campaign_id,
                row["incoming_email"],
                import_id,
                incoming.get("raw") or {},
                *values.values(),
            )
            if added:
                out.added += 1
            else:  # that email is already in this campaign: one email appears once per campaign
                applied = "skip"
                out.not_possible += 1
        else:
            out.skipped += 1
        await conn.execute(
            "update public.lead_duplicates set resolution = $2, resolved_by = $3, resolved_at = now() where id = $1",
            row["id"],
            applied,
            ctx.principal.user_id,
        )
        out.resolved += 1
    await record(
        conn,
        ctx,
        "leads.duplicates_resolved",
        target_table="lead_imports",
        target_id=import_id,
        diff={"resolution": body.resolution, **out.model_dump()},
    )
    return out


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------


async def list_leads(
    conn: asyncpg.Connection,
    *,
    campaign_id: UUID | None,
    status: str | None,
    q: str | None,
    limit: int,
    offset: int,
) -> LeadPage:
    where = (
        "($1::uuid is null or campaign_id = $1) and ($2::text is null or status = $2) and ($3::text is null or"
        " email ilike $3 or company ilike $3 or concat_ws(' ', first_name, last_name) ilike $3)"
    )
    pattern = f"%{q.strip()}%" if q and q.strip() else None
    total: int = await conn.fetchval(f"select count(*) from public.leads where {where}", campaign_id, status, pattern)
    rows = await conn.fetch(
        f"select {_LEAD_COLS} from public.leads where {where} order by created_at desc, email limit $4 offset $5",
        campaign_id,
        status,
        pattern,
        limit,
        offset,
    )
    return LeadPage(items=[LeadOut(**dict(r)) for r in rows], total=total)


async def update_lead(conn: asyncpg.Connection, ctx: TenantContext, lead_id: UUID, body: LeadUpdate) -> LeadOut:
    changes = body.model_dump(exclude_unset=True)
    if changes:
        sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(changes))
        result = await conn.execute(f"update public.leads set {sets} where id = $1", lead_id, *changes.values())
        if result == "UPDATE 0":
            raise NotFound("There's no such lead.", code="lead_not_found")
        await record(
            conn, ctx, "lead.updated", target_table="leads", target_id=lead_id, diff={"fields": sorted(changes)}
        )
    row = await conn.fetchrow(f"select {_LEAD_COLS} from public.leads where id = $1", lead_id)
    if row is None:
        raise NotFound("There's no such lead.", code="lead_not_found")
    return LeadOut(**dict(row))


async def delete_lead(conn: asyncpg.Connection, ctx: TenantContext, lead_id: UUID) -> None:
    status = await conn.fetchval("select status from public.leads where id = $1", lead_id)
    if status in ("pushed", "replied"):
        raise Conflict(
            "This lead is already in Instantly/HeyReach. Mark it Do not contact instead.", code="lead_pushed"
        )
    row = await conn.fetchrow("delete from public.leads where id = $1 returning id", lead_id)
    if row is None:
        raise NotFound("There's no such lead, or you need the admin role to delete it.", code="lead_not_found")
    await record(conn, ctx, "lead.deleted", target_table="leads", target_id=lead_id)


async def start_apollo_search(
    conn: asyncpg.Connection,
    ctx: TenantContext,
    settings: Settings,
    queue: JobQueue,
    campaign_id: UUID,
    body: ApolloSearchIn,
) -> ImportOut:
    campaign = await conn.fetchrow("select status from public.campaigns where id = $1", campaign_id)
    if campaign is None:
        raise NotFound("There's no such campaign.", code="campaign_not_found")
    if not body.filters():
        raise Unprocessable("Give at least a job title, a location or a keyword.", code="search_too_broad")
    if not await conn.fetchval("select exists(select 1 from public.integrations where provider = 'apollo')"):
        raise Unprocessable("Connect Apollo in Settings first.", code="apollo_not_connected")
    await ensure_access(conn, settings)
    await ensure_token_budget(conn, settings, worker=False)
    import_id: UUID = await conn.fetchval(
        """
        insert into public.lead_imports (campaign_id, original_filename, source, header_map, created_by)
        values ($1, $2, 'apollo', $3, $4) returning id
        """,
        campaign_id,
        body.label()[:200],
        body.model_dump(),
        ctx.principal.user_id,
    )
    await queue.enqueue(
        "apollo_search", ctx.tenant_id, str(import_id), job_id=f"apollo:{import_id}", queue=QUEUE_IMPORTS
    )
    await record(
        conn,
        ctx,
        "leads.apollo_search",
        target_table="lead_imports",
        target_id=import_id,
        diff={"max_people": body.max_people},
    )
    return await get_import(conn, import_id)
