"""Tests for DeployerRunner (drives the vendored NV-CM Deployer).

Split by interpreter: the pure helpers and the helm/kubectl ``uninstall`` argv
work on any Python (they never import the StrEnum-based vendored package). The
tests that actually construct the Deployer subclass are guarded to 3.11+, which
is what the runtime image and CI run.
"""

from __future__ import annotations

import sys
import uuid

import pytest

from daalu_automation.config_manager_controller import deployer_runner as dr

_PY311 = sys.version_info >= (3, 11)


def _row(**kw):
    from daalu_automation.models import ConfigManagerTenant

    defaults = dict(
        tenant_id=uuid.UUID("22222222222222222222222222222222"),
        namespace="cm-acme",
        base_hostname="acme.cm.daalu.io",
        components={},
        size_profile="small",
        chart_version="1.3.0",
    )
    defaults.update(kw)
    return ConfigManagerTenant(**defaults)


# ── Pure helpers (any Python) ──────────────────────────────────────────


def test_daalu_values_overlay_flat_http_only_gateway() -> None:
    overlay = dr._daalu_values_overlay(_row())
    gw = overlay["gateway"]
    assert gw["createGatewayClass"] is False
    assert gw["nodePort"]["enabled"] is False
    # Pins the shared class name both ways the chart reads it.
    assert gw["gatewayClassName"] == dr.GATEWAY_CLASS
    assert gw["className"] == dr.GATEWAY_CLASS
    # Hub terminates TLS at its wildcard + reverse-proxies over the tunnel, so
    # the tenant gateway is HTTP-only with no cert (not the Deployer's
    # self-signed HTTPS local-dev default).
    assert [ll["protocol"] for ll in gw["listeners"]] == ["HTTP"]
    assert gw["certificates"]["enabled"] is False
    # FLAT per-component human hostnames (<tool>-<slug>.<zone>), not nested
    # <tool>.<slug>.<zone> — base_hostname acme.cm.daalu.io → slug=acme.
    assert overlay["nautobot"]["gateway"]["hostname"] == "nautobot-acme.cm.daalu.io"
    assert overlay["renderService"]["gateway"]["hostname"] == "render-acme.cm.daalu.io"
    # Interactive OIDC redirect OFF (hub injects a service Bearer; the gateway's
    # oauth2 session cookie can't be shared across flat sibling hosts → would
    # loop). JWT validation stays on via the chart.
    assert overlay["oidc"]["interactiveRedirect"] is False


def test_deep_merge_overrides_nested_without_dropping_siblings() -> None:
    base = {"gateway": {"enabled": True, "createGatewayClass": True, "x": 1}}
    merged = dr._deep_merge(base, dr._daalu_values_overlay(_row()))
    gw = merged["gateway"]
    assert gw["enabled"] is True  # sibling preserved
    assert gw["x"] == 1  # sibling preserved
    assert gw["createGatewayClass"] is False  # overridden
    assert gw["nodePort"] == {"enabled": False}  # added


def test_strip_empty_image_pull_secrets_drops_empty_entries() -> None:
    # helm_values emits [""] when no pull secret is configured → chart renders
    # [{}] → breaks every helm upgrade's strategic merge. Strip it to [].
    v = {"global": {"imagePullSecrets": [""], "other": 1}}
    dr._strip_empty_image_pull_secrets(v)
    assert v["global"]["imagePullSecrets"] == []
    assert v["global"]["other"] == 1  # sibling preserved


def test_strip_empty_image_pull_secrets_keeps_real_secret() -> None:
    v = {"global": {"imagePullSecrets": ["regcred-harbor", ""]}}
    dr._strip_empty_image_pull_secrets(v)
    assert v["global"]["imagePullSecrets"] == ["regcred-harbor"]


def test_strip_empty_image_pull_secrets_noop_when_absent() -> None:
    v = {"global": {"imagePullPolicy": "IfNotPresent"}}
    dr._strip_empty_image_pull_secrets(v)  # must not raise
    assert "imagePullSecrets" not in v["global"]


# ── uninstall: helm + kubectl argv via injected runner (any Python) ─────


