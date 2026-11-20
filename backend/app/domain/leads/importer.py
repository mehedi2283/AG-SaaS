"""The import_leads job: CSV on the imports volume -> leads + lead_duplicates (rule 8).

Idempotent: the import row is locked and only 'uploaded' (or a stale 'processing') rows are worked on, and all
rows are written in ONE transaction, so a crash leaves nothing half-imported and a re-run starts clean.
Duplicates never block the import: they go to lead_duplicates for review and the rest finishes.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import anyio
import asyncpg
import structlog

from app.db.tenant_tx import as_worker
from app.domain.leads.normalize import LEAD_COLUMNS, lead_values, map_headers, normalize_email, normalize_header
from app.workers.context import JobFailed, tenant_job

log = structlog.get_logger()

MAX_ERRORS = 100  # errors kept on the import row for the user to see
CHUNK = 1000
_TEXT_COLS = tuple(c for c in LEAD_COLUMNS if c not in ("email", "company_size"))


@dataclass
class Parsed:
    header_map: dict[str, str]
    leads: list[dict[str, Any]] = field(default_factory=list)  # unique, valid, in file order
    repeated: list[dict[str, Any]] = field(default_factory=list)  # later copies of an email in the file
    errors: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    failed: int = 0


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")  # Excel on Windows


def parse_csv(data: bytes) -> Parsed:
    """Pure: bytes -> normalised rows. Unit-tested without a database."""
    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:20_000], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        headers = next(reader)
    except StopIteration as exc:
        raise JobFailed("empty_file") from exc
    mapping = map_headers(headers)
    if "email" not in mapping.values():
        raise JobFailed("no_email_column")
    parsed = Parsed(header_map={h: mapping.get(i, normalize_header(h)) for i, h in enumerate(headers)})
    seen: set[str] = set()
    for line_no, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue  # blank line
        parsed.total += 1
        values = lead_values(row, mapping)
        email = normalize_email(values.pop("email"))  # type: ignore[arg-type]
        if email is None:
            parsed.failed += 1
            if len(parsed.errors) < MAX_ERRORS:
                parsed.errors.append({"row": line_no, "error": "missing or invalid email"})
            continue
        raw = {normalize_header(h): (row[i] if i < len(row) else "") for i, h in enumerate(headers) if h.strip()}
        lead = {"email": email, **values, "raw": raw}
        (parsed.repeated if email in seen else parsed.leads).append(lead)
        seen.add(email)
    return parsed


@tenant_job
async def import_leads(ctx: dict[str, Any], tenant_id: UUID, import_id: str) -> dict[str, Any]:
    pool: asyncpg.Pool = ctx["pool"]
    iid = UUID(import_id)
    async with as_worker(pool, tenant_id) as conn:
        imp = await conn.fetchrow(
            "select id, campaign_id, file_path, status from public.lead_imports where id = $1 for update", iid
        )
        if imp is None or imp["status"] not in ("uploaded", "processing"):
            return {"skipped": imp["status"] if imp else "missing"}
        if not imp["file_path"]:
            await _fail(conn, iid, "file_missing")
            return {"failed": "file_missing"}
        await conn.execute(
            "update public.lead_imports set status = 'processing', started_at = now(), error_code = null where id = $1",
            iid,
        )
    structlog.contextvars.bind_contextvars(import_id=import_id)

    try:
        data = await anyio.to_thread.run_sync(Path(imp["file_path"]).read_bytes)
        parsed = await anyio.to_thread.run_sync(parse_csv, data)
    except FileNotFoundError:
        async with as_worker(pool, tenant_id) as conn:
            await _fail(conn, iid, "file_missing")
        return {"failed": "file_missing"}
    except JobFailed as exc:
        async with as_worker(pool, tenant_id) as conn:
            await _fail(conn, iid, exc.error_code)
        raise

    async with as_worker(pool, tenant_id) as conn:
        result, _ = await _write(conn, imp["campaign_id"], iid, parsed)
    log.info("leads.imported", **result)
    return result


async def _fail(conn: asyncpg.Connection, import_id: UUID, error_code: str) -> None:
    log.warning("leads.import_failed", error_code=error_code)
    await conn.execute(
        "update public.lead_imports set status = 'failed', error_code = $2, finished_at = now() where id = $1",
        import_id,
        error_code,
    )


async def _write(
    conn: asyncpg.Connection, campaign_id: UUID, import_id: UUID, parsed: Parsed, source: str = "csv"
) -> tuple[dict[str, Any], dict[str, UUID]]:
    # Anyone already anywhere in the workspace is a duplicate for review, not a new lead.
    emails = [lead["email"] for lead in parsed.leads]
    existing: dict[str, UUID] = {
        r["email"]: r["id"]
        for r in await conn.fetch(
            "select distinct on (email) email, id from public.leads where email = any($1::text[])"
            " order by email, created_at",
            emails,
        )
    }
    new = [lead for lead in parsed.leads if lead["email"] not in existing]
    dup_rows: list[tuple[UUID, UUID | None, str, dict[str, Any], str]] = [
        (import_id, existing[lead["email"]], lead["email"], lead, "exists_in_tenant")
        for lead in parsed.leads
        if lead["email"] in existing
    ]

    created: dict[str, UUID] = {}
    for start in range(0, len(new), CHUNK):
        chunk = new[start : start + CHUNK]
        rows = await conn.fetch(
            f"""
            insert into public.leads
              (campaign_id, source_import_id, source, email, company_size, raw, {", ".join(_TEXT_COLS)})
            select $1, $2, $3, * from unnest(
              $4::text[], $5::int[], $6::jsonb[], {", ".join(f"${i + 7}::text[]" for i in range(len(_TEXT_COLS)))}
            )
            on conflict (tenant_id, campaign_id, email) do nothing
            returning id, email
            """,
            campaign_id,
            import_id,
            source,
            [lead["email"] for lead in chunk],
            [lead.get("company_size") for lead in chunk],
            [lead["raw"] for lead in chunk],
            *([lead.get(col) for lead in chunk] for col in _TEXT_COLS),
        )
        created.update({r["email"]: r["id"] for r in rows})

    for lead in parsed.repeated:
        dup_rows.append(
            (
                import_id,
                created.get(lead["email"]) or existing.get(lead["email"]),
                lead["email"],
                lead,
                "repeated_in_file",
            )
        )
    if dup_rows:
        await conn.executemany(
            "insert into public.lead_duplicates (import_id, existing_lead_id, incoming_email, incoming, reason)"
            " values ($1, $2, $3, $4, $5)",
            dup_rows,
        )

    status = "completed_with_errors" if parsed.failed else "completed"
    await conn.execute(
        """
        update public.lead_imports
           set status = $2, total_rows = $3, imported_rows = $4, failed_rows = $5, duplicate_rows = $6,
               header_map = $7, errors = $8, finished_at = now()
         where id = $1
        """,
        import_id,
        status,
        parsed.total,
        len(created),
        parsed.failed,
        len(dup_rows),
        parsed.header_map,
        parsed.errors,
    )
    summary = {
        "status": status,
        "total": parsed.total,
        "imported": len(created),
        "duplicates": len(dup_rows),
        "failed": parsed.failed,
    }
    return summary, created
