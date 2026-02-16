import sys, pprint
print("\n=== sys.path ===")
pprint.pp(sys.path)
print("================\n")

import subprocess
from pathlib import Path
import types

import yaml
import builtins

from daalu.helm.cli_runner import HelmCliRunner
from daalu.config.models import RepoSpec, ReleaseSpec, ValuesRef



class DummyCP:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def test_add_repo_calls_helm_repo_add(monkeypatch):
    calls = []

    def fake_run(argv, check=False, text=False, capture_output=False, env=None):
        calls.append(argv)
        return DummyCP(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    h = HelmCliRunner(kube_context="ctx1")
    h.add_repo(RepoSpec(name="openstack", url="https://example.test/helm/"))

    assert calls[0][:3] == ["helm", "--kube-context", "ctx1"]
    assert calls[0][3:6] == ["repo", "add", "openstack"]


def test_upgrade_install_builds_expected_argv(monkeypatch, tmp_path: Path):
    calls = []
    def fake_run(argv, check=False, text=False, capture_output=False, env=None):
        calls.append(argv)
        return DummyCP(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    rel = ReleaseSpec(
        name="svc",
        namespace="ns",
        chart="openstack/keystone",
        version="1.2.3",
        values=ValuesRef(files=["values/keystone.yaml"]),
        create_namespace=True, atomic=True, wait=True, timeout_seconds=900
    )

    h = HelmCliRunner()
    h.upgrade_install(rel)

    argv = calls[0]
    assert argv[:3] == ["helm", "upgrade", "--install"]
    assert "-n" in argv and "ns" in argv
    assert "--version" in argv and "1.2.3" in argv
    assert any(x.endswith("values/keystone.yaml") for x in argv)


def test_values_inline_writes_temp_file(monkeypatch, tmp_path: Path):
    calls = []

    def fake_run(argv, check=False, text=False, capture_output=False, env=None):
        calls.append(argv)
        return DummyCP(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    # Stub NamedTemporaryFile to a deterministic path
    class DummyTF:
        def __init__(self, *a, **kw):
            self.name = str(tmp_path / "inline.yaml")
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def write(self, s): Path(self.name).write_text(s)
        def flush(self): pass

    import tempfile as _tempfile
    monkeypatch.setattr(_tempfile, "NamedTemporaryFile", lambda *a, **k: DummyTF())

    rel = ReleaseSpec(
        name="svc",
        namespace="ns",
        chart="repo/chart",
        values=ValuesRef(inline={"a": {"b": 1}})
    )

    h = HelmCliRunner()
    h.upgrade_install(rel)

    # Ensure the temp file exists and contains YAML
    data = yaml.safe_load(Path(tmp_path/"inline.yaml").read_text())
    assert data == {"a": {"b": 1}}

    argv = calls[0]
    assert "-f" in argv and str(tmp_path/"inline.yaml") in argv


def test_diff_accepts_rc2(monkeypatch):
    def fake_run(argv, check=False, text=False, capture_output=False, env=None):
        return DummyCP(2, out="DIFF HERE")

    monkeypatch.setattr(subprocess, "run", fake_run)

    rel = ReleaseSpec(name="svc", namespace="ns", chart="repo/chart")
    h = HelmCliRunner()
    out = h.diff(rel)
    assert "DIFF HERE" in out


def test_lint_ok(monkeypatch):
    def fake_run(argv, check=False, text=False, capture_output=False, env=None):
        return DummyCP(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    rel = ReleaseSpec(name="svc", namespace="ns", chart="repo/chart")
    h = HelmCliRunner()
    h.lint(rel)  # should not raise
