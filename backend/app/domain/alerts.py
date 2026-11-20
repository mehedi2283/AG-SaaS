"""Error alerts to a workspace's owners and admins (v1 scope). One email per tenant and problem per day."""

from __future__ import annotations

import html
from typing import Any
from uuid import UUID

import structlog

from app.db.tenant_tx import as_worker
from app.providers.http import ProviderError
from app.providers.sendgrid import Mailer

log = structlog.get_logger()

# JobFailed error codes that need a person to do something, and what to tell them.
ALERTS: dict[str, tuple[str, str]] = {
    "provider_rejected": (
        "A campaign couldn't be launched or a reply couldn't be sent",
        "Instantly or HeyReach refused a request. Open the campaign in Gazi to see the reason, then check the "
        "account and its API key in Settings.",
    ),
    "llm_quota_exceeded": (
        "This month's AI allowance is used up",
        "Gazi has paused writing sequences and drafting replies until the allowance resets. Add a token pack in "
        "Settings, Billing to continue now.",
    ),
    "billing_paused": (
        "Your workspace is paused",
        "Payment didn't go through, so launches and AI drafting are paused. Update billing in Settings to continue.",
    ),
    "instantly_not_connected": (
        "Instantly isn't connected",
        "Connect Instantly in Settings to launch email campaigns.",
    ),
    "heyreach_not_connected": (
        "HeyReach isn't connected",
        "Connect HeyReach in Settings to launch LinkedIn campaigns.",
    ),
    "apollo_not_connected": ("Apollo isn't connected", "Connect Apollo in Settings to search for buyers."),
}


def mailer(ctx: dict[str, Any]) -> Mailer | None:
    settings = ctx["settings"]
    if settings.sendgrid_api_key is None:
        return None
    return Mailer(ctx["http"], settings.sendgrid_api_key.get_secret_value(), settings.email_from)


async def alert_admins(ctx: dict[str, Any], tenant_id: UUID, error_code: str) -> None:
    if error_code not in ALERTS:
        return
    redis = ctx.get("redis")
    if redis is not None and not await redis.set(f"gazi:alert:{tenant_id}:{error_code}", "1", ex=86_400, nx=True):
        return  # already told them today
    async with as_worker(ctx["pool"], tenant_id) as conn:
        emails = [r["email"] for r in await conn.fetch("select email from app.tenant_admin_emails()")]
        name = await conn.fetchval("select name from public.tenants")
    subject, text = ALERTS[error_code]
    send = mailer(ctx)
    if send is None or not emails:
        log.warning("alert.not_emailed", error_code=error_code, reason="no mailer" if send is None else "no admins")
        return
    body = f"{text}\n\nWorkspace: {name}\n\nYou're getting this because you're an owner or admin in Gazi."
    try:
        await send.send(
            emails,
            f"Gazi: {subject}",
            "".join(f"<p>{html.escape(p)}</p>" for p in body.split("\n\n")),
            body,
        )
    except ProviderError as exc:
        log.error("alert.send_failed", error_code=error_code, provider_status=exc.status)
        return
    log.info("alert.sent", error_code=error_code, recipients=len(emails))
