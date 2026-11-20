from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import SecretStr

from app.core import auth
from app.core.config import Settings
from app.core.errors import Unauthorized

pytestmark = pytest.mark.anyio

SIGNER = ec.generate_private_key(ec.SECP256R1())
INTRUDER = ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def fake_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    client = SimpleNamespace(get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=SIGNER.public_key()))
    monkeypatch.setattr(auth, "_jwks_client", lambda _url: client)


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": SecretStr("postgresql://x"),
        "gotrue_service_key": SecretStr("x"),
        "kek": SecretStr("x"),
        "jwt_jwks_url": "http://auth/.well-known/jwks.json",
    }
    return Settings(**(values | overrides))


def _claims(**extra: Any) -> dict[str, Any]:
    return {"sub": str(uuid.uuid4()), "aud": "authenticated", "exp": int(time.time()) + 60, **extra}


async def _verify(token: str, settings: Settings) -> auth.Principal:
    key, algorithms = await auth.signing_key(token, settings)
    return auth.verify_token(token, key, algorithms, settings)


async def test_es256_token_from_jwks_is_accepted_with_workspace_claims() -> None:
    tenant = uuid.uuid4()
    token = jwt.encode(_claims(tenant_id=str(tenant), tenant_role="admin"), SIGNER, algorithm="ES256")
    principal = await _verify(token, _settings())
    assert principal.tenant_id == tenant and principal.tenant_role == "admin"


async def test_token_signed_by_another_key_is_rejected() -> None:
    token = jwt.encode(_claims(), INTRUDER, algorithm="ES256")
    with pytest.raises(Unauthorized):
        await _verify(token, _settings())


async def test_hs256_is_refused_when_no_secret_is_configured() -> None:
    token = jwt.encode(_claims(), "any-secret-at-all-long-enough-for-hs256", algorithm="HS256")
    with pytest.raises(Unauthorized):
        await _verify(token, _settings())


async def test_unsigned_token_is_refused() -> None:
    token = jwt.encode(_claims(), None, algorithm="none")
    with pytest.raises(Unauthorized):
        await _verify(token, _settings(jwt_secret=SecretStr("s" * 40)))


def test_settings_need_a_verifier() -> None:
    with pytest.raises(ValueError):
        _settings(jwt_jwks_url=None)