async def test_release_status_parses_deployed() -> None:
    async def fake_runner(argv, env):
        assert argv[0] == "helm" and argv[1] == "status"
        assert "cm-222222222222" in argv and "-o" in argv and "json" in argv
        return 0, '{"info": {"status": "deployed"}}', ""

    runner = dr.DeployerRunner(chart_dir="/charts/x", runner=fake_runner)
    assert await runner.release_status(row=_row(), kubeconfig=None) == "deployed"


async def test_release_status_empty_on_error_or_unparseable() -> None:
    async def err_runner(argv, env):
        return 1, "", "release: not found"

    async def junk_runner(argv, env):
        return 0, "not json", ""

    r1 = dr.DeployerRunner(chart_dir="/x", runner=err_runner)
    r2 = dr.DeployerRunner(chart_dir="/x", runner=junk_runner)
    assert await r1.release_status(row=_row(), kubeconfig=None) == ""  # absent → provision
    assert await r2.release_status(row=_row(), kubeconfig=None) == ""


async def test_uninstall_runs_helm_then_namespace_sweep() -> None:
    calls: list[list[str]] = []

    async def fake_runner(argv, env):
        calls.append(argv)
        return 0, "ok", ""

    runner = dr.DeployerRunner(chart_dir="/charts/x", runner=fake_runner)
    res = await runner.uninstall(row=_row(), kubeconfig=None)

    assert res.ok is True
    assert len(calls) == 2
    helm_argv, kubectl_argv = calls
    assert helm_argv[0] == "helm" and "uninstall" in helm_argv
    assert "cm-222222222222" in helm_argv  # stable release name
    assert "cm-acme" in helm_argv
    assert kubectl_argv[0] == "kubectl"
    assert kubectl_argv[1:4] == ["delete", "namespace", "cm-acme"]
    assert "--ignore-not-found" in kubectl_argv


async def test_uninstall_failure_is_reported_and_skips_sweep() -> None:
    calls: list[list[str]] = []

    async def fake_runner(argv, env):
        calls.append(argv)
        return 1, "", "release not found"

    runner = dr.DeployerRunner(chart_dir="/charts/x", runner=fake_runner)
    res = await runner.uninstall(row=_row(), kubeconfig=None)

    assert res.ok is False
    assert "release not found" in res.error
    # Sweep must not run if the helm uninstall failed.
    assert len(calls) == 1


async def test_uninstall_namespace_sweep_failure_is_nonfatal() -> None:
    async def fake_runner(argv, env):
        if argv[0] == "kubectl":
            return 1, "", "forbidden"
        return 0, "ok", ""

    runner = dr.DeployerRunner(chart_dir="/charts/x", runner=fake_runner)
    res = await runner.uninstall(row=_row(), kubeconfig=None)
    # Release is gone; lingering namespace is a sweep concern, not a failure.
    assert res.ok is True


# ── Capture callback (any Python) ──────────────────────────────────────


def test_capture_callback_keeps_bounded_log_tail() -> None:
    cb = dr._CaptureCallback(tenant_id="t")
    for i in range(dr._LOG_TAIL + 25):
        cb.on_log(f"line {i}")
    assert len(cb.log_tail) == dr._LOG_TAIL
    assert cb.log_tail[-1] == f"line {dr._LOG_TAIL + 24}"


# ── _KubeconfigEnv: must satisfy BOTH subprocess + Python-client paths ──


def test_kubeconfig_env_writes_default_location_and_restores(tmp_path, monkeypatch):
    """The kubernetes Python client ignores $KUBECONFIG and only reads
    ~/.kube/config. _KubeconfigEnv must therefore write that file (so the
    Deployer's in-process K8sClient() loads), set $KUBECONFIG (for helm/
    kubectl subprocesses), and restore both on exit. Regression for the
    "Invalid kube-config file. No configuration found." prereqs failure.
    """
    import yaml

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KUBECONFIG", raising=False)
    cfg = tmp_path / ".kube" / "config"
    kc = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "x",
        "clusters": [{"name": "c", "cluster": {"server": "https://h:6443"}}],
        "contexts": [{"name": "x", "context": {"cluster": "c", "user": "u"}}],
        "users": [{"name": "u", "user": {"token": "t"}}],
    }
    assert not cfg.exists()
    with dr._KubeconfigEnv(kc):
        # Python client path: default location exists with our config.
        assert cfg.exists()
        assert yaml.safe_load(cfg.read_text())["current-context"] == "x"
        # Subprocess path: env var points at a (different) temp file.
        assert dr.os.environ.get("KUBECONFIG")
    # Clean slate on exit.
    assert not cfg.exists()
    assert "KUBECONFIG" not in dr.os.environ


