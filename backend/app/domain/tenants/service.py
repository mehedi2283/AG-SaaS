"""Workspace AI settings (auto-reply switch, model choice), stored in tenants.settings."""

from __future__ import annotations

from typing import Any

import asyncpg

from app.core.auth import TenantContext
from app.core.config import Settings
from app.core.errors import Forbidden, Unprocessable
from app.domain.audit.repo import record
from app.domain.tenants import repo
from app.domain.tenants.schemas import AiSettings, AiSettingsOut, AiSettingsUpdate

_KEYS = ("auto_reply", "writing_model", "classify_model")


def resolve_ai_settings(stored: dict[str, Any], settings: Settings) -> AiSettings:
    """Stored choices with defaults filled in. A model that's no longer offered falls back to the default.

    Shared by the API and workers, so both see the same effective settings.
    """
    allowed = settings.allowed_models
    writing, classify = stored.get("writing_model"), stored.get("classify_model")
    return AiSettings(
        auto_reply=bool(stored.get("auto_reply", False)),  # off until a person turns it on
        writing_model=writing if writing in allowed else settings.default_writing_model,
        classify_model=classify if classify in allowed else settings.default_classify_model,
    )


async def get_ai_settings(conn: asyncpg.Connection, settings: Settings) -> AiSettingsOut:
    resolved = resolve_ai_settings(await repo.tenant_settings(conn), settings)
    return AiSettingsOut(**resolved.model_dump(), available_models=settings.allowed_models)


async def save_ai_settings(
    conn: asyncpg.Connection, ctx: TenantContext, settings: Settings, update: AiSettingsUpdate
) -> AiSettingsOut:
    changes = update.model_dump(exclude_none=True)
    for key in ("writing_model", "classify_model"):
        if key in changes and changes[key] not in settings.allowed_models:
            raise Unprocessable(f"{changes[key]!r} isn't one of the available models.", code="unknown_model")
    if changes and not await repo.merge_tenant_settings(conn, {k: changes[k] for k in _KEYS if k in changes}):
        raise Forbidden("Only admins can change AI settings.", code="insufficient_role")
    if changes:
        await record(
            conn, ctx, "workspace.ai_settings_changed", target_table="tenants", target_id=ctx.tenant_id, diff=changes
        )
    return await get_ai_settings(conn, settings)
