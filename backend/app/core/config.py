"""Settings from environment variables (GAZI_*) and Docker secret files (/run/secrets/gazi_*)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from fastapi import Request
from pydantic import SecretStr, model_validator
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

    # Supabase Auth (GoTrue). Current Supabase signs tokens with asymmetric keys (ES256) published at
    # <auth url>/.well-known/jwks.json; set jwt_jwks_url to that. jwt_secret verifies legacy HS256 tokens.
    # The service key is used ONLY for the GoTrue admin API (app_metadata.active_tenant_id), never for data.
    jwt_jwks_url: str | None = None
    jwt_secret: SecretStr | None = None
    jwt_audience: str = "authenticated"
    gotrue_url: str = "http://auth:9999"
    gotrue_service_key: SecretStr

    # Key-encryption key: base64 of 32 random bytes. Wraps each tenant's data key.
    kek: SecretStr
    kek_version: int = 1

    max_webhook_bytes: int = 512_000

    # Where the web app lives; Stripe sends people back here after checkout and the billing portal.
    app_url: str = "http://localhost:3000"

    # Stripe (platform account, one key). Billing is on only when the key and price are set; production
    # refuses to start without them, so a missing setting can never mean "everything is free".
    stripe_secret_key: SecretStr | None = None
    stripe_webhook_secret: SecretStr | None = None
    stripe_price_id: str | None = None
    trial_days: int = 14

    @property
    def billing_enabled(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_price_id)

    @model_validator(mode="after")
    def _one_token_verifier(self) -> Settings:
        if not self.jwt_jwks_url and not self.jwt_secret:
            raise ValueError("set GAZI_JWT_JWKS_URL (ES256/RS256) and/or GAZI_JWT_SECRET (HS256)")
        if self.environment == "production" and not (self.billing_enabled and self.stripe_webhook_secret):
            raise ValueError(
                "production needs GAZI_STRIPE_SECRET_KEY, GAZI_STRIPE_PRICE_ID and GAZI_STRIPE_WEBHOOK_SECRET"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from the environment


def app_settings(request: Request) -> Settings:
    """The settings the running app was built with (use this in dependencies, not get_settings)."""
    settings: Settings = request.app.state.settings
    return settings
