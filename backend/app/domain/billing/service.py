"""Workspace billing: Stripe Checkout to subscribe, the Stripe portal to manage, webhooks to record state.

Subscription state is written only from signature-verified Stripe events. The webhook is applied inline
rather than queued: it is one row update with no provider or LLM call, so it stays well inside Stripe's
timeout, and a failure returns 503 so Stripe retries (the stored event is then processed again).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import asyncpg
import structlog

from app.core.auth import TenantContext
from app.core.config import Settings
from app.core.errors import Conflict, NotFound, PaymentRequired, ServiceUnavailable
from app.db.tenant_tx import as_worker
from app.domain.audit.repo import record
from app.domain.billing import repo
from app.domain.billing.schemas import BillingAccess, BillingOut, PlanOut, RedirectOut, StripeWebhookResult
from app.providers.stripe import BillingProvider

log = structlog.get_logger()

SUBSCRIPTION_EVENTS = {
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "customer.subscription.paused",
    "customer.subscription.resumed",
}
# A subscription in one of these states can't be restarted through Checkout; it is managed in the portal.
_LIVE = {"trialing", "active", "past_due", "unpaid", "paused", "incomplete"}


def _require(settings: Settings, billing: BillingProvider | None) -> BillingProvider:
    if billing is None or not settings.billing_enabled:
        raise ServiceUnavailable("Billing isn't set up on this server yet.", code="billing_not_configured")
    return billing


async def get_billing(conn: asyncpg.Connection, settings: Settings, billing: BillingProvider | None) -> BillingOut:
    sub = await repo.subscription(conn)
    if billing is None or not settings.billing_enabled:
        # Local development without Stripe keys: nothing is gated.
        return BillingOut(
            enabled=False,
            access="active",
            status="none",
            trial_end=None,
            current_period_end=None,
            cancel_at_period_end=False,
            grace_ends_at=None,
            trial_available=False,
            plan=None,
        )
    access, grace_ends_at = await repo.access(conn)
    assert settings.stripe_price_id is not None
    price = await billing.get_price(settings.stripe_price_id)
    return BillingOut(
        enabled=True,
        access=cast(BillingAccess, access),  # the SQL function returns only these four values
        status=sub["status"] if sub else "none",
        trial_end=sub["trial_end"] if sub else None,
        current_period_end=sub["current_period_end"] if sub else None,
        cancel_at_period_end=bool(sub and sub["cancel_at_period_end"]),
        grace_ends_at=grace_ends_at,
        trial_available=not (sub and sub["stripe_subscription_id"]),
        plan=PlanOut(
            price_id=price.id,
            unit_amount=price.unit_amount,
            currency=price.currency,
            interval=price.interval,
            trial_days=settings.trial_days,
        ),
    )


async def start_checkout(
    conn: asyncpg.Connection, ctx: TenantContext, settings: Settings, billing: BillingProvider | None
) -> RedirectOut:
    provider = _require(settings, billing)
    sub = await repo.subscription(conn)
    if sub and sub["stripe_subscription_id"] and sub["status"] in _LIVE:
        raise Conflict(
            "This workspace already has a subscription. Use Manage billing to change it.", code="already_subscribed"
        )

    customer_id = sub["stripe_customer_id"] if sub else None
    if customer_id is None:
        name = await repo.tenant_name(conn) or str(ctx.tenant_id)
        customer_id = await repo.save_customer(conn, await provider.create_customer(ctx.tenant_id, name))

    # One free trial per workspace: a workspace that subscribed before starts paying straight away.
    trial_days = settings.trial_days if not (sub and sub["stripe_subscription_id"]) else None
    assert settings.stripe_price_id is not None
    base = settings.app_url.rstrip("/")
    url = await provider.create_checkout_session(
        tenant_id=ctx.tenant_id,
        customer_id=customer_id,
        price_id=settings.stripe_price_id,
        trial_days=trial_days,
        success_url=f"{base}/settings?billing=success",
        cancel_url=f"{base}/settings?billing=canceled",
    )
    await record(conn, ctx, "billing.checkout_started", target_table="subscriptions", diff={"trial_days": trial_days})
    return RedirectOut(url=url)


async def open_portal(
    conn: asyncpg.Connection, ctx: TenantContext, settings: Settings, billing: BillingProvider | None
) -> RedirectOut:
    provider = _require(settings, billing)
    sub = await repo.subscription(conn)
    if sub is None:
        raise NotFound("This workspace has no billing account yet. Start the free trial first.", code="no_customer")
    url = await provider.create_portal_session(sub["stripe_customer_id"], f"{settings.app_url.rstrip('/')}/settings")
    await record(conn, ctx, "billing.portal_opened", target_table="subscriptions")
    return RedirectOut(url=url)


async def ensure_access(conn: asyncpg.Connection, settings: Settings) -> None:
    """Gate for launches and AI use (API and workers). Grace still passes; paused and none do not."""
    if not settings.billing_enabled:
        return
    access, _ = await repo.access(conn)
    if access == "none":
        raise PaymentRequired("Start the free trial to use this.", code="subscription_required")
    if access == "paused":
        raise PaymentRequired(
            "This workspace is paused because payment didn't go through. Update billing to continue.",
            code="billing_paused",
        )


# ---------------------------------------------------------------------------
# Stripe webhook
# ---------------------------------------------------------------------------


def _ts(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if value else None


def _subscription_fields(obj: dict[str, Any]) -> dict[str, Any]:
    items = (obj.get("items") or {}).get("data") or [{}]
    first = items[0] if isinstance(items[0], dict) else {}
    # Newer Stripe API versions carry the billing period on the item, older ones on the subscription.
    period_end = obj.get("current_period_end") or first.get("current_period_end")
    return {
        "subscription_id": str(obj["id"]),
        "status": str(obj["status"]),
        "price_id": (first.get("price") or {}).get("id"),
        "trial_end": _ts(obj.get("trial_end")),
        "current_period_end": _ts(period_end),
        "cancel_at_period_end": bool(obj.get("cancel_at_period_end")),
    }


async def handle_stripe_webhook(
    pool: asyncpg.Pool, billing: BillingProvider | None, settings: Settings, body: bytes, signature: str | None
) -> StripeWebhookResult:
    provider = _require(settings, billing)
    event = provider.parse_event(body, signature)
    event_id, event_type = str(event.get("id", "")), str(event.get("type", ""))
    obj: dict[str, Any] = (event.get("data") or {}).get("object") or {}
    structlog.contextvars.bind_contextvars(stripe_event_id=event_id, stripe_event_type=event_type)

    customer = obj.get("customer")
    customer_id = customer.get("id") if isinstance(customer, dict) else customer
    if not customer_id:
        log.info("stripe.event_without_customer")
        return StripeWebhookResult(outcome="ignored")

    async with as_worker(pool, None) as conn:
        tenant_id = await repo.resolve_customer(conn, str(customer_id))
    if tenant_id is None:
        # Not a Gazi customer (or the account is shared with another product). 200 so Stripe stops retrying.
        log.warning("stripe.unknown_customer")
        return StripeWebhookResult(outcome="unknown_customer")
    structlog.contextvars.bind_contextvars(tenant_id=str(tenant_id))

    async with as_worker(pool, tenant_id) as conn:
        row_id, status = await repo.store_event(conn, event_id, event_type, event)
        if status in ("processed", "ignored"):
            log.info("stripe.duplicate_event")
            return StripeWebhookResult(outcome="duplicate")
        if event_type not in SUBSCRIPTION_EVENTS:
            await repo.mark_event(conn, row_id, "ignored")
            return StripeWebhookResult(outcome="ignored")
        failed: Exception | None = None
        applied = False
        try:
            async with conn.transaction():  # savepoint: a failure leaves the event row to mark as failed
                applied = await repo.apply_subscription(
                    conn, **_subscription_fields(obj), event_created=datetime.fromtimestamp(int(event["created"]), UTC)
                )
        except (asyncpg.PostgresError, KeyError, TypeError, ValueError) as exc:
            log.error("stripe.apply_failed", error_type=type(exc).__name__, error_code="stripe_event_invalid")
            await repo.mark_event(conn, row_id, "failed", "stripe_event_invalid")
            failed = exc
        else:
            await repo.mark_event(conn, row_id, "processed" if applied else "ignored", None if applied else "stale")
    if failed is not None:
        # Stripe retries; the stored event (status failed) is processed again on the next delivery.
        raise ServiceUnavailable("The Stripe event couldn't be applied.", code="stripe_event_failed") from failed
    log.info("stripe.event_applied" if applied else "stripe.stale_event")
    return StripeWebhookResult(outcome="processed" if applied else "stale")
