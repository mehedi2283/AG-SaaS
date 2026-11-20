"""The reply pipeline (replies queue, its own worker, rule 3).

handle_webhook_event -> match the lead, store the reply (dedupe), classify intent
draft_reply          -> draft an answer (guards), then auto-send after a delay or hold for approval
send_reply           -> add links by code (interested/question only, rule 6) + signature, send via the provider

Links are never written by the model: the draft stores the model's text only; the calendar link / attachment and
the signature are added at send time. Below the confidence threshold, with auto-reply off, or for intents that
shouldn't get an automatic answer, the draft waits for a person (rule 6).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
import structlog
from arq import Retry

from app.core.crypto import Crypto
from app.db.tenant_tx import as_worker
from app.domain.bookings.service import apply_booking
from app.domain.campaigns.formatting import email_body
from app.domain.generation.jobs import lead_context
from app.domain.integrations.secrets import provider_config, sender_signature
from app.domain.replies.parse import Inbound, normalize_profile, parse_heyreach, parse_instantly
from app.domain.tenants.service import resolve_ai_settings
from app.llm.client import LlmClient
from app.llm.guards import check_reply
from app.llm.outputs import ReplyClassification, ReplyDraft
from app.llm.prompts import ensure_prompt
from app.providers.heyreach import HeyReachClient
from app.providers.http import ProviderError
from app.providers.instantly import InstantlyClient
from app.queue import QUEUE_REPLIES
from app.workers.context import JobFailed, tenant_job

log = structlog.get_logger()

CONFIDENCE_THRESHOLD = 0.85
AUTO_SEND_DELAY = 15 * 60  # seconds; the n8n flow waited ~15 minutes before answering
DRAFT_INTENTS = {"interested", "question", "referral", "not_interested"}
AUTO_SEND_INTENTS = {"interested", "question", "referral", "not_interested"}
LINK_INTENTS = {"interested", "question"}


@tenant_job
async def handle_webhook_event(ctx: dict[str, Any], tenant_id: UUID, event_id: str) -> dict[str, Any]:
    pool: asyncpg.Pool = ctx["pool"]
    eid = UUID(event_id)
    async with as_worker(pool, tenant_id) as conn:
        event = await conn.fetchrow(
            "select id, provider, payload, status from public.webhook_events where id = $1 for update", eid
        )
        if event is None or event["status"] not in ("received", "queued", "failed"):
            return {"skipped": event["status"] if event else "missing"}
        if event["provider"] == "calendly":
            booking = await apply_booking(conn, eid, event["payload"])
            await conn.execute(
                "update public.webhook_events set status = $2, processed_at = now(), attempts = attempts + 1"
                " where id = $1",
                eid,
                "ignored" if "ignored" in booking else "processed",
            )
            return booking
        inbound = (
            parse_instantly(event["payload"]) if event["provider"] == "instantly" else parse_heyreach(event["payload"])
        )
        result = await _store(conn, eid, inbound)
        await conn.execute(
            "update public.webhook_events set status = $2, processed_at = now(), attempts = attempts + 1 where id = $1",
            eid,
            "processed" if result.get("reply_id") or result.get("lead_status") else "ignored",
        )
    reply_id = result.get("reply_id")
    if reply_id:
        await _classify(ctx, tenant_id, UUID(reply_id))
    return result


async def _store(conn: asyncpg.Connection, event_id: UUID, inbound: Inbound) -> dict[str, Any]:
    column = "instantly_campaign_id" if inbound.channel == "email" else "heyreach_campaign_id"
    campaign_id = await conn.fetchval(f"select id from public.campaigns where {column} = $1", inbound.campaign_ref)
    lead_id = None
    if inbound.channel == "email" and inbound.lead_email:
        lead_id = await conn.fetchval(
            "select id from public.leads where email = $1 order by (campaign_id = $2) desc nulls last limit 1",
            inbound.lead_email,
            campaign_id,
        )
    elif inbound.lead_profile_url:
        wanted = normalize_profile(inbound.lead_profile_url)
        lead_id = await conn.fetchval(
            "select id from public.leads where campaign_id = $1 and linkedin_url is not null"
            " and regexp_replace(lower(split_part(linkedin_url, '?', 1)), '^https?://(www\\.)?|/$', '', 'g') = $2"
            " limit 1",
            campaign_id,
            wanted,
        )
    if inbound.kind in ("bounce", "unsubscribe"):
        status = "bounced" if inbound.kind == "bounce" else "unsubscribed"
        if lead_id:
            await conn.execute("update public.leads set status = $2 where id = $1", lead_id, status)
        return {"lead_status": status if lead_id else None}
    if inbound.kind != "reply" or not inbound.body or not inbound.provider_message_id:
        return {"ignored": inbound.kind}

    reply_id = await conn.fetchval(
        """
        insert into public.replies (lead_id, campaign_id, webhook_event_id, channel, provider_message_id, thread_ref,
                                    inbound_body)
        values ($1, $2, $3, $4, $5, $6, $7)
        on conflict (tenant_id, channel, provider_message_id) do nothing
        returning id
        """,
        lead_id,
        campaign_id,
        event_id,
        inbound.channel,
        inbound.provider_message_id[:500],
        inbound.thread_ref,
        inbound.body[:20000],
    )
    if reply_id is None:
        return {"duplicate": True}
    if lead_id:
        await conn.execute(
            "update public.leads set status = 'replied' where id = $1 and status not in ('unsubscribed', 'bounced',"
            " 'do_not_contact')",
            lead_id,
        )
    if inbound.auto_reply:
        await conn.execute(
            "update public.replies set intent = 'out_of_office', confidence = 1, rationale = 'Marked automatic by the"
            " sender', status = 'classified' where id = $1",
            reply_id,
        )
        return {"reply_id": None, "auto_reply": True}
    log.info("reply.stored", reply_id=str(reply_id), matched=bool(lead_id))
    return {"reply_id": str(reply_id)}


async def _context(conn: asyncpg.Connection, reply_id: UUID) -> asyncpg.Record:
    row = await conn.fetchrow(
        """
        select r.*, l.first_name, l.last_name, l.title, l.company, l.industry, l.city, l.country, l.company_size,
               c.offer_summary, c.offer_details, c.attachment_url, c.sender_identity_id, c.channel as campaign_channel,
               (select m.body from public.lead_messages m where m.lead_id = r.lead_id and m.status = 'pushed'
                 order by m.step desc limit 1) as our_message
          from public.replies r
          left join public.leads l on l.id = r.lead_id
          left join public.campaigns c on c.id = r.campaign_id
         where r.id = $1
        """,
        reply_id,
    )
    assert row is not None
    return row


async def _classify(ctx: dict[str, Any], tenant_id: UUID, reply_id: UUID) -> None:
    pool: asyncpg.Pool = ctx["pool"]
    llm: LlmClient = ctx["llm"]
    async with as_worker(pool, tenant_id) as conn:
        reply = await _context(conn, reply_id)
        ai = resolve_ai_settings(await conn.fetchval("select settings from public.tenants") or {}, ctx["settings"])
        prompt = await ensure_prompt(conn, "reply_classify", ai.classify_model)
    try:
        result = await llm.complete(
            pool,
            tenant_id,
            model=ai.classify_model,
            system=prompt.system,
            user=prompt.render({"our_message": reply["our_message"], "reply": reply["inbound_body"]}),
            output=ReplyClassification,
            temperature=prompt.temperature,
            ref_table="replies",
            ref_id=reply_id,
            job_id=ctx.get("job_id"),
        )
    except JobFailed as exc:
        async with as_worker(pool, tenant_id) as conn:
            await conn.execute("update public.replies set error_code = $2 where id = $1", reply_id, exc.error_code)
        log.warning("reply.classify_failed", error_code=exc.error_code)
        return
    out: ReplyClassification = result.output
    async with as_worker(pool, tenant_id) as conn:
        await conn.execute(
            "update public.replies set intent = $2, confidence = $3, rationale = $4, status = 'classified',"
            " error_code = null where id = $1",
            reply_id,
            out.intent,
            round(out.confidence, 3),
            out.rationale,
        )
        if out.intent in ("unsubscribe", "bounce") and reply["lead_id"]:
            await conn.execute(
                "update public.leads set status = $2 where id = $1",
                reply["lead_id"],
                "unsubscribed" if out.intent == "unsubscribe" else "bounced",
            )
    if out.intent in DRAFT_INTENTS:
        await ctx["queue"].enqueue(
            "draft_reply", tenant_id, str(reply_id), job_id=f"draft:{reply_id}", queue=QUEUE_REPLIES
        )


@tenant_job
async def draft_reply(ctx: dict[str, Any], tenant_id: UUID, reply_id: str) -> dict[str, Any]:
    pool: asyncpg.Pool = ctx["pool"]
    llm: LlmClient = ctx["llm"]
    rid = UUID(reply_id)
    async with as_worker(pool, tenant_id) as conn:
        reply = await _context(conn, rid)
        if reply["status"] not in ("classified", "drafted", "awaiting_approval") or reply["sent_at"]:
            return {"skipped": reply["status"]}
        settings = await conn.fetchval("select settings from public.tenants") or {}
        ai = resolve_ai_settings(settings, ctx["settings"])
        prompt = await ensure_prompt(conn, "reply_draft", ai.writing_model)
        sender_name = await conn.fetchval(
            "select display_name from public.sender_identities where id = $1", reply["sender_identity_id"]
        )
    user = prompt.render(
        {
            "offer_summary": reply["offer_summary"],
            "offer_details": reply["offer_details"],
            "intent": reply["intent"],
            "our_message": reply["our_message"],
            "reply": reply["inbound_body"],
            **lead_context(reply),
        }
    )
    body, problems = None, ["not drafted"]
    for _ in range(3):
        try:
            result = await llm.complete(
                pool,
                tenant_id,
                model=ai.writing_model,
                system=prompt.system,
                user=user,
                output=ReplyDraft,
                temperature=prompt.temperature,
                ref_table="replies",
                ref_id=rid,
                job_id=ctx.get("job_id"),
            )
        except JobFailed as exc:
            if exc.error_code == "llm_bad_output":
                continue
            async with as_worker(pool, tenant_id) as conn:
                await conn.execute(
                    "update public.replies set status = 'awaiting_approval', error_code = $2 where id = $1",
                    rid,
                    exc.error_code,
                )
            return {"failed": exc.error_code}
        problems = check_reply(result.output.body, sender_name)
        if not problems:
            body = result.output.body
            break
        log.info("reply.guard_rejected", problems=problems)

    auto = (
        body is not None
        and bool(settings.get("auto_reply"))
        and reply["intent"] in AUTO_SEND_INTENTS
        and reply["confidence"] is not None
        and float(reply["confidence"]) >= CONFIDENCE_THRESHOLD
    )
    async with as_worker(pool, tenant_id) as conn:
        await conn.execute(
            """
            update public.replies set draft_body = $2, status = $3, prompt_template_id = $4, prompt_version = $5,
                   error_code = $6
             where id = $1
            """,
            rid,
            body,
            "drafted" if auto else "awaiting_approval",
            prompt.id if body else None,
            prompt.version if body else None,
            None if body else "guard_rejected",
        )
    if auto:
        await ctx["queue"].enqueue(
            "send_reply",
            tenant_id,
            reply_id,
            job_id=f"send:{reply_id}",
            queue=QUEUE_REPLIES,
            defer_seconds=AUTO_SEND_DELAY,
        )
    log.info("reply.drafted", auto_send=auto, ok=body is not None)
    return {"drafted": body is not None, "auto_send": auto}


def booking_link(base: str, campaign_id: UUID | None) -> str:
    """The Calendly link with ?utm_campaign=<campaign id>, which is how bookings are matched back (docs/status.md)."""
    if not campaign_id:
        return base
    return f"{base}{'&' if '?' in base else '?'}utm_campaign={campaign_id}"


@tenant_job
async def send_reply(ctx: dict[str, Any], tenant_id: UUID, reply_id: str) -> dict[str, Any]:
    pool: asyncpg.Pool = ctx["pool"]
    crypto: Crypto = ctx["crypto"]
    rid = UUID(reply_id)
    async with as_worker(pool, tenant_id) as conn:
        reply = await _context(conn, rid)
        if reply["status"] != "drafted" or reply["sent_at"]:
            return {"skipped": reply["status"]}
        text = (reply["final_body"] or reply["draft_body"] or "").strip()
        if not text:
            return {"skipped": "empty"}
        links: list[str] = []
        if reply["intent"] in LINK_INTENTS:  # rule 6: links only here, and only added by code
            if reply["intent"] == "interested":
                try:
                    calendly = await provider_config(conn, crypto, tenant_id, "calendly")
                    if calendly.get("booking_url"):
                        links.append(booking_link(str(calendly["booking_url"]), reply["campaign_id"]))
                except JobFailed:
                    pass  # no Calendly connected: send without a booking link
            if reply["attachment_url"]:
                links.append(str(reply["attachment_url"]))
        provider = "instantly" if reply["channel"] == "email" else "heyreach"
        config = await provider_config(conn, crypto, tenant_id, provider)
        sender = await conn.fetchrow(
            "select id, signature_image_url, instantly_eaccount from public.sender_identities where id = $1",
            reply["sender_identity_id"],
        )
        signature = await sender_signature(conn, crypto, tenant_id, sender["id"]) if sender else None
    full_text = text + "".join(f"\n\n{link}" for link in links)
    thread = reply["thread_ref"] or {}
    try:
        async with ctx["limits"].slot(tenant_id, provider):
            if provider == "instantly":
                html_body = email_body(full_text, signature, sender["signature_image_url"] if sender else None)
                subject = thread.get("subject") or ""
                await InstantlyClient(ctx["http"], config["api_key"]).reply(
                    eaccount=thread.get("eaccount") or (sender["instantly_eaccount"] if sender else ""),
                    reply_to_uuid=thread["reply_to_uuid"],
                    subject=subject if subject.lower().startswith("re:") else f"Re: {subject}".strip(),
                    html=html_body,
                    text=full_text + (f"\n\n{signature}" if signature else ""),
                )
            else:
                await HeyReachClient(ctx["http"], config["api_key"]).send_message(
                    account_id=str(thread["account_id"]),
                    conversation_id=str(thread["conversation_id"]),
                    message=full_text,
                )
    except ProviderError as exc:
        if exc.retryable:
            raise Retry(defer=min(max(exc.retry_after or 30, 5), 600)) from exc
        async with as_worker(pool, tenant_id) as conn:
            await conn.execute(
                "update public.replies set status = 'failed', error_code = 'provider_rejected' where id = $1", rid
            )
        raise JobFailed("provider_rejected", exc.message) from exc
    async with as_worker(pool, tenant_id) as conn:
        await conn.execute(
            "update public.replies set status = 'sent', sent_at = now(), final_body = $2 where id = $1", rid, full_text
        )
    log.info("reply.sent", provider=provider, links=len(links))
    return {"sent": True, "links": len(links)}
