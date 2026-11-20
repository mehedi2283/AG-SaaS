"""Launching an email campaign in Instantly, with Instantly replaced by recorded responses (httpx.MockTransport)."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import psycopg
import pytest
from arq import Retry

from app.core.config import Settings
from app.domain.campaigns.launch import launch_campaign
from app.domain.generation.jobs import generate_campaign, generate_lead_sequence
from app.workers.limits import RateLimiter
from tests.api.conftest import FakeQueue
from tests.api.test_campaigns_leads import APOLLO_CSV, LeadsHarness, lapi, new_campaign, run_import, workspace  # noqa: F401
from tests.api.test_generation import FakeRedis, openai, worker_ctx
from tests.rls.conftest import Seed

pytestmark = pytest.mark.anyio


def instantly(calls: list[tuple[str, str, Any]], fail: dict[str, httpx.Response] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, path, body))
        assert request.headers["authorization"] == "Bearer inst_live_key_1234"
        for prefix, response in (fail or {}).items():
            if path.startswith(prefix):
                return response
        if path == "/api/v2/campaigns" and request.method == "POST":
            return httpx.Response(200, json={"id": "inst-campaign-1", "name": body["name"]})
        if path == "/api/v2/leads":
            return httpx.Response(200, json={"id": f"inst-lead-{len(calls)}"})
        if path == "/api/v2/emails/reply":
            return httpx.Response(200, json={"id": "sent-reply-1"})
        if path.endswith("/activate"):
            return httpx.Response(200, json={"status": 1})
        return httpx.Response(404, json={"message": "unexpected call"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def ready_campaign(
    lapi: LeadsHarness, seed: Seed, settings: Settings
) -> tuple[Any, dict[str, str], dict[str, Any]]:
    tenant_id, headers = await workspace(lapi, seed)
    await lapi.client.put("/v1/integrations/instantly", json={"api_key": "inst_live_key_1234"}, headers=headers)
    await lapi.client.post(
        "/v1/sender-identities",
        json={
            "display_name": "Rahim Uddin",
            "signature": "Rahim Uddin\nExport Manager, Gazi Textiles",
            "signature_image_url": "https://cdn.example.com/sig.png",
            "instantly_eaccount": "rahim@gazi.example",
            "is_default": True,
        },
        headers=headers,
    )
    campaign = await new_campaign(
        lapi,
        headers,
        sequence_count=2,
        offer_summary="Organic cotton knitwear.",
        offer_details="GOTS certified.",
        sending_days=["mon", "wed"],
        timezone="Europe/Lisbon",
    )
    await lapi.client.post(
        f"/v1/lead-imports?campaign_id={campaign['id']}",
        content=APOLLO_CSV,
        headers={**headers, "content-type": "text/csv"},
    )
    await run_import(lapi, tenant_id)
    await lapi.client.post(f"/v1/campaigns/{campaign['id']}/generate", headers=headers)
    queue = FakeQueue()
    ctx = worker_ctx(lapi, settings, openai([], []), queue)
    job = next(j for j in reversed(lapi.queue.jobs) if j["function"] == "generate_campaign")
    await generate_campaign(ctx, str(tenant_id), *job["args"])
    for j in queue.jobs:
        await generate_lead_sequence(ctx, str(tenant_id), *j["args"])
    return tenant_id, headers, campaign


def launch_ctx(lapi: LeadsHarness, settings: Settings, http: httpx.AsyncClient) -> dict[str, Any]:
    return {
        "pool": lapi.pool,
        "crypto": lapi.crypto,
        "http": http,
        "limits": RateLimiter(FakeRedis()),
        "settings": settings,
        "job_id": "launch-test",
    }


async def test_launch_creates_the_campaign_and_pushes_leads_with_variables(
    lapi: LeadsHarness, seed: Seed, settings: Settings, superuser: psycopg.Connection
) -> None:
    tenant_id, headers, campaign = await ready_campaign(lapi, seed, settings)
    r = await lapi.client.post(f"/v1/campaigns/{campaign['id']}/launch", headers=headers)
    assert r.status_code == 202, r.text
    job = lapi.queue.jobs[-1]
    assert job["function"] == "launch_campaign" and job["queue"] == "gazi:sync"

    calls: list[tuple[str, str, Any]] = []
    ctx = launch_ctx(lapi, settings, instantly(calls))
    assert (await launch_campaign(ctx, str(tenant_id), *job["args"])) == {"pushed": 2}

    created = calls[0][2]
    assert calls[0][:2] == ("POST", "/api/v2/campaigns")
    steps = created["sequences"][0]["steps"]
    assert [s["variants"][0]["subject"] for s in steps] == ["{{custom_subject_1}}", "{{custom_subject_2}}"]
    assert [s["delay"] for s in steps] == [3, 0]  # wait after each step
    schedule = created["campaign_schedule"]["schedules"][0]
    assert schedule["days"] == {"1": True, "3": True} and schedule["timezone"] == "Europe/Lisbon"
    assert created["email_list"] == ["rahim@gazi.example"] and created["stop_on_reply"] is True

    leads = [c[2] for c in calls if c[1] == "/api/v2/leads"]
    assert len(leads) == 2 and all(lead["campaign"] == "inst-campaign-1" for lead in leads)
    variables = leads[0]["custom_variables"]
    assert set(variables) == {"custom_subject_1", "personalization_1", "custom_subject_2", "personalization_2"}
    follow_up = variables["personalization_2"]
    assert follow_up.startswith("<p>") and "Export Manager, Gazi Textiles" in follow_up
    assert '<img src="https://cdn.example.com/sig.png"' in follow_up
    assert not re.search(r"https?://(?!cdn\.example\.com/sig\.png)", follow_up)  # the signature image is the only URL
    assert calls[-1][1] == "/api/v2/campaigns/inst-campaign-1/activate"

    row = superuser.execute(
        "select launch_status, status, instantly_campaign_id from public.campaigns where id = %s", [campaign["id"]]
    ).fetchone()
    assert row == ("launched", "active", "inst-campaign-1")
    statuses = superuser.execute(
        "select status, count(*) from public.leads where campaign_id = %s group by 1", [campaign["id"]]
    ).fetchall()
    assert statuses == [("pushed", 2)]

    # Launching again pushes nothing twice and creates no second Instantly campaign.
    calls.clear()
    assert (await launch_campaign(ctx, str(tenant_id), *job["args"])) == {"pushed": 0}
    assert not any(c[1] == "/api/v2/campaigns" and c[0] == "POST" for c in calls)


async def test_rate_limits_retry_and_rejections_fail_the_launch(
    lapi: LeadsHarness, seed: Seed, settings: Settings, superuser: psycopg.Connection
) -> None:
    tenant_id, headers, campaign = await ready_campaign(lapi, seed, settings)
    await lapi.client.post(f"/v1/campaigns/{campaign['id']}/launch", headers=headers)
    job = lapi.queue.jobs[-1]

    busy = {"/api/v2/campaigns": httpx.Response(429, headers={"retry-after": "42"})}
    with pytest.raises(Retry):
        await launch_campaign(launch_ctx(lapi, settings, instantly([], busy)), str(tenant_id), *job["args"])

    bad = {"/api/v2/campaigns": httpx.Response(400, json={"message": "timezone is not supported"})}
    result = await launch_campaign(launch_ctx(lapi, settings, instantly([], bad)), str(tenant_id), *job["args"])
    assert result == {"failed": "provider_rejected"}
    row = superuser.execute(
        "select launch_status, launch_error from public.campaigns where id = %s", [campaign["id"]]
    ).fetchone()
    assert row == ("failed", "Instantly said: HTTP 400: timezone is not supported")
    assert "inst_live_key" not in (row[1] or "")


async def test_launch_needs_a_connected_account(lapi: LeadsHarness, seed: Seed) -> None:
    _, headers = await workspace(lapi, seed)
    campaign = await new_campaign(lapi, headers)
    r = await lapi.client.post(f"/v1/campaigns/{campaign['id']}/launch", headers=headers)
    assert r.status_code == 422 and r.json()["error_code"] == "instantly_not_connected"
