import subprocess
from pathlib import Path

import yaml

from daalu.helm.cli_runner import HelmCliRunner
from daalu.config.models import RepoSpec, ReleaseSpec, ValuesRef


class DummyCP:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


# Helper: strip the "sudo -E" prefix that HelmCliRunner injects for local runs
def _helm_args(argv):
    """Return the argv starting from the helm binary (skips 'sudo -E' prefix)."""
    if argv and argv[0] == "sudo":
        # Skip "sudo", "-E", then return the rest
        return [a for a in argv if a not in ("sudo", "-E")]
    return argv


def test_add_repo_calls_helm_repo_add(monkeypatch):
    calls = []

    def fake_run(argv, check=False, text=False, capture_output=False, env=None):
        calls.append(argv)
        return DummyCP(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    h = HelmCliRunner(kube_context="ctx1")
    h.add_repo(RepoSpec(name="openstack", url="https://example.test/helm/"))

    # Command is prefixed with sudo -E for local execution; skip that prefix
    args = _helm_args(calls[0])
    assert "--kube-context" in args
    assert "ctx1" in args
    assert "repo" in args
    assert "add" in args
    assert "openstack" in args


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

    args = _helm_args(calls[0])
    assert "upgrade" in args
    assert "--install" in args
    assert "-n" in args and "ns" in args
    assert "--version" in args and "1.2.3" in args
    assert any(x.endswith("values/keystone.yaml") for x in args)


def test_values_inline_writes_temp_file(monkeypatch, tmp_path: Path):
    calls = []

    def fake_run(argv, check=False, text=False, capture_output=False, env=None):
        calls.append(argv)
        return DummyCP(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    # Stub NamedTemporaryFile to a deterministic path.
    # Accumulate all write() calls in a buffer and flush on __exit__,
    # because yaml.safe_dump calls write() multiple times.
    class DummyTF:
        def __init__(self, *a, **kw):
            self.name = str(tmp_path / "inline.yaml")
            self._buf: list[str] = []
        def __enter__(self): return self
        def __exit__(self, *a):
            Path(self.name).write_text("".join(self._buf))
            return False
        def write(self, s): self._buf.append(s)
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

    args = _helm_args(calls[0])
    assert "-f" in args and str(tmp_path/"inline.yaml") in args


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
