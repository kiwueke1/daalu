"""Cloud-account config: sealed secrets, cache invalidation, role test, copilot tools.

DB-free: ``cloud_config.load`` is monkeypatched where a stored row is needed.
"""

from __future__ import annotations

import sys
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from daalu_automation.core import cloud_aws, cloud_azure, cloud_config, cloud_gcp

TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


# ── sealing ──────────────────────────────────────────────────────────────


def test_seal_and_open_round_trip():
    cfg = {"access_key_id": "AKIA1", "secret_access_key": "s3cret", "region": "eu-west-1"}
    sealed = cloud_config.seal("aws", cfg)
    assert "secret_access_key" not in sealed
    assert sealed["secret_access_key_ciphertext"] != "s3cret"
    assert sealed["access_key_id"] == "AKIA1"
    assert cloud_config.open_config("aws", sealed) == cfg


def test_gcp_json_object_is_sealed_as_text():
    sealed = cloud_config.seal("gcp", {"project_id": "p", "service_account_json": {"type": "sa"}})
    opened = cloud_config.open_config("gcp", sealed)
    assert opened["service_account_json"] == '{"type": "sa"}'


def test_write_path_seals_all_three_providers():
    from daalu_automation.api.routers.integrations import _seal_credential_config

    assert "client_secret" not in _seal_credential_config("azure", {"client_secret": "x"})
    assert "session_token" not in _seal_credential_config("aws", {"session_token": "t"})
    assert "service_account_json" not in _seal_credential_config("gcp", {"service_account_json": "{}"})
    # Non-cloud providers keep their own mapping.
    assert "password_ciphertext" in _seal_credential_config("ssh_credentials", {"password": "p"})


def test_ciphertext_is_redacted_on_read():
    from daalu_automation.api.routers.integrations import _redact

    out = _redact("aws", cloud_config.seal("aws", {"access_key_id": "A", "secret_access_key": "s"}))
    assert out["secret_access_key_ciphertext"] == "***"
    assert out["access_key_id"] == "A"


def test_onboarding_counts_ciphertext_as_configured():
    from daalu_automation.api.routers.onboarding import _missing_for

    sealed = cloud_config.seal(
        "aws", {"access_key_id": "A", "secret_access_key": "s", "region": "us-east-1"}
    )
    assert _missing_for("aws", sealed) == []
    assert _missing_for("aws", {"access_key_id": "A", "region": "r"}) == ["secret_access_key"]


@pytest.mark.asyncio
async def test_legacy_plaintext_row_is_sealed_on_read(monkeypatch):
    row = types.SimpleNamespace(config={"tenant_id": "t", "client_id": "c", "client_secret": "plain",
                                        "subscription_id": "s"})
    commits = []

    class _Result:
        def scalar_one_or_none(self):
            return row

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, _stmt):
            return _Result()

        async def commit(self):
            commits.append(1)

        async def rollback(self):  # pragma: no cover
            pass

    import daalu_automation.database as db_mod

    monkeypatch.setattr(db_mod, "AsyncSessionLocal", lambda: _DB())
    cfg, fp = await cloud_config.load(TENANT, "azure")
    assert cfg["client_secret"] == "plain"
    assert "client_secret" not in row.config and "client_secret_ciphertext" in row.config
    assert commits == [1]
    assert fp == cloud_config.fingerprint(row.config)


# ── cache invalidation ───────────────────────────────────────────────────


def _fake_load(store: dict):
    async def load(_tenant, provider):
        cfg = store[provider]
        return cloud_config.open_config(provider, cfg), cloud_config.fingerprint(cfg)

    return load


@pytest.mark.asyncio
async def test_aws_session_rebuilt_after_key_rotation(monkeypatch):
    store = {"aws": {"access_key_id": "A", "secret_access_key": "old", "region": "us-east-1"}}
    monkeypatch.setattr(cloud_config, "load", _fake_load(store))
    built = []
    monkeypatch.setattr(cloud_aws, "build_session", lambda cfg, region: (built.append(cfg["secret_access_key"]) or object(), None))
    monkeypatch.setattr(cloud_aws, "_session_cache", {})

    s1 = await cloud_aws._session(TENANT)
    assert await cloud_aws._session(TENANT) is s1  # cached
    store["aws"] = {**store["aws"], "secret_access_key": "new"}
    s2 = await cloud_aws._session(TENANT)
    assert s2 is not s1
    assert built == ["old", "new"]


