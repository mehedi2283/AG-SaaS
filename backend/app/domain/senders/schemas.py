from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

# Plain check, no extra dependency: the mailbox address as Instantly knows it.
Email = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=320)
]


class SenderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    # The signature block (plain text or simple HTML). Encrypted for the workspace; never returned by the API.
    signature: str | None = Field(default=None, max_length=5000)
    # The only URL allowed in follow-ups and replies (rule 6): an <img> added by code.
    signature_image_url: HttpUrl | None = None
    instantly_eaccount: Email | None = None  # the Instantly mailbox that sends
    heyreach_account_id: str | None = Field(default=None, max_length=100)  # the LinkedIn account in HeyReach
    is_default: bool = False


class SenderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    signature: str | None = Field(default=None, max_length=5000)
    signature_image_url: HttpUrl | None = None
    instantly_eaccount: Email | None = None
    heyreach_account_id: str | None = Field(default=None, max_length=100)
    is_default: bool | None = None


class SenderOut(BaseModel):
    id: UUID
    display_name: str
    title: str | None
    company: str | None
    has_signature: bool
    signature_image_url: str | None
    instantly_eaccount: str | None
    heyreach_account_id: str | None
    is_default: bool
    updated_at: datetime
