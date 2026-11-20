from __future__ import annotations

import base64
import os
import uuid

import pytest

from app.core.crypto import Crypto, DecryptionFailed, Sealed, aad
from app.core.logging import REDACTED, redact_secrets


def _crypto(version: int = 1) -> Crypto:
    return Crypto.from_base64(base64.b64encode(os.urandom(32)).decode(), version)


def test_round_trip_and_binding_to_tenant_and_purpose() -> None:
    crypto, tenant, other = _crypto(), uuid.uuid4(), uuid.uuid4()
    dek = crypto.new_wrapped_dek()
    sealed = crypto.encrypt(dek, b"hr_live_key", aad(tenant, "integration:heyreach"))
    assert crypto.decrypt(dek, sealed, aad(tenant, "integration:heyreach")) == b"hr_live_key"
    for wrong in (aad(other, "integration:heyreach"), aad(tenant, "integration:instantly")):
        with pytest.raises(DecryptionFailed):
            crypto.decrypt(dek, sealed, wrong)


def test_tampering_and_foreign_keys_fail() -> None:
    crypto, tenant = _crypto(), uuid.uuid4()
    dek = crypto.new_wrapped_dek()
    sealed = crypto.encrypt(dek, b"secret", aad(tenant, "x"))
    flipped = Sealed(bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:], sealed.nonce)
    with pytest.raises(DecryptionFailed):
        crypto.decrypt(dek, flipped, aad(tenant, "x"))
    with pytest.raises(DecryptionFailed):
        _crypto().decrypt(dek, sealed, aad(tenant, "x"))  # different KEK


def test_kek_must_be_32_bytes() -> None:
    with pytest.raises(ValueError):
        Crypto({1: b"short"}, 1)


def test_log_redaction_covers_nested_secrets() -> None:
    event = {
        "event": "integration.saved",
        "api_key": "sk-123",
        "headers": {"Authorization": "Bearer abc", "accept": "json"},
        "items": [{"token": "t"}, {"name": "ok"}],
        "provider": "instantly",
    }
    out = redact_secrets(None, "info", event)
    assert out["api_key"] == REDACTED and out["headers"]["Authorization"] == REDACTED
    assert out["headers"]["accept"] == "json" and out["items"][0]["token"] == REDACTED
    assert out["provider"] == "instantly" and out["event"] == "integration.saved"
