"""Registry of every ARQ job. Each takes tenant_id first and is wrapped in @tenant_job."""

from __future__ import annotations

from typing import Any

from app.domain.campaigns.launch import launch_campaign, set_campaign_running
from app.domain.generation.jobs import generate_campaign, generate_lead_sequence
from app.domain.leads.importer import import_leads
from app.domain.replies.jobs import draft_reply, handle_webhook_event, send_reply

ALL: list[Any] = [
    import_leads,
    generate_campaign,
    generate_lead_sequence,
    launch_campaign,
    set_campaign_running,
    handle_webhook_event,
    draft_reply,
    send_reply,
]
