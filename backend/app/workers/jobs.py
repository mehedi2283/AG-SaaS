"""Registry of every ARQ job. Each takes tenant_id first and is wrapped in @tenant_job."""

from __future__ import annotations

from typing import Any

from app.domain.generation.jobs import generate_campaign, generate_lead_sequence
from app.domain.leads.importer import import_leads

ALL: list[Any] = [import_leads, generate_campaign, generate_lead_sequence]
