"""Symmetric encryption for secrets at rest (Google refresh tokens).

Refresh tokens are long-lived OAuth credentials; storing them in plaintext in
the database would mean a DB dump hands over every user's Drive. They are
encrypted with Fernet (AES-128-CBC + HMAC-SHA256), keyed by a scrypt-derived
key from ``PORTAL_SECRET_KEY``.
"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_SALT = b"coursework-portal.token-encryption.v1"


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = hashlib.scrypt(
        settings.portal_secret_key.encode(), salt=_SALT, n=2**14, r=8, p=1, dklen=32
    )
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(plaintext: str | None) -> bytes | None:
    if not plaintext:
        return None
    return _fernet().encrypt(plaintext.encode())


def decrypt_secret(ciphertext: bytes | None) -> str | None:
    if not ciphertext:
        return None
    try:
        return _fernet().decrypt(ciphertext).decode()
    except InvalidToken:
        # Secret key changed or the row is corrupt — treat as "not linked".
        return None
