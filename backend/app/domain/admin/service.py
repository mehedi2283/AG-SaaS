"""Superuser actions that need more than one step (GoTrue, Stripe). Simple ones go straight to the repo."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import asyncpg
import structlog

from app.core.auth import Principal
from app.core.config import Settings
from app.core.errors import Conflict
from app.domain.admin import repo
from app.domain.admin.schemas import AdminTenant, AdminUser
from app.providers.gotrue import AuthAdmin
from app.providers.stripe import BillingProvider

log = structlog.get_logger()


async def set_free_access(
    conn: asyncpg.Connection,
    settings: Settings,
    billing: BillingProvider | None,
    tenant_id: UUID,
    until: datetime | None,
) -> AdminTenant:
    """Free access until a date. A workspace still on its Stripe trial also gets the trial moved to that date,
    so Stripe doesn't charge the card during the free period. A paying subscription keeps being billed."""
    current = await repo.tenant(conn, tenant_id, settings.included_tokens)
    await repo.set_free_access(conn, tenant_id, until)  # validates the date and logs
    if (
        until is not None
        and billing is not None
        and settings.billing_enabled
        and current.subscription_status == "trialing"
        and current.stripe_subscription_id
        and (current.trial_end is None or current.trial_end < until)
    ):
        await billing.set_trial_end(current.stripe_subscription_id, until)
        await repo.log(
            conn,
            "workspace.trial_extended",
            tenant_id,
            None,
            {"from": current.trial_end.isoformat() if current.trial_end else None, "to": until.isoformat()},
        )
    return await repo.tenant(conn, tenant_id, settings.included_tokens)


async def set_login_enabled(
    conn: asyncpg.Connection, principal: Principal, auth_admin: AuthAdmin, user_id: UUID, enabled: bool
) -> AdminUser:
    target = await repo.user(conn, user_id)
    if not enabled and (user_id == principal.user_id or target.is_platform_admin):
        raise Conflict("Superusers can't be disabled here (that includes you).", code="cannot_disable_superuser")
    await auth_admin.set_login_enabled(user_id, enabled)
    await repo.log(conn, "user.login_enabled" if enabled else "user.login_disabled", None, user_id, {})
    log.info("admin.login_changed", target_user_id=str(user_id), enabled=enabled)
    # The ban reaches public.users through the auth.users trigger; report the state we just set.
    return target.model_copy(
        update={"login_enabled": enabled, "banned_until": None if enabled else target.banned_until or datetime.now(UTC)}
    )
