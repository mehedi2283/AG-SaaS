"""Instantly API v2 client (email sending layer).

Gazi creates the campaign and its steps; each step's subject/body is the lead's custom variables
custom_subject_{n} / personalization_{n} (CLAUDE.md provider gotchas), so Instantly's sequence sends follow-ups.
Shapes follow Instantly's public v2 API; verify against a live account before relying on them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.providers.http import request

BASE = "https://api.instantly.ai/api/v2"
_DAY_INDEX = {"sun": "0", "mon": "1", "tue": "2", "wed": "3", "thu": "4", "fri": "5", "sat": "6"}


@dataclass(frozen=True)
class CampaignSpec:
    name: str
    sender_email: str
    timezone: str
    days: list[str]
    send_from: str  # "09:00"
    send_to: str
    daily_limit: int
    delays: list[int]  # wait in days before each step (step 1 = 0)


def campaign_body(spec: CampaignSpec) -> dict[str, Any]:
    steps = []
    for index in range(len(spec.delays)):
        n = index + 1
        # Instantly's delay is the wait AFTER this step before the next one.
        wait_after = spec.delays[index + 1] if index + 1 < len(spec.delays) else 0
        steps.append(
            {
                "type": "email",
                "delay": wait_after,
                "variants": [{"subject": f"{{{{custom_subject_{n}}}}}", "body": f"{{{{personalization_{n}}}}}"}],
            }
        )
    return {
        "name": spec.name,
        "campaign_schedule": {
            "schedules": [
                {
                    "name": "Gazi schedule",
                    "timing": {"from": spec.send_from, "to": spec.send_to},
                    "days": {_DAY_INDEX[d]: True for d in spec.days},
                    "timezone": spec.timezone,
                }
            ]
        },
        "sequences": [{"steps": steps}],
        "email_list": [spec.sender_email],
        "daily_limit": spec.daily_limit,
        "stop_on_reply": True,
        "link_tracking": False,
        "open_tracking": False,
        "text_only": False,
    }


class InstantlyClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str) -> None:
        self._http = http
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        return await request(self._http, "instantly", method, f"{BASE}{path}", headers=self._headers, **kw)

    async def create_campaign(self, spec: CampaignSpec) -> str:
        data = await self._call("POST", "/campaigns", json=campaign_body(spec))
        return str(data["id"])

    async def activate(self, campaign_id: str) -> None:
        await self._call("POST", f"/campaigns/{campaign_id}/activate")

    async def pause(self, campaign_id: str) -> None:
        await self._call("POST", f"/campaigns/{campaign_id}/pause")

    async def add_lead(self, campaign_id: str, lead: dict[str, Any], variables: dict[str, str]) -> str:
        body = {
            "campaign": campaign_id,
            "email": lead["email"],
            "first_name": lead.get("first_name"),
            "last_name": lead.get("last_name"),
            "company_name": lead.get("company"),
            "website": lead.get("website"),
            "custom_variables": variables,
            "skip_if_in_campaign": True,
        }
        data = await self._call("POST", "/leads", json={k: v for k, v in body.items() if v is not None})
        return str(data.get("id", "")) if isinstance(data, dict) else ""

    async def reply(self, *, eaccount: str, reply_to_uuid: str, subject: str, html: str, text: str) -> str:
        data = await self._call(
            "POST",
            "/emails/reply",
            json={
                "eaccount": eaccount,
                "reply_to_uuid": reply_to_uuid,
                "subject": subject,
                "body": {"html": html, "text": text},
            },
        )
        return str(data.get("id", "")) if isinstance(data, dict) else ""

    async def check_key(self) -> None:
        await self._call("GET", "/campaigns", params={"limit": 1})

    async def campaign_analytics(self, campaign_id: str, start: str, end: str) -> dict[str, Any]:
        data = await self._call(
            "GET", "/campaigns/analytics", params={"id": campaign_id, "start_date": start, "end_date": end}
        )
        if isinstance(data, list):
            return data[0] if data else {}
        return data or {}
