from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr

Provider = Literal["instantly", "heyreach", "apollo", "calendly"]


class ApiKeyConfig(BaseModel):
    """Instantly, HeyReach and Apollo: one API key."""

    model_config = ConfigDict(extra="forbid")
    api_key: SecretStr = Field(min_length=8, max_length=500)


class CalendlyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    booking_url: HttpUrl
    api_token: SecretStr = Field(min_length=8, max_length=2000)


class IntegrationOut(BaseModel):
    provider: Provider
    key_last4: str | None
    status: Literal["unverified", "active", "invalid"]
    last_verified_at: datetime | None
    last_error: str | None
    updated_at: datetime
