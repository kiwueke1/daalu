"""Unit tests for the standalone daalu CLI.

Run from the repo root with the cli dir on the path:

    PYTHONPATH=cli pytest cli/tests -q
"""

from __future__ import annotations

import json

import httpx
import respx
from daalu_cli import agent, auth
from daalu_cli.config import (
    Credentials,
    clear_credentials,
    load_credentials,
    save_credentials,
)

# ── config ───────────────────────────────────────────────────────────────────


def test_credentials_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("DAALU_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("DAALU_HOST", raising=False)
    monkeypatch.delenv("DAALU_TOKEN", raising=False)
    save_credentials(Credentials(host="https://ops.daalu.io/", token="dpat_x", model="m"))
    loaded = load_credentials()
    assert loaded is not None
    assert loaded.host_clean == "https://ops.daalu.io"
    assert loaded.token == "dpat_x"
    assert loaded.model == "m"
    # File is 0600 on POSIX.
    mode = (tmp_path / "credentials.json").stat().st_mode & 0o777
    assert mode == 0o600
    assert clear_credentials() is True
    assert load_credentials() is None


def test_env_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DAALU_CONFIG_DIR", str(tmp_path))
    (tmp_path / "credentials.json").write_text(json.dumps({"host": "h", "token": "t"}))
    monkeypatch.setenv("DAALU_HOST", "https://env-host")
    monkeypatch.setenv("DAALU_TOKEN", "dpat_env")
    loaded = load_credentials()
    assert loaded.host == "https://env-host"
    assert loaded.token == "dpat_env"


# ── agent tool parsing + safety ──────────────────────────────────────────────


def test_extract_tool_request():
    tool, tail = agent._extract_tool("sure\n\ndaalu:read src/app.py\n")
    assert tool == ("read", "src/app.py")
    none, _ = agent._extract_tool("just chatting, no tools here")
    assert none is None


def test_safe_path_blocks_escape(tmp_path):
    sess = agent._Session(root=tmp_path, client=None, model="m")  # type: ignore[arg-type]
    assert agent._safe_path(sess, "ok.txt") == (tmp_path / "ok.txt")
    assert agent._safe_path(sess, "../escape.txt") is None


def test_read_tool_returns_contents(tmp_path):
    (tmp_path / "hello.txt").write_text("hi there")
    sess = agent._Session(root=tmp_path, client=None, model="m")  # type: ignore[arg-type]
    out = agent._read(sess, "hello.txt")
    assert "hi there" in out


# ── login (express --code) ───────────────────────────────────────────────────


@respx.mock
def test_login_with_code_stores_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DAALU_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("DAALU_HOST", raising=False)
    monkeypatch.delenv("DAALU_TOKEN", raising=False)
    host = "https://ops.daalu.io"
    respx.post(f"{host}/api/v1/cli/device/token").mock(
        return_value=httpx.Response(
            200, json={"status": "approved", "access_token": "dpat_abc", "host": host}
        )
    )
    respx.get(f"{host}/api/v1/coding/config").mock(
        return_value=httpx.Response(200, json={"default_model": "qwen/x", "models": []})
    )
    rc = auth.login(host, code="dvc_live_test")
    assert rc == 0
    creds = load_credentials()
    assert creds.token == "dpat_abc"
    assert creds.model == "qwen/x"


@respx.mock
def test_login_with_expired_code_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("DAALU_CONFIG_DIR", str(tmp_path))
    host = "https://ops.daalu.io"
    respx.post(f"{host}/api/v1/cli/device/token").mock(
        return_value=httpx.Response(200, json={"status": "expired"})
    )
    assert auth.login(host, code="dvc_live_old") == 1
