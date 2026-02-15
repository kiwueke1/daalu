# tests/bootstrap/test_ceph_manager.py
from __future__ import annotations

import types
import builtins
from pathlib import Path

import pytest


# ---- Fakes for paramiko (like your node bootstrap tests) ----

class _FakeChannel:
    def __init__(self, rc=0): self._rc = rc
    def recv_exit_status(self): return self._rc

class _Buf:
    def __init__(self, s=""): self._s = s
    def read(self): return self._s.encode()

class FakeSSHClient:
    """
    Captures exec_command() calls and returns pre-canned outputs for some commands.
    """
    def __init__(self, log, responses=None):
        self.log = log
        self._responses = responses or {}
    def set_missing_host_key_policy(self, policy): pass
    def connect(self, **kw):
        self.log.append(("connect", kw))
    def exec_command(self, cmd, timeout=None):
        self.log.append(("exec", cmd))
        out, err, rc = self._responses.get(cmd, ("", "", 0))
        stdout = _Buf(out)
        stderr = _Buf(err)
        ch = _FakeChannel(rc)
        stdout.channel = ch
        return types.SimpleNamespace(write=lambda *a, **k: None, flush=lambda: None), stdout, stderr
    def close(self):
        self.log.append(("close",))
    def open_sftp(self):  # not used here
        raise NotImplementedError


class FakeParamikoModule:
    class SSHClient(FakeSSHClient): pass
    class AutoAddPolicy: pass
    class RSAKey:
        @staticmethod
        def from_private_key_file(path): return "PKEY"


def _reload_ceph_manager_with_fake_paramiko(monkeypatch, ops_log, responses=None):
    """
    Monkeypatch paramiko inside the module *before* importing CephManager to bind fakes.
    """
    fake_paramiko = FakeParamikoModule
    # Ensure import hook
    import sys
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)

    # Import target after monkeypatch
    from importlib import reload
    import daalu.bootstrap.ceph.manager as mod
    reload(mod)

    # Inject factory for SSHClient with our ops_log and responses
    def make_client(*a, **k):
        return FakeSSHClient(ops_log, responses=responses or {})
    mod.paramiko.SSHClient = make_client  # type: ignore
    mod.paramiko.AutoAddPolicy = fake_paramiko.AutoAddPolicy  # type: ignore
    return mod


def test_ceph_manager_deploy_happy_path(monkeypatch):
    ops = []

    # The manager checks cephadm presence, pulls image, bootstraps, sets image,
    # adds hosts, applies mon/mgr/osd, and prints `ceph -s`.
    # We only need to return 0 rc for each command; output is not critical here.
    # Special-case the `command -v cephadm || echo MISSING` so that it's "present".
    responses = {
        "bash -lc 'command -v cephadm || echo MISSING'": ("", "", 0),
    }

    mod = _reload_ceph_manager_with_fake_paramiko(monkeypatch, ops, responses)

    # Build inputs
    from daalu.bootstrap.ceph.models import CephHost, CephConfig
    hosts = [
        CephHost(hostname="ceph-1", address="10.0.0.11", username="ubuntu"),
        CephHost(hostname="ceph-2", address="10.0.0.12", username="ubuntu"),
        CephHost(hostname="ceph-3", address="10.0.0.13", username="ubuntu"),
    ]
    cfg = CephConfig(version="18.2.1", image=None, mgr_count=2, mon_count=None)

    manager = mod.CephManager()
    manager.deploy(hosts, cfg)

    # Validate key commands were executed on the primary host.
    executed_cmds = [c for t, c in ops if t == "exec"]
    joined = "\n".join(executed_cmds)

    # Image chosen from version when not supplied
    assert "quay.io/ceph/ceph:v18.2.1" in joined

    # Bootstrap with --mon-ip of primary + image
    assert "cephadm --image quay.io/ceph/ceph:v18.2.1 bootstrap --mon-ip 10.0.0.11" in joined

    # Set global container image
    assert "cephadm shell -- ceph config set global container_image quay.io/ceph/ceph:v18.2.1" in joined

    # Add the other hosts
    assert "cephadm shell -- ceph orch host add ceph-2 10.0.0.12" in joined
    assert "cephadm shell -- ceph orch host add ceph-3 10.0.0.13" in joined

    # Apply mon & mgr placements
    # mon_count = min(3, len(hosts)) -> 3
    assert 'cephadm shell -- ceph orch apply mon --placement="count:3"' in joined
    assert 'cephadm shell -- ceph orch apply mgr --placement="count:2"' in joined

    # Apply OSDs (all available devices)
    assert "cephadm shell -- ceph orch apply osd --all-available-devices" in joined

    # ceph -s (health summary)
    assert "cephadm shell -- ceph -s" in joined


def test_ceph_manager_explicit_image(monkeypatch):
    ops = []
    mod = _reload_ceph_manager_with_fake_paramiko(monkeypatch, ops, {
        "bash -lc 'command -v cephadm || echo MISSING'": ("", "", 0),
    })

    from daalu.bootstrap.ceph.models import CephHost, CephConfig
    hosts = [
        CephHost(hostname="ceph-1", address="10.0.0.11", username="ubuntu"),
        CephHost(hostname="ceph-2", address="10.0.0.12", username="ubuntu"),
    ]
    cfg = CephConfig(version="18.2.1", image="quay.io/ceph/ceph:v18.2.2")

    manager = mod.CephManager()
    manager.deploy(hosts, cfg)

    executed_cmds = [c for t, c in ops if t == "exec"]
    joined = "\n".join(executed_cmds)

    assert "quay.io/ceph/ceph:v18.2.2" in joined
    assert "bootstrap --mon-ip 10.0.0.11" in joined


def test_ceph_manager_no_hosts_raises(monkeypatch):
    ops = []
    mod = _reload_ceph_manager_with_fake_paramiko(monkeypatch, ops, {})

    from daalu.bootstrap.ceph.models import CephHost, CephConfig
    manager = mod.CephManager()

    with pytest.raises(ValueError):
        manager.deploy([], CephConfig())
