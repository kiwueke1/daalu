"""Helpers for the `daalu login` device flow.

Pure functions for minting + hashing the two codes a device authorization
carries. Kept separate from the router so they're trivially unit-testable and
so the hashing convention lives in exactly one place.

* ``device_code`` — the secret the CLI holds. High-entropy, namespaced
  ``dvc_live_`` so it's recognisable in logs/configs. Stored only as a sha256
  hash (same discipline as PATs in ``core/auth``).
* ``user_code`` — the short code a human reads off the terminal and into the
  browser. Drawn from an unambiguous alphabet (no 0/O/1/I/L) and formatted
  ``XXXX-XXXX`` so it's easy to type without mistakes.
"""

from __future__ import annotations

import hashlib
import secrets

DEVICE_CODE_PREFIX = "dvc_live_"

# Unambiguous — drops 0/O/1/I/L and vowels that form words by accident.
_USER_CODE_ALPHABET = "BCDFGHJKMNPQRSTVWXZ23456789"
_USER_CODE_LEN = 8  # rendered as two groups of four


def generate_device_code() -> str:
    """A fresh cleartext device code (the CLI's secret)."""
    return f"{DEVICE_CODE_PREFIX}{secrets.token_urlsafe(32)}"


def hash_device_code(code: str) -> str:
    """sha256 hex of a device code — what we persist + look up by."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def generate_user_code() -> str:
    """A short, human-typable code formatted ``XXXX-XXXX`` (upper-case)."""
    raw = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(_USER_CODE_LEN))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_user_code(value: str) -> str:
    """Canonicalise a user-supplied code for lookup.

    Upper-cases, strips whitespace, and re-inserts the dash so
    ``wdjbmjht``, ``WDJB MJHT`` and ``WDJB-MJHT`` all match the stored form.
    """
    cleaned = "".join(ch for ch in value.upper() if ch.isalnum())
    if len(cleaned) == _USER_CODE_LEN:
        return f"{cleaned[:4]}-{cleaned[4:]}"
    return value.strip().upper()
