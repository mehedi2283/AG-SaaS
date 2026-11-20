from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.llm.outputs import Intent


class ReplyOut(BaseModel):
    id: UUID
    channel: str
    status: str
    intent: str | None
    confidence: float | None
    rationale: str | None
    inbound_body: str | None
    draft_body: str | None
    final_body: str | None
    error_code: str | None
    received_at: datetime
    sent_at: datetime | None
    lead_id: UUID | None
    lead_name: str | None
    lead_email: str | None
    lead_company: str | None
    campaign_id: UUID | None
    campaign_name: str | None
    our_message: str | None
    will_add_link: bool  # a calendar link / attachment is added when sending (interested / question only)


class ReplyPage(BaseModel):
    items: list[ReplyOut]
    total: int
    waiting: int  # awaiting approval, for the nav badge


class ReplyEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Intent | None = None
    draft_body: str | None = Field(default=None, min_length=1, max_length=4000)
