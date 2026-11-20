"""Shared HTTP handling for provider clients: every failure is classified as retryable or permanent."""

from __future__ import annotations

from typing import Any

import httpx
import structlog

log = structlog.get_logger()


class ProviderError(Exception):
    """A provider call failed. `retryable` errors (429, 5xx, timeouts, network) are worth trying again later."""

    def __init__(self, provider: str, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message
        self.retryable = retryable
        self.status = status
        self.retry_after: float | None = None


async def request(
    client: httpx.AsyncClient,
    provider: str,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json: Any = None,
    params: dict[str, Any] | None = None,
    seconds: float = 30,
) -> Any:
    try:
        response = await client.request(method, url, headers=headers, json=json, params=params, timeout=seconds)
    except httpx.TimeoutException as exc:
        raise ProviderError(provider, "timed out", retryable=True) from exc
    except httpx.HTTPError as exc:
        raise ProviderError(provider, f"unreachable ({type(exc).__name__})", retryable=True) from exc
    if response.status_code == 429 or response.status_code >= 500:
        err = ProviderError(provider, f"HTTP {response.status_code}", retryable=True, status=response.status_code)
        try:
            err.retry_after = float(response.headers.get("retry-after", ""))
        except ValueError:
            err.retry_after = None
        raise err
    if response.status_code >= 400:
        detail = _detail(response)
        log.warning("provider.rejected", provider=provider, status=response.status_code, path=httpx.URL(url).path)
        raise ProviderError(provider, detail, retryable=False, status=response.status_code)
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def _detail(response: httpx.Response) -> str:
    """A short, safe reason for the user (never echoes headers or the request)."""
    if response.status_code in (401, 403):
        return "the API key was rejected; check it in Settings"
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(body, dict):
        for key in ("message", "error", "detail", "title"):
            if isinstance(body.get(key), str):
                return f"HTTP {response.status_code}: {body[key][:200]}"
    return f"HTTP {response.status_code}"
