import types
from pathlib import Path
import builtins

import pytest

from daalu.node_bootstrap.ssh_bootstrapper import SshBootstrapper
from daalu.node_bootstrap.models import Host, NodeBootstrapPlan, NodeBootstrapOptions

# ----------------- Fakes for Paramiko -----------------

class _FakeChannel:
    def __init__(self, rc=0): self._rc = rc
    def recv_exit_status(self): return self._rc

class _Buf:
    def __init__(self, s=""): self._s = s
    def read(self): return self._s.encode()

class FakeSFTP:
    def __init__(self, log): self.log = log
    def file(self, path, mode):
        self.log.append(("sftp_file", path, mode))
        return _FakeFile(self.log, path)
    def put(self, local, remote):
        self.log.append(("sftp_put", local, remote))
    def close(self): self.log.append(("sftp_close",))

class _FakeFile:
    def __init__(self, log, path):
        self._buf = []
        self.log = log
        self.path = path
    def write(self, data):
        self._buf.append(data)
    def close(self):
        self.log.append(("sftp_write", self.path, "".join(self._buf)))

class FakeSSHClient:
    def __init__(self, log, responses=None):
        self.log = log
        self._sftp = FakeSFTP(log)
        self._responses = responses or {}
    def set_missing_host_key_policy(self, policy): pass
    def connect(self, **kw):
        self.log.append(("connect", kw))
    def open_sftp(self):
        self.log.append(("open_sftp",))
        return self._sftp
    def exec_command(self, cmd, timeout=None):
        self.log.append(("exec", cmd))
        # default response
        out = self._responses.get(cmd, ("", "", 0))
        stdout = _Buf(out[0])
        stderr = _Buf(out[1])
        ch = _FakeChannel(out[2])
        stdout.channel = ch
        return types.SimpleNamespace(write=lambda *a, **k: None, flush=lambda: None), stdout, stderr
    def close(self):
        self.log.append(("close",))

class FakeParamikoModule:
    class SSHClient(FakeSSHClient): pass
    class SFTPClient(FakeSFTP): pass
    class AutoAddPolicy: pass
    class RSAKey:
        @staticmethod
        def from_private_key_file(path): return "PKEY"

# ----------------- Tests -----------------

def test_full_bootstrap_happy_path(monkeypatch, tmp_path: Path):
    # Arrange: monkeypatch paramiko to our fakes
    ops_log = []
    fake_paramiko = FakeParamikoModule
    monkeypatch.setitem(__import__("sys").modules, "paramiko", fake_paramiko)

    # Re-import after monkeypatch to bind fakes (important if module cached earlier)
    from importlib import reload
    import daalu.node_bootstrap.ssh_bootstrapper as mod
    reload(mod)

    # Prepare a host and options
    pubkey = tmp_path / "id_rsa.pub"
    pubkey.write_text("ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCfakekey== kez@local")

    host = Host(
        hostname="node-1",
        address="10.0.0.11",
        username="ubuntu",
        authorized_key_path=pubkey,
    )

    # Responses for commands that return outputs
    responses = {
        # ens18 IP query
        "bash -lc 'ip -4 addr show ens18 | awk '/inet /{print $2}' | cut -d/ -f1'": ("192.168.1.10\n", "", 0),
    }
    # Inject responses into FakeSSHClient
    def make_client(*a, **k):
        return FakeSSHClient(ops_log, responses=responses)

    mod.paramiko.SSHClient = make_client  # type: ignore[attr-defined]
    mod.paramiko.AutoAddPolicy = fake_paramiko.AutoAddPolicy  # type: ignore[attr-defined]

    # Also stub subprocess clusterctl for kubeconfig fetch
    import subprocess as real_sub
    def fake_run(argv, capture_output=False, text=False, check=False):
        if argv[:3] == ["clusterctl", "get", "kubeconfig"]:
            return types.SimpleNamespace(returncode=0, stdout="apiVersion: v1\nclusters: []\n", stderr="")
        return real_sub.run(argv, capture_output=capture_output, text=text, check=check)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    # Act
    bs = mod.SshBootstrapper()
    plan = NodeBootstrapPlan()  # run all roles
    opts = NodeBootstrapOptions(kubeconfig_content=None)  # forces clusterctl fetch
    bs.bootstrap([host], plan, opts)

    # Assert: check that some key commands ran
    executed = [e for e in ops_log if e[0] == "exec"]
    exec_cmds = "\n".join(cmd for _, cmd in executed)

    # apparmor & packages
    assert "apt-get install -y apparmor apparmor-utils python3-pip python3-setuptools" in exec_cmds
    # kubeconfig upload (sftp_write to tmp then install)
    assert any(e[0] == "sftp_write" and "/tmp/.daalu_tmp_" in e[1] for e in ops_log)
    # netplan apply
    # (no renderer given; netplan role will no-op -> OK)

    # sudoers file created
    assert "/etc/sudoers.d/kez" in exec_cmds
    # hostname set
    assert "hostnamectl set-hostname node-1" in exec_cmds
    # /etc/hosts appended with FQDN
    assert "openstack-infra" not in exec_cmds  # ensure not using old regex here
    assert " /etc/hosts" in exec_cmds

