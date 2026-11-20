"""Sequence generation jobs: one LLM call per lead and step (rule 4), guards in code (rule 5), regenerate on a
violation, store prompt_template_id + prompt_version on every message."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import asyncpg
import structlog

from app.db.tenant_tx import as_worker
from app.domain.leads.normalize import clean_text
from app.domain.tenants.service import resolve_ai_settings
from app.llm.client import LlmClient
from app.llm.guards import check_step
from app.llm.outputs import EmailStep, LinkedInStep
from app.llm.prompts import ensure_prompt
from app.queue import QUEUE_LLM, JobQueue
from app.workers.context import JobFailed, tenant_job

log = structlog.get_logger()

MAX_ATTEMPTS = 3
_DONE = ("ok", "approved", "pushed")


# Extra Apollo columns kept in leads.raw (CSV headers after normalize_header; apollo._lead uses the same keys).
_DETAILS = (
    ("seniority", "Seniority"),
    ("departments", "Department"),
    ("keywords", "Company focus"),
    ("technologies", "Uses"),
    ("annual_revenue", "Annual revenue"),
    ("total_funding", "Total funding"),
    ("latest_funding", "Latest funding round"),
    ("latest_funding_amount", "Latest round amount"),
    ("last_raised_at", "Last raised"),
)


def lead_details(raw: Mapping[str, Any] | None) -> str | None:
    """The useful extra columns as one line, each value capped so a huge cell can't flood the prompt."""
    raw = raw or {}
    values = ((label, clean_text(str(raw[key]), 200)) for key, label in _DETAILS if raw.get(key) is not None)
    return "; ".join(f"{label}: {text}" for label, text in values if text) or None


def lead_context(lead: Mapping[str, Any] | asyncpg.Record) -> dict[str, Any]:
    name = " ".join(p for p in (lead["first_name"], lead["last_name"]) if p)
    location = ", ".join(p for p in (lead["city"], lead["country"]) if p)
    return {
        "lead_name": name or None,
        "lead_title": lead["title"],
        "lead_company": lead["company"],
        "lead_industry": lead["industry"],
        "lead_location": location or None,
        "lead_size": f"{lead['company_size']} employees" if lead["company_size"] else None,
        "lead_details": lead_details(lead.get("raw")),
    }


@tenant_job
async def generate_campaign(ctx: dict[str, Any], tenant_id: UUID, campaign_id: str) -> dict[str, Any]:
    """Queue every 'new' lead of the campaign for writing. Leads with unresolved duplicates aren't leads yet."""
    pool: asyncpg.Pool = ctx["pool"]
    queue: JobQueue = ctx["queue"]
    cid = UUID(campaign_id)
    async with as_worker(pool, tenant_id) as conn:
        campaign = await conn.fetchrow("select channel from public.campaigns where id = $1", cid)
        if campaign is None:
            return {"skipped": "campaign_missing"}
        settings = resolve_ai_settings(
            await conn.fetchval("select settings from public.tenants") or {}, ctx["settings"]
        )
        kind = "email_step" if campaign["channel"] == "email" else "linkedin_step"
        prompt = await ensure_prompt(conn, kind, settings.writing_model)
        steps = [
            r["step"]
            for r in await conn.fetch(
                "select step from public.campaign_steps where campaign_id = $1 order by step", cid
            )
        ]
        leads = [
            r["id"]
            for r in await conn.fetch(
                "update public.leads set status = 'generating' where campaign_id = $1 and status = 'new' returning id",
                cid,
            )
        ]
        if leads and steps:
            await conn.execute(
                """
                insert into public.lead_messages (lead_id, campaign_id, step, channel, prompt_template_id,
                                                  prompt_version, model)
                select l, $1, s, $2, $3, $4, $5 from unnest($6::uuid[]) l cross join unnest($7::int[]) s
                on conflict (tenant_id, lead_id, step) do nothing
                """,
                cid,
                campaign["channel"],
                prompt.id,
                prompt.version,
                settings.writing_model,
                leads,
                steps,
            )
    for lead_id in leads:
        await queue.enqueue(
            "generate_lead_sequence", tenant_id, str(lead_id), job_id=f"gen-lead:{lead_id}", queue=QUEUE_LLM
        )
    log.info("generation.queued", campaign_id=campaign_id, leads=len(leads))
    return {"queued": len(leads)}


