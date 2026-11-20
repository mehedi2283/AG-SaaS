"""Stripe client (platform account, one key). Used for checkout, the customer portal and webhook signatures.

Errors are classified: rate limits, connection failures and Stripe 5xx are retryable; everything else
(bad request, auth, permission) is permanent. Neither ever carries the API key into logs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

import stripe
import structlog

from app.core.errors import Unauthorized, UpstreamError

log = structlog.get_logger()

_RETRYABLE = (stripe.RateLimitError, stripe.APIConnectionError, stripe.APIError)


@dataclass(frozen=True)
class PriceInfo:
    id: str
    unit_amount: int | None
    currency: str
    interval: str | None


class BillingProvider(Protocol):
    async def create_customer(self, tenant_id: UUID, name: str) -> str: ...

    async def create_checkout_session(
        self,
        *,
        tenant_id: UUID,
        customer_id: str,
        price_id: str,
        trial_days: int | None,
        success_url: str,
        cancel_url: str,
    ) -> str: ...

    async def create_portal_session(self, customer_id: str, return_url: str) -> str: ...

    async def get_price(self, price_id: str) -> PriceInfo: ...

    def parse_event(self, body: bytes, signature: str | None) -> dict[str, Any]: ...


def _fail(op: str, exc: stripe.StripeError) -> UpstreamError:
    retryable = isinstance(exc, _RETRYABLE)
    log.error(
        "stripe.request_failed",
        op=op,
        retryable=retryable,
        error_type=type(exc).__name__,
        http_status=exc.http_status,
        stripe_code=exc.code,
    )
    if retryable:
        return UpstreamError("Stripe didn't respond. Try again in a moment.", code="stripe_unavailable")
    return UpstreamError("Stripe rejected the request. Check the billing settings.", code="stripe_rejected")


class StripeBilling:
    def __init__(self, api_key: str, webhook_secret: str | None) -> None:
        self._client = stripe.StripeClient(api_key, http_client=stripe.HTTPXClient(), max_network_retries=2)
        self._webhook_secret = webhook_secret
        self._prices: dict[str, PriceInfo] = {}

    async def create_customer(self, tenant_id: UUID, name: str) -> str:
        # No email: Checkout asks for the billing email itself, so receipts go where the payer wants.
        params: dict[str, Any] = {"name": name, "metadata": {"gazi_tenant_id": str(tenant_id)}}
        try:
            # Same key for 24h -> same customer, so a double click never makes two customers.
            customer = await self._client.v1.customers.create_async(
                params,  # type: ignore[arg-type]
                {"idempotency_key": f"gazi-customer-{tenant_id}"},
            )
        except stripe.StripeError as exc:
            raise _fail("customers.create", exc) from exc
        return str(customer.id)

    async def create_checkout_session(
        self,
        *,
        tenant_id: UUID,
        customer_id: str,
        price_id: str,
        trial_days: int | None,
        success_url: str,
        cancel_url: str,
    ) -> str:
        subscription_data: dict[str, Any] = {"metadata": {"gazi_tenant_id": str(tenant_id)}}
        if trial_days:
            subscription_data["trial_period_days"] = trial_days
        params: dict[str, Any] = {
            "mode": "subscription",
            "customer": customer_id,
            "client_reference_id": str(tenant_id),
            "line_items": [{"price": price_id, "quantity": 1}],
            "payment_method_collection": "always",  # card up front, even with a trial
            "subscription_data": subscription_data,
            "success_url": success_url,
            "cancel_url": cancel_url,
        }
        try:
            session = await self._client.v1.checkout.sessions.create_async(params)  # type: ignore[arg-type]
        except stripe.StripeError as exc:
            raise _fail("checkout.sessions.create", exc) from exc
        if not session.url:
            raise UpstreamError("Stripe didn't return a checkout page.", code="stripe_rejected")
        return str(session.url)

    async def create_portal_session(self, customer_id: str, return_url: str) -> str:
        try:
            session = await self._client.v1.billing_portal.sessions.create_async(
                {"customer": customer_id, "return_url": return_url}
            )
        except stripe.StripeError as exc:
            raise _fail("billing_portal.sessions.create", exc) from exc
        return str(session.url)

    async def get_price(self, price_id: str) -> PriceInfo:
        if price_id not in self._prices:
            try:
                price = await self._client.v1.prices.retrieve_async(price_id)
            except stripe.StripeError as exc:
                raise _fail("prices.retrieve", exc) from exc
            recurring = price.recurring
            self._prices[price_id] = PriceInfo(
                id=price.id,
                unit_amount=price.unit_amount,
                currency=price.currency,
                interval=recurring.interval if recurring else None,
            )
        return self._prices[price_id]

    def parse_event(self, body: bytes, signature: str | None) -> dict[str, Any]:
        """Verify the Stripe-Signature header (HMAC, constant-time, 5-minute tolerance), then parse."""
        if not self._webhook_secret or not signature:
            log.warning("stripe.webhook_unsigned")
            raise Unauthorized("Missing Stripe signature.", code="bad_stripe_signature")
        try:
            stripe.WebhookSignature.verify_header(body.decode(), signature, self._webhook_secret)
        except (stripe.SignatureVerificationError, UnicodeDecodeError) as exc:
            log.warning("stripe.webhook_bad_signature")
            raise Unauthorized("Invalid Stripe signature.", code="bad_stripe_signature") from exc
        event: dict[str, Any] = json.loads(body)
        return event