def test_netplan_renderer(monkeypatch):
    # minimal monkeypatch to intercept commands
    ops_log = []
    fake_paramiko = FakeParamikoModule
    monkeypatch.setitem(__import__("sys").modules, "paramiko", fake_paramiko)
    from importlib import reload
    import daalu.node_bootstrap.ssh_bootstrapper as mod
    reload(mod)

    def make_client(*a, **k): return FakeSSHClient(ops_log)
    mod.paramiko.SSHClient = make_client  # type: ignore

    host = Host(hostname="n1", address="1.2.3.4", username="ubuntu")
    def renderer(h: Host) -> str:
        return "network:\n  version: 2\n  ethernets: {}\n"

    bs = mod.SshBootstrapper()
    plan = NodeBootstrapPlan(run_apparmor=False, run_ssh_and_hostname=False, run_inotify_limits=False, run_istio_modules=False, run_netplan=True)
    opts = NodeBootstrapOptions(netplan_renderer=renderer)

    bs.bootstrap([host], plan, opts)

    # Should have written netplan content then applied it
    assert any(e[0] == "sftp_write" and e[1].startswith("/tmp/.daalu_tmp_") for e in ops_log)
    assert any("netplan apply" in e[1] for e in ops_log if e[0] == "exec")

def test_inotify_and_istio(monkeypatch):
    ops_log = []
    fake_paramiko = FakeParamikoModule
    monkeypatch.setitem(__import__("sys").modules, "paramiko", fake_paramiko)
    from importlib import reload
    import daalu.node_bootstrap.ssh_bootstrapper as mod
    reload(mod)

    def make_client(*a, **k): return FakeSSHClient(ops_log)
    mod.paramiko.SSHClient = make_client  # type: ignore

    host = Host(hostname="n2", address="5.6.7.8", username="ubuntu")
    plan = NodeBootstrapPlan(run_apparmor=False, run_netplan=False, run_ssh_and_hostname=False, run_inotify_limits=True, run_istio_modules=True)
    opts = NodeBootstrapOptions()

    bs = mod.SshBootstrapper()
    bs.bootstrap([host], plan, opts)

    # sysctl conf appended and sysctl -p executed
    assert any("sysctl -p /etc/sysctl.conf" in e[1] for e in ops_log if e[0] == "exec")
    # modules-load file written & modprobe invoked
    assert any(e[0] == "sftp_write" and "99-istio-modules.conf" in e[1] for e in ops_log)
    assert any("modprobe br_netfilter" in e[1] for e in ops_log if e[0] == "exec")
