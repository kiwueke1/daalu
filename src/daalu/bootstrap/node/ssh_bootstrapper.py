# daalu/src/daalu/bootstrap/node/ssh_bootstrapper.py

from __future__ import annotations

import io
import os
import posixpath
import tempfile
import textwrap
import base64
from dataclasses import dataclass
from typing import List, Optional, Tuple

import time
import paramiko
import subprocess

from .interface import NodeBootstrapper
from .models import Host, NodeBootstrapPlan, NodeBootstrapOptions
import logging

log = logging.getLogger("daalu")


@dataclass
class _SSHHandles:
    client: paramiko.SSHClient
    sftp: paramiko.SFTPClient


class SshBootstrapper(NodeBootstrapper):
    """
    A bootstrapper that ports the remaining Ansible roles to Python+SSH:
      - apparmor_setup        (apt repos, packages, service, kubernetes pip, kubeconfig copy)
      - netplan_config        (render/push netplan YAML and apply)
      - ssh_and_hostname      (user, authorized_keys, sudoers, hostname, /etc/hosts entry)
      - inotify_limits        (sysctl keys)
      - istio_modules         (modules-load config + modprobe list)
    """

    def __init__(self, connect_timeout: float = 15.0, cmd_timeout: float = 120.0):
        self.connect_timeout = connect_timeout
        self.cmd_timeout = cmd_timeout

    # ------------------ connection & utils ------------------

    def _connect(self, host):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        pkey = None
        if host.pkey_path:
            key_path = str(host.pkey_path)
            try:
                pkey = paramiko.Ed25519Key.from_private_key_file(key_path)
            except paramiko.ssh_exception.SSHException:
                try:
                    pkey = paramiko.RSAKey.from_private_key_file(key_path)
                except paramiko.ssh_exception.SSHException:
                    pkey = paramiko.ECDSAKey.from_private_key_file(key_path)

        client.connect(
            hostname=host.address,
            port=host.port,
            username=host.username,
            pkey=pkey,
            password=None,          # IMPORTANT
            look_for_keys=False,
            allow_agent=False,
            timeout=30,
        )


        # Try to open an SFTP session, but don't fail if it can't
        sftp = None
        try:
            sftp = client.open_sftp()
        except Exception:
            pass

        return _SSHHandles(client=client, sftp=sftp)

    def _close(self, h: _SSHHandles):
        try:
            if h.sftp:
                h.sftp.close()
        except Exception:
            pass
        finally:
            h.client.close()
            
    def _connect_1(self, host):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        pkey = None
        if host.pkey_path:
            key_path = str(host.pkey_path)
            try:
                # Try ED25519 first (most modern)
                pkey = paramiko.Ed25519Key.from_private_key_file(key_path)
            except paramiko.ssh_exception.SSHException:
                try:
                    # Then try RSA
                    pkey = paramiko.RSAKey.from_private_key_file(key_path)
                except paramiko.ssh_exception.SSHException:
                    try:
                        # Fallback to ECDSA
                        pkey = paramiko.ECDSAKey.from_private_key_file(key_path)
                    except Exception as e:
                        raise RuntimeError(f"Unsupported private key format for {key_path}: {e}")

        client.connect(
            hostname=host.address,
            port=host.port,
            username=host.username,
            pkey=pkey,
            password=None,          # IMPORTANT
            look_for_keys=False,
            allow_agent=False,
            timeout=30,
        )



    def _close_1(self, h: _SSHHandles):
        try:
            h.sftp.close()
        finally:
            h.close()

    def _run(self, h: _SSHHandles, cmd: str, sudo: bool = False, stdin_data: Optional[str] = None) -> Tuple[int, str, str]:
        """
        Run a shell command. If sudo=True, feed the become password to sudo -S.
        """
        if sudo:
            cmd = f"sudo -S bash -lc {self._q(cmd)}"
            if stdin_data is None:
                stdin_data = ""
        else:
            cmd = f"bash -lc {self._q(cmd)}"

        stdin, stdout, stderr = h.client.exec_command(cmd, timeout=self.cmd_timeout)
        if sudo and stdin_data is not None:
            # write become password followed by newline first if provided
            if stdin_data:
                stdin.write(stdin_data + "\n")
            else:
                # Allow sudo without password too
                pass
        stdin.flush()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, out, err

    def _q(self, s: str) -> str:
        """
        Quote for bash -lc.
        """
        return "'" + s.replace("'", "'\"'\"'") + "'"

    def _ensure_dir(self, h: _SSHHandles, path: str, mode: int = 0o700, sudo: bool = False):
        self._run(h, f"install -d -m {oct(mode)[2:]} {path}", sudo=sudo)

    def _put_content(self, h: _SSHHandles, content: str, remote_path: str, mode: int = 0o644, owner: Optional[str] = None, sudo: bool = True):
        """
        Upload content to a temp path then move with sudo to final destination to preserve root-owned targets.
        """
        tmp_remote = f"/tmp/.daalu_tmp_{os.getpid()}_{next(_counter)}"
        # SFTP write to temp
        with h.sftp.file(tmp_remote, "w") as f:
            f.write(content)
        # Move with sudo, set perms/owner
        chown = f" && chown {owner} {remote_path}" if owner else ""
        cmd = f"install -m {oct(mode)[2:]} -o root -g root {tmp_remote} {remote_path}{chown} ; rm -f {tmp_remote}"
        self._run(h, cmd, sudo=sudo)

    def _put_file(self, h: _SSHHandles, local_path: str, remote_path: str, mode: int = 0o644, sudo: bool = True):
        tmp_remote = f"/tmp/.daalu_tmp_{os.getpid()}_{next(_counter)}"
        h.sftp.put(local_path, tmp_remote)
        cmd = f"install -m {oct(mode)[2:]} -o root -g root {tmp_remote} {remote_path} ; rm -f {tmp_remote}"
        self._run(h, cmd, sudo=sudo)

    def _append_line(self, h: _SSHHandles, line: str, remote_path: str, sudo: bool = True):
        """
        Append a line (if not already present) to a file.
        """
        # create file if missing
        self._run(h, f"touch {remote_path}", sudo=sudo)
        # idempotent append
        cmd = f"grep -qxF {self._q(line)} {remote_path} || echo {self._q(line)} >> {remote_path}"
        self._run(h, cmd, sudo=sudo)

    # ------------------ roles ------------------

    def _kubeconfig_content(self, opts: NodeBootstrapOptions) -> str:
        # 1) If explicitly provided, always prefer it
        if opts.kubeconfig_content:
            return opts.kubeconfig_content

        # 2) Fetch kubeconfig from Cluster API secret (v1beta2-compatible)
        #
        # Secret name convention:
        #   <cluster-name>-kubeconfig
        #
        # Namespace:
        #   MUST be the Cluster API namespace (Metal3 usually uses "metal3")
        #
        namespace = opts.cluster_namespace
        secret_name = f"{opts.cluster_name}-kubeconfig"

        cmd = [
            "kubectl",
            "get",
            "secret",
            secret_name,
            "-n",
            namespace,
            "-o",
            "jsonpath={.data.value}",
        ]

        try:
            cp = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"Failed to fetch kubeconfig secret '{secret_name}' "
                f"in namespace '{namespace}'. "
                f"Is the control plane ready?"
            ) from e

        # 3) Decode base64 kubeconfig
        try:
            kubeconfig_bytes = base64.b64decode(cp.stdout)
            return kubeconfig_bytes.decode("utf-8")
        except Exception as e:
            raise RuntimeError(
                f"Failed to decode kubeconfig from secret '{secret_name}'"
            ) from e


    def role_apparmor_setup(self, h: _SSHHandles, host: Host, opts: NodeBootstrapOptions):
        """
        - Ensure APT repos are correct (resolves $(lsb_release -cs))
        - Install apparmor + Python tools
        - Enable apparmor and install kubernetes Python client
        - Write kubeconfig for user
        """
        # Detect codename dynamically
        rc, codename, _ = self._run(h, "lsb_release -cs", sudo=False)
        codename = codename.strip() or "jammy"

        # Clean invalid lines
        self._run(h, "sudo sed -i '/\\$(lsb_release/d' /etc/apt/sources.list", sudo=True)

        # Correct repos
        repos = [
            f"deb http://archive.ubuntu.com/ubuntu {codename} main universe",
            f"deb http://archive.ubuntu.com/ubuntu {codename}-updates main universe",
            f"deb http://security.ubuntu.com/ubuntu {codename}-security main universe",
        ]

        for r in repos:
            self._append_line(h, r, "/etc/apt/sources.list", sudo=True)

        # Update + install
        self._run(h, "apt-get update -y", sudo=True)
        self._run(h, "DEBIAN_FRONTEND=noninteractive apt-get install -y apparmor apparmor-utils python3-pip python3-setuptools", sudo=True)
        self._run(h, "systemctl enable --now apparmor", sudo=True)
        self._run(h, "pip3 install --upgrade kubernetes", sudo=True)

        # Kubeconfig setup
        kube_dir = f"/home/{host.username}/.kube"
        self._ensure_dir(h, kube_dir, mode=0o700, sudo=True)
        content = self._kubeconfig_content(opts)
        self._put_content(h, content, opts.kubeconfig_remote_path.format(username=host.username),
                        mode=0o600, owner=f"{host.username}:{host.username}", sudo=True)


    def role_netplan_config(self, h: _SSHHandles, host: Host, opts: NodeBootstrapOptions):
        """
        - Render or use provided netplan YAML string for this host
        - Write to /etc/netplan/01-netcfg.yaml
        - netplan apply
        """
        if host.netplan_content is not None:
            rendered = host.netplan_content
        elif opts.netplan_renderer:
            rendered = opts.netplan_renderer(host)
        else:
            # No-op if nothing to render
            return
        self._put_content(h, rendered, opts.netplan_dest_path, mode=0o644, sudo=True)
        self._run(h, "netplan apply", sudo=True)

    def role_ssh_and_hostname(self, h: _SSHHandles, host: Host, opts: NodeBootstrapOptions):
        """
        - Install passlib (optional; we generate hash via openssl here)
        - Create user 'kez' (opts.managed_user) with password hash
        - Ensure ~/.ssh with authorized_keys
        - Passwordless sudo for the user
        - Set hostname
        - Add /etc/hosts entry for FQDN using ens18 address
        """
        # Password hash via openssl -6 (SHA-512). If not present, fallback to chpasswd without -e.
        # Create user if not exists, set password & add to sudo
        self._run(h, f"id -u {opts.managed_user} || useradd -m -s /bin/bash {opts.managed_user}", sudo=True)
        # set password (encrypted)
        self._run(h, f"echo '{opts.managed_user}:{opts.managed_user_password_plain}' | chpasswd", sudo=True)

        # ensure in sudo group
        self._run(h, f"usermod -aG sudo {opts.managed_user}", sudo=True)

        # ~/.ssh and authorized_keys
        ssh_dir = f"/home/{opts.managed_user}/.ssh"
        self._ensure_dir(h, ssh_dir, mode=0o700, sudo=True)
        self._run(h, f"chown -R {opts.managed_user}:{opts.managed_user} {ssh_dir}", sudo=True)
        if host.authorized_key_path:
            with open(host.authorized_key_path, "r", encoding="utf-8") as f:
                key = f.read().strip()
            self._put_content(h, key + "\n", f"{ssh_dir}/authorized_keys", mode=0o600, owner=f"{opts.managed_user}:{opts.managed_user}", sudo=True)

        # passwordless sudo
        sudo_line = f"{opts.managed_user} ALL=(ALL) NOPASSWD:ALL"
        self._put_content(h, sudo_line + "\n", f"/etc/sudoers.d/{opts.managed_user}", mode=0o440, sudo=True)

        # hostname — use short name for hostnamectl
        if "." in host.hostname:
            short_hostname = host.hostname.split(".", 1)[0]
            fqdn = host.hostname
        else:
            short_hostname = host.hostname
            fqdn = f"{host.hostname}.{opts.domain_suffix}"

        self._run(h, f"hostnamectl set-hostname {short_hostname}", sudo=True)

        # /etc/hosts — clean up stale image-build hostname (e.g. UBUNTU_24.04_NODE_IMAGE_...)
        # and ensure the 127.0.0.1 line only has "localhost"
        self._run(
            h,
            r"sed -i 's/^127\.0\.0\.1.*/127.0.0.1 localhost/' /etc/hosts",
            sudo=True,
        )

        # Add FQDN entry using the known address from inventory
        # so that `hostname -f` resolves correctly
        hosts_entry = f"{host.address} {fqdn} {short_hostname}"
        self._append_line(h, hosts_entry, "/etc/hosts", sudo=True)

    def role_inotify_limits(self, h: _SSHHandles, host: Host, opts: NodeBootstrapOptions):
        """
        Set sysctl keys for inotify limits and reload.
        """
        lines = [
            f"fs.inotify.max_user_instances = {opts.inotify_max_user_instances}",
            f"fs.inotify.max_user_watches = {opts.inotify_max_user_watches}",
        ]
        for ln in lines:
            self._append_line(h, ln, "/etc/sysctl.conf", sudo=True)
        self._run(h, "sysctl -p /etc/sysctl.conf || true", sudo=True)

    def role_istio_modules(self, h: _SSHHandles, host: Host, opts: NodeBootstrapOptions):
        """
        Write /etc/modules-load.d/99-istio-modules.conf and modprobe required modules.
        """
        content = textwrap.dedent("""\
            br_netfilter
            ip_tables
            iptable_filter
            iptable_mangle
            iptable_nat
            iptable_raw
            nf_nat
            x_tables
            xt_REDIRECT
            xt_conntrack
            xt_multiport
            xt_owner
            xt_tcpudp
        """)
        self._put_content(h, content, "/etc/modules-load.d/99-istio-modules.conf", mode=0o644, sudo=True)
        mods = [ln.strip() for ln in content.splitlines() if ln.strip()]
        for m in mods:
            self._run(h, f"modprobe {m} || true", sudo=True)

    # ------------------ public API ------------------

    def bootstrap(self, hosts: List[Host], plan: NodeBootstrapPlan, opts: NodeBootstrapOptions) -> None:
        """
        Connect to each host and run the requested roles.
        """
        for i, host in enumerate(hosts, 1):
            log.info("[nodes] Bootstrapping %s (%d/%d)...", host.hostname, i, len(hosts))
            # Retry SSH connection — freshly provisioned nodes may not be ready yet
            h = None
            for attempt in range(1, 31):
                try:
                    h = self._connect(host)
                    break
                except (paramiko.ssh_exception.AuthenticationException,
                        paramiko.ssh_exception.SSHException,
                        OSError) as e:
                    if attempt == 30:
                        raise RuntimeError(
                            f"Failed to SSH into {host.address} as '{host.username}' "
                            f"after 30 attempts: {e}"
                        ) from e
                    log.info(
                        "[%s] SSH not ready (attempt %d/30, %s: %s), retrying in 20s...",
                        host.hostname, attempt, type(e).__name__, e,
                    )
                    time.sleep(20)
            try:
                if plan.run_apparmor:
                    log.info("[%s] Running apparmor setup...", host.hostname)
                    self.role_apparmor_setup(h, host, opts)
                if plan.run_netplan:
                    log.info("[%s] Configuring netplan...", host.hostname)
                    self.role_netplan_config(h, host, opts)
                if plan.run_ssh_and_hostname:
                    log.info("[%s] Configuring SSH and hostname...", host.hostname)
                    self.role_ssh_and_hostname(h, host, opts)
                if plan.run_inotify_limits:
                    log.info("[%s] Setting inotify limits...", host.hostname)
                    self.role_inotify_limits(h, host, opts)
                if plan.run_istio_modules:
                    log.info("[%s] Loading istio kernel modules...", host.hostname)
                    self.role_istio_modules(h, host, opts)
                log.info("[%s] Bootstrap complete", host.hostname)
            finally:
                self._close(h)


# simple counter for unique temp names
def _counter_gen():
    i = 0
    while True:
        i += 1
        yield i
_counter = _counter_gen()
