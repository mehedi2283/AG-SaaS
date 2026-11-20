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
from app.core.errors import Conflict, NotFound, PaymentRequired, ServiceUnavailable, Unprocessable
from app.db.tenant_tx import as_worker
from app.domain.audit.repo import record
from app.domain.billing import repo
from app.domain.billing.schemas import (
    BillingAccess,
    BillingOut,
    PackOut,
    PlanOut,
    RedirectOut,
    StripeWebhookResult,
    TokensOut,
)
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


def _next_month_start(now: datetime) -> datetime:
    return datetime(now.year + now.month // 12, now.month % 12 + 1, 1, tzinfo=UTC)


async def tokens(conn: asyncpg.Connection, settings: Settings, *, worker: bool = False) -> TokensOut:
    addon = await repo.addon_tokens(conn) if settings.billing_enabled else 0
    return TokensOut(
        used=await repo.month_usage(conn, worker=worker),
        cap=settings.included_tokens + addon,
        included=settings.included_tokens,
        addon=addon,
        resets_at=_next_month_start(datetime.now(UTC)),
    )


async def ensure_token_budget(conn: asyncpg.Connection, settings: Settings, *, worker: bool = True) -> TokensOut:
    """Check before every LLM call (rule 2). Over the monthly cap -> llm_quota_exceeded; nothing else pauses."""
    budget = await tokens(conn, settings, worker=worker)
    if budget.used >= budget.cap:
        raise PaymentRequired(
            "This month's AI allowance is used up. Add a token pack, or wait until it resets.",
            code="llm_quota_exceeded",
            extra={"tokens_used": budget.used, "token_cap": budget.cap, "resets_at": budget.resets_at.isoformat()},
        )
    return budget


async def _packs(settings: Settings, provider: BillingProvider, owned: list[dict[str, Any]]) -> list[PackOut]:
    counts = {str(a.get("price_id")): int(a.get("quantity") or 0) for a in owned}
    packs = []
    for price_id, pack_tokens in settings.token_packs.items():
        price = await provider.get_price(price_id)
        packs.append(
            PackOut(
                price_id=price_id,
                tokens=pack_tokens,
                unit_amount=price.unit_amount,
                currency=price.currency,
                quantity=counts.get(price_id, 0),
            )
        )
    return packs


async def get_billing(conn: asyncpg.Connection, settings: Settings, billing: BillingProvider | None) -> BillingOut:
    sub = await repo.subscription(conn)
    allowance = await tokens(conn, settings)
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
            tokens=allowance,
            packs=[],
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
        tokens=allowance,
        packs=await _packs(settings, billing, sub["addons"] if sub else []),
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


async def change_pack(
    conn: asyncpg.Connection,
    ctx: TenantContext,
    settings: Settings,
    billing: BillingProvider | None,
    price_id: str,
    *,
    add: bool,
) -> None:
    """Add or remove one token pack on the subscription (prorated, charged now). The webhook records the result.

    Adding a pack during the free trial ends the trial: paid tokens start a paid subscription.
    """
    provider = _require(settings, billing)
    if price_id not in settings.token_packs:
        raise Unprocessable("That isn't one of the token packs.", code="unknown_pack")
    sub = await repo.subscription(conn)
    if not sub or not sub["stripe_subscription_id"] or sub["status"] not in ("trialing", "active"):
        raise Conflict("Token packs need an active subscription. Fix billing first.", code="subscription_not_active")

    items = await provider.subscription_items(sub["stripe_subscription_id"])
    current = next((i for i in items if i.price_id == price_id), None)
    change: dict[str, Any]
    if add:
        change = {"id": current.id, "quantity": current.quantity + 1} if current else {"price": price_id, "quantity": 1}
    elif current is None:
        raise NotFound("This workspace doesn't have that token pack.", code="pack_not_found")
    elif current.quantity > 1:
        change = {"id": current.id, "quantity": current.quantity - 1}
    else:
        change = {"id": current.id, "deleted": True}
    end_trial = add and sub["status"] == "trialing"
    await provider.change_items(sub["stripe_subscription_id"], [change], end_trial=end_trial)
    await record(
        conn,
        ctx,
        "billing.token_pack_added" if add else "billing.token_pack_removed",
        target_table="subscriptions",
        diff={"price_id": price_id, "tokens": settings.token_packs[price_id], "ended_trial": end_trial},
    )


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


def _subscription_fields(obj: dict[str, Any], settings: Settings) -> dict[str, Any]:
    items = [i for i in ((obj.get("items") or {}).get("data") or []) if isinstance(i, dict)]
    packs = settings.token_packs

    def price_of(item: dict[str, Any]) -> str | None:
        return (item.get("price") or {}).get("id")

    base = next((i for i in items if price_of(i) == settings.stripe_price_id), None)
    base = base or next((i for i in items if price_of(i) not in packs), items[0] if items else {})
    counts: dict[str, int] = {}
    for item in items:
        price_id = price_of(item)
        if price_id in packs:
            counts[price_id] = counts.get(price_id, 0) + int(item.get("quantity") or 1)
    addons = [{"price_id": p, "quantity": q} for p, q in counts.items()]
    # Newer Stripe API versions carry the billing period on the item, older ones on the subscription.
    period_end = obj.get("current_period_end") or base.get("current_period_end")
    return {
        "subscription_id": str(obj["id"]),
        "status": str(obj["status"]),
        "price_id": price_of(base),
        "addon_tokens": sum(packs[p] * q for p, q in counts.items()),
        "addons": addons,
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
                created = datetime.fromtimestamp(int(event["created"]), UTC)
                fields = _subscription_fields(obj, settings)
                applied = await repo.apply_subscription(conn, **fields, event_created=created)
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
