"""Credential + config storage for the daalu CLI.

The login flow drops a small JSON file at ``~/.daalu/credentials.json`` (mode
0600 on POSIX). Everything the CLI needs after login lives there: which Daalu
instance to talk to, the bearer token, and the default model.

Environment overrides (for CI / ephemeral shells) take precedence over the
file: ``DAALU_HOST`` and ``DAALU_TOKEN``.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path

DEFAULT_HOST = "https://ops.daalu.io"


def config_dir() -> Path:
    """``~/.daalu`` — honoured across POSIX and Windows (``%USERPROFILE%``)."""
    override = os.environ.get("DAALU_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".daalu"


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


@dataclass(frozen=True)
class Credentials:
    host: str
    token: str
    model: str | None = None

    @property
    def host_clean(self) -> str:
        return self.host.rstrip("/")


def load_credentials() -> Credentials | None:
    """Return stored credentials, with env vars layered on top.

    ``DAALU_HOST`` / ``DAALU_TOKEN`` can fully substitute the file (useful in
    CI where you inject a PAT directly and never run ``daalu login``).
    """
    env_host = os.environ.get("DAALU_HOST")
    env_token = os.environ.get("DAALU_TOKEN")

    data: dict = {}
    path = credentials_path()
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}

    host = env_host or data.get("host")
    token = env_token or data.get("token")
    if not host or not token:
        return None
    return Credentials(host=host, token=token, model=data.get("model"))


def save_credentials(creds: Credentials) -> Path:
    """Persist credentials to ``~/.daalu/credentials.json`` with mode 0600."""
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = credentials_path()
    payload = {"host": creds.host_clean, "token": creds.token}
    if creds.model:
        payload["model"] = creds.model
    path.write_text(json.dumps(payload, indent=2) + "\n")
    try:  # best-effort on POSIX; no-op semantics on Windows
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return path


def update_model(creds: Credentials, model: str | None) -> Credentials:
    updated = replace(creds, model=model)
    save_credentials(updated)
    return updated


def clear_credentials() -> bool:
    """Delete the credentials file. Returns True if a file was removed."""
    path = credentials_path()
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
