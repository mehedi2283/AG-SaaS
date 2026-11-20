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

    # Uploaded CSVs: a volume shared by the API and the import worker, kept 7 days (rule 8).
    imports_dir: str = "/data/imports"
    max_import_bytes: int = 50 * 1024 * 1024  # Nginx allows 50m on /v1/lead-imports

    # Where the web app lives; Stripe sends people back here after checkout and the billing portal.
    app_url: str = "http://localhost:3000"

    # Stripe (platform account, one key). Billing is on only when the key and price are set; production
    # refuses to start without them, so a missing setting can never mean "everything is free".
    stripe_secret_key: SecretStr | None = None
    stripe_webhook_secret: SecretStr | None = None
    stripe_price_id: str | None = None
    trial_days: int = 14
    # Monthly token packs on top of the base plan, as "price_id:tokens,price_id:tokens".
    stripe_token_packs: str = ""

    # OpenAI: ONE platform key (rule 2), from GAZI_OPENAI_API_KEY or the Docker secret gazi_openai_api_key.
    openai_api_key: SecretStr | None = None

    # SendGrid (one platform account) for weekly reports and alerts. Without a key, reports are still written and
    # stored, and marked as not emailed.
    sendgrid_api_key: SecretStr | None = None
    email_from: str = "reports@gazi.app"

    # AI allowance per workspace and calendar month (UTC), before packs. Applies with or without billing.
    included_tokens: int = 2_000_000
    # Models a workspace may choose from (comma-separated), and the defaults for new workspaces.
    llm_models: str = "gpt-5.5,gpt-5.4-mini,gpt-4o-mini"
    default_writing_model: str = "gpt-5.5"
    default_classify_model: str = "gpt-4o-mini"

    @property
    def billing_enabled(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_price_id)

    @property
    def token_packs(self) -> dict[str, int]:
        """price_id -> tokens a month, in the configured order."""
        packs: dict[str, int] = {}
        for entry in filter(None, (e.strip() for e in self.stripe_token_packs.split(","))):
            price_id, _, tokens = entry.partition(":")
            packs[price_id.strip()] = int(tokens)
        return packs

    @property
    def allowed_models(self) -> list[str]:
        return [m.strip() for m in self.llm_models.split(",") if m.strip()]

    @model_validator(mode="after")
    def _one_token_verifier(self) -> Settings:
        if not self.jwt_jwks_url and not self.jwt_secret:
            raise ValueError("set GAZI_JWT_JWKS_URL (ES256/RS256) and/or GAZI_JWT_SECRET (HS256)")
        if self.environment == "production" and not (self.billing_enabled and self.stripe_webhook_secret):
            raise ValueError(
                "production needs GAZI_STRIPE_SECRET_KEY, GAZI_STRIPE_PRICE_ID and GAZI_STRIPE_WEBHOOK_SECRET"
            )
        try:
            packs = self.token_packs
        except ValueError as exc:
            raise ValueError("GAZI_STRIPE_TOKEN_PACKS must look like price_abc:2000000,price_def:5000000") from exc
        if any(tokens <= 0 for tokens in packs.values()):
            raise ValueError("every token pack needs a positive token count")
        for model in (self.default_writing_model, self.default_classify_model):
            if model not in self.allowed_models:
                raise ValueError(f"default model {model!r} is not in GAZI_LLM_MODELS")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from the environment


def app_settings(request: Request) -> Settings:
    """The settings the running app was built with (use this in dependencies, not get_settings)."""
    settings: Settings = request.app.state.settings
    return settings
