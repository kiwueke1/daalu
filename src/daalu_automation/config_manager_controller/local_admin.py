"""Break-glass local admin credentials for an NV-CM environment.

Every human-facing NV-CM tool authenticates through the hub: the tool proxy
validates the Daalu session and injects a Keycloak Bearer, and the tools trust
that. Nautobot is the one component in the stack that *also* ships its own
Django login (``/login/``), and the chart bootstraps a superuser for it. An
audit of the 1.3.0 chart found nothing comparable elsewhere — Temporal Web,
config-store, render, ZTP, DHCP, MCP and the config-browser UI are all
gateway-JWT-only, and the NATS/Redis passwords are infrastructure credentials
with no login surface. So "the local admin account" means the Nautobot one.

Upstream generates that password randomly per install and never surfaces it,
which leaves an operator with no way in when SSO itself is what's broken —
precisely when they most need one. This module lets the person creating the
environment choose the credential, stores it encrypted on the row (the
``secrets_ciphertext`` bag, which exists for exactly this), and hands it back
once at creation.

Ordinary users never touch this: they are already signed in to Daalu and are
SSO'd into the tools by the proxy. This is the operator's break-glass path.
"""

from __future__ import annotations

import json
import re
import secrets
import string
from typing import Any

from daalu_automation.core.crypto import decrypt_secret, encrypt_secret

# Keys inside the encrypted bag. Namespaced by tool so the bag can grow if a
# future component gains a local login without ambiguity.
_USER_KEY = "nautobot_admin_username"
_PASS_KEY = "nautobot_admin_password"
# The chart-generated Nautobot superuser API token, mirrored off the env
# namespace's ``nautobot-token`` Secret so the hub can authenticate SoT /
# bulk-import calls without re-reading the cluster.
_API_TOKEN_KEY = "nautobot_api_token"

DEFAULT_ADMIN_USERNAME = "admin"

# Django usernames: letters, digits, and @/./+/-/_ (see Django's
# UnicodeUsernameValidator). Enforced here so a bad value fails at the API
# boundary with a clear message rather than inside the Nautobot bootstrap job,
# where it surfaces as an opaque CrashLoop.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9@.+_-]{1,150}$")

# The password is injected into the bootstrap job via a Secret and is also
# hashed by Django's sha512_crypt, so it has no shell-expansion exposure. The
# floor is about resisting credential stuffing on an internet-reachable login.
MIN_PASSWORD_LEN = 12

_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*-_=+"


class LocalAdminError(ValueError):
    """Invalid operator-supplied local admin credential."""


def generate_password(length: int = 28) -> str:
    """A strong password for when the creator didn't choose one."""
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def validate(username: str | None, password: str | None) -> tuple[str, str]:
    """Normalise and check an operator-supplied credential pair.

    Either may be omitted: the username falls back to the chart's ``admin``,
    and an omitted password is generated (and returned, so the caller can show
    it once). Raises :class:`LocalAdminError` on a value Nautobot would reject.
    """
    user = (username or "").strip() or DEFAULT_ADMIN_USERNAME
    if not _USERNAME_RE.match(user):
        raise LocalAdminError(
            "admin_username may only contain letters, digits and @ . + - _ "
            "(max 150 characters)"
        )

    pw = password or ""
    if not pw:
        return user, generate_password()
    if len(pw) < MIN_PASSWORD_LEN:
        raise LocalAdminError(
            f"admin_password must be at least {MIN_PASSWORD_LEN} characters"
        )
    return user, pw


def to_ciphertext(username: str, password: str) -> str:
    """Encrypt the credential bag for storage on the row."""
    return encrypt_secret(json.dumps({_USER_KEY: username, _PASS_KEY: password}))


def from_row(row: Any) -> tuple[str, str]:
    """Read (username, password) back off a row.

    Tolerates rows written before this bag existed — those hold a bare random
    token rather than JSON, and legacy releases generated the Nautobot password
    inside the installer. Such a row reports the chart default username and an
    empty password, which callers treat as "leave the installer's generated
    value alone" rather than as a credential to stamp.
    """
    blob = getattr(row, "secrets_ciphertext", None)
    if not blob:
        return DEFAULT_ADMIN_USERNAME, ""
    try:
        data = json.loads(decrypt_secret(blob))
    except Exception:
        # Undecryptable (rotated SECRET_KEY) or pre-JSON legacy value. Never
        # fail provisioning over a break-glass credential we can regenerate.
        return DEFAULT_ADMIN_USERNAME, ""
    if not isinstance(data, dict):
        return DEFAULT_ADMIN_USERNAME, ""
    return (
        str(data.get(_USER_KEY) or DEFAULT_ADMIN_USERNAME),
        str(data.get(_PASS_KEY) or ""),
    )


def _bag_from_row(row: Any) -> dict[str, str]:
    """The decrypted bag, or ``{}`` for empty/legacy/undecryptable rows."""
    blob = getattr(row, "secrets_ciphertext", None)
    if not blob:
        return {}
    try:
        data = json.loads(decrypt_secret(blob))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def api_token_from_row(row: Any) -> str:
    """The mirrored Nautobot API token, or ``""`` when not stamped yet."""
    return str(_bag_from_row(row).get(_API_TOKEN_KEY) or "")


def with_api_token(row: Any, token: str) -> str:
    """Re-encrypt the row's bag with the Nautobot API token merged in.

    Preserves the break-glass credential keys; a legacy/undecryptable bag is
    replaced rather than kept (its old value was unreadable anyway).
    """
    bag = _bag_from_row(row)
    bag[_API_TOKEN_KEY] = token
    return encrypt_secret(json.dumps(bag))