def test_kubeconfig_env_restores_preexisting_default(tmp_path, monkeypatch):
    """A pre-existing ~/.kube/config is backed up and restored byte-for-byte."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KUBECONFIG", raising=False)
    cfg = tmp_path / ".kube" / "config"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("ORIGINAL-CONTENT\n")
    with dr._KubeconfigEnv({"apiVersion": "v1", "kind": "Config"}):
        assert cfg.read_text() != "ORIGINAL-CONTENT\n"
    assert cfg.read_text() == "ORIGINAL-CONTENT\n"


def test_kubeconfig_env_none_is_noop(tmp_path, monkeypatch):
    """kubeconfig=None (local/in-cluster) must not create or touch files."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KUBECONFIG", raising=False)
    cfg = tmp_path / ".kube" / "config"
    with dr._KubeconfigEnv(None):
        assert not cfg.exists()
        assert "KUBECONFIG" not in dr.os.environ
    assert not cfg.exists()


# ── Deployer subclass overlay (3.11+ only — matches runtime/CI) ─────────


@pytest.mark.skipif(not _PY311, reason="constructs the StrEnum-based Deployer")
def test_daalu_deployer_overrides_patch_gateway_and_generate_values() -> None:
    from nv_config_manager_installer.deployer import Deployer, DeployOptions
    from nv_config_manager_installer.schema import NVConfigManagerInstallConfig

    cfg = NVConfigManagerInstallConfig()
    cfg.cluster.hostname = "acme.cm.daalu.io"
    opts = DeployOptions(chart_dir="/charts/x")

    class _Cb:
        def on_step_update(self, step): ...
        def on_log(self, message): ...
        def on_complete(self, success, endpoints): ...

    deployer = dr._build_daalu_deployer(cfg, opts, _Cb(), {})
    assert isinstance(deployer, Deployer)
    # Our subclass replaces these two methods.
    assert type(deployer)._patch_gateway is not Deployer._patch_gateway
    assert type(deployer)._generate_values is not Deployer._generate_values


@pytest.mark.skipif(not _PY311, reason="constructs the StrEnum-based Deployer")
def test_daalu_deployer_generate_values_merges_gateway_overlay(tmp_path) -> None:
    import yaml

    from nv_config_manager_installer.deployer import Deployer, DeployOptions
    from nv_config_manager_installer.schema import NVConfigManagerInstallConfig

    cfg = NVConfigManagerInstallConfig()
    cfg.cluster.hostname = "acme.cm.daalu.io"
    opts = DeployOptions(chart_dir="/charts/x")

    class _Cb:
        def on_step_update(self, step): ...
        def on_log(self, message): ...
        def on_complete(self, success, endpoints): ...

    deployer = dr._build_daalu_deployer(cfg, opts, _Cb(), dr._daalu_values_overlay(_row()))

    # Stand in for the upstream _generate_values: write a values file with the
    # upstream defaults we need to override (nested host + HTTPS gateway).
    values_file = tmp_path / "values.yaml"
    values_file.write_text(
        yaml.safe_dump(
            {
                "gateway": {
                    "enabled": True,
                    "createGatewayClass": True,
                    "nodePort": {"enabled": True},
                    "listeners": [
                        {"name": "https", "protocol": "HTTPS", "port": 443}
                    ],
                },
                "nautobot": {"gateway": {"hostname": "nautobot.acme.cm.daalu.io"}},
            }
        )
    )
    deployer._values_file = values_file

    # Monkeypatch the upstream super()._generate_values to a no-op so we test
    # only the overlay merge our subclass adds on top.
    orig = Deployer._generate_values
    Deployer._generate_values = lambda self: None  # type: ignore[assignment]
    try:
        deployer._generate_values()
    finally:
        Deployer._generate_values = orig  # type: ignore[assignment]

    result = yaml.safe_load(values_file.read_text())
    gw = result["gateway"]
    assert gw["enabled"] is True  # preserved
    assert gw["createGatewayClass"] is False  # overlaid
    assert gw["nodePort"]["enabled"] is False  # overlaid
    assert gw["className"] == dr.GATEWAY_CLASS
    # Hub-terminated overlay forces HTTP-only + flat hostnames over the
    # upstream's HTTPS / nested defaults.
    assert [ll["protocol"] for ll in gw["listeners"]] == ["HTTP"]
    assert result["nautobot"]["gateway"]["hostname"] == "nautobot-acme.cm.daalu.io"


