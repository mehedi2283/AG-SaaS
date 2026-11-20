"""HeyReach public API client (LinkedIn sending layer).

Gazi creates the lead list, adds leads with custom fields personalization_{n}, creates the campaign with its
schedule and sequence (connection request, then messages with delays) and starts it, as the n8n workflow did
(docs/status.md). Conversation lookups and replies are scoped by the SENDING ACCOUNT id (CLAUDE.md gotchas).
Endpoint shapes follow HeyReach's public API; verify against a live account before relying on them.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.providers.http import request

BASE = "https://api.heyreach.io/api/public"
_DAYS = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}


def lead_body(lead: dict[str, Any], variables: dict[str, str]) -> dict[str, Any]:
    return {
        "firstName": lead.get("first_name") or "",
        "lastName": lead.get("last_name") or "",
        "profileUrl": lead["linkedin_url"],
        "companyName": lead.get("company") or "",
        "position": lead.get("title") or "",
        "emailAddress": lead.get("email") or "",
        "customUserFields": [{"name": k, "value": v} for k, v in variables.items()],
    }


def sequence_body(delays: list[int]) -> list[dict[str, Any]]:
    """Step 1 is the connection request; later steps are messages after a delay. Text comes from custom fields."""
    steps: list[dict[str, Any]] = [{"type": "ConnectionRequest", "message": "{personalization_1}", "delayInDays": 0}]
    for index, delay in enumerate(delays[1:], start=2):
        steps.append({"type": "Message", "message": f"{{personalization_{index}}}", "delayInDays": delay})
    return steps


class HeyReachClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str) -> None:
        self._http = http
        self._headers = {"X-API-KEY": api_key, "Content-Type": "application/json", "Accept": "application/json"}

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        return await request(self._http, "heyreach", method, f"{BASE}{path}", headers=self._headers, **kw)

    async def check_key(self) -> None:
        await self._call("GET", "/auth/CheckApiKey")

    async def create_list(self, name: str) -> str:
        data = await self._call("POST", "/list/CreateEmptyList", json={"name": name, "type": "USER_LIST"})
        return str(data["id"])

    async def add_leads(self, list_id: str, leads: list[dict[str, Any]]) -> None:
        for start in range(0, len(leads), 100):  # HeyReach accepts up to 100 per call
            await self._call(
                "POST", "/list/AddLeadsToListV2", json={"listId": int(list_id), "leads": leads[start : start + 100]}
            )

    async def create_campaign(
        self,
        *,
        name: str,
        list_id: str,
        account_id: str,
        days: list[str],
        send_from: str,
        send_to: str,
        timezone: str,
        daily_limit: int,
    ) -> str:
        data = await self._call(
            "POST",
            "/campaign/Create",
            json={
                "name": name,
                "listId": int(list_id),
                "linkedInAccountIds": [int(account_id)] if account_id.isdigit() else [account_id],
                "schedule": {
                    "days": [_DAYS[d] for d in days],
                    "startTime": send_from,
                    "endTime": send_to,
                    "timezone": timezone,
                },
                "dailyLimit": daily_limit,
            },
        )
        return str(data["id"] if isinstance(data, dict) else data)

    async def update_sequence(self, campaign_id: str, delays: list[int]) -> None:
        await self._call(
            "POST", "/campaign/UpdateSequence", json={"campaignId": int(campaign_id), "steps": sequence_body(delays)}
        )

    async def start(self, campaign_id: str) -> None:
        await self._call("POST", "/campaign/StartCampaign", params={"campaignId": campaign_id})

    async def pause(self, campaign_id: str) -> None:
        await self._call("POST", "/campaign/Pause", params={"campaignId": campaign_id})

    async def resume(self, campaign_id: str) -> None:
        await self._call("POST", "/campaign/Resume", params={"campaignId": campaign_id})

    async def send_message(self, *, account_id: str, conversation_id: str, message: str) -> None:
        await self._call(
            "POST",
            "/inbox/SendMessage",
            json={
                "message": message,
                "subject": "",
                "conversationId": conversation_id,
                "linkedInAccountId": int(account_id) if account_id.isdigit() else account_id,
            },
        )
