"""The reply desk (API side): inbox, edit a draft, approve + send, dismiss, redraft."""

from __future__ import annotations

from uuid import UUID

import asyncpg

from app.core.auth import TenantContext
from app.core.errors import Conflict, NotFound, Unprocessable
from app.domain.audit.repo import record
from app.domain.replies.jobs import LINK_INTENTS
from app.domain.replies.schemas import ReplyEdit, ReplyOut, ReplyPage
from app.llm.guards import check_reply
from app.queue import QUEUE_REPLIES, JobQueue

_COLS = """
  r.id, r.channel, r.status, r.intent, r.confidence::float as confidence, r.rationale, r.inbound_body, r.draft_body,
  r.final_body, r.error_code, r.received_at, r.sent_at, r.lead_id,
  nullif(btrim(concat_ws(' ', l.first_name, l.last_name)), '') as lead_name, l.email as lead_email,
  l.company as lead_company, r.campaign_id, c.name as campaign_name,
  (select m.body from public.lead_messages m where m.lead_id = r.lead_id and m.status = 'pushed'
    order by m.step desc limit 1) as our_message,
  coalesce(r.intent in ('interested', 'question'), false) as will_add_link
"""
_FROM = (
    "public.replies r left join public.leads l on l.id = r.lead_id left join public.campaigns c on c.id = r.campaign_id"
)


async def list_replies(
    conn: asyncpg.Connection, *, view: str, intent: str | None, campaign_id: UUID | None, limit: int, offset: int
) -> ReplyPage:
    statuses = {
        "waiting": ("awaiting_approval", "classified", "received", "failed"),
        "scheduled": ("drafted",),
        "sent": ("sent",),
        "all": ("received", "classified", "drafted", "awaiting_approval", "sent", "failed", "ignored"),
    }[view]
    where = (
        "r.status = any($1::text[]) and ($2::text is null or r.intent = $2)"
        " and ($3::uuid is null or r.campaign_id = $3)"
    )
    total: int = await conn.fetchval(f"select count(*) from {_FROM} where {where}", list(statuses), intent, campaign_id)
    rows = await conn.fetch(
        f"select {_COLS} from {_FROM} where {where} order by r.received_at desc limit $4 offset $5",
        list(statuses),
        intent,
        campaign_id,
        limit,
        offset,
    )
    waiting: int = await conn.fetchval("select count(*) from public.replies where status = 'awaiting_approval'")
    return ReplyPage(items=[ReplyOut(**dict(r)) for r in rows], total=total, waiting=waiting)


async def get_reply(conn: asyncpg.Connection, reply_id: UUID) -> ReplyOut:
    row = await conn.fetchrow(f"select {_COLS} from {_FROM} where r.id = $1", reply_id)
    if row is None:
        raise NotFound("There's no such reply.", code="reply_not_found")
    return ReplyOut(**dict(row))


async def edit_reply(conn: asyncpg.Connection, ctx: TenantContext, reply_id: UUID, body: ReplyEdit) -> ReplyOut:
    current = await get_reply(conn, reply_id)
    if current.status == "sent":
        raise Conflict("This reply was already sent.", code="reply_sent")
    changes = body.model_dump(exclude_unset=True)
    if "draft_body" in changes:
        sender = await conn.fetchval(
            "select s.display_name from public.campaigns c"
            " join public.sender_identities s on s.id = c.sender_identity_id where c.id = $1",
            current.campaign_id,
        )
        problems = check_reply(changes["draft_body"], sender)
        if problems:
            raise Unprocessable(
                "This reply breaks the sending rules: " + "; ".join(problems) + ".",
                code="reply_rejected",
                extra={"problems": problems},
            )
        # If a person writes the whole reply (the AI draft failed), it is recorded against the workspace's active
        # draft prompt, because a stored draft always carries a prompt version (rule 4).
        await conn.execute(
            """
            update public.replies r
               set draft_body = $2,
                   status = case when r.status = 'drafted' then 'awaiting_approval' else r.status end,
                   prompt_template_id = coalesce(r.prompt_template_id, p.id),
                   prompt_version = coalesce(r.prompt_version, p.version)
              from (select null::int as one) d
              left join public.prompt_templates p on p.kind = 'reply_draft' and p.is_active
             where r.id = $1
            """,
            reply_id,
            changes["draft_body"],
        )
    if "intent" in changes:
        await conn.execute("update public.replies set intent = $2 where id = $1", reply_id, changes["intent"])
    await record(
        conn, ctx, "reply.edited", target_table="replies", target_id=reply_id, diff={"fields": sorted(changes)}
    )
    return await get_reply(conn, reply_id)


async def approve_and_send(conn: asyncpg.Connection, ctx: TenantContext, queue: JobQueue, reply_id: UUID) -> ReplyOut:
    current = await get_reply(conn, reply_id)
    if current.status == "sent":
        raise Conflict("This reply was already sent.", code="reply_sent")
    if not (current.draft_body or "").strip():
        raise Unprocessable("Write the reply first.", code="reply_empty")
    if current.channel == "email" and current.lead_id is None:
        raise Unprocessable(
            "Gazi couldn't match this reply to a buyer, so it can't answer it here.", code="reply_unmatched"
        )
    await conn.execute(
        "update public.replies set status = 'drafted', approved_by = $2, approved_at = now() where id = $1",
        reply_id,
        ctx.principal.user_id,
    )
    stamp = await conn.fetchval("select extract(epoch from now())::bigint")
    await queue.enqueue(
        "send_reply", ctx.tenant_id, str(reply_id), job_id=f"send:{reply_id}:{stamp}", queue=QUEUE_REPLIES
    )
    await record(
        conn,
        ctx,
        "reply.approved",
        target_table="replies",
        target_id=reply_id,
        diff={"intent": current.intent, "link": current.intent in LINK_INTENTS},
    )
    return await get_reply(conn, reply_id)


async def dismiss(conn: asyncpg.Connection, ctx: TenantContext, reply_id: UUID) -> ReplyOut:
    current = await get_reply(conn, reply_id)
    if current.status == "sent":
        raise Conflict("This reply was already sent.", code="reply_sent")
    await conn.execute("update public.replies set status = 'ignored' where id = $1", reply_id)
    await record(conn, ctx, "reply.dismissed", target_table="replies", target_id=reply_id)
    return await get_reply(conn, reply_id)


async def redraft(conn: asyncpg.Connection, ctx: TenantContext, queue: JobQueue, reply_id: UUID) -> None:
    current = await get_reply(conn, reply_id)
    if current.status == "sent" or current.intent is None:
        raise Conflict("This reply can't be drafted again.", code="reply_not_draftable")
    await conn.execute("update public.replies set status = 'classified' where id = $1", reply_id)
    stamp = await conn.fetchval("select extract(epoch from now())::bigint")
    await queue.enqueue(
        "draft_reply", ctx.tenant_id, str(reply_id), job_id=f"draft:{reply_id}:{stamp}", queue=QUEUE_REPLIES
    )