@pytest.mark.asyncio
async def test_aws_assumed_role_refreshed_before_expiry(monkeypatch):
    store = {"aws": {"access_key_id": "A", "secret_access_key": "s", "role_arn": "arn:aws:iam::1:role/r"}}
    monkeypatch.setattr(cloud_config, "load", _fake_load(store))
    soon = datetime.now(timezone.utc) + timedelta(minutes=2)
    calls = []

    def build(cfg, region):
        calls.append(1)
        return object(), soon

    monkeypatch.setattr(cloud_aws, "build_session", build)
    monkeypatch.setattr(cloud_aws, "_session_cache", {})
    await cloud_aws._session(TENANT)
    await cloud_aws._session(TENANT)  # inside the 5-minute margin → rebuilt
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_gcp_and_azure_creds_rebuilt_after_rotation(monkeypatch):
    store = {
        "gcp": {"project_id": "p", "service_account_json": '{"k": 1}'},
        "azure": {"tenant_id": "t", "client_id": "c", "client_secret": "old", "subscription_id": "s"},
    }
    monkeypatch.setattr(cloud_config, "load", _fake_load(store))

    sa = types.ModuleType("google.oauth2.service_account")
    sa.Credentials = types.SimpleNamespace(from_service_account_info=lambda info: ("gcp", info["k"]))
    google, oauth2 = types.ModuleType("google"), types.ModuleType("google.oauth2")
    oauth2.service_account = sa
    google.oauth2 = oauth2
    identity = types.ModuleType("azure.identity")
    identity.ClientSecretCredential = lambda **kw: ("az", kw["client_secret"])
    azure = types.ModuleType("azure")
    azure.identity = identity
    for name, mod in {"google": google, "google.oauth2": oauth2, "google.oauth2.service_account": sa,
                      "azure": azure, "azure.identity": identity}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(cloud_gcp, "_creds_cache", {})
    monkeypatch.setattr(cloud_azure, "_creds_cache", {})

    assert (await cloud_gcp._credentials(TENANT))[0] == ("gcp", 1)
    assert (await cloud_azure._credentials(TENANT))[0] == ("az", "old")
    store["gcp"] = {**store["gcp"], "service_account_json": '{"k": 2}'}
    store["azure"] = {**store["azure"], "client_secret": "new"}
    assert (await cloud_gcp._credentials(TENANT))[0] == ("gcp", 2)
    assert (await cloud_azure._credentials(TENANT))[0] == ("az", "new")


# ── connection test ──────────────────────────────────────────────────────


class _STS:
    def __init__(self, arn):
        self.arn = arn

    def get_caller_identity(self):
        return {"Arn": self.arn}


class _Sess:
    def __init__(self, arn):
        self.arn = arn

    def client(self, _name):
        return _STS(self.arn)


def test_aws_test_assumes_the_role(monkeypatch):
    from daalu_automation.api.routers import onboarding

    seen = []

    def build(cfg, region):
        seen.append(cfg.get("role_arn"))
        if cfg.get("role_arn"):
            return _Sess("arn:aws:sts::1:assumed-role/r/daalu"), None
        return _Sess("arn:aws:iam::1:user/u"), None

    monkeypatch.setattr(cloud_aws, "build_session", build)
    ok, msg = onboarding._test_aws_sync(
        {"access_key_id": "A", "secret_access_key": "s", "role_arn": "arn:aws:iam::1:role/r"}
    )
    assert ok and "assumed-role" in msg
    assert seen == [None, "arn:aws:iam::1:role/r"]


def test_aws_test_fails_on_bad_role(monkeypatch):
    from daalu_automation.api.routers import onboarding

    def build(cfg, region):
        if cfg.get("role_arn"):
            raise RuntimeError("AccessDenied: not authorized to perform sts:AssumeRole")
        return _Sess("arn:aws:iam::1:user/u"), None

    monkeypatch.setattr(cloud_aws, "build_session", build)
    ok, msg = onboarding._test_aws_sync(
        {"access_key_id": "A", "secret_access_key": "s", "role_arn": "arn:aws:iam::1:role/wrong"}
    )
    assert not ok
    assert "The keys work" in msg and "AccessDenied" in msg


# ── copilot ──────────────────────────────────────────────────────────────


def test_copilot_offers_cloud_tools_only_for_connected_providers():
    from daalu_automation.api.routers import copilot
    from daalu_automation.core import kube_tools

    names = lambda defs: {d["function"]["name"] for d in defs}  # noqa: E731
    none = names(copilot._tool_definitions())
    aws = names(copilot._tool_definitions({"aws"}))
    assert not none & copilot._ALL_CLOUD_TOOLS
    assert copilot._CLOUD_TOOLS["aws"] <= aws
    assert not aws & (copilot._CLOUD_TOOLS["gcp"] | copilot._CLOUD_TOOLS["azure"])
    # Every cloud tool exists and is read-only (the copilot has no approval UI).
    for name in copilot._ALL_CLOUD_TOOLS:
        assert name in kube_tools.TOOLS
        assert not kube_tools.TOOLS[name].requires_approval
