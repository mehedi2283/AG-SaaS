"""Apollo search + buyer scoring (v1 scope).

The API stores a search as a lead_imports row (source 'apollo', parameters in header_map) and queues apollo_search.
The job searches Apollo, reveals work emails (credits), scores each person as a buyer with the workspace's sorting
model (lead_score prompt, usage logged), and writes them like a CSV import: duplicates go to review.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
import structlog
from arq import Retry
from pydantic import BaseModel, ConfigDict, Field

from app.db.tenant_tx import as_worker
from app.domain.generation.jobs import lead_context
from app.domain.integrations.secrets import provider_config
from app.domain.leads.importer import Parsed, _write
from app.domain.leads.normalize import clean_text, normalize_email
from app.domain.tenants.service import resolve_ai_settings
from app.llm.client import LlmClient
from app.llm.outputs import LeadScore
from app.llm.prompts import ensure_prompt
from app.providers.apollo import ApolloClient
from app.providers.http import ProviderError
from app.workers.context import JobFailed, tenant_job

log = structlog.get_logger()


class ApolloSearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    titles: list[str] = Field(default_factory=list, max_length=20)  # e.g. "Head of Buying"
    locations: list[str] = Field(default_factory=list, max_length=20)  # e.g. "Portugal"
    keywords: str | None = Field(default=None, max_length=200)  # company keywords, e.g. "knitwear"
    employee_ranges: list[str] = Field(default_factory=list, max_length=10)  # Apollo format "51,200"
    max_people: int = Field(default=25, ge=1, le=100)
    min_score: int = Field(default=0, ge=0, le=10)  # leave out people scored below this

    def filters(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.titles:
            out["person_titles"] = self.titles
        if self.locations:
            out["person_locations"] = self.locations
        if self.keywords:
            out["q_organization_keyword_tags"] = [self.keywords]
        if self.employee_ranges:
            out["organization_num_employees_ranges"] = self.employee_ranges
        return out

    def label(self) -> str:
        parts = [", ".join(self.titles[:2]), ", ".join(self.locations[:2]), self.keywords or ""]
        return "Apollo: " + (" / ".join(p for p in parts if p) or "search")


def _joined(values: Any) -> str | None:
    return (", ".join(str(v) for v in values[:10]) or None) if isinstance(values, list) else None


def _lead(person: dict[str, Any]) -> dict[str, Any] | None:
    email = normalize_email(person.get("email"))
    if email is None:
        return None
    org = person.get("organization") or {}
    size = org.get("estimated_num_employees")
    return {
        "email": email,
        "first_name": clean_text(person.get("first_name")),
        "last_name": clean_text(person.get("last_name")),
        "title": clean_text(person.get("title")),
        "company": clean_text(org.get("name")),
        "linkedin_url": clean_text(person.get("linkedin_url"), 2000),
        "website": clean_text(org.get("website_url"), 2000),
        "industry": clean_text(org.get("industry")),
        "city": clean_text(person.get("city")),
        "country": clean_text(person.get("country")),
        "company_size": int(size) if isinstance(size, int | float) else None,
        # Same keys as the CSV columns, so generation.lead_details reads both.
        "raw": {
            "apollo_person_id": person.get("id"),
            "seniority": person.get("seniority"),
            "departments": _joined(person.get("departments")),
            "keywords": _joined(org.get("keywords")),
            "technologies": _joined(org.get("technology_names")),
            "annual_revenue": org.get("annual_revenue_printed"),
            "total_funding": org.get("total_funding_printed"),
            "latest_funding": org.get("latest_funding_stage"),
            "last_raised_at": org.get("latest_funding_round_date"),
        },
        "apollo_person_id": person.get("id"),
    }


@tenant_job
async def apollo_search(ctx: dict[str, Any], tenant_id: UUID, import_id: str) -> dict[str, Any]:
    pool: asyncpg.Pool = ctx["pool"]
    llm: LlmClient = ctx["llm"]
    iid = UUID(import_id)
    async with as_worker(pool, tenant_id) as conn:
        imp = await conn.fetchrow(
            "select id, campaign_id, status, header_map from public.lead_imports where id = $1 for update", iid
        )
        if imp is None or imp["status"] not in ("uploaded", "processing"):
            return {"skipped": imp["status"] if imp else "missing"}
        await conn.execute(
            "update public.lead_imports set status = 'processing', started_at = now() where id = $1", iid
        )
        try:
            config = await provider_config(conn, ctx["crypto"], tenant_id, "apollo")
        except JobFailed:
            await conn.execute(
                "update public.lead_imports set status = 'failed', error_code = 'apollo_not_connected',"
                " finished_at = now() where id = $1",
                iid,
            )
            raise
        campaign = await conn.fetchrow(
            "select offer_summary, offer_details from public.campaigns where id = $1", imp["campaign_id"]
        )
        ai = resolve_ai_settings(await conn.fetchval("select settings from public.tenants") or {}, ctx["settings"])
        prompt = await ensure_prompt(conn, "lead_score", ai.classify_model)
    search = ApolloSearchIn.model_validate(imp["header_map"])
    client = ApolloClient(ctx["http"], config["api_key"])

    try:
        people: list[dict[str, Any]] = []
        page = 1
        while len(people) < search.max_people and page <= 5:
            async with ctx["limits"].slot(tenant_id, "apollo"):
                found = await client.search_people(search.filters(), page, min(25, search.max_people))
            if not found:
                break
            people.extend(found)
            page += 1
        people = people[: search.max_people]
        revealed: list[dict[str, Any]] = []
        for start in range(0, len(people), 10):
            async with ctx["limits"].slot(tenant_id, "apollo"):
                revealed.extend(await client.reveal([str(p["id"]) for p in people[start : start + 10] if p.get("id")]))
    except ProviderError as exc:
        if exc.retryable:
            raise Retry(defer=min(max(exc.retry_after or 30, 5), 600)) from exc
        async with as_worker(pool, tenant_id) as conn:
            await conn.execute(
                "update public.lead_imports set status = 'failed', error_code = 'provider_rejected', errors = $2,"
                " finished_at = now() where id = $1",
                iid,
                [{"row": 0, "error": f"Apollo said: {exc.message}"}],
            )
        raise JobFailed("provider_rejected", exc.message) from exc

    leads: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for person in revealed:
        lead = _lead(person)
        if lead is None:
            continue
        try:
            result = await llm.complete(
                pool,
                tenant_id,
                model=ai.classify_model,
                system=prompt.system,
                user=prompt.render({**dict(campaign or {}), **lead_context(lead)}),
                output=LeadScore,
                temperature=prompt.temperature,
                ref_table="lead_imports",
                ref_id=iid,
                job_id=ctx.get("job_id"),
            )
            score: LeadScore = result.output
        except JobFailed as exc:
            if exc.error_code == "llm_bad_output":
                score = LeadScore(buyer_score=1, confidence=1, is_decision_maker=False, reason="not scored")
            else:
                raise
        if score.buyer_score < search.min_score:
            errors.append(
                {
                    "row": len(errors) + 1,
                    "error": f"{lead['email']} scored {score.buyer_score}, below {search.min_score}",
                }
            )
            continue
        lead["score"] = score
        leads.append(lead)

    parsed = Parsed(header_map={}, total=len(revealed), failed=len(errors) + (len(revealed) - len(leads) - len(errors)))
    seen: set[str] = set()
    for lead in leads:
        (parsed.repeated if lead["email"] in seen else parsed.leads).append(lead)
        seen.add(lead["email"])
    parsed.errors = errors[:100]
    async with as_worker(pool, tenant_id) as conn:
        summary, created = await _write(conn, imp["campaign_id"], iid, parsed, source="apollo")
        for lead in leads:
            lead_id = created.get(lead["email"])
            if lead_id is None:
                continue
            s: LeadScore = lead["score"]
            await conn.execute(
                """
                update public.leads set buyer_score = $2, buyer_confidence = $3, is_decision_maker = $4,
                       buyer_reason = $5, apollo_person_id = $6
                 where id = $1
                """,
                lead_id,
                s.buyer_score,
                s.confidence,
                s.is_decision_maker,
                s.reason,
                lead["apollo_person_id"],
            )
        await conn.execute("update public.lead_imports set header_map = $2 where id = $1", iid, search.model_dump())
    log.info("apollo.done", **summary)
    return summary
