from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

# active: trialing or paid · grace: lapsed < 7 days, banner only · paused: no launches or AI · none: never subscribed
BillingAccess = Literal["active", "grace", "paused", "none"]


class PlanOut(BaseModel):
    price_id: str
    unit_amount: int | None  # smallest currency unit, e.g. 9900 = $99.00
    currency: str
    interval: str | None
    trial_days: int


class TokensOut(BaseModel):
    """This calendar month's AI allowance (UTC). Over the cap, AI writing and drafting pause."""

    used: int
    cap: int
    included: int
    addon: int
    resets_at: datetime


class PackOut(BaseModel):
    price_id: str
    tokens: int
    unit_amount: int | None
    currency: str
    quantity: int  # how many of this pack the workspace has


class PackChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price_id: str


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
    tokens: TokensOut
    packs: list[PackOut]


class RedirectOut(BaseModel):
    """A Stripe-hosted page to send the browser to."""

    url: str


class StripeWebhookResult(BaseModel):
    ok: bool = True
    outcome: Literal["processed", "ignored", "duplicate", "stale", "unknown_customer"]
