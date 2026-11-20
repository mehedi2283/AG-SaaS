"""Starting generation, reviewing and editing written messages (API side)."""

from __future__ import annotations

from uuid import UUID

import asyncpg

from app.core.auth import TenantContext
from app.core.config import Settings
from app.core.errors import Conflict, NotFound, Unprocessable
from app.domain.audit.repo import record
from app.domain.billing.service import ensure_access, ensure_token_budget
from app.domain.generation.schemas import GenerateOut, MessageEdit, MessageOut, MessagePage
from app.llm.guards import check_step
from app.queue import QUEUE_LLM, JobQueue

_MSG_COLS = """
  m.id, m.lead_id, l.email as lead_email, nullif(btrim(concat_ws(' ', l.first_name, l.last_name)), '') as lead_name,
  l.company as lead_company, m.step, m.channel, m.subject, m.body, m.status, m.validation_errors, m.error_code,
  m.model, m.prompt_version, m.updated_at
"""


async def start(
    conn: asyncpg.Connection, ctx: TenantContext, settings: Settings, queue: JobQueue, campaign_id: UUID
) -> GenerateOut:
    campaign = await conn.fetchrow(
        "select offer_summary, sender_identity_id from public.campaigns where id = $1", campaign_id
    )
    if campaign is None:
        raise NotFound("There's no such campaign.", code="campaign_not_found")
    if not (campaign["offer_summary"] or "").strip():
        raise Unprocessable("Describe what you sell in the campaign settings first.", code="offer_missing")
    if campaign["sender_identity_id"] is None:
        raise Unprocessable("Choose who the campaign sends as first.", code="sender_missing")
    await ensure_access(conn, settings)
    await ensure_token_budget(conn, settings, worker=False)
    count: int = await conn.fetchval(
        "select count(*) from public.leads where campaign_id = $1 and status = 'new'", campaign_id
    )
    if count == 0:
        raise Conflict("There are no new buyers to write for.", code="nothing_to_generate")
    # A new job id each time someone asks, so a second batch (new imports) isn't swallowed by the first.
    await queue.enqueue(
        "generate_campaign",
        ctx.tenant_id,
        str(campaign_id),
        job_id=f"gen:{campaign_id}:{count}:{await conn.fetchval('select extract(epoch from now())::bigint')}",
        queue=QUEUE_LLM,
    )
    await record(
        conn, ctx, "campaign.generation_started", target_table="campaigns", target_id=campaign_id, diff={"leads": count}
    )
    return GenerateOut(queued_leads=count)


async def list_messages(
    conn: asyncpg.Connection, campaign_id: UUID, *, lead_id: UUID | None, status: str | None, limit: int, offset: int
) -> MessagePage:
    where = "m.campaign_id = $1 and ($2::uuid is null or m.lead_id = $2) and ($3::text is null or m.status = $3)"
    total: int = await conn.fetchval(
        f"select count(*) from public.lead_messages m where {where}", campaign_id, lead_id, status
    )
    rows = await conn.fetch(
        f"select {_MSG_COLS} from public.lead_messages m join public.leads l on l.id = m.lead_id where {where}"
        " order by (m.status = 'failed') desc, l.email, m.step limit $4 offset $5",
        campaign_id,
        lead_id,
        status,
        limit,
        offset,
    )
    return MessagePage(items=[MessageOut(**dict(r)) for r in rows], total=total)


async def edit_message(conn: asyncpg.Connection, ctx: TenantContext, message_id: UUID, body: MessageEdit) -> MessageOut:
    row = await conn.fetchrow(
        """
        select m.step, m.channel, m.status, s.display_name
          from public.lead_messages m
          join public.campaigns c on c.id = m.campaign_id
          left join public.sender_identities s on s.id = c.sender_identity_id
         where m.id = $1
        """,
        message_id,
    )
    if row is None:
        raise NotFound("There's no such message.", code="message_not_found")
    if row["status"] == "pushed":
        raise Conflict("This message is already in Instantly/HeyReach.", code="message_pushed")
    subject = body.subject if row["channel"] == "email" else None
    problems = check_step(row["channel"], row["step"], subject, body.body, row["display_name"])
    if problems:
        raise Unprocessable(
            "This message breaks the sending rules: " + "; ".join(problems) + ".",
            code="message_rejected",
            extra={"problems": problems},
        )
    await conn.execute(
        "update public.lead_messages set subject = $2, body = $3, status = 'approved', approved_by = $4,"
        " approved_at = now() where id = $1",
        message_id,
        subject,
        body.body,
        ctx.principal.user_id,
    )
    await record(conn, ctx, "message.edited", target_table="lead_messages", target_id=message_id)
    out = await conn.fetchrow(
        f"select {_MSG_COLS} from public.lead_messages m join public.leads l on l.id = m.lead_id where m.id = $1",
        message_id,
    )
    assert out is not None
    return MessageOut(**dict(out))


async def regenerate_lead(
    conn: asyncpg.Connection, ctx: TenantContext, settings: Settings, queue: JobQueue, lead_id: UUID
) -> None:
    """Write a lead's failed (or all unpushed) messages again."""
    lead = await conn.fetchrow("select status from public.leads where id = $1", lead_id)
    if lead is None:
        raise NotFound("There's no such lead.", code="lead_not_found")
    if lead["status"] not in ("new", "ready"):
        raise Conflict("This lead can't be rewritten now.", code="lead_busy")
    await ensure_access(conn, settings)
    await ensure_token_budget(conn, settings, worker=False)
    await conn.execute(
        "update public.lead_messages set status = 'pending', attempt = 0, error_code = null,"
        " validation_errors = '[]' where lead_id = $1 and status in ('failed', 'ok')",
        lead_id,
    )
    await conn.execute("update public.leads set status = 'new' where id = $1", lead_id)
    campaign_id = await conn.fetchval("select campaign_id from public.leads where id = $1", lead_id)
    await queue.enqueue(
        "generate_campaign",
        ctx.tenant_id,
        str(campaign_id),
        job_id=f"gen:{campaign_id}:lead:{lead_id}:{await conn.fetchval('select extract(epoch from now())::bigint')}",
        queue=QUEUE_LLM,
    )
    await record(conn, ctx, "lead.regenerate", target_table="leads", target_id=lead_id)
