from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

LeadStatus = Literal["new", "generating", "ready", "pushed", "replied", "unsubscribed", "bounced", "do_not_contact"]
Resolution = Literal["skip", "update_existing", "add_anyway"]


class ImportOut(BaseModel):
    id: UUID
    campaign_id: UUID
    original_filename: str
    status: str
    total_rows: int
    imported_rows: int
    failed_rows: int
    duplicate_rows: int
    pending_duplicates: int
    errors: list[dict[str, Any]]
    error_code: str | None
    file_kept_until: datetime | None  # None once the file is purged
    created_at: datetime
    finished_at: datetime | None


class DuplicateOut(BaseModel):
    id: UUID
    incoming_email: str
    incoming: dict[str, Any]
    reason: Literal["exists_in_tenant", "repeated_in_file"]
    resolution: str
    existing_lead_id: UUID | None
    existing_campaign: str | None
    existing_name: str | None
    resolved_at: datetime | None


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolution: Resolution
    ids: list[UUID] | None = Field(default=None, max_length=1000)
    all_pending: bool = False  # apply to every pending duplicate of this import

    @model_validator(mode="after")
    def _target(self) -> ResolveIn:
        if not self.all_pending and not self.ids:
            raise ValueError("give ids, or set all_pending")
        return self


class ResolveOut(BaseModel):
    resolved: int
    added: int
    updated: int
    skipped: int
    not_possible: int  # e.g. "add anyway" for an email already in the same campaign


class LeadOut(BaseModel):
    id: UUID
    campaign_id: UUID
    email: str
    first_name: str | None
    last_name: str | None
    title: str | None
    company: str | None
    linkedin_url: str | None
    website: str | None
    industry: str | None
    city: str | None
    country: str | None
    company_size: int | None
    status: str
    source: str
    buyer_score: int | None
    created_at: datetime


class LeadPage(BaseModel):
    items: list[LeadOut]
    total: int


class LeadUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    first_name: str | None = Field(default=None, max_length=500)
    last_name: str | None = Field(default=None, max_length=500)
    title: str | None = Field(default=None, max_length=500)
    company: str | None = Field(default=None, max_length=500)
    status: Literal["do_not_contact", "new"] | None = None  # people can block or unblock a lead; the rest is automatic
