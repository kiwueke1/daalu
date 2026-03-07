# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

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

    def role_containerd_registry(self, h: _SSHHandles, host: Host, opts: NodeBootstrapOptions):
        """
        Configure the container runtime to pull from local registries with
        TLS skip-verify (Harbor's self-signed cert is not in the system trust store).

        Writes two configs:
          1. containerd certs.d (containerd >= 1.5):
               /etc/containerd/certs.d/{registry}/hosts.toml  (skip_verify = true)
             Also ensures config_path is set in containerd's config.toml.

          2. CRI-O / Podman fallback:
               /etc/containers/registries.conf.d/00-local.conf

        Restarts both runtimes if present.
        """
        if not opts.insecure_registries:
            return

        # -- containerd: certs.d / hosts.toml ---------------------------------
        for registry in opts.insecure_registries:
            hosts_toml = (
                f'server = "https://{registry}"\n\n'
                f'[host."https://{registry}"]\n'
                f'  capabilities = ["pull", "resolve"]\n'
                f'  skip_verify = true\n'
            )
            certs_dir = f"/etc/containerd/certs.d/{registry}"
            self._run(h, f"mkdir -p {certs_dir}", sudo=True)
            self._put_content(
                h, hosts_toml, f"{certs_dir}/hosts.toml", mode=0o644, sudo=True,
            )

        # Ensure containerd config.toml references certs.d
        patch_cmd = (
            "CONFIG=/etc/containerd/config.toml\n"
            "[ -f \"$CONFIG\" ] || exit 0\n"
            "if grep -q 'config_path' \"$CONFIG\"; then\n"
            "  sed -i 's|config_path = .*|config_path = \"/etc/containerd/certs.d\"|' \"$CONFIG\"\n"
            "else\n"
            "  printf '\\n[plugins.\"io.containerd.grpc.v1.cri\".registry]\\n  config_path = \"/etc/containerd/certs.d\"\\n' >> \"$CONFIG\"\n"
            "fi"
        )
        self._run(h, patch_cmd, sudo=True)

        rc, _, _ = self._run(h, "systemctl is-active containerd", sudo=False)
        if rc == 0:
            self._run(h, "systemctl restart containerd", sudo=True)
            log.debug("[%s] containerd restarted with updated registry config", host.hostname)

        # -- CRI-O / Podman fallback ------------------------------------------
        blocks = "\n".join(
            f'[[registry]]\nlocation = "{r}"\ninsecure = true'
            for r in opts.insecure_registries
        )
        self._run(h, "mkdir -p /etc/containers/registries.conf.d", sudo=True)
        self._put_content(
            h,
            blocks + "\n",
            "/etc/containers/registries.conf.d/00-local.conf",
            mode=0o644,
            sudo=True,
        )

        rc, _, _ = self._run(h, "systemctl is-active crio", sudo=False)
        if rc == 0:
            self._run(h, "systemctl restart crio", sudo=True)
            log.debug("[%s] crio restarted with updated registry config", host.hostname)

        log.debug(
            "[%s] containerd/crio configured for insecure registries: %s",
            host.hostname, opts.insecure_registries,
        )

    def role_migrate_provisioning_bridge(
        self,
        h: _SSHHandles,
        host: Host,
        opts: NodeBootstrapOptions,
    ) -> None:
        """
        Replace the provisioning Linux bridge with an OVS internal port,
        freeing eno1 to be the physical uplink for OVS br-ex.

        BEFORE (Metal3 provisioning leaves nodes in this state):
            eno1 ──master──► provisioning (Linux bridge, IP: 10.10.x.x/y)
            OVS br-ex ──wants eno1──► ERROR: Device or resource busy

        AFTER:
            OVS br-ex:
              ├── eno1          (physical uplink — now free)
              └── provisioning  (OVS type=internal port, IP: 10.10.x.x/y)

        WHY NO VLAN / SWITCH CHANGES ARE NEEDED
        ----------------------------------------
        The provisioning IP (e.g. 10.10.50.x) lives on the OVS internal port,
        which is in the same OVS bridge as eno1.  Traffic to/from that IP
        flows through OVS exactly as it did through the Linux bridge — same
        wire, same MAC, no tags.  The switch and pfSense see no difference.

        PERSISTENCE
        -----------
        • OVS stores bridge topology in its database (/etc/openvswitch/conf.db).
          After restart, OVS recreates br-ex, eno1 port, and provisioning port
          automatically — no netplan involvement needed for the bridge structure.
        • The IP address on the provisioning port is kernel state, not stored by
          OVS.  A systemd oneshot service (ovs-provisioning-ip.service) waits for
          the OVS port to appear and then assigns the IP on every boot.
        • The netplan file (52-ironicendpoint.yaml) is updated to remove the Linux
          bridge definition so it is not recreated on reboot.

        IDEMPOTENCY
        -----------
        If eno1 is already free from the Linux bridge this role is a no-op.

        REQUIREMENT
        -----------
        OVS must already be deployed (the br-ex bridge must exist in the OVSDB)
        before running this role.  Run it after the OpenvSwitch Helm chart is
        deployed but before OVN/Neutron.
        """
        nic = opts.provisioning_bridge_nic
        bridge = opts.provisioning_bridge_name
        ovs_br = opts.provisioning_ovs_bridge

        # ── idempotency: skip if eno1 is already free ────────────────────────
        rc, out, _ = self._run(h, f"ip link show dev {nic}", sudo=False)
        if f"master {bridge}" not in out:
            log.info(
                "[%s] %s is not mastered by %s — migration already done, skipping",
                host.hostname, nic, bridge,
            )
            return

        # ── save current provisioning IP before touching anything ────────────
        rc, prov_ip_raw, _ = self._run(
            h,
            f"ip -o addr show dev {bridge} | awk '/inet /{{print $4; exit}}'",
            sudo=False,
        )
        prov_ip = prov_ip_raw.strip()
        if not prov_ip:
            raise RuntimeError(
                f"[{host.hostname}] Could not read IP from {bridge} — "
                "is the provisioning bridge up?"
            )

        log.info(
            "[%s] Migrating provisioning bridge (IP %s) to OVS internal port on %s...",
            host.hostname, prov_ip, ovs_br,
        )

        # ── step 1: detach eno1 from the Linux bridge ─────────────────────────
        self._run(h, f"ip link set {nic} nomaster", sudo=True)

        # ── step 2: give eno1 to OVS br-ex ───────────────────────────────────
        self._run(h, f"ovs-vsctl --may-exist add-port {ovs_br} {nic}", sudo=True)

        # ── step 3: delete the Linux bridge (IP vanishes for ~1 s) ───────────
        self._run(h, f"ip link del {bridge}", sudo=True)

        # ── step 4: create OVS internal port with the same name ──────────────
        self._run(
            h,
            f"ovs-vsctl --may-exist add-port {ovs_br} {bridge}"
            f" -- set Interface {bridge} type=internal",
            sudo=True,
        )

        # ── step 5: restore the IP ────────────────────────────────────────────
        self._run(h, f"ip link set {bridge} up", sudo=True)
        self._run(h, f"ip addr add {prov_ip} dev {bridge}", sudo=True)

        log.info(
            "[%s] Live migration done — provisioning IP %s restored on OVS port",
            host.hostname, prov_ip,
        )

        # ── step 6: systemd service for IP persistence across reboots ─────────
        self._install_ovs_provisioning_ip_service(h, host, bridge, prov_ip)

        # ── step 7: remove Linux bridge from netplan ──────────────────────────
        self._remove_provisioning_bridge_from_netplan(h, host, bridge)

    def _install_ovs_provisioning_ip_service(
        self,
        h: _SSHHandles,
        host: Host,
        bridge: str,
        prov_ip: str,
    ) -> None:
        """
        Install a systemd oneshot service that waits for the OVS-managed
        provisioning interface to appear (after the OVS DaemonSet pod starts)
        and then assigns the saved IP address to it.

        The service runs on every boot, ensuring the provisioning IP is always
        present regardless of how long the OVS pod takes to initialise.
        """
        svc = "ovs-provisioning-ip"

        script_content = textwrap.dedent(f"""\
            #!/bin/bash
            # Wait for OVS to create the provisioning internal port.
            # The OVS DaemonSet pod may take up to 120 s to start after boot.
            for i in $(seq 60); do
                ip link show {bridge} >/dev/null 2>&1 && break
                sleep 2
            done

            if ! ip link show {bridge} >/dev/null 2>&1; then
                echo "ERROR: timed out waiting for OVS port '{bridge}'" >&2
                exit 1
            fi

            ip link set {bridge} up
            ip addr replace {prov_ip} dev {bridge}
            echo "OK: {bridge} IP assigned ({prov_ip})"
        """)

        unit_content = textwrap.dedent(f"""\
            [Unit]
            Description=Assign IP to OVS {bridge} internal port
            After=network.target
            After=network-online.target
            Wants=network-online.target

            [Service]
            Type=oneshot
            RemainAfterExit=yes
            ExecStart=/usr/local/bin/{svc}.sh

            [Install]
            WantedBy=multi-user.target
        """)

        self._put_content(
            h, script_content, f"/usr/local/bin/{svc}.sh", mode=0o755, sudo=True,
        )
        self._put_content(
            h, unit_content, f"/etc/systemd/system/{svc}.service", mode=0o644, sudo=True,
        )

        self._run(h, "systemctl daemon-reload", sudo=True)
        self._run(h, f"systemctl enable {svc}.service", sudo=True)
        # Start it now too — the interface already exists from our live migration
        self._run(h, f"systemctl start {svc}.service || true", sudo=True)

        log.info(
            "[%s] systemd service %s.service installed and enabled", host.hostname, svc,
        )

    def _remove_provisioning_bridge_from_netplan(
        self,
        h: _SSHHandles,
        host: Host,
        bridge: str,
    ) -> None:
        """
        Remove the provisioning Linux bridge stanza from whichever netplan
        YAML file defines it, so it is not recreated on the next reboot.

        Does NOT run ``netplan apply`` — the live network state is already
        correct, and applying would attempt to delete the OVS-managed interface
        which would fail.  The change takes effect on the next reboot.
        """
        script = (
            "import yaml, glob, sys\n"
            f"BRIDGE = '{bridge}'\n"
            "updated = False\n"
            "for path in sorted(glob.glob('/etc/netplan/*.yaml')):\n"
            "    with open(path) as f:\n"
            "        data = yaml.safe_load(f)\n"
            "    if not data:\n"
            "        continue\n"
            "    net = data.get('network', {})\n"
            "    bridges = net.get('bridges', {})\n"
            "    if BRIDGE not in bridges:\n"
            "        continue\n"
            "    del bridges[BRIDGE]\n"
            "    if not bridges:\n"
            "        net.pop('bridges', None)\n"
            "    with open(path, 'w') as f:\n"
            "        yaml.dump(data, f, default_flow_style=False)\n"
            "    print(f'Removed Linux bridge {BRIDGE!r} from {path}')\n"
            "    updated = True\n"
            "    break\n"
            "if not updated:\n"
            "    print(f'No netplan file found for bridge {BRIDGE!r}', file=sys.stderr)\n"
        )

        rc, out, err = self._run(h, f"python3 -c {self._q(script)}", sudo=True)
        if rc != 0:
            log.warning(
                "[%s] Could not remove provisioning bridge from netplan: %s",
                host.hostname, err or out,
            )
        else:
            log.info("[%s] %s", host.hostname, out.strip())
            log.info(
                "[%s] Netplan updated — Linux bridge will not be recreated on reboot",
                host.hostname,
            )

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
                if plan.run_containerd_registry and opts.insecure_registries:
                    log.info("[%s] Configuring containerd insecure registries...", host.hostname)
                    self.role_containerd_registry(h, host, opts)
                if plan.run_migrate_provisioning_bridge:
                    log.info("[%s] Migrating provisioning bridge to OVS internal port...", host.hostname)
                    self.role_migrate_provisioning_bridge(h, host, opts)
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