@tenant_job
async def generate_lead_sequence(ctx: dict[str, Any], tenant_id: UUID, lead_id: str) -> dict[str, Any]:
    pool: asyncpg.Pool = ctx["pool"]
    llm: LlmClient = ctx["llm"]
    lid = UUID(lead_id)
    structlog.contextvars.bind_contextvars(lead_id=lead_id)
    async with as_worker(pool, tenant_id) as conn:
        lead = await conn.fetchrow("select * from public.leads where id = $1", lid)
        if lead is None or lead["status"] != "generating":
            return {"skipped": lead["status"] if lead else "missing"}
        campaign = await conn.fetchrow(
            """
            select c.id, c.channel, c.offer_summary, c.offer_details, s.display_name, s.title, s.company
              from public.campaigns c left join public.sender_identities s on s.id = c.sender_identity_id
             where c.id = $1
            """,
            lead["campaign_id"],
        )
        assert campaign is not None
        ai = resolve_ai_settings(await conn.fetchval("select settings from public.tenants") or {}, ctx["settings"])
        kind = "email_step" if campaign["channel"] == "email" else "linkedin_step"
        prompt = await ensure_prompt(conn, kind, ai.writing_model)
        steps = await conn.fetch(
            "select step, angle from public.campaign_steps where campaign_id = $1 order by step", campaign["id"]
        )
        messages = {
            r["step"]: r
            for r in await conn.fetch(
                "select id, step, subject, body, status, attempt from public.lead_messages where lead_id = $1", lid
            )
        }

    output = EmailStep if campaign["channel"] == "email" else LinkedInStep
    base = {
        "offer_summary": campaign["offer_summary"],
        "offer_details": campaign["offer_details"],
        "sender_name": campaign["display_name"],
        "sender_title": campaign["title"],
        "sender_company": campaign["company"],
        "steps": len(steps),
        **lead_context(lead),
    }
    previous: list[str] = []
    written = failed = 0
    for step in steps:
        message = messages.get(step["step"])
        if message is None:
            continue
        if message["status"] in _DONE:
            previous.append(f"Step {step['step']}: {message['subject'] or ''}\n{message['body']}".strip())
            continue
        attempt, problems, result = message["attempt"], [], None
        while attempt < MAX_ATTEMPTS:
            attempt += 1
            user = prompt.render(
                {
                    **base,
                    "step": step["step"],
                    "angle": step["angle"],
                    "previous": "\n\n".join(previous) or "none (this is the first message)",
                }
            )
            try:
                result = await llm.complete(
                    pool,
                    tenant_id,
                    model=ai.writing_model,
                    system=prompt.system,
                    user=user,
                    output=output,
                    temperature=prompt.temperature,
                    ref_table="lead_messages",
                    ref_id=message["id"],
                    job_id=ctx.get("job_id"),
                )
            except JobFailed as exc:
                if exc.error_code in ("llm_bad_output",):
                    problems = ["the model's answer wasn't valid"]
                    continue
                async with as_worker(pool, tenant_id) as conn:  # quota, billing, config: stop this lead for now
                    await conn.execute(
                        "update public.lead_messages set status = 'failed', error_code = $2, attempt = $3"
                        " where id = $1",
                        message["id"],
                        exc.error_code,
                        attempt - 1,
                    )
                    await conn.execute("update public.leads set status = 'new' where id = $1", lid)
                raise
            subject = getattr(result.output, "subject", None)
            problems = check_step(
                campaign["channel"], step["step"], subject, result.output.body, campaign["display_name"]
            )
            if not problems:
                break
            log.info("generation.guard_rejected", step=step["step"], problems=problems, attempt=attempt)
        ok = result is not None and not problems
        async with as_worker(pool, tenant_id) as conn:
            await conn.execute(
                """
                update public.lead_messages
                   set subject = $2, body = $3, status = $4, validation_errors = $5, attempt = $6,
                       prompt_template_id = $7, prompt_version = $8, model = $9, error_code = $10
                 where id = $1
                """,
                message["id"],
                getattr(result.output, "subject", None) if ok and result else None,
                result.output.body if ok and result else None,
                "ok" if ok else "failed",
                problems,
                attempt,
                prompt.id,
                prompt.version,
                result.model if result else ai.writing_model,
                None if ok else "guard_rejected",
            )
        if ok and result:
            written += 1
            previous.append(
                f"Step {step['step']}: {getattr(result.output, 'subject', '') or ''}\n{result.output.body}".strip()
            )
        else:
            failed += 1
            break  # later steps build on this one; stop and let a person look

    async with as_worker(pool, tenant_id) as conn:
        remaining = await conn.fetchval(
            "select count(*) from public.lead_messages"
            " where lead_id = $1 and status not in ('ok', 'approved', 'pushed')",
            lid,
        )
        await conn.execute(
            "update public.leads set status = $2 where id = $1 and status = 'generating'",
            lid,
            "ready" if remaining == 0 else "new",
        )
    log.info("generation.lead_done", written=written, failed=failed)
    return {"written": written, "failed": failed}
