"""Encryption helpers.

Every byte of student PII that is cached on the cloud host or written to the
vault is encrypted with a Fernet (AES-128-CBC + HMAC) key held in secrets.
The plaintext key master copy is kept university-side. Losing the key means the
cached/vaulted data is unrecoverable — which is the point.
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from .config import load_config


class CryptoError(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = (load_config().fernet_key or "").strip()
    if not key:
        raise CryptoError(
            "this app has no encryption key, so student data cannot be stored. "
            "Setup is not finished: whoever installed PeerParley needs to add a "
            "fernet_key to the app's secrets (DEPLOYMENT.md, 'Encryption key', "
            "has the two lines they need)."
        )
    try:
        return Fernet(key.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(f"Invalid fernet_key: {exc}") from exc


def encrypt_bytes(data: bytes) -> bytes:
    return _fernet().encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    try:
        return _fernet().decrypt(token)
    except InvalidToken as exc:
        raise CryptoError("Could not decrypt — wrong key or corrupt data.") from exc