# ── Unwedging a release stuck mid-operation ────────────────────────────
#
# A controller restart during `helm upgrade --install` leaves the release in
# `pending-*` forever: helm only writes a terminal status when the operation
# finishes, and every later install then fails in under a second with "another
# operation is in progress". The retry loop can never clear that on its own,
# so the reconciler has to. Observed in prod after a CD rollout restarted the
# controller mid-install.


def _runner_capturing(argv_log, *, status_json="", rc=0):
    async def _run(argv, env=None):
        argv_log.append(argv)
        if argv[1] == "status":
            return (0, status_json, "") if status_json else (1, "", "not found")
        return (rc, "", "" if rc == 0 else "boom")

    return _run


def _mk(argv_log, status_json="", rc=0):
    return dr.DeployerRunner(
        chart_dir="deploy/charts/nv-config-manager-1.3.0",
        runner=_runner_capturing(argv_log, status_json=status_json, rc=rc),
    )


@pytest.mark.asyncio
async def test_deployed_release_is_left_alone() -> None:
    argv: list[list[str]] = []
    cleared = await _mk(argv, '{"info":{"status":"deployed"}}').clear_pending_release(
        row=_row()
    )
    assert cleared is None
    # Only the status probe ran — nothing was mutated.
    assert [a[1] for a in argv] == ["status"]


@pytest.mark.asyncio
async def test_pending_install_drops_the_stuck_release_record() -> None:
    argv: list[list[str]] = []
    cleared = await _mk(
        argv, '{"info":{"status":"pending-install"}}'
    ).clear_pending_release(row=_row())
    assert cleared == "pending-install"
    delete = [a for a in argv if a[1] == "delete"][0]
    # Scoped to this release *and* the pending record — must not touch the
    # deployed record of a healthy release in the same namespace.
    sel = delete[delete.index("-l") + 1]
    assert "owner=helm" in sel
    assert f"name={dr.release_name(_row())}" in sel
    assert "status=pending-install" in sel
    assert "--ignore-not-found" in delete
    assert "rollback" not in [a[1] for a in argv]


@pytest.mark.asyncio
async def test_pending_upgrade_rolls_back_instead_of_deleting() -> None:
    # An upgrade has a previous good revision, so rolling back is recoverable
    # where deleting the record would strand the release.
    argv: list[list[str]] = []
    cleared = await _mk(
        argv, '{"info":{"status":"pending-upgrade"}}'
    ).clear_pending_release(row=_row())
    assert cleared == "pending-upgrade"
    assert [a[1] for a in argv] == ["status", "rollback"]
    assert "delete" not in [a[1] for a in argv]


@pytest.mark.asyncio
async def test_absent_release_is_not_treated_as_wedged() -> None:
    argv: list[list[str]] = []
    assert await _mk(argv).clear_pending_release(row=_row()) is None


@pytest.mark.asyncio
async def test_failed_unwedge_reports_none_rather_than_raising() -> None:
    # The caller runs this best-effort before provisioning; a failure here
    # must not abort the reconcile.
    argv: list[list[str]] = []
    runner = _mk(argv, '{"info":{"status":"pending-install"}}', rc=1)
    assert await runner.clear_pending_release(row=_row()) is None
