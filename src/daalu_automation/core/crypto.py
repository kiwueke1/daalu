"""At-rest encryption for sensitive material the platform stores in
the database (WireGuard private keys today, anything similar later).

The key is derived from ``settings.secret_key`` so installs that already
rotate ``secret_key`` invalidate every encrypted column — which is the
behaviour we want: a fresh ``secret_key`` should not be able to read
old ciphertext. Operators who want to rotate without losing data must
re-encrypt before swapping the key.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from daalu_automation.config import get_settings


def _fernet() -> Fernet:
    # Fernet wants a 32-byte url-safe-base64 key; derive deterministically
    # from secret_key so multiple processes agree without needing a
    # separate FERNET_KEY env var.
    digest = hashlib.sha256(get_settings().secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as e:  # pragma: no cover — defensive
        raise ValueError(
            "ciphertext does not decrypt with the current secret_key "
            "(was secret_key rotated without re-encrypting?)"
        ) from e
