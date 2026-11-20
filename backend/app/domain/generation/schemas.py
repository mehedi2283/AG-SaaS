from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class GenerateOut(BaseModel):
    queued_leads: int


class MessageOut(BaseModel):
    id: UUID
    lead_id: UUID
    lead_email: str
    lead_name: str | None
    lead_company: str | None
    step: int
    channel: str
    subject: str | None
    body: str | None
    status: str
    validation_errors: list[Any]
    error_code: str | None
    model: str
    prompt_version: int
    updated_at: datetime


class MessagePage(BaseModel):
    items: list[MessageOut]
    total: int


class MessageEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str | None = Field(default=None, max_length=150)
    body: str = Field(min_length=1, max_length=4000)
