"""Settings from environment variables (GAZI_*) and Docker secret files (/run/secrets/gazi_*)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from fastapi import Request
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_SECRETS_DIR = Path(os.environ.get("GAZI_SECRETS_DIR", "/run/secrets"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GAZI_",
        secrets_dir=_SECRETS_DIR if _SECRETS_DIR.is_dir() else None,
        extra="ignore",
    )

    environment: str = "development"
    log_level: str = "INFO"

    # Postgres login role: member of `authenticated` and `gazi_worker`, never superuser or BYPASSRLS.
    database_url: SecretStr
    redis_url: str = "redis://localhost:6379/0"

    # Supabase Auth (GoTrue). The JWT secret verifies user tokens; the service key is used ONLY for the
    # GoTrue admin API (setting app_metadata.active_tenant_id), never for tenant data.
    jwt_secret: SecretStr
    jwt_audience: str = "authenticated"
    gotrue_url: str = "http://auth:9999"
    gotrue_service_key: SecretStr

    # Key-encryption key: base64 of 32 random bytes. Wraps each tenant's data key.
    kek: SecretStr
    kek_version: int = 1

    max_webhook_bytes: int = 512_000


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from the environment


def app_settings(request: Request) -> Settings:
    """The settings the running app was built with (use this in dependencies, not get_settings)."""
    settings: Settings = request.app.state.settings
    return settings
