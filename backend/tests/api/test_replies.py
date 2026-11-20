"""The reply pipeline end to end: webhook -> classify -> draft -> approve -> send (OpenAI and Instantly recorded)."""

from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest

from app.core.config import Settings
from app.domain.campaigns.launch import launch_campaign
from app.domain.replies.jobs import AUTO_SEND_DELAY, draft_reply, handle_webhook_event, send_reply
from app.workers.limits import RateLimiter
from tests.api.conftest import FakeQueue
from tests.api.test_campaigns_leads import LeadsHarness, lapi  # noqa: F401
from tests.api.test_generation import FakeRedis, openai, worker_ctx
from tests.api.test_launch import instantly, launch_ctx, ready_campaign
from tests.rls.conftest import Seed

pytestmark = pytest.mark.anyio


async def launched(
    lapi: LeadsHarness, seed: Seed, settings: Settings
) -> tuple[Any, dict[str, str], dict[str, Any], str]:
    tenant_id, headers, campaign = await ready_campaign(lapi, seed, settings)
    await lapi.client.put(
        "/v1/integrations/calendly",
        json={"booking_url": "https://calendly.com/rahim/intro", "api_token": "cal_token_123"},
        headers=headers,
    )
    await lapi.client.post(f"/v1/campaigns/{campaign['id']}/launch", headers=headers)
    await launch_campaign(launch_ctx(lapi, settings, instantly([])), str(tenant_id), *lapi.queue.jobs[-1]["args"])
    endpoint = (await lapi.client.post("/v1/webhook-endpoints", json={"provider": "instantly"}, headers=headers)).json()
    return tenant_id, headers, campaign, endpoint["url_path"]


def reply_event(
    email_id: str = "em-1", text: str = "Yes, this looks interesting. Can we talk next week?"
) -> dict[str, Any]:
    return {
        "event_type": "reply_received",
        "campaign_id": "inst-campaign-1",
        "email_id": email_id,
        "lead_email": "ana@moda.pt",
        "email_account": "rahim@gazi.example",
        "reply_subject": "Re: Knitwear",
        "reply_text": text,
    }


def ctx_for(
    lapi: LeadsHarness, settings: Settings, replies: list[dict[str, Any]], http_calls: list[Any]
) -> dict[str, Any]:
    queue = FakeQueue()
    ctx = worker_ctx(lapi, settings, openai(replies, []), queue)
    ctx.update(crypto=lapi.crypto, limits=RateLimiter(FakeRedis()))
    ctx["instantly"] = instantly(http_calls)
    return ctx


async def test_reply_is_classified_drafted_and_sent_after_approval(
    lapi: LeadsHarness, seed: Seed, settings: Settings, superuser: psycopg.Connection
) -> None:
    tenant_id, headers, campaign, path = await launched(lapi, seed, settings)
    r = await lapi.client.post(path, json=reply_event())
    assert r.status_code == 200 and r.json() == {"ok": True, "duplicate": False}
    event_job = lapi.queue.jobs[-1]
    assert event_job["function"] == "handle_webhook_event" and event_job["queue"] == "gazi:replies"

    calls: list[Any] = []
    ctx = ctx_for(
        lapi,
        settings,
        [
            {"intent": "interested", "confidence": 0.93, "rationale": "Asks for a call."},
            {"body": "Great to hear, Ana. Next Tuesday or Wednesday works for a 20-minute call."},
        ],
        calls,
    )
    result = await handle_webhook_event(ctx, str(tenant_id), *event_job["args"])
    reply_id = result["reply_id"]
    assert ctx["queue"].jobs[-1]["function"] == "draft_reply"
    assert (await draft_reply(ctx, str(tenant_id), reply_id)) == {"drafted": True, "auto_send": False}  # auto-reply off

    page = (await lapi.client.get("/v1/replies", headers=headers)).json()
    assert page["waiting"] == 1
    reply = page["items"][0]
    assert reply["intent"] == "interested" and reply["lead_email"] == "ana@moda.pt" and reply["will_add_link"] is True
    assert reply["campaign_name"] == campaign["name"] and reply["inbound_body"].startswith("Yes")
    lead_status = superuser.execute(
        "select status from public.leads where email = 'ana@moda.pt' and tenant_id = %s", [tenant_id]
    ).fetchone()
    assert lead_status == ("replied",)

    r = await lapi.client.patch(
        f"/v1/replies/{reply_id}", json={"draft_body": "Book here: https://calendly.com/x"}, headers=headers
    )
    assert r.status_code == 422 and r.json()["error_code"] == "reply_rejected"

    r = await lapi.client.post(f"/v1/replies/{reply_id}/send", headers=headers)
    assert r.status_code == 202 and r.json()["status"] == "drafted"
    send_job = lapi.queue.jobs[-1]
    assert send_job["function"] == "send_reply"

    sent: list[tuple[str, str, Any]] = []
    ctx["http"] = instantly(sent)
    assert (await send_reply(ctx, str(tenant_id), *send_job["args"])) == {"sent": True, "links": 1}
    method, url_path, body = sent[-1]
    assert (method, url_path) == ("POST", "/api/v2/emails/reply")
    assert body["reply_to_uuid"] == "em-1" and body["eaccount"] == "rahim@gazi.example"
    assert body["subject"] == "Re: Knitwear"
    assert f"https://calendly.com/rahim/intro?utm_campaign={campaign['id']}" in body["body"]["html"]
    assert "Export Manager, Gazi Textiles" in body["body"]["html"]
    final = superuser.execute("select status, final_body from public.replies where id = %s", [reply_id]).fetchone()
    assert final is not None and final[0] == "sent" and "utm_campaign" in final[1]

    # The same webhook again is a duplicate, and a second send does nothing.
    assert (await lapi.client.post(path, json=reply_event())).json()["duplicate"] is True
    assert (await send_reply(ctx, str(tenant_id), *send_job["args"])) == {"skipped": "sent"}


