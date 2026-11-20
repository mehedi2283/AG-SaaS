"""Campaign settings and their sequence (one row per step, each with its own angle)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from app.core.auth import TenantContext
from app.core.config import Settings
from app.core.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.domain.audit.repo import record
from app.domain.billing.service import ensure_access
from app.domain.campaigns import repo
from app.domain.campaigns.schemas import (
    CampaignCounts,
    CampaignIn,
    CampaignOut,
    CampaignUpdate,
    Channel,
    StepOut,
    StepsIn,
)
from app.queue import QUEUE_SYNC, JobQueue

# The angles the n8n workflows used (docs/status.md), as the starting sequence for a new campaign.
DEFAULT_STEPS: dict[Channel, list[tuple[int, str]]] = {
    "email": [
        (0, "Hook: name a problem this buyer likely has and how the sender solves it. Soft call to action."),
        (3, "Case study: one concrete result for a similar company."),
        (4, "Market trend: a shift in the buyer's market and why it matters now."),
        (5, "Resource offer: offer a useful resource (catalogue, price list, samples) in words, no link."),
        (7, "Breakup: short, polite last note; leave the door open."),
    ],
    "linkedin": [
        (0, "Connection request: short and specific, why connecting makes sense (max 200 characters)."),
        (2, "Thanks for connecting; one relevant observation about their company."),
        (4, "Case study: one concrete result for a similar company."),
        (5, "Resource offer: offer a useful resource in words, no link."),
        (7, "Breakup: short, polite last note."),
    ],
}
FALLBACK_ANGLE = "Follow-up: a new, relevant reason to reply, without repeating earlier messages."
# After launch, Instantly/HeyReach hold the schedule and sequence, so only these may still change here.
EDITABLE_AFTER_LAUNCH = {"name", "report_emails", "attachment_url", "offer_summary", "offer_details"}


def default_steps(channel: Channel, count: int) -> list[tuple[int, int, str]]:
    base = DEFAULT_STEPS[channel]
    steps = []
    for n in range(1, count + 1):
        delay, angle = base[n - 1] if n <= len(base) else (5, FALLBACK_ANGLE)
        steps.append((n, delay if n > 1 else 0, angle))
    # The breakup angle belongs on the last step, whatever the length.
    if 1 < count < len(base):
        steps[-1] = (count, steps[-1][1], base[-1][1])
    return steps


def _out(row: asyncpg.Record) -> CampaignOut:
    data = dict(row)
    counts = CampaignCounts(**{k: data.pop(k) for k in repo.COUNT_KEYS})
    return CampaignOut(**data, counts=counts)


def _plain(values: dict[str, Any]) -> dict[str, Any]:
    if values.get("attachment_url") is not None:
        values["attachment_url"] = str(values["attachment_url"])
    return values


async def list_campaigns(conn: asyncpg.Connection) -> list[CampaignOut]:
    return [_out(r) for r in await repo.list_campaigns(conn)]


async def get_campaign(conn: asyncpg.Connection, campaign_id: UUID) -> CampaignOut:
    row = await repo.get_campaign(conn, campaign_id)
    if row is None:
        raise NotFound("There's no such campaign.", code="campaign_not_found")
    return _out(row)


async def create_campaign(conn: asyncpg.Connection, ctx: TenantContext, body: CampaignIn) -> CampaignOut:
    values = _plain(body.model_dump(exclude={"sequence_count"}))
    if values["sender_identity_id"] is None:
        values["sender_identity_id"] = await conn.fetchval(
            "select id from public.sender_identities where is_default limit 1"
        )
    values["created_by"] = ctx.principal.user_id
    async with conn.transaction():
        campaign_id = await repo.insert_campaign(conn, values)
        await repo.replace_steps(conn, campaign_id, default_steps(body.channel, body.sequence_count))
    await record(
        conn,
        ctx,
        "campaign.created",
        target_table="campaigns",
        target_id=campaign_id,
        diff={"channel": body.channel, "steps": body.sequence_count},
    )
    return await get_campaign(conn, campaign_id)


async def update_campaign(
    conn: asyncpg.Connection, ctx: TenantContext, campaign_id: UUID, body: CampaignUpdate
) -> CampaignOut:
    current = await get_campaign(conn, campaign_id)
    changes = _plain(body.model_dump(exclude_unset=True))
    if current.launch_status != "not_launched" and set(changes) - EDITABLE_AFTER_LAUNCH:
        raise Conflict(
            "This campaign is already launched; its schedule and sender live in Instantly/HeyReach now.",
            code="campaign_launched",
        )
    send_from, send_to = changes.get("send_from", current.send_from), changes.get("send_to", current.send_to)
    if send_from >= send_to:
        raise Unprocessable("The sending window must start before it ends.", code="bad_send_window")
    if changes and not await repo.update_campaign(conn, campaign_id, changes):
        raise Forbidden("You need the member role or higher to change campaigns.", code="insufficient_role")
    await record(
        conn,
        ctx,
        "campaign.updated",
        target_table="campaigns",
        target_id=campaign_id,
        diff={"fields": sorted(changes)},
    )
    return await get_campaign(conn, campaign_id)


async def get_steps(conn: asyncpg.Connection, campaign_id: UUID) -> list[StepOut]:
    await get_campaign(conn, campaign_id)
    return [StepOut(**dict(r)) for r in await repo.steps(conn, campaign_id)]


async def save_steps(conn: asyncpg.Connection, ctx: TenantContext, campaign_id: UUID, body: StepsIn) -> list[StepOut]:
    current = await get_campaign(conn, campaign_id)
    if current.launch_status != "not_launched":
        raise Conflict("The sequence can't change after launch.", code="campaign_launched")
    written = await conn.fetchval(
        "select exists(select 1 from public.lead_messages where campaign_id = $1 and step > $2)",
        campaign_id,
        len(body.steps),
    )
    if written:
        raise Conflict(
            "Messages are already written for steps you're removing. Delete those leads' messages first.",
            code="steps_in_use",
        )
    await repo.replace_steps(conn, campaign_id, [(s.step, s.delay_days, s.angle) for s in body.steps])
    await record(
        conn,
        ctx,
        "campaign.steps_saved",
        target_table="campaigns",
        target_id=campaign_id,
        diff={"steps": len(body.steps)},
    )
    return await get_steps(conn, campaign_id)


async def delete_campaign(conn: asyncpg.Connection, ctx: TenantContext, campaign_id: UUID) -> None:
    current = await get_campaign(conn, campaign_id)
    if current.launch_status in ("launching", "launched"):
        raise Conflict("Pause and archive a launched campaign instead of deleting it.", code="campaign_launched")
    if not await repo.delete_campaign(conn, campaign_id):
        raise Forbidden("You need the admin role to delete campaigns.", code="insufficient_role")
    await record(conn, ctx, "campaign.deleted", target_table="campaigns", target_id=campaign_id)


async def request_launch(
    conn: asyncpg.Connection, ctx: TenantContext, settings: Settings, queue: JobQueue, campaign_id: UUID
) -> CampaignOut:
    """Check everything a launch needs, then hand it to the sync worker (provider calls never run in the API)."""
    current = await get_campaign(conn, campaign_id)
    if current.launch_status == "launching":
        raise Conflict("This campaign is already launching.", code="campaign_launching")
    await ensure_access(conn, settings)
    provider = "instantly" if current.channel == "email" else "heyreach"
    if not await conn.fetchval("select exists(select 1 from public.integrations where provider = $1)", provider):
        raise Unprocessable(f"Connect {provider.title()} in Settings first.", code=f"{provider}_not_connected")
    sender = await conn.fetchrow(
        "select instantly_eaccount, heyreach_account_id from public.sender_identities where id = $1",
        current.sender_identity_id,
    )
    if sender is None:
        raise Unprocessable("Choose who the campaign sends as.", code="sender_missing")
    if current.channel == "email" and not sender["instantly_eaccount"]:
        raise Unprocessable("The sender needs its Instantly mailbox (Settings, Senders).", code="sender_no_mailbox")
    if current.channel == "linkedin" and not sender["heyreach_account_id"]:
        raise Unprocessable(
            "The sender needs its HeyReach LinkedIn account (Settings, Senders).", code="sender_no_linkedin_account"
        )
    if current.counts.ready == 0:
        raise Conflict("No buyers are ready yet. Write their messages first.", code="nothing_ready")
    stamp = await conn.fetchval("select extract(epoch from now())::bigint")
    await queue.enqueue(
        "launch_campaign", ctx.tenant_id, str(campaign_id), job_id=f"launch:{campaign_id}:{stamp}", queue=QUEUE_SYNC
    )
    await record(
        conn,
        ctx,
        "campaign.launch_requested",
        target_table="campaigns",
        target_id=campaign_id,
        diff={"ready": current.counts.ready},
    )
    return current


async def request_running(
    conn: asyncpg.Connection, ctx: TenantContext, queue: JobQueue, campaign_id: UUID, *, running: bool
) -> None:
    current = await get_campaign(conn, campaign_id)
    if current.launch_status != "launched":
        raise Conflict("Only a launched campaign can be paused or resumed.", code="campaign_not_launched")
    stamp = await conn.fetchval("select extract(epoch from now())::bigint")
    await queue.enqueue(
        "set_campaign_running",
        ctx.tenant_id,
        str(campaign_id),
        "true" if running else "false",
        job_id=f"run:{campaign_id}:{stamp}",
        queue=QUEUE_SYNC,
    )
    await record(
        conn, ctx, "campaign.resumed" if running else "campaign.paused", target_table="campaigns", target_id=campaign_id
    )
