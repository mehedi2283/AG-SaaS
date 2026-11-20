"""Envelope encryption: a KEK (Docker secret) wraps one data key (DEK) per tenant; the DEK encrypts secrets.

Every ciphertext is bound to its tenant and purpose through AES-GCM associated data, so a ciphertext
copied to another tenant or another row fails to decrypt. Decrypt only here, only in memory.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.errors import InternalError

NONCE_BYTES = 12
_DEK_AAD = b"gazi:dek"


class DecryptionFailed(InternalError):
    code = "decryption_failed"


@dataclass(frozen=True)
class Sealed:
    ciphertext: bytes
    nonce: bytes


def aad(tenant_id: UUID, purpose: str) -> bytes:
    """Associated data: e.g. aad(tenant, "integration:instantly")."""
    return f"{tenant_id}:{purpose}".encode()


class Crypto:
    def __init__(self, keks: dict[int, bytes], current_version: int) -> None:
        if current_version not in keks:
            raise ValueError("current KEK version is not loaded")
        for version, key in keks.items():
            if len(key) != 32 or not 0 < version < 256:
                raise ValueError("each KEK must be 32 bytes with a version between 1 and 255")
        self._keks = keks
        self._version = current_version

    @classmethod
    def from_base64(cls, kek_b64: str, version: int) -> Crypto:
        return cls({version: base64.b64decode(kek_b64, validate=True)}, version)

    def new_wrapped_dek(self) -> bytes:
        """A fresh tenant data key, wrapped: version byte + nonce + ciphertext."""
        return self._wrap(AESGCM.generate_key(bit_length=256))

    def _wrap(self, dek: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        sealed = AESGCM(self._keks[self._version]).encrypt(nonce, dek, _DEK_AAD)
        return bytes([self._version]) + nonce + sealed

    def _unwrap(self, wrapped: bytes) -> bytes:
        version, nonce, sealed = wrapped[0], wrapped[1 : 1 + NONCE_BYTES], wrapped[1 + NONCE_BYTES :]
        key = self._keks.get(version)
        if key is None:
            raise DecryptionFailed("This data key was wrapped with a key-encryption key that isn't loaded.")
        try:
            return AESGCM(key).decrypt(nonce, sealed, _DEK_AAD)
        except InvalidTag as exc:
            raise DecryptionFailed("The tenant data key could not be unwrapped.") from exc

    def encrypt(self, wrapped_dek: bytes, plaintext: bytes, associated: bytes) -> Sealed:
        nonce = os.urandom(NONCE_BYTES)
        return Sealed(AESGCM(self._unwrap(wrapped_dek)).encrypt(nonce, plaintext, associated), nonce)

    def decrypt(self, wrapped_dek: bytes, sealed: Sealed, associated: bytes) -> bytes:
        try:
            return AESGCM(self._unwrap(wrapped_dek)).decrypt(sealed.nonce, sealed.ciphertext, associated)
        except InvalidTag as exc:
            raise DecryptionFailed("A stored secret could not be decrypted.") from exc
