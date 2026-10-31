"""Encryption for stored OAuth tokens (Fernet: AES-128-CBC + HMAC-SHA256)."""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from .logging_setup import get_logger

log = get_logger("crypto")


class VaultError(Exception):
    pass


class TokenVault:
    def __init__(self, key: str | None):
        if not key:
            # Ephemeral key: fine for local sample mode, tokens do not survive a restart.
            key = Fernet.generate_key().decode()
            self.ephemeral = True
            log.warning("TOKEN_ENCRYPTION_KEY not set, using an ephemeral key")
        else:
            self.ephemeral = False
        try:
            self._f = Fernet(key.encode() if isinstance(key, str) else key)
        except Exception as exc:  # bad key format
            raise VaultError("TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc

    def encrypt(self, plaintext: str) -> str:
        return self._f.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._f.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise VaultError("Stored token cannot be decrypted with the current key") from exc
