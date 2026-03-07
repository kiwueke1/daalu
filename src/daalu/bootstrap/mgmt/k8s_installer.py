# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/mgmt/k8s_installer.py

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from daalu.bootstrap.mgmt.models import MgmtClusterConfig
from daalu.utils.ssh_runner import SSHRunner

log = logging.getLogger("daalu")


class K8sInstaller:
    """
    Installs Kubernetes (kubeadm) on a fresh Ubuntu machine via SSH,
    then installs Cilium CNI using a local helm invocation against the
    newly-created cluster.

    After install(), the caller can retrieve the kubeconfig text via
    the `kubeconfig_text` attribute and write it to a local path.
    """

    def __init__(self, ssh: SSHRunner, cfg: MgmtClusterConfig):
        self._ssh = ssh
        self._cfg = cfg
        self.kubeconfig_text: str = ""

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def install(self) -> str:
        """
        Run the full K8s install sequence.
        Returns the kubeconfig file content (YAML text).
        Idempotent: skips phases that are already complete.
        """
        log.info("[mgmt/k8s] Preparing node...")
        self._setup_passwordless_sudo()
        self._setup_provisioning_interface()

        if self._cluster_is_running():
            log.info("[mgmt/k8s] Cluster already running — skipping installation")
            self.kubeconfig_text = self._fetch_kubeconfig()
            return self.kubeconfig_text

        self._disable_swap()
        self._load_kernel_modules()
        self._set_sysctl()
        self._install_containerd()
        self._install_kube_tools()
        self._kubeadm_init()
        self.kubeconfig_text = self._fetch_kubeconfig()
        log.info("[mgmt/k8s] Kubernetes installed successfully")
        return self.kubeconfig_text

    def _cluster_is_running(self) -> bool:
        """Return True if the K8s API server is already up on the remote node."""
        rc, _, _ = self._ssh.run(
            "kubectl --kubeconfig /etc/kubernetes/admin.conf get nodes",
            sudo=True,
        )
        return rc == 0

    def install_cilium(self, kubeconfig_path: str) -> None:
        """
        Install Cilium CNI via Helm against the newly-created cluster.
        Called by the manager after writing the kubeconfig to a local file.
        """
        log.info("[mgmt/k8s] Installing Cilium CNI v%s...", self._cfg.cilium_version)
        subprocess.run(
            ["helm", "repo", "add", "cilium", "https://helm.cilium.io/"],
            check=False,
        )
        subprocess.run(["helm", "repo", "update"], check=True)
        subprocess.run(
            [
                "helm", "--kubeconfig", kubeconfig_path,
                "upgrade", "--install", "cilium", "cilium/cilium",
                "--version", self._cfg.cilium_version,
                "--namespace", "kube-system",
                "--set", "kubeProxyReplacement=true",
                "--set", f"k8sServiceHost={self._cfg.host}",
                "--set", "k8sServicePort=6443",
                # Required so the kube-apiserver process (running on the host,
                # not in a pod) can reach ClusterIP services such as admission
                # webhooks.  Without socket-level LB, host-namespace connections
                # to ClusterIP addresses return EPERM because there is no
                # kube-proxy to DNAT them and Cilium's eBPF only intercepts
                # pod-level traffic by default.
                "--set", "socketLB.enabled=true",
                "--set", "socketLB.hostNamespaceOnly=false",
                "--wait",
                "--timeout", "5m",
            ],
            check=True,
        )
        log.info("[mgmt/k8s] Cilium installed")

    # ------------------------------------------------------------------
    # Step 0 — grant passwordless sudo so all subsequent commands work
    # ------------------------------------------------------------------

    def _setup_passwordless_sudo(self) -> None:
        """
        Write a sudoers drop-in that grants NOPASSWD to the SSH user.

        Uses `sudo -S` to pipe the SSH password for the one-time bootstrap.
        If no ssh_password is configured (key-based auth with NOPASSWD already
        set up), this step is skipped.
        """
        password = self._cfg.ssh_password
        username = self._cfg.ssh_username

        if not password:
            log.debug(
                "[mgmt/k8s] No ssh_password in config — "
                "assuming passwordless sudo is already configured"
            )
            return

        log.info("[mgmt/k8s] Configuring passwordless sudo for '%s'...", username)

        sudoers_line = f"{username} ALL=(ALL) NOPASSWD:ALL"
        sudoers_file = "/etc/sudoers.d/daalu-mgmt-nopasswd"

        # Use sudo -S with bash -c so the sudoers content is embedded in the
        # command and only the password is read from stdin (avoids tee also
        # reading the password line as file content).
        import shlex
        inner = (
            f"printf '%s\\n' {shlex.quote(sudoers_line)} > {sudoers_file} "
            f"&& chmod 0440 {sudoers_file}"
        )
        cmd = f"sudo -S bash -c {shlex.quote(inner)}"
        stdin, stdout, stderr = self._ssh.client.exec_command(cmd)
        stdin.write(password + "\n")
        stdin.channel.shutdown_write()

        stdout.read()  # drain
        err = stderr.read().decode()
        rc = stdout.channel.recv_exit_status()

        if rc != 0:
            raise RuntimeError(
                f"Failed to configure passwordless sudo (exit {rc}). "
                f"Check that the SSH password is correct.\nstderr: {err}"
            )

        log.info("[mgmt/k8s] Passwordless sudo configured")

    # ------------------------------------------------------------------
    # Step 0b — assign static IP to provisioning interface
    # ------------------------------------------------------------------

    def _setup_provisioning_interface(self) -> None:
        """
        Assign a static IPv4 address to the provisioning interface and write
        a netplan drop-in so the address survives reboots.

        Skipped if provisioning_ip is not configured.
        """
        ip = self._cfg.provisioning_ip
        iface = self._cfg.provisioning_interface
        if not ip:
            return

        prefix = self._cfg.provisioning_prefix
        cidr = f"{ip}/{prefix}"

        # Check whether the IP is already present
        rc, out, _ = self._ssh.run(f"ip addr show dev {iface} 2>/dev/null", sudo=False)
        if rc == 0 and f" {ip}/" in out:
            log.info("[mgmt/k8s] %s already has %s — skipping", iface, cidr)
        else:
            log.info("[mgmt/k8s] Assigning %s to %s...", cidr, iface)
            self._run(f"ip addr add {cidr} dev {iface}", check=False)

        # Write a netplan drop-in (higher priority than cloud-init's 50-*)
        netplan_content = (
            "network:\n"
            "  version: 2\n"
            "  ethernets:\n"
            f"    {iface}:\n"
            "      dhcp4: false\n"
            "      addresses:\n"
            f"        - {cidr}\n"
        )
        netplan_path = "/etc/netplan/60-provisioning-static.yaml"
        rc2, existing, _ = self._ssh.run(f"cat {netplan_path} 2>/dev/null", sudo=True)
        if existing.strip() != netplan_content.strip():
            log.info("[mgmt/k8s] Writing netplan static config for %s...", iface)
            self._ssh.put_text(netplan_content, netplan_path, sudo=True)
            self._run(f"chmod 600 {netplan_path}")
            self._run("netplan apply", check=False)  # OVS warning is harmless

        log.info("[mgmt/k8s] Provisioning interface %s configured with %s", iface, cidr)

    # ------------------------------------------------------------------
    # Step 1 — disable swap (kubelet refuses to start with swap on)
    # ------------------------------------------------------------------

    def _disable_swap(self) -> None:
        log.info("[mgmt/k8s] Disabling swap...")
        self._run("swapoff -a")
        self._run("sed -i '/swap/s/^/#/' /etc/fstab")

    # ------------------------------------------------------------------
    # Step 2 — kernel modules required by containerd / K8s networking
    # ------------------------------------------------------------------

    def _load_kernel_modules(self) -> None:
        log.info("[mgmt/k8s] Loading kernel modules...")
        modules_conf = "overlay\nbr_netfilter\n"
        self._ssh.put_text(modules_conf, "/etc/modules-load.d/k8s.conf", sudo=True)
        self._run("modprobe overlay")
        self._run("modprobe br_netfilter")

    # ------------------------------------------------------------------
    # Step 3 — sysctl params for K8s networking
    # ------------------------------------------------------------------

    def _set_sysctl(self) -> None:
        log.info("[mgmt/k8s] Setting sysctl params...")
        sysctl_conf = (
            "net.bridge.bridge-nf-call-iptables  = 1\n"
            "net.bridge.bridge-nf-call-ip6tables = 1\n"
            "net.ipv4.ip_forward                 = 1\n"
        )
        self._ssh.put_text(sysctl_conf, "/etc/sysctl.d/k8s.conf", sudo=True)
        self._run("sysctl --system")

    # ------------------------------------------------------------------
    # Step 4 — containerd (CRI)
    # ------------------------------------------------------------------

    def _install_containerd(self) -> None:
        rc, _, _ = self._run("systemctl is-active containerd", check=False)
        if rc == 0:
            log.info("[mgmt/k8s] containerd already running — skipping")
            return
        log.info("[mgmt/k8s] Installing containerd...")
        self._run("apt-get update -qq")
        self._run("apt-get install -y containerd apt-transport-https ca-certificates curl gpg")
        self._run("mkdir -p /etc/containerd")
        self._run("containerd config default > /etc/containerd/config.toml")
        # Enable SystemdCgroup — required for kubeadm clusters
        self._run(
            'sed -i "s/SystemdCgroup = false/SystemdCgroup = true/" '
            "/etc/containerd/config.toml"
        )
        self._run("systemctl restart containerd")
        self._run("systemctl enable containerd")

    # ------------------------------------------------------------------
    # Step 5 — kubeadm / kubelet / kubectl
    # ------------------------------------------------------------------

    def _install_kube_tools(self) -> None:
        ver = self._cfg.kubernetes_version
        rc, _, _ = self._run("kubeadm version", check=False)
        if rc == 0:
            log.info("[mgmt/k8s] kubeadm already installed — skipping")
            return
        log.info("[mgmt/k8s] Installing kubeadm/kubelet/kubectl v%s...", ver)

        keyring_dir = "/etc/apt/keyrings"
        keyring_path = f"{keyring_dir}/kubernetes-apt-keyring.gpg"
        apt_source = (
            f"deb [signed-by={keyring_path}] "
            f"https://pkgs.k8s.io/core:/stable:/v{ver}/deb/ /"
        )

        self._run(f"mkdir -p {keyring_dir}")
        self._run(f"rm -f {keyring_path}")
        self._run(
            f"curl -fsSL --retry 5 --retry-delay 3 "
            f"https://pkgs.k8s.io/core:/stable:/v{ver}/deb/Release.key "
            f"| gpg --batch --dearmor -o {keyring_path}"
        )
        self._ssh.put_text(
            apt_source + "\n",
            "/etc/apt/sources.list.d/kubernetes.list",
            sudo=True,
        )
        self._run("apt-get update -qq")
        self._run("apt-get install -y kubelet kubeadm kubectl")
        self._run("apt-mark hold kubelet kubeadm kubectl")
        self._run("systemctl enable kubelet")

    # ------------------------------------------------------------------
    # Step 6 — kubeadm init
    # ------------------------------------------------------------------

    def _kubeadm_init(self) -> None:
        # Reset any previous (partial) init — safe to run on a fresh node
        self._run("kubeadm reset --force", check=False)
        log.info("[mgmt/k8s] Running kubeadm init (pod CIDR: %s)...", self._cfg.pod_cidr)
        rc, out, err = self._ssh.run(
            f"kubeadm init "
            f"--pod-network-cidr={self._cfg.pod_cidr} "
            f"--service-cidr={self._cfg.service_cidr} "
            f"--apiserver-advertise-address={self._cfg.host} "
            f"--skip-phases=addon/kube-proxy",  # Cilium replaces kube-proxy
            sudo=True,
            timeout=300,
        )
        if rc != 0:
            raise RuntimeError(f"kubeadm init failed (exit {rc}):\n{err}")
        log.info("[mgmt/k8s] kubeadm init complete")

        # Make kubectl work for root and the SSH user on the remote node
        self._run(
            "mkdir -p $HOME/.kube && "
            "cp /etc/kubernetes/admin.conf $HOME/.kube/config && "
            "chown $(id -u):$(id -g) $HOME/.kube/config"
        )

        # Single-node mgmt cluster — remove control-plane taint so workloads schedule
        log.info("[mgmt/k8s] Removing control-plane taint for single-node cluster...")
        self._run(
            "kubectl --kubeconfig /etc/kubernetes/admin.conf "
            "taint nodes --all node-role.kubernetes.io/control-plane-",
            check=False,  # ignore if already absent
        )

    # ------------------------------------------------------------------
    # Step 7 — fetch kubeconfig from remote
    # ------------------------------------------------------------------

    def _fetch_kubeconfig(self) -> str:
        log.info("[mgmt/k8s] Fetching kubeconfig from remote node...")
        rc, out, err = self._ssh.run(
            "cat /etc/kubernetes/admin.conf", sudo=True
        )
        if rc != 0 or not out.strip():
            raise RuntimeError(f"Failed to read kubeconfig from remote node: {err}")
        return out

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _run(self, cmd: str, check: bool = True) -> tuple[int, str, str]:
        rc, out, err = self._ssh.run(cmd, sudo=True)
        if check and rc != 0:
            raise RuntimeError(f"Remote command failed (exit {rc}): {cmd!r}\n{err}")
        return rc, out, err
