"""Apollo search + scoring, weekly reports and error alerts, against the real database (providers recorded)."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import psycopg
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.domain.alerts import alert_admins
from app.domain.leads.apollo import apollo_search
from app.domain.reports.jobs import report_period, weekly_report
from app.llm.client import LlmClient
from app.workers.limits import RateLimiter
from tests.api.conftest import FakeQueue
from tests.api.test_campaigns_leads import LeadsHarness, lapi, new_campaign, workspace  # noqa: F401
from tests.api.test_generation import FakeRedis
from tests.rls.conftest import Seed

pytestmark = pytest.mark.anyio


class SetNxRedis(FakeRedis):
    def __init__(self) -> None:
        super().__init__()
        self.keys: set[str] = set()

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        if nx and key in self.keys:
            return False
        self.keys.add(key)
        return True


def providers(calls: list[tuple[str, Any]], scores: list[int]) -> httpx.AsyncClient:
    """One client for Apollo, OpenAI and SendGrid, like the worker's shared http client."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.url.host + request.url.path, body))
        if request.url.path.endswith("/mixed_people/api_search"):
            return httpx.Response(
                200,
                json={
                    "people": [
                        {
                            "id": "ap-1",
                            "first_name": "Ana",
                            "title": "Head of Buying",
                            "organization": {"name": "Moda Viva"},
                        },
                        {"id": "ap-2", "first_name": "Ben", "title": "Intern", "organization": {"name": "Retail Co"}},
                    ]
                }
                if body["page"] == 1
                else {"people": []},
            )
        if request.url.path.endswith("/people/bulk_match"):
            return httpx.Response(
                200,
                json={
                    "matches": [
                        {
                            "id": "ap-1",
                            "first_name": "Ana",
                            "last_name": "Silva",
                            "title": "Head of Buying",
                            "email": "ana@modaviva.example",
                            "linkedin_url": "https://linkedin.com/in/ana",
                            "country": "Portugal",
                            "organization": {
                                "name": "Moda Viva",
                                "industry": "Apparel",
                                "estimated_num_employees": 240,
                            },
                        },
                        {
                            "id": "ap-2",
                            "first_name": "Ben",
                            "title": "Intern",
                            "email": "ben@retail.example",
                            "organization": {"name": "Retail Co"},
                        },
                    ]
                },
            )
        if request.url.host == "api.openai.com":
            schema = body["response_format"]["json_schema"]["name"]
            content: dict[str, Any] = (
                {"buyer_score": scores.pop(0), "confidence": 80, "is_decision_maker": True, "reason": "Buyer role."}
                if schema == "LeadScore"
                else {"summary": "Two replies this week, one interested. Keep going."}
            )
            return httpx.Response(
                200,
                json={
                    "model": body["model"],
                    "choices": [{"message": {"content": json.dumps(content)}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 10},
                },
            )
        if request.url.host == "api.sendgrid.com":
            return httpx.Response(202)
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ctx_with(lapi: LeadsHarness, settings: Settings, http: httpx.AsyncClient, **extra: Any) -> dict[str, Any]:
    local = settings.model_copy(update={"openai_api_key": SecretStr("sk-test"), **extra})
    limits = RateLimiter(FakeRedis())
    return {
        "pool": lapi.pool,
        "crypto": lapi.crypto,
        "http": http,
        "limits": limits,
        "settings": local,
        "llm": LlmClient(http, local, limits),
        "queue": FakeQueue(),
        "redis": SetNxRedis(),
        "job_id": "t",
    }


async def test_apollo_search_reveals_scores_and_imports(
    lapi: LeadsHarness, seed: Seed, settings: Settings, superuser: psycopg.Connection
) -> None:
    tenant_id, headers = await workspace(lapi, seed)
    campaign = await new_campaign(lapi, headers, offer_summary="Organic knitwear for EU retailers.")
    r = await lapi.client.post(
        f"/v1/campaigns/{campaign['id']}/apollo-search", json={"titles": ["Head of Buying"]}, headers=headers
    )
    assert r.status_code == 422 and r.json()["error_code"] == "apollo_not_connected"

    await lapi.client.put("/v1/integrations/apollo", json={"api_key": "apollo_key_9999"}, headers=headers)
    r = await lapi.client.post(
        f"/v1/campaigns/{campaign['id']}/apollo-search",
        json={"titles": ["Head of Buying"], "locations": ["Portugal"], "max_people": 10, "min_score": 5},
        headers=headers,
    )
    assert r.status_code == 202, r.text
    imp = r.json()
    assert imp["source"] == "apollo" and imp["original_filename"].startswith("Apollo: Head of Buying")
    job = lapi.queue.jobs[-1]
    assert job["function"] == "apollo_search" and job["queue"] == "gazi:imports"

    calls: list[tuple[str, Any]] = []
    ctx = ctx_with(lapi, settings, providers(calls, scores=[9, 2]))
    result = await apollo_search(ctx, str(tenant_id), *job["args"])
    assert result["imported"] == 1 and result["status"] == "completed_with_errors"  # Ben scored below 5
    search_call = next(body for url, body in calls if url.endswith("mixed_people/api_search"))
    assert search_call["person_titles"] == ["Head of Buying"] and search_call["person_locations"] == ["Portugal"]

    lead = superuser.execute(
        "select email, source, buyer_score, is_decision_maker, apollo_person_id, company_size from public.leads"
        " where campaign_id = %s",
        [campaign["id"]],
    ).fetchall()
    assert lead == [("ana@modaviva.example", "apollo", 9, True, "ap-1", 240)]
    usage = superuser.execute("select count(*) from public.usage_logs where tenant_id = %s", [tenant_id]).fetchone()
    assert usage == (2,)  # one scoring call per revealed person


async def test_weekly_report_is_written_once_and_emailed(
    lapi: LeadsHarness, seed: Seed, settings: Settings, superuser: psycopg.Connection
) -> None:
    tenant_id, headers = await workspace(lapi, seed)
    campaign = await new_campaign(lapi, headers, report_emails=["owner@gazi.example"])
    start, _ = report_period(date.today())
    calls: list[tuple[str, Any]] = []
    ctx = ctx_with(lapi, settings, providers(calls, scores=[]), sendgrid_api_key=SecretStr("sg-test"))

    assert (await weekly_report(ctx, str(tenant_id), campaign["id"], start.isoformat())) == {"status": "sent"}
    mail = next(body for url, body in calls if url.startswith("api.sendgrid.com"))
    assert mail["personalizations"][0]["to"] == [{"email": "owner@gazi.example"}]
    assert "Keep going" in mail["content"][0]["value"]

    reports = (await lapi.client.get(f"/v1/reports?campaign_id={campaign['id']}", headers=headers)).json()
    assert reports[0]["status"] == "sent" and reports[0]["summary"].startswith("Two replies")
    # Running it again for the same week sends nothing.
    assert (await weekly_report(ctx, str(tenant_id), campaign["id"], start.isoformat())) == {"skipped": "sent"}


async def test_report_without_email_settings_is_kept_but_not_sent(
    lapi: LeadsHarness, seed: Seed, settings: Settings
) -> None:
    tenant_id, headers = await workspace(lapi, seed)
    campaign = await new_campaign(lapi, headers, report_emails=["owner@gazi.example"])
    start, _ = report_period(date.today())
    ctx = ctx_with(lapi, settings, providers([], scores=[]))
    assert (await weekly_report(ctx, str(tenant_id), campaign["id"], start.isoformat())) == {"status": "skipped"}
    report = (await lapi.client.get(f"/v1/reports?campaign_id={campaign['id']}", headers=headers)).json()[0]
    assert report["error_code"] == "email_not_configured" and report["metrics"]["replies_this_week"] == 0


async def test_alerts_go_to_admins_once_a_day(lapi: LeadsHarness, seed: Seed, settings: Settings) -> None:
    calls: list[tuple[str, Any]] = []
    ctx = ctx_with(lapi, settings, providers(calls, scores=[]), sendgrid_api_key=SecretStr("sg-test"))
    await alert_admins(ctx, seed.a.id, "llm_quota_exceeded")
    await alert_admins(ctx, seed.a.id, "llm_quota_exceeded")
    await alert_admins(ctx, seed.a.id, "no_email_column")  # not something to alert about
    mails = [body for url, body in calls if url.startswith("api.sendgrid.com")]
    assert len(mails) == 1
    recipients = {r["email"] for r in mails[0]["personalizations"][0]["to"]}
    assert recipients == {"owner@alpha.example.com", "admin@alpha.example.com"}
