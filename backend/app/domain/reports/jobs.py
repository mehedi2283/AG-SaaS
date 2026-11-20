"""Weekly campaign reports (v1 scope): Saturdays 09:00 UTC, the previous seven days, per launched campaign with
report recipients. Numbers come from Gazi's own data (plus Instantly's analytics when available); a short, honest
summary is written by the model (weekly_report prompt; no spin, numbers only from the data) and emailed.

One row per campaign and period (unique), so the job is idempotent: a sent report is never sent twice.
"""

from __future__ import annotations

import html
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg
import structlog
from arq import Retry

from app.db.tenant_tx import as_worker
from app.domain.alerts import mailer
from app.domain.integrations.secrets import provider_config
from app.domain.tenants.service import resolve_ai_settings
from app.llm.outputs import WeeklySummary
from app.llm.prompts import ensure_prompt
from app.providers.http import ProviderError
from app.providers.instantly import InstantlyClient
from app.queue import QUEUE_SYNC, JobQueue
from app.workers.context import JobFailed, tenant_job

log = structlog.get_logger()

_INTENTS = ("interested", "question", "referral", "not_interested", "out_of_office", "unsubscribe", "bounce", "other")


def report_period(today: date) -> tuple[date, date]:
    """The seven days before today (Saturday -> the previous Saturday to Friday)."""
    end = today - timedelta(days=1)
    return end - timedelta(days=6), end


async def weekly_reports(ctx: dict[str, Any]) -> int:
    """Cron: queue one report job per launched campaign that has recipients (ids found via a definer function)."""
    queue: JobQueue = ctx["queue"]
    start, _ = report_period(datetime.now(UTC).date())
    async with as_worker(ctx["pool"], None) as conn:
        targets = await conn.fetch("select tenant_id, campaign_id from app.report_targets()")
    for row in targets:
        await queue.enqueue(
            "weekly_report",
            row["tenant_id"],
            str(row["campaign_id"]),
            start.isoformat(),
            job_id=f"report:{row['campaign_id']}:{start.isoformat()}",
            queue=QUEUE_SYNC,
        )
    log.info("reports.queued", count=len(targets))
    return len(targets)


async def _metrics(conn: asyncpg.Connection, campaign_id: UUID, start: date, end: date) -> dict[str, Any]:
    window = (
        datetime.combine(start, datetime.min.time(), UTC),
        datetime.combine(end + timedelta(days=1), datetime.min.time(), UTC),
    )
    replies = {
        r["intent"] or "unclassified": r["n"]
        for r in await conn.fetch(
            "select intent, count(*)::int as n from public.replies where campaign_id = $1 and received_at >= $2"
            " and received_at < $3 group by 1",
            campaign_id,
            *window,
        )
    }
    return {
        "buyers_in_sequence": await conn.fetchval(
            "select count(*)::int from public.leads where campaign_id = $1 and status in ('pushed', 'replied')",
            campaign_id,
        ),
        "buyers_added_this_week": await conn.fetchval(
            "select count(*)::int from public.leads where campaign_id = $1 and created_at >= $2 and created_at < $3",
            campaign_id,
            *window,
        ),
        "replies_this_week": sum(replies.values()),
        "replies_by_intent": {k: replies.get(k, 0) for k in _INTENTS if replies.get(k)},
        "replies_sent_this_week": await conn.fetchval(
            "select count(*)::int from public.replies where campaign_id = $1 and sent_at >= $2 and sent_at < $3",
            campaign_id,
            *window,
        ),
        "calls_booked_this_week": await conn.fetchval(
            "select count(*)::int from public.bookings where campaign_id = $1 and created_at >= $2 and created_at < $3"
            " and canceled_at is null",
            campaign_id,
            *window,
        ),
    }


def render(campaign: str, start: date, end: date, metrics: dict[str, Any], summary: str | None) -> tuple[str, str]:
    lines = [
        f"Buyers in the sequence: {metrics['buyers_in_sequence']}",
        f"Buyers added this week: {metrics['buyers_added_this_week']}",
        f"Replies this week: {metrics['replies_this_week']}",
        *(f"  {k.replace('_', ' ')}: {v}" for k, v in metrics["replies_by_intent"].items()),
        f"Replies you sent: {metrics['replies_sent_this_week']}",
        f"Calls booked: {metrics['calls_booked_this_week']}",
    ]
    if "emails_sent" in metrics:
        lines.insert(1, f"Emails sent (Instantly): {metrics['emails_sent']}")
    period = f"{start:%d %b} to {end:%d %b %Y}"
    text = f"{campaign}, {period}\n\n" + (f"{summary}\n\n" if summary else "") + "\n".join(lines)
    rows = "".join(f"<tr><td style='padding:4px 16px 4px 0'>{html.escape(line.strip())}</td></tr>" for line in lines)
    body = (
        f"<div style='font-family:Arial,sans-serif;color:#1e1b26;max-width:560px'>"
        f"<p style='color:#6b6877;margin:0'>Weekly report, {html.escape(period)}</p>"
        f"<h2 style='margin:4px 0 16px'>{html.escape(campaign)}</h2>"
        + (f"<p style='line-height:1.6'>{html.escape(summary)}</p>" if summary else "")
        + f"<table style='border-collapse:collapse;font-size:14px'>{rows}</table>"
        "<p style='color:#9a97a5;font-size:12px;margin-top:24px'>Sent by Gazi.</p></div>"
    )
    return body, text


