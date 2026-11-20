from __future__ import annotations

from datetime import datetime, time
from typing import Literal
from uuid import UUID
from zoneinfo import available_timezones

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from app.domain.senders.schemas import Email

Channel = Literal["email", "linkedin"]
Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_ZONES = available_timezones()
WEEKDAYS: tuple[Day, ...] = ("mon", "tue", "wed", "thu", "fri")


class _Schedule(BaseModel):
    @field_validator("timezone", check_fields=False)
    @classmethod
    def _zone(cls, value: str | None) -> str | None:
        if value is not None and value not in _ZONES:
            raise ValueError("unknown time zone")
        return value

    @field_validator("attachment_url", check_fields=False)
    @classmethod
    def _https(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and value.scheme != "https":
            raise ValueError("the attachment link must start with https://")
        return value


class CampaignIn(_Schedule):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    channel: Channel
    offer_summary: str | None = Field(default=None, max_length=4000)
    offer_details: str | None = Field(default=None, max_length=12000)
    attachment_url: HttpUrl | None = None
    report_emails: list[Email] = Field(default_factory=list, max_length=10)
    timezone: str = "UTC"
    sending_days: list[Day] = Field(default_factory=lambda: list(WEEKDAYS), min_length=1)
    send_from: time = time(9, 0)
    send_to: time = time(17, 0)
    daily_limit: int = Field(default=50, ge=1, le=1000)
    sender_identity_id: UUID | None = None
    sequence_count: int = Field(default=5, ge=1, le=10)

    @model_validator(mode="after")
    def _window(self) -> CampaignIn:
        if self.send_from >= self.send_to:
            raise ValueError("send_from must be before send_to")
        return self


class CampaignUpdate(_Schedule):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    offer_summary: str | None = Field(default=None, max_length=4000)
    offer_details: str | None = Field(default=None, max_length=12000)
    attachment_url: HttpUrl | None = None
    report_emails: list[Email] | None = Field(default=None, max_length=10)
    timezone: str | None = None
    sending_days: list[Day] | None = Field(default=None, min_length=1)
    send_from: time | None = None
    send_to: time | None = None
    daily_limit: int | None = Field(default=None, ge=1, le=1000)
    sender_identity_id: UUID | None = None


class CampaignCounts(BaseModel):
    leads: int
    new: int  # not written yet
    generating: int
    ready: int  # every step written, not yet pushed
    failed_messages: int
    pushed: int
    replied: int
    pending_duplicates: int
    bookings: int


class CampaignOut(BaseModel):
    id: UUID
    name: str
    channel: Channel
    status: str
    launch_status: str
    launch_error: str | None
    launched_at: datetime | None
    offer_summary: str | None
    offer_details: str | None
    attachment_url: str | None
    report_emails: list[str]
    timezone: str
    sending_days: list[str]
    send_from: time
    send_to: time
    daily_limit: int
    sender_identity_id: UUID | None
    sequence_count: int
    counts: CampaignCounts
    created_at: datetime
    updated_at: datetime


class StepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: int = Field(ge=1, le=10)
    delay_days: int = Field(ge=0, le=60)
    angle: str = Field(min_length=1, max_length=500)


class StepOut(StepIn):
    pass


class StepsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[StepIn] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def _shape(self) -> StepsIn:
        numbers = [s.step for s in self.steps]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("steps must be numbered 1, 2, 3… in order")
        if self.steps[0].delay_days != 0 or any(s.delay_days == 0 for s in self.steps[1:]):
            raise ValueError("step 1 goes out at once (0 days); every later step needs a delay of 1 day or more")
        return self
