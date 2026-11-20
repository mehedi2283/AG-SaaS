from __future__ import annotations

import json
import uuid

import psycopg
import pytest

from app.core.crypto import Sealed, aad
from tests.api.conftest import Harness, auth, unique_slug
from tests.rls.conftest import Seed

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Auth and errors
# ---------------------------------------------------------------------------


async def test_missing_token_is_a_problem_json_401(api: Harness) -> None:
    r = await api.client.get("/v1/me", headers={"x-request-id": "req-123"})
    assert r.status_code == 401
    assert r.headers["content-type"] == "application/problem+json"
    body = r.json()
    assert body["error_code"] == "missing_token" and body["request_id"] == "req-123"


@pytest.mark.parametrize(
    ("kwargs", "code"), [({"expires_in": -10}, "token_expired"), ({"secret": "wrong-secret-" * 4}, "invalid_token")]
)
async def test_bad_tokens_are_rejected(api: Harness, seed: Seed, kwargs: dict, code: str) -> None:
    r = await api.client.get("/v1/me", headers=auth(seed.a.users["owner"], **kwargs))
    assert r.status_code == 401 and r.json()["error_code"] == code


async def test_me_lists_workspaces(api: Harness, seed: Seed) -> None:
    r = await api.client.get("/v1/me", headers=auth(seed.a.users["admin"], seed.a.id, "admin"))
    assert r.status_code == 200
    body = r.json()
    assert body["active_tenant_id"] == str(seed.a.id) and body["active_role"] == "admin"
    assert [w["tenant_id"] for w in body["workspaces"]] == [str(seed.a.id)]


async def test_tenant_routes_need_an_active_workspace(api: Harness, seed: Seed) -> None:
    r = await api.client.get("/v1/integrations", headers=auth(seed.a.users["owner"]))
    assert r.status_code == 403 and r.json()["error_code"] == "no_active_tenant"


async def test_forged_tenant_claim_gets_nothing(api: Harness, seed: Seed) -> None:
    # Claims say admin of B, but the user only belongs to A: RLS re-checks membership.
    r = await api.client.get("/v1/integrations", headers=auth(seed.a.users["owner"], seed.b.id, "admin"))
    assert r.status_code == 200 and r.json() == []


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------


async def test_create_workspace_makes_caller_owner(api: Harness, seed: Seed) -> None:
    r = await api.client.post(
        "/v1/tenants", json={"name": "Charlie Ltd", "slug": unique_slug()}, headers=auth(seed.outsider)
    )
    assert r.status_code == 201
    assert r.json()["role"] == "owner"


async def test_taken_slug_is_a_409(api: Harness, seed: Seed) -> None:
    r = await api.client.post("/v1/tenants", json={"name": "Dup", "slug": "alpha"}, headers=auth(seed.outsider))
    assert r.status_code == 409 and r.json()["error_code"] == "slug_taken"


async def test_switch_workspace_only_to_own_membership(api: Harness, seed: Seed) -> None:
    user = seed.a.users["member"]
    r = await api.client.post("/v1/session/active-tenant", json={"tenant_id": str(seed.b.id)}, headers=auth(user))
    assert r.status_code == 403 and api.auth_admin.calls == []
    r = await api.client.post("/v1/session/active-tenant", json={"tenant_id": str(seed.a.id)}, headers=auth(user))
    assert r.status_code == 200 and r.json()["refresh_session"] is True
    assert api.auth_admin.calls == [(user, seed.a.id)]


# ---------------------------------------------------------------------------
# Integrations (encrypted secrets)
# ---------------------------------------------------------------------------


