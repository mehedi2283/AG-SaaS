"""Apollo API client (per-workspace key): people search, then email reveal (people/bulk_match, uses credits)."""

from __future__ import annotations

from typing import Any

import httpx

from app.providers.http import request

BASE = "https://api.apollo.io/api/v1"


class ApolloClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str) -> None:
        self._http = http
        self._headers = {"X-Api-Key": api_key, "Content-Type": "application/json", "Cache-Control": "no-cache"}

    async def search_people(self, filters: dict[str, Any], page: int, per_page: int) -> list[dict[str, Any]]:
        data = await request(
            self._http,
            "apollo",
            "POST",
            f"{BASE}/mixed_people/api_search",
            headers=self._headers,
            json={**filters, "page": page, "per_page": per_page},
        )
        people = (data or {}).get("people") or []
        return [p for p in people if isinstance(p, dict)]

    async def reveal(self, person_ids: list[str]) -> list[dict[str, Any]]:
        """Up to 10 per call. Returns matched people with work emails where Apollo has one."""
        data = await request(
            self._http,
            "apollo",
            "POST",
            f"{BASE}/people/bulk_match",
            headers=self._headers,
            json={"details": [{"id": pid} for pid in person_ids[:10]], "reveal_personal_emails": False},
        )
        matches = (data or {}).get("matches") or []
        return [m for m in matches if isinstance(m, dict)]

    async def check_key(self) -> None:
        await request(self._http, "apollo", "GET", f"{BASE}/auth/health", headers=self._headers)
