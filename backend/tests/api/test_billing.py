"""Billing API + Stripe webhook against the real database. Stripe itself is faked, except signature checks,
which run through the real Stripe verification code."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.crypto import Crypto
from app.db.pool import create_pool
from app.main import create_app
from app.providers.stripe import PriceInfo, StripeBilling
from tests.api.conftest import FakeAuthAdmin, FakeQueue, Harness, auth, unique_slug
from tests.rls.conftest import Seed

pytestmark = pytest.mark.anyio

WEBHOOK_SECRET = "whsec_test_" + "x" * 32
PRICE = "price_test_gazi"


@dataclass
class FakeStripe:
    customers: list[UUID] = field(default_factory=list)
    checkouts: list[dict[str, Any]] = field(default_factory=list)
    portals: list[str] = field(default_factory=list)
    _verify: StripeBilling = field(default_factory=lambda: StripeBilling("sk_test_unused", WEBHOOK_SECRET))

    async def create_customer(self, tenant_id: UUID, name: str) -> str:
        self.customers.append(tenant_id)
        return "cus_" + uuid.uuid4().hex[:14]

    async def create_checkout_session(self, **kwargs: Any) -> str:
        self.checkouts.append(kwargs)
        return "https://checkout.stripe.com/c/pay/cs_test_123"

    async def create_portal_session(self, customer_id: str, return_url: str) -> str:
        self.portals.append(customer_id)
        return "https://billing.stripe.com/p/session/test_123"

    async def get_price(self, price_id: str) -> PriceInfo:
        return PriceInfo(id=price_id, unit_amount=9900, currency="usd", interval="month")

    def parse_event(self, body: bytes, signature: str | None) -> dict[str, Any]:
        return self._verify.parse_event(body, signature)


@dataclass
class BillingHarness(Harness):
    stripe: FakeStripe = field(default_factory=FakeStripe)


@pytest.fixture
async def bapi(settings: Settings, crypto: Crypto, dsn: str) -> AsyncIterator[BillingHarness]:
    enabled = settings.model_copy(
        update={
            "stripe_secret_key": SecretStr("sk_test_unused"),
            "stripe_webhook_secret": SecretStr(WEBHOOK_SECRET),
            "stripe_price_id": PRICE,
            "app_url": "https://app.example.com",
        }
    )
    pool = await create_pool(enabled.database_url.get_secret_value(), max_size=4)
    queue, auth_admin, fake = FakeQueue(), FakeAuthAdmin(), FakeStripe()
    app = create_app(enabled, pool=pool, queue=queue, auth_admin=auth_admin, crypto=crypto, billing_provider=fake)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield BillingHarness(client, queue, auth_admin, crypto, dsn, fake)
    await pool.close()


async def new_workspace(h: Harness, seed: Seed) -> tuple[UUID, dict[str, str]]:
    r = await h.client.post(
        "/v1/tenants", json={"name": "Delta Co", "slug": unique_slug()}, headers=auth(seed.outsider)
    )
    assert r.status_code == 201, r.text
    tenant_id = UUID(r.json()["tenant_id"])
    return tenant_id, auth(seed.outsider, tenant_id, "owner")


def signed(event: dict[str, Any], secret: str = WEBHOOK_SECRET) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(event).encode()
    t = int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"stripe-signature": f"t={t},v1={sig}", "content-type": "application/json"}


def sub_event(customer: str, status: str, *, created: int, event_id: str | None = None, **extra: Any) -> dict[str, Any]:
    period_end = int(time.time()) + 30 * 86400
    return {
        "id": event_id or "evt_" + uuid.uuid4().hex[:20],
        "type": "customer.subscription.updated",
        "created": created,
        "data": {
            "object": {
                "id": "sub_test123",
                "object": "subscription",
                "customer": customer,
                "status": status,
                "cancel_at_period_end": False,
                "trial_end": int(time.time()) + 14 * 86400 if status == "trialing" else None,
                "items": {"data": [{"price": {"id": PRICE}, "current_period_end": period_end}]},
                **extra,
            }
        },
    }


async def customer_of(superuser: psycopg.Connection, tenant_id: UUID) -> str:
    row = superuser.execute(
        "select stripe_customer_id from public.subscriptions where tenant_id = %s", [tenant_id]
    ).fetchone()
    assert row is not None
    return str(row[0])


# ---------------------------------------------------------------------------


async def test_billing_is_off_without_stripe_settings(api: Harness, seed: Seed) -> None:
    r = await api.client.get("/v1/billing", headers=auth(seed.a.users["viewer"], seed.a.id, "viewer"))
    assert r.status_code == 200
    assert r.json()["enabled"] is False and r.json()["access"] == "active"
    r = await api.client.post("/v1/billing/checkout", headers=auth(seed.a.users["owner"], seed.a.id, "owner"))
    assert r.status_code == 503 and r.json()["error_code"] == "billing_not_configured"


async def test_new_workspace_needs_a_plan(bapi: BillingHarness, seed: Seed) -> None:
    _, headers = await new_workspace(bapi, seed)
    body = (await bapi.client.get("/v1/billing", headers=headers)).json()
    assert body["enabled"] is True and body["access"] == "none" and body["trial_available"] is True
    assert body["plan"] == {
        "price_id": PRICE,
        "unit_amount": 9900,
        "currency": "usd",
        "interval": "month",
        "trial_days": 14,
    }


async def test_checkout_needs_admin(bapi: BillingHarness, seed: Seed) -> None:
    r = await bapi.client.post("/v1/billing/checkout", headers=auth(seed.a.users["member"], seed.a.id, "member"))
    assert r.status_code == 403 and r.json()["error_code"] == "insufficient_role"
    assert bapi.stripe.customers == [] and bapi.stripe.checkouts == []


async def test_checkout_starts_trial_and_reuses_the_customer(
    bapi: BillingHarness, seed: Seed, superuser: psycopg.Connection
) -> None:
    tenant_id, headers = await new_workspace(bapi, seed)
    for _ in range(2):
        r = await bapi.client.post("/v1/billing/checkout", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["url"].startswith("https://checkout.stripe.com/")
    assert bapi.stripe.customers == [tenant_id]  # one Stripe customer, however often checkout is opened
    first = bapi.stripe.checkouts[0]
    assert first["trial_days"] == 14 and first["price_id"] == PRICE and first["tenant_id"] == tenant_id
    assert first["success_url"] == "https://app.example.com/settings?billing=success"
    assert first["customer_id"] == await customer_of(superuser, tenant_id)
    audit = superuser.execute(
        "select count(*) from public.audit_log where tenant_id = %s and action = 'billing.checkout_started'",
        [tenant_id],
    ).fetchone()
    assert audit == (2,)


async def test_webhook_rejects_bad_signatures(bapi: BillingHarness, seed: Seed) -> None:
    body, headers = signed(sub_event("cus_alpha", "active", created=int(time.time())), secret="whsec_wrong")
    r = await bapi.client.post("/webhooks/stripe", content=body, headers=headers)
    assert r.status_code == 401 and r.json()["error_code"] == "bad_stripe_signature"
    r = await bapi.client.post("/webhooks/stripe", content=body)
    assert r.status_code == 401


async def test_unknown_customer_is_acknowledged_and_ignored(bapi: BillingHarness) -> None:
    body, headers = signed(sub_event("cus_notgazi", "active", created=int(time.time())))
    r = await bapi.client.post("/webhooks/stripe", content=body, headers=headers)
    assert r.status_code == 200 and r.json()["outcome"] == "unknown_customer"


async def test_subscription_lifecycle(bapi: BillingHarness, seed: Seed, superuser: psycopg.Connection) -> None:
    tenant_id, headers = await new_workspace(bapi, seed)
    await bapi.client.post("/v1/billing/checkout", headers=headers)
    customer = await customer_of(superuser, tenant_id)
    now = int(time.time())

    # Trial starts.
    trialing = sub_event(customer, "trialing", created=now - 100)
    body, sig = signed(trialing)
    r = await bapi.client.post("/webhooks/stripe", content=body, headers=sig)
    assert r.status_code == 200 and r.json()["outcome"] == "processed"
    billing = (await bapi.client.get("/v1/billing", headers=headers)).json()
    assert billing["access"] == "active" and billing["status"] == "trialing" and billing["trial_end"]
    assert billing["current_period_end"] and billing["trial_available"] is False

    # Stripe delivers the same event twice.
    r = await bapi.client.post("/webhooks/stripe", content=body, headers=sig)
    assert r.json()["outcome"] == "duplicate"

    # Can't open a second checkout while subscribed.
    r = await bapi.client.post("/v1/billing/checkout", headers=headers)
    assert r.status_code == 409 and r.json()["error_code"] == "already_subscribed"

    # Payment fails: grace period, everything still works.
    body, sig = signed(sub_event(customer, "past_due", created=now - 50))
    assert (await bapi.client.post("/webhooks/stripe", content=body, headers=sig)).json()["outcome"] == "processed"
    billing = (await bapi.client.get("/v1/billing", headers=headers)).json()
    assert billing["access"] == "grace" and billing["grace_ends_at"]

    # An older event arriving late never overwrites the newer state.
    body, sig = signed(sub_event(customer, "active", created=now - 75))
    assert (await bapi.client.post("/webhooks/stripe", content=body, headers=sig)).json()["outcome"] == "stale"
    assert (await bapi.client.get("/v1/billing", headers=headers)).json()["access"] == "grace"

    # Card fixed: back to active, grace cleared.
    body, sig = signed(sub_event(customer, "active", created=now - 10))
    await bapi.client.post("/webhooks/stripe", content=body, headers=sig)
    billing = (await bapi.client.get("/v1/billing", headers=headers)).json()
    assert billing["access"] == "active" and billing["grace_ends_at"] is None

    # Canceled, 8 days ago: paused. A new checkout gets no second trial.
    canceled = sub_event(customer, "canceled", created=now)
    body, sig = signed({**canceled, "type": "customer.subscription.deleted"})
    await bapi.client.post("/webhooks/stripe", content=body, headers=sig)
    superuser.execute(
        "update public.subscriptions set lapsed_at = now() - interval '8 days' where tenant_id = %s", [tenant_id]
    )
    assert (await bapi.client.get("/v1/billing", headers=headers)).json()["access"] == "paused"
    r = await bapi.client.post("/v1/billing/checkout", headers=headers)
    assert r.status_code == 200 and bapi.stripe.checkouts[-1]["trial_days"] is None

    statuses = superuser.execute(
        "select status, count(*) from public.billing_events where tenant_id = %s group by 1 order by 1", [tenant_id]
    ).fetchall()
    assert statuses == [("ignored", 1), ("processed", 4)]  # the stale one is kept, marked ignored


async def test_other_event_types_are_stored_and_ignored(
    bapi: BillingHarness, seed: Seed, superuser: psycopg.Connection
) -> None:
    tenant_id, headers = await new_workspace(bapi, seed)
    await bapi.client.post("/v1/billing/checkout", headers=headers)
    customer = await customer_of(superuser, tenant_id)
    event = {
        "id": "evt_" + uuid.uuid4().hex[:20],
        "type": "invoice.paid",
        "created": int(time.time()),
        "data": {"object": {"id": "in_123", "customer": customer}},
    }
    body, sig = signed(event)
    r = await bapi.client.post("/webhooks/stripe", content=body, headers=sig)
    assert r.status_code == 200 and r.json()["outcome"] == "ignored"
    assert (await bapi.client.get("/v1/billing", headers=headers)).json()["access"] == "none"


async def test_portal_needs_a_customer(bapi: BillingHarness, seed: Seed) -> None:
    _, headers = await new_workspace(bapi, seed)
    r = await bapi.client.post("/v1/billing/portal", headers=headers)
    assert r.status_code == 404 and r.json()["error_code"] == "no_customer"
    await bapi.client.post("/v1/billing/checkout", headers=headers)
    r = await bapi.client.post("/v1/billing/portal", headers=headers)
    assert r.status_code == 200 and r.json()["url"].startswith("https://billing.stripe.com/")
