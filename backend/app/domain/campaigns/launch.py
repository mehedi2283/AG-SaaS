"""Launching campaigns in Instantly (email) / HeyReach (LinkedIn), and pausing/resuming them.

launch_campaign is idempotent: it creates the provider campaign once (its id is stored), pushes every 'ready' lead
that isn't pushed yet (re-running it later adds newly written leads), then starts sending. Provider errors are
classified: retryable ones defer the job (arq.Retry), permanent ones mark the launch failed with a short reason.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
import structlog
from arq import Retry

from app.core.crypto import Crypto
from app.db.tenant_tx import as_worker
from app.domain.campaigns.formatting import email_body, linkedin_body
from app.domain.integrations.secrets import provider_config, sender_signature
from app.providers.heyreach import HeyReachClient, lead_body
from app.providers.http import ProviderError
from app.providers.instantly import CampaignSpec, InstantlyClient
from app.workers.context import JobFailed, tenant_job
from app.workers.limits import RateLimiter

log = structlog.get_logger()


async def _fail(pool: asyncpg.Pool, tenant_id: UUID, campaign_id: UUID, reason: str) -> None:
    async with as_worker(pool, tenant_id) as conn:
        await conn.execute(
            "update public.campaigns set launch_status = 'failed', launch_error = $2 where id = $1",
            campaign_id,
            reason[:500],
        )


def _retry(exc: ProviderError) -> Retry:
    return Retry(defer=min(max(exc.retry_after or 30, 5), 600))


@tenant_job
async def launch_campaign(ctx: dict[str, Any], tenant_id: UUID, campaign_id: str) -> dict[str, Any]:
    pool: asyncpg.Pool = ctx["pool"]
    crypto: Crypto = ctx["crypto"]
    limits: RateLimiter = ctx["limits"]
    cid = UUID(campaign_id)
    structlog.contextvars.bind_contextvars(campaign_id=campaign_id)

    async with as_worker(pool, tenant_id) as conn:
        campaign = await conn.fetchrow("select * from public.campaigns where id = $1 for update", cid)
        if campaign is None:
            return {"skipped": "missing"}
        if campaign["launch_status"] not in ("not_launched", "failed", "launching", "launched"):
            return {"skipped": campaign["launch_status"]}
        sender = await conn.fetchrow(
            "select * from public.sender_identities where id = $1", campaign["sender_identity_id"]
        )
        provider = "instantly" if campaign["channel"] == "email" else "heyreach"
        try:
            config = await provider_config(conn, crypto, tenant_id, provider)
        except JobFailed as exc:
            await conn.execute(
                "update public.campaigns set launch_status = 'failed', launch_error = $2 where id = $1",
                cid,
                f"Connect {provider.title()} in Settings first.",
            )
            raise exc
        signature = await sender_signature(conn, crypto, tenant_id, sender["id"]) if sender else None
        steps = [
            r["delay_days"]
            for r in await conn.fetch(
                "select delay_days from public.campaign_steps where campaign_id = $1 order by step", cid
            )
        ]
        leads = await conn.fetch(
            """
            select l.* from public.leads l
             where l.campaign_id = $1 and l.status = 'ready'
               and not exists (select 1 from public.lead_messages m
                                where m.lead_id = l.id and m.status not in ('ok', 'approved'))
             order by l.created_at
            """,
            cid,
        )
        messages: dict[UUID, list[asyncpg.Record]] = {}
        for row in await conn.fetch(
            "select lead_id, step, subject, body from public.lead_messages where campaign_id = $1"
            " and status in ('ok', 'approved') order by step",
            cid,
        ):
            messages.setdefault(row["lead_id"], []).append(row)
        await conn.execute(
            "update public.campaigns set launch_status = 'launching', launch_error = null where id = $1"
            " and launch_status <> 'launched'",
            cid,
        )
    if sender is None:
        await _fail(pool, tenant_id, cid, "Choose who the campaign sends as.")
        raise JobFailed("sender_missing")

    try:
        if provider == "instantly":
            pushed = await _launch_email(
                ctx, pool, limits, tenant_id, campaign, sender, config, signature, steps, leads, messages
            )
        else:
            pushed = await _launch_linkedin(
                ctx, pool, limits, tenant_id, campaign, sender, config, steps, leads, messages
            )
    except ProviderError as exc:
        if exc.retryable:
            log.warning("launch.retry", provider=exc.provider, status=exc.status)
            raise _retry(exc) from exc
        log.warning("launch.failed", provider=exc.provider, status=exc.status, error_code="provider_rejected")
        await _fail(pool, tenant_id, cid, f"{exc.provider.title()} said: {exc.message}")
        raise JobFailed("provider_rejected", exc.message) from exc

    async with as_worker(pool, tenant_id) as conn:
        await conn.execute(
            "update public.campaigns set launch_status = 'launched', status = 'active',"
            " launched_at = coalesce(launched_at, now()), launch_error = null where id = $1",
            cid,
        )
    log.info("launch.done", pushed=pushed)
    return {"pushed": pushed}


async def _mark_pushed(
    pool: asyncpg.Pool, tenant_id: UUID, lead_id: UUID, column: str | None, value: str | None
) -> None:
    async with as_worker(pool, tenant_id) as conn:
        if column:
            await conn.execute(
                f"update public.leads set status = 'pushed', {column} = $2 where id = $1", lead_id, value
            )
        else:
            await conn.execute("update public.leads set status = 'pushed' where id = $1", lead_id)
        await conn.execute(
            "update public.lead_messages set status = 'pushed' where lead_id = $1 and status in ('ok', 'approved')",
            lead_id,
        )


async def _launch_email(
    ctx: dict[str, Any],
    pool: asyncpg.Pool,
    limits: RateLimiter,
    tenant_id: UUID,
    campaign: asyncpg.Record,
    sender: asyncpg.Record,
    config: dict[str, Any],
    signature: str | None,
    steps: list[int],
    leads: list[asyncpg.Record],
    messages: dict[UUID, list[asyncpg.Record]],
) -> int:
    if not sender["instantly_eaccount"]:
        await _fail(
            pool, tenant_id, campaign["id"], "The sender has no Instantly mailbox. Add it in Settings, Senders."
        )
        raise JobFailed("sender_no_mailbox")
    client = InstantlyClient(ctx["http"], config["api_key"])
    instantly_id = campaign["instantly_campaign_id"]
    if not instantly_id:
        spec = CampaignSpec(
            name=campaign["name"],
            sender_email=sender["instantly_eaccount"],
            timezone=campaign["timezone"],
            days=list(campaign["sending_days"]),
            send_from=campaign["send_from"].strftime("%H:%M"),
            send_to=campaign["send_to"].strftime("%H:%M"),
            daily_limit=campaign["daily_limit"],
            delays=steps,
        )
        async with limits.slot(tenant_id, "instantly"):
            instantly_id = await client.create_campaign(spec)
        async with as_worker(pool, tenant_id) as conn:  # stored at once, so a retry never creates a second one
            await conn.execute(
                "update public.campaigns set instantly_campaign_id = $2 where id = $1", campaign["id"], instantly_id
            )

    pushed = 0
    for lead in leads:
        steps_written = messages.get(lead["id"], [])
        if len(steps_written) < len(steps):
            continue
        variables: dict[str, str] = {}
        for message in steps_written:
            variables[f"custom_subject_{message['step']}"] = message["subject"] or ""
            variables[f"personalization_{message['step']}"] = email_body(
                message["body"], signature, sender["signature_image_url"]
            )
        async with limits.slot(tenant_id, "instantly"):
            lead_ref = await client.add_lead(instantly_id, dict(lead), variables)
        await _mark_pushed(pool, tenant_id, lead["id"], "instantly_lead_id", lead_ref or None)
        pushed += 1
    if campaign["launch_status"] != "launched" or campaign["status"] != "active":
        async with limits.slot(tenant_id, "instantly"):
            await client.activate(instantly_id)
    return pushed


async def _launch_linkedin(
    ctx: dict[str, Any],
    pool: asyncpg.Pool,
    limits: RateLimiter,
    tenant_id: UUID,
    campaign: asyncpg.Record,
    sender: asyncpg.Record,
    config: dict[str, Any],
    steps: list[int],
    leads: list[asyncpg.Record],
    messages: dict[UUID, list[asyncpg.Record]],
) -> int:
    account = sender["heyreach_account_id"]
    if not account:
        await _fail(pool, tenant_id, campaign["id"], "The sender has no HeyReach LinkedIn account. Add it in Settings.")
        raise JobFailed("sender_no_linkedin_account")
    client = HeyReachClient(ctx["http"], config["api_key"])
    list_id = campaign["heyreach_list_id"]
    if not list_id:
        async with limits.slot(tenant_id, "heyreach"):
            list_id = await client.create_list(f"Gazi: {campaign['name']}")
        async with as_worker(pool, tenant_id) as conn:
            await conn.execute(
                "update public.campaigns set heyreach_list_id = $2 where id = $1", campaign["id"], list_id
            )

    batch: list[tuple[UUID, dict[str, Any]]] = []
    for lead in leads:
        written = messages.get(lead["id"], [])
        if not lead["linkedin_url"] or len(written) < len(steps):
            continue  # LinkedIn needs a profile URL
        variables = {f"personalization_{m['step']}": linkedin_body(m["body"]) for m in written}
        batch.append((lead["id"], lead_body(dict(lead), variables)))
    if batch:
        async with limits.slot(tenant_id, "heyreach"):
            await client.add_leads(list_id, [body for _, body in batch])
        for lead_id, _ in batch:
            await _mark_pushed(pool, tenant_id, lead_id, None, None)

    heyreach_id = campaign["heyreach_campaign_id"]
    if not heyreach_id:
        async with limits.slot(tenant_id, "heyreach"):
            heyreach_id = await client.create_campaign(
                name=campaign["name"],
                list_id=list_id,
                account_id=account,
                days=list(campaign["sending_days"]),
                send_from=campaign["send_from"].strftime("%H:%M"),
                send_to=campaign["send_to"].strftime("%H:%M"),
                timezone=campaign["timezone"],
                daily_limit=campaign["daily_limit"],
            )
            await client.update_sequence(heyreach_id, steps)
        async with as_worker(pool, tenant_id) as conn:
            await conn.execute(
                "update public.campaigns set heyreach_campaign_id = $2 where id = $1", campaign["id"], heyreach_id
            )
        async with limits.slot(tenant_id, "heyreach"):
            await client.start(heyreach_id)
    return len(batch)


@tenant_job
async def set_campaign_running(ctx: dict[str, Any], tenant_id: UUID, campaign_id: str, running: str) -> dict[str, Any]:
    """Pause ('false') or resume ('true') a launched campaign in its provider, then record it."""
    pool: asyncpg.Pool = ctx["pool"]
    cid, run = UUID(campaign_id), running == "true"
    async with as_worker(pool, tenant_id) as conn:
        campaign = await conn.fetchrow("select * from public.campaigns where id = $1", cid)
        if campaign is None or campaign["launch_status"] != "launched":
            return {"skipped": "not_launched"}
        provider = "instantly" if campaign["channel"] == "email" else "heyreach"
        config = await provider_config(conn, ctx["crypto"], tenant_id, provider)
    try:
        async with ctx["limits"].slot(tenant_id, provider):
            if provider == "instantly":
                client = InstantlyClient(ctx["http"], config["api_key"])
                await (client.activate if run else client.pause)(campaign["instantly_campaign_id"])
            else:
                heyreach = HeyReachClient(ctx["http"], config["api_key"])
                await (heyreach.resume if run else heyreach.pause)(campaign["heyreach_campaign_id"])
    except ProviderError as exc:
        if exc.retryable:
            raise _retry(exc) from exc
        raise JobFailed("provider_rejected", exc.message) from exc
    async with as_worker(pool, tenant_id) as conn:
        await conn.execute("update public.campaigns set status = $2 where id = $1", cid, "active" if run else "paused")
    return {"running": run}
