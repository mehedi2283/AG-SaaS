from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

# active: trialing or paid · grace: lapsed < 7 days, banner only · paused: no launches or AI · none: never subscribed
BillingAccess = Literal["active", "grace", "paused", "none"]


class PlanOut(BaseModel):
    price_id: str
    unit_amount: int | None  # smallest currency unit, e.g. 9900 = $99.00
    currency: str
    interval: str | None
    trial_days: int


class BillingOut(BaseModel):
    enabled: bool
    access: BillingAccess
    status: str
    trial_end: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    grace_ends_at: datetime | None
    trial_available: bool
    plan: PlanOut | None


class RedirectOut(BaseModel):
    """A Stripe-hosted page to send the browser to."""

    url: str


class StripeWebhookResult(BaseModel):
    ok: bool = True
    outcome: Literal["processed", "ignored", "duplicate", "stale", "unknown_customer"]
