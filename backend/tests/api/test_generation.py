"""Sequence generation against the real database, with OpenAI replaced by recorded responses (httpx.MockTransport)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import psycopg
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.domain.generation.jobs import generate_campaign, generate_lead_sequence
from app.llm.client import LlmClient
from app.workers.limits import RateLimiter
from tests.api.conftest import FakeQueue
from tests.api.test_campaigns_leads import APOLLO_CSV, LeadsHarness, lapi, new_campaign, run_import, workspace  # noqa: F401
from tests.rls.conftest import Seed

pytestmark = pytest.mark.anyio


class FakeRedis:
    """Enough of Redis for RateLimiter: the bucket always has a token, slots are counted."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}

    async def eval(self, *_: Any) -> int:
        return 0

    async def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def decr(self, key: str) -> int:
        self.counters[key] -= 1
        return self.counters[key]

    async def expire(self, *_: Any) -> None:
        return None


def openai(replies: list[dict[str, Any]], seen: list[dict[str, Any]]) -> httpx.AsyncClient:
    """Each request gets the next recorded reply, as OpenAI's chat completions shape with usage."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        content = (
            replies.pop(0)
            if replies
            else {"subject": "Knitwear", "body": "Hi Ana, a fresh idea for your range. Worth a call?"}
        )
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": {"content": json.dumps(content)}}],
                "usage": {"prompt_tokens": 400, "completion_tokens": 100},
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def setup_campaign(lapi: LeadsHarness, seed: Seed) -> tuple[Any, dict[str, str], dict[str, Any]]:
    tenant_id, headers = await workspace(lapi, seed)
    await lapi.client.post(
        "/v1/sender-identities", json={"display_name": "Rahim Uddin", "is_default": True}, headers=headers
    )
    campaign = await new_campaign(
        lapi,
        headers,
        sequence_count=2,
        offer_summary="Organic cotton knitwear for EU retailers.",
        offer_details="GOTS certified. MOQ 300 per colour. 45-day lead time.",
    )
    await lapi.client.post(
        f"/v1/lead-imports?campaign_id={campaign['id']}",
        content=APOLLO_CSV,
        headers={**headers, "content-type": "text/csv"},
    )
    await run_import(lapi, tenant_id)
    return tenant_id, headers, campaign


def worker_ctx(lapi: LeadsHarness, settings: Settings, http: httpx.AsyncClient, queue: FakeQueue) -> dict[str, Any]:
    local = settings.model_copy(update={"openai_api_key": SecretStr("sk-test"), "imports_dir": str(lapi.imports_dir)})
    return {
        "pool": lapi.pool,
        "queue": queue,
        "settings": local,
        "llm": LlmClient(http, local, RateLimiter(FakeRedis())),
        "job_id": "test-job",
    }


async def test_generation_writes_each_step_and_regenerates_on_a_guard_hit(
    lapi: LeadsHarness,
    seed: Seed,
    settings: Settings,
    superuser: psycopg.Connection,
) -> None:
    tenant_id, headers, campaign = await setup_campaign(lapi, seed)
    r = await lapi.client.post(f"/v1/campaigns/{campaign['id']}/generate", headers=headers)
    assert r.status_code == 202, r.text
    assert r.json() == {"queued_leads": 2}

    seen: list[dict[str, Any]] = []
    replies = [
        {
            "subject": "Knitwear for Moda",
            "body": "Hi Ana, Moda SA's new range caught my eye. We make GOTS knitwear for EU stores. Worth a chat?",
        },
        # Step 2, first try, breaks two rules: a link and a sign-off. The job must ask again.
        {"subject": "One more idea", "body": "Our catalogue is at https://gazi.example/c\n\nBest regards,"},
        {"subject": "One more idea", "body": "Happy to send our catalogue and a few samples if useful, Ana. Should I?"},
    ]
    http = openai(replies, seen)
    queue = FakeQueue()
    ctx = worker_ctx(lapi, settings, http, queue)
    job = next(j for j in lapi.queue.jobs if j["function"] == "generate_campaign")
    assert (await generate_campaign(ctx, str(tenant_id), *job["args"])) == {"queued": 2}

    ana = next(j for j in queue.jobs if j["function"] == "generate_lead_sequence")
    lead_id = ana["args"][0]
    first_lead_email = superuser.execute("select email from public.leads where id = %s", [lead_id]).fetchone()
    result = await generate_lead_sequence(ctx, str(tenant_id), lead_id)
    assert result == {"written": 2, "failed": 0}
    assert len(seen) == 3  # step 2 was regenerated once
    assert seen[0]["response_format"]["json_schema"]["strict"] is True
    assert "temperature" not in seen[0]  # gpt-5.x takes its default

    messages = superuser.execute(
        "select step, status, body, prompt_version, model, attempt from public.lead_messages where lead_id = %s order by step",
        [lead_id],
    ).fetchall()
    assert [(m[0], m[1], m[3], m[4]) for m in messages] == [(1, "ok", 1, "gpt-5.5"), (2, "ok", 1, "gpt-5.5")]
    assert "https://" not in messages[1][2] and messages[1][5] == 2
    assert superuser.execute("select status from public.leads where id = %s", [lead_id]).fetchone() == ("ready",)
    usage = superuser.execute(
        "select count(*), sum(total_tokens) from public.usage_logs where tenant_id = %s", [tenant_id]
    ).fetchone()
    assert usage == (3, 1500)
    assert first_lead_email is not None

    page = (await lapi.client.get(f"/v1/campaigns/{campaign['id']}/messages?lead_id={lead_id}", headers=headers)).json()
    assert page["total"] == 2 and page["items"][0]["subject"] == "Knitwear for Moda"

    # People get the same rules when they edit.
    message_id = page["items"][1]["id"]
    r = await lapi.client.patch(
        f"/v1/lead-messages/{message_id}",
        json={"subject": "x", "body": "See www.gazi.example for more."},
        headers=headers,
    )
    assert r.status_code == 422 and "link in a follow-up" in r.json()["problems"]
    r = await lapi.client.patch(
        f"/v1/lead-messages/{message_id}",
        json={"subject": "Samples", "body": "Ana, I can send three samples this week if you'd like to feel the yarn."},
        headers=headers,
    )
    assert r.status_code == 200 and r.json()["status"] == "approved"


async def test_generation_stops_at_the_token_cap(
    lapi: LeadsHarness,
    seed: Seed,
    settings: Settings,
    superuser: psycopg.Connection,
) -> None:
    tenant_id, headers, campaign = await setup_campaign(lapi, seed)
    await lapi.client.post(f"/v1/campaigns/{campaign['id']}/generate", headers=headers)
    seen: list[dict[str, Any]] = []
    queue = FakeQueue()
    ctx = worker_ctx(lapi, settings.model_copy(update={"included_tokens": 500}), openai([], seen), queue)
    job = next(j for j in reversed(lapi.queue.jobs) if j["function"] == "generate_campaign")
    await generate_campaign(ctx, str(tenant_id), *job["args"])
    first, second = [j["args"][0] for j in queue.jobs]

    await generate_lead_sequence(ctx, str(tenant_id), first)  # 500 tokens used by step 1; step 2 is over the cap
    assert len(seen) == 1
    assert superuser.execute("select status from public.leads where id = %s", [first]).fetchone() == ("new",)
    failed = superuser.execute(
        "select error_code from public.lead_messages where lead_id = %s and step = 2", [first]
    ).fetchone()
    assert failed == ("llm_quota_exceeded",)

    assert (await generate_lead_sequence(ctx, str(tenant_id), second)) == {"failed": "llm_quota_exceeded"}
    assert len(seen) == 1  # no model call over the cap


async def test_generation_needs_an_offer(lapi: LeadsHarness, seed: Seed) -> None:
    _, headers = await workspace(lapi, seed)
    campaign = await new_campaign(lapi, headers)
    r = await lapi.client.post(f"/v1/campaigns/{campaign['id']}/generate", headers=headers)
    assert r.status_code == 422 and r.json()["error_code"] == "offer_missing"