@tenant_job
async def weekly_report(ctx: dict[str, Any], tenant_id: UUID, campaign_id: str, period_start: str) -> dict[str, Any]:
    pool: asyncpg.Pool = ctx["pool"]
    cid, start = UUID(campaign_id), date.fromisoformat(period_start)
    end = start + timedelta(days=6)
    async with as_worker(pool, tenant_id) as conn:
        campaign = await conn.fetchrow("select * from public.campaigns where id = $1", cid)
        if campaign is None:
            return {"skipped": "missing"}
        await conn.execute(
            "insert into public.campaign_reports (campaign_id, period_start, period_end) values ($1, $2, $3)"
            " on conflict (tenant_id, campaign_id, period_start) do nothing",
            cid,
            start,
            end,
        )
        report = await conn.fetchrow(
            "select id, status from public.campaign_reports where campaign_id = $1 and period_start = $2 for update",
            cid,
            start,
        )
        assert report is not None
        if report["status"] in ("sent", "skipped"):
            return {"skipped": report["status"]}
        metrics = await _metrics(conn, cid, start, end)
        replies = await conn.fetch(
            "select intent, left(inbound_body, 300) as text from public.replies where campaign_id = $1"
            " and received_at >= $2 order by received_at desc limit 8",
            cid,
            datetime.combine(start, datetime.min.time(), UTC),
        )
        ai = resolve_ai_settings(await conn.fetchval("select settings from public.tenants") or {}, ctx["settings"])
        prompt = await ensure_prompt(conn, "weekly_report", ai.writing_model)
        instantly_key = None
        if campaign["channel"] == "email" and campaign["instantly_campaign_id"]:
            try:
                instantly_key = (await provider_config(conn, ctx["crypto"], tenant_id, "instantly"))["api_key"]
            except JobFailed:
                instantly_key = None
    if instantly_key:
        try:
            async with ctx["limits"].slot(tenant_id, "instantly"):
                analytics = await InstantlyClient(ctx["http"], instantly_key).campaign_analytics(
                    campaign["instantly_campaign_id"], start.isoformat(), end.isoformat()
                )
            for key in ("emails_sent_count", "emails_sent"):
                if isinstance(analytics.get(key), int):
                    metrics["emails_sent"] = analytics[key]
                    break
        except ProviderError as exc:
            log.info("report.no_instantly_analytics", provider_status=exc.status)

    summary = None
    try:
        result = await ctx["llm"].complete(
            pool,
            tenant_id,
            model=ai.writing_model,
            system=prompt.system,
            user=prompt.render(
                {
                    "campaign_name": campaign["name"],
                    "channel": campaign["channel"],
                    "period": f"{start:%d %b} to {end:%d %b %Y}",
                    "metrics": metrics,
                    "replies": "\n".join(f"- ({r['intent'] or 'unclassified'}) {r['text']}" for r in replies) or "none",
                }
            ),
            output=WeeklySummary,
            temperature=prompt.temperature,
            ref_table="campaign_reports",
            ref_id=report["id"],
            job_id=ctx.get("job_id"),
        )
        summary = result.output.summary
    except JobFailed as exc:
        log.info("report.no_summary", error_code=exc.error_code)  # the numbers still go out

    body, text = render(campaign["name"], start, end, metrics, summary)
    send = mailer(ctx)
    status: str = "skipped"
    error_code: str | None = "email_not_configured"
    if send is not None:
        try:
            await send.send(list(campaign["report_emails"]), f"Gazi weekly report: {campaign['name']}", body, text)
            status, error_code = "sent", None
        except ProviderError as exc:
            if exc.retryable:
                raise Retry(defer=300) from exc
            status, error_code = "failed", "email_rejected"
    async with as_worker(pool, tenant_id) as conn:
        await conn.execute(
            """
            update public.campaign_reports
               set metrics = $2, summary = $3, sent_to = $4, status = $5, error_code = $6,
                   prompt_template_id = $7, prompt_version = $8, sent_at = case when $5 = 'sent' then now() end
             where id = $1
            """,
            report["id"],
            metrics,
            summary,
            list(campaign["report_emails"]),
            status,
            error_code,
            prompt.id if summary else None,
            prompt.version if summary else None,
        )
    log.info("report.done", status=status)
    return {"status": status}
