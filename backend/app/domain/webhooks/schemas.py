from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

WebhookProvider = Literal["instantly", "heyreach", "calendly"]


class EndpointCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: WebhookProvider
    # Instantly and Calendly can send a custom header; when true, requests must carry X-Gazi-Secret.
    require_secret: bool = False


class EndpointOut(BaseModel):
    id: UUID
    provider: WebhookProvider
    token_prefix: str
    has_secret: bool
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None


class EndpointCreated(EndpointOut):
    """Returned once. Gazi stores only a hash of the token and an encrypted copy of the secret."""

    url_path: str
    secret: str | None


class IngestResult(BaseModel):
    ok: bool = True
    duplicate: bool
