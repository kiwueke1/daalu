# tests/bootstrap/test_ceph_manager.py
from __future__ import annotations

import types
import builtins
from pathlib import Path

import pytest


# ---- Fakes for paramiko (like your node bootstrap tests) ----

class _FakeChannel:
    def __init__(self, rc=0, out=b"", err=b""):
        self._rc = rc
        self._out = out
        self._err = err
        self._done = False
    def recv_exit_status(self): return self._rc
    def exit_status_ready(self):
        self._done = True  # signal done on first check
        return True
    def recv_ready(self): return bool(self._out)
    def recv_stderr_ready(self): return bool(self._err)
    def recv(self, n):
        chunk, self._out = self._out[:n], self._out[n:]
        return chunk
    def recv_stderr(self, n):
        chunk, self._err = self._err[:n], self._err[n:]
        return chunk
    def close(self): pass

class _Buf:
    def __init__(self, s=""):
        self._s = s
        self.channel = _FakeChannel(0, s.encode(), b"")
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
        ch = _FakeChannel(rc, out.encode(), err.encode())
        stdout = _Buf(out)
        stderr = _Buf(err)
        stdout.channel = ch
        stderr.channel = ch
        return types.SimpleNamespace(write=lambda *a, **k: None, flush=lambda: None), stdout, stderr
    def close(self):
        self.log.append(("close", None))
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

    # CephManager now requires a bus argument
    from daalu.observers.dispatcher import EventBus
    manager = mod.CephManager(bus=EventBus(observers=[]))
    manager.deploy(hosts, cfg)

    # Validate key commands were executed on the primary host.
    executed_cmds = [c for t, c in ops if t == "exec"]
    joined = "\n".join(executed_cmds)

    # Image derived from version when none explicitly supplied
    assert "quay.io/ceph/ceph:v18.2.1" in joined

    # cephadm is installed / checked for presence
    assert "cephadm" in joined

    # Mon and mgr placement applied across all 3 hosts
    assert 'ceph orch apply mon' in joined
    assert 'ceph orch apply mgr' in joined

    # ceph -s (health summary) at the end
    assert "ceph -s" in joined


def test_ceph_manager_explicit_image(monkeypatch):
    ops = []
    mod = _reload_ceph_manager_with_fake_paramiko(monkeypatch, ops, {
        "bash -lc 'command -v cephadm || echo MISSING'": ("", "", 0),
    })

    from daalu.bootstrap.ceph.models import CephHost, CephConfig
    from daalu.observers.dispatcher import EventBus
    hosts = [
        CephHost(hostname="ceph-1", address="10.0.0.11", username="ubuntu"),
        CephHost(hostname="ceph-2", address="10.0.0.12", username="ubuntu"),
    ]
    cfg = CephConfig(version="18.2.1", image="quay.io/ceph/ceph:v18.2.2")

    manager = mod.CephManager(bus=EventBus(observers=[]))
    manager.deploy(hosts, cfg)

    executed_cmds = [c for t, c in ops if t == "exec"]
    joined = "\n".join(executed_cmds)

    # Explicit image should be used in commands
    assert "quay.io/ceph/ceph:v18.2.2" in joined
    # cephadm should run on the primary host
    assert "cephadm" in joined


def test_ceph_manager_no_hosts_raises(monkeypatch):
    ops = []
    mod = _reload_ceph_manager_with_fake_paramiko(monkeypatch, ops, {})

    from daalu.bootstrap.ceph.models import CephHost, CephConfig
    from daalu.observers.dispatcher import EventBus
    manager = mod.CephManager(bus=EventBus(observers=[]))

    with pytest.raises(ValueError):
        manager.deploy([], CephConfig())
