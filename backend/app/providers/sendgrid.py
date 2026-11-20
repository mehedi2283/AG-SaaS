"""SendGrid (one platform account) for weekly reports and alerts. Off when GAZI_SENDGRID_API_KEY is unset."""

from __future__ import annotations

import httpx

from app.providers.http import request

URL = "https://api.sendgrid.com/v3/mail/send"


class Mailer:
    def __init__(self, http: httpx.AsyncClient, api_key: str, sender: str, sender_name: str = "Gazi") -> None:
        self._http = http
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._from = {"email": sender, "name": sender_name}

    async def send(self, to: list[str], subject: str, html: str, text: str) -> None:
        await request(
            self._http,
            "sendgrid",
            "POST",
            URL,
            headers=self._headers,
            json={
                "personalizations": [{"to": [{"email": e} for e in to]}],
                "from": self._from,
                "subject": subject,
                "content": [{"type": "text/plain", "value": text}, {"type": "text/html", "value": html}],
                "tracking_settings": {"click_tracking": {"enable": False}, "open_tracking": {"enable": False}},
            },
        )
