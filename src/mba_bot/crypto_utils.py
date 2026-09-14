"""Encryption at rest for customer exchange API credentials.

Symmetric encryption (Fernet: AES-128-CBC + HMAC) keyed by `ENCRYPTION_KEY`,
an operator secret that lives only in the deployment environment, never in
the database. Losing this key means every stored credential becomes
unrecoverable - back it up outside the database.
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class EncryptionNotConfigured(RuntimeError):
    pass


class DecryptionFailed(RuntimeError):
    pass


def generate_key() -> str:
    """Run once per deployment: `python -m mba_bot.crypto_utils` prints one."""
    return Fernet.generate_key().decode("utf-8")


def _fernet(key: str | None) -> Fernet:
    if not key:
        raise EncryptionNotConfigured("ENCRYPTION_KEY is not set - refusing to handle API credentials")
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise EncryptionNotConfigured(f"ENCRYPTION_KEY is not a valid Fernet key: {exc}") from exc


def encrypt(plaintext: str, *, key: str | None) -> str:
    return _fernet(key).encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str, *, key: str | None) -> str:
    try:
        return _fernet(key).decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise DecryptionFailed("could not decrypt credential - wrong key or corrupted data") from exc


if __name__ == "__main__":
    print(generate_key())