async def test_auto_reply_waits_15_minutes_and_only_when_confident(
    lapi: LeadsHarness, seed: Seed, settings: Settings, superuser: psycopg.Connection
) -> None:
    tenant_id, headers, _, path = await launched(lapi, seed, settings)
    await lapi.client.patch("/v1/workspace/ai-settings", json={"auto_reply": True}, headers=headers)

    await lapi.client.post(path, json=reply_event("em-2", "What is your MOQ per colour?"))
    ctx = ctx_for(
        lapi,
        settings,
        [
            {"intent": "question", "confidence": 0.95, "rationale": "Asks MOQ."},
            {"body": "Our minimum is 300 pieces per colour, Ana."},
        ],
        [],
    )
    result = await handle_webhook_event(ctx, str(tenant_id), *lapi.queue.jobs[-1]["args"])
    assert (await draft_reply(ctx, str(tenant_id), result["reply_id"])) == {"drafted": True, "auto_send": True}
    scheduled = ctx["queue"].jobs[-1]
    assert scheduled["function"] == "send_reply" and scheduled["defer"] == AUTO_SEND_DELAY

    await lapi.client.post(path, json=reply_event("em-3", "Maybe, not sure yet."))
    ctx = ctx_for(
        lapi,
        settings,
        [{"intent": "question", "confidence": 0.6, "rationale": "Unclear."}, {"body": "Happy to help, Ana."}],
        [],
    )
    result = await handle_webhook_event(ctx, str(tenant_id), *lapi.queue.jobs[-1]["args"])
    assert (await draft_reply(ctx, str(tenant_id), result["reply_id"])) == {"drafted": True, "auto_send": False}


async def test_unsubscribe_marks_the_lead_and_drafts_nothing(
    lapi: LeadsHarness, seed: Seed, settings: Settings, superuser: psycopg.Connection
) -> None:
    tenant_id, _, _, path = await launched(lapi, seed, settings)
    await lapi.client.post(path, json=reply_event("em-4", "Please remove me from your list."))
    ctx = ctx_for(lapi, settings, [{"intent": "unsubscribe", "confidence": 0.99, "rationale": "Asks removal."}], [])
    await handle_webhook_event(ctx, str(tenant_id), *lapi.queue.jobs[-1]["args"])
    assert not any(j["function"] == "draft_reply" for j in ctx["queue"].jobs)
    assert superuser.execute(
        "select status from public.leads where email = 'ana@moda.pt' and tenant_id = %s", [tenant_id]
    ).fetchone() == ("unsubscribed",)


async def test_calendly_booking_is_matched_by_utm_campaign(
    lapi: LeadsHarness, seed: Seed, settings: Settings, superuser: psycopg.Connection
) -> None:
    tenant_id, headers, campaign, _ = await launched(lapi, seed, settings)
    endpoint = (await lapi.client.post("/v1/webhook-endpoints", json={"provider": "calendly"}, headers=headers)).json()
    body = {
        "event": "invitee.created",
        "payload": {
            "uri": "https://api.calendly.com/scheduled_events/E1/invitees/I1",
            "email": "Ana@Moda.pt",
            "name": "Ana Silva",
            "scheduled_event": {"start_time": "2026-12-01T10:00:00Z"},
            "tracking": {"utm_campaign": campaign["id"]},
        },
    }
    assert (
        await lapi.client.post(
            endpoint["url_path"], content=json.dumps(body), headers={"content-type": "application/json"}
        )
    ).status_code == 200
    job = lapi.queue.jobs[-1]
    assert job["queue"] == "gazi:sync"
    ctx = ctx_for(lapi, settings, [], [])
    assert (await handle_webhook_event(ctx, str(tenant_id), *job["args"]))["booking"] == "invitee.created"
    campaign_now = (await lapi.client.get(f"/v1/campaigns/{campaign['id']}", headers=headers)).json()
    assert campaign_now["counts"]["bookings"] == 1
    row = superuser.execute(
        "select b.campaign_id, l.email from public.bookings b join public.leads l on l.id = b.lead_id"
        " where b.tenant_id = %s",
        [tenant_id],
    ).fetchone()
    assert row is not None and str(row[0]) == campaign["id"] and row[1] == "ana@moda.pt"