async def test_admin_saves_key_encrypted_and_masked(api: Harness, seed: Seed, superuser: psycopg.Connection) -> None:
    headers = auth(seed.a.users["admin"], seed.a.id, "admin")
    r = await api.client.put(
        "/v1/integrations/heyreach", json={"api_key": "hr_live_secret_value_9f3c"}, headers=headers
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["key_last4"] == "9f3c" and body["status"] == "unverified"
    assert "secret_value" not in r.text

    row = superuser.execute(
        "select i.config_ciphertext, i.config_nonce, t.wrapped_dek from public.integrations i"
        " join public.tenants t on t.id = i.tenant_id where i.tenant_id = %s and i.provider = 'heyreach'",
        [seed.a.id],
    ).fetchone()
    assert row is not None and b"secret_value" not in row[0]
    plain = api.crypto.decrypt(row[2], Sealed(row[0], row[1]), aad(seed.a.id, "integration:heyreach"))
    assert json.loads(plain) == {"api_key": "hr_live_secret_value_9f3c"}

    audit = superuser.execute(
        "select diff from public.audit_log where tenant_id = %s and action = 'integration.saved'", [seed.a.id]
    ).fetchall()
    assert audit and all("api_key" not in json.dumps(d[0]) for d in audit)


async def test_member_cannot_touch_integrations(api: Harness, seed: Seed) -> None:
    headers = auth(seed.a.users["member"], seed.a.id, "member")
    r = await api.client.put("/v1/integrations/instantly", json={"api_key": "x" * 20}, headers=headers)
    assert r.status_code == 403 and r.json()["error_code"] == "insufficient_role"


async def test_invalid_integration_payload_is_422(api: Harness, seed: Seed) -> None:
    headers = auth(seed.a.users["admin"], seed.a.id, "admin")
    r = await api.client.put("/v1/integrations/calendly", json={"api_token": "x" * 20}, headers=headers)
    assert r.status_code == 422 and r.json()["error_code"] == "invalid_request"


async def test_role_claim_is_not_trusted_alone(api: Harness, seed: Seed) -> None:
    # A member presenting a forged "admin" role passes the fast check but RLS refuses the write.
    headers = auth(seed.a.users["member"], seed.a.id, "admin")
    r = await api.client.put("/v1/integrations/apollo", json={"api_key": "x" * 20}, headers=headers)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


async def _endpoint(api: Harness, seed: Seed, provider: str, require_secret: bool = False) -> dict:
    headers = auth(seed.b.users["owner"], seed.b.id, "owner")
    listed = (await api.client.get("/v1/webhook-endpoints", headers=headers)).json()
    for e in listed:
        if e["provider"] == provider and e["revoked_at"] is None:
            await api.client.post(f"/v1/webhook-endpoints/{e['id']}/revoke", headers=headers)
    r = await api.client.post(
        "/v1/webhook-endpoints", json={"provider": provider, "require_secret": require_secret}, headers=headers
    )
    assert r.status_code == 201, r.text
    return r.json()


def _instantly_reply(campaign: str = "inst-bravo", event_id: str | None = None) -> dict:
    return {
        "id": event_id or uuid.uuid4().hex,
        "campaign_id": campaign,
        "eaccount": "s@x.com",
        "from_email": "lee@bravo-prospect.com",
        "body": "Sounds good",
    }


async def test_webhook_is_stored_once_and_enqueued_once(api: Harness, seed: Seed) -> None:
    ep = await _endpoint(api, seed, "instantly")
    payload = _instantly_reply()
    first = await api.client.post(ep["url_path"], json=payload)
    second = await api.client.post(ep["url_path"], json=payload)
    assert first.status_code == 200 and first.json() == {"ok": True, "duplicate": False}
    assert second.status_code == 200 and second.json()["duplicate"] is True
    assert len(api.queue.jobs) == 1
    job = api.queue.jobs[0]
    assert job["function"] == "handle_webhook_event" and job["tenant_id"] == seed.b.id
    assert job["queue"] == "gazi:replies" and job["job_id"].startswith("webhook:")


async def test_unknown_token_or_wrong_provider_is_404(api: Harness, seed: Seed) -> None:
    ep = await _endpoint(api, seed, "instantly")
    assert (await api.client.post("/webhooks/instantly/wh_nope", json=_instantly_reply())).status_code == 404
    wrong = ep["url_path"].replace("/instantly/", "/heyreach/")
    assert (await api.client.post(wrong, json=_instantly_reply())).status_code == 404


async def test_campaign_from_another_tenant_is_404(api: Harness, seed: Seed) -> None:
    ep = await _endpoint(api, seed, "instantly")
    r = await api.client.post(ep["url_path"], json=_instantly_reply(campaign="inst-alpha"))
    assert r.status_code == 404 and api.queue.jobs == []


async def test_missing_campaign_and_bad_json_are_422(api: Harness, seed: Seed) -> None:
    ep = await _endpoint(api, seed, "instantly")
    no_campaign = {k: v for k, v in _instantly_reply().items() if k != "campaign_id"}
    assert (await api.client.post(ep["url_path"], json=no_campaign)).json()["error_code"] == "missing_campaign"
    r = await api.client.post(ep["url_path"], content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json()["error_code"] == "invalid_json"


async def test_webhook_secret_is_enforced(api: Harness, seed: Seed) -> None:
    ep = await _endpoint(api, seed, "instantly", require_secret=True)
    assert ep["secret"] and ep["has_secret"] is True
    r = await api.client.post(ep["url_path"], json=_instantly_reply())
    assert r.status_code == 401 and r.json()["error_code"] == "bad_webhook_secret"
    r = await api.client.post(ep["url_path"], json=_instantly_reply(), headers={"x-gazi-secret": "wrong"})
    assert r.status_code == 401
    r = await api.client.post(ep["url_path"], json=_instantly_reply(), headers={"x-gazi-secret": ep["secret"]})
    assert r.status_code == 200


async def test_rotated_token_stops_working(api: Harness, seed: Seed) -> None:
    ep = await _endpoint(api, seed, "instantly")
    headers = auth(seed.b.users["owner"], seed.b.id, "owner")
    new = (await api.client.post(f"/v1/webhook-endpoints/{ep['id']}/rotate", headers=headers)).json()
    assert (await api.client.post(ep["url_path"], json=_instantly_reply())).status_code == 404
    assert (await api.client.post(new["url_path"], json=_instantly_reply())).status_code == 200


async def test_queue_outage_keeps_event_and_returns_503(
    api: Harness, seed: Seed, superuser: psycopg.Connection
) -> None:
    ep = await _endpoint(api, seed, "instantly")
    api.queue.fail = True
    event = _instantly_reply()
    r = await api.client.post(ep["url_path"], json=event)
    assert r.status_code == 503 and r.json()["error_code"] == "queue_unavailable"
    status = superuser.execute(
        "select status from public.webhook_events where tenant_id = %s and dedupe_key = %s", [seed.b.id, event["id"]]
    ).fetchone()
    assert status == ("received",)


async def test_calendly_needs_no_campaign(api: Harness, seed: Seed) -> None:
    ep = await _endpoint(api, seed, "calendly")
    body = {
        "event": "invitee.created",
        "payload": {"uri": f"https://api.calendly.com/inv/{uuid.uuid4()}", "email": "lee@bravo-prospect.com"},
    }
    r = await api.client.post(ep["url_path"], json=body)
    assert r.status_code == 200 and api.queue.jobs[-1]["queue"] == "gazi:sync"
