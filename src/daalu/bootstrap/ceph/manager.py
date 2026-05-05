# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import os
import logging
import paramiko
from typing import List, Optional, Tuple
import sys
import datetime
from pathlib import Path
from .models import CephHost, CephConfig
from datetime import datetime

log = logging.getLogger("daalu")


from ...observers.dispatcher import EventBus
from ...observers.console import ConsoleObserver
from ...observers.events import (
    new_ctx,
    CephStarted,
    CephProgress,
    CephFailed,
    CephSucceeded,
)


class CephManager:
    """
    Bootstraps a Ceph cluster via cephadm:
      - Use CEPHADM_IMAGE (derived from version unless given)
      - Deploy mon/mgr (from 'cephs' group)
      - Deploy OSDs (all-available-devices by default)
    All cephadm orchestration commands are executed on the PRIMARY host.
    """

    def __init__(self, bus: EventBus, connect_timeout: float = 20.0, cmd_timeout: float = 300.0):
        self.connect_timeout = connect_timeout
        self.cmd_timeout = cmd_timeout
        self.bus = bus or EventBus(observers=[ConsoleObserver()])
        self.run_ctx = new_ctx(env="workload", context="default")
        self._log_file = self._init_log_file()

    #-------------- Logging helpers ----------


    def _init_log_file(self) -> Path:
        """Create a timestamped log file under ./logs/"""
        log_dir = Path(__file__).resolve().parent / "logs"
        log_dir.mkdir(exist_ok=True)
        #timestamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        log_file = log_dir / f"ceph-deploy-{timestamp}.log"
        with open(log_file, "w") as f:
            f.write(f"# Ceph deployment log started {timestamp} UTC\n\n")
        return log_file

    def _log(self, message: str):
        """
        Write a concise timestamped message to both:
        - CLI (stdout)
        - Main deployment log file (./logs/ceph-deploy-*.log)
        """
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"[{timestamp}] {message}"

        # Print to CLI (high-level summary)
        log.debug(line)

        # Write to deployment log file
        with open(self._log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")



    # ------------- SSH helpers -------------

    def _connect(self, host: CephHost) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        pkey = None

        if host.pkey_path:
            try:
                # Try RSA first
                pkey = paramiko.RSAKey.from_private_key_file(host.pkey_path)
            except paramiko.ssh_exception.SSHException:
                try:
                    # Try ED25519
                    pkey = paramiko.Ed25519Key.from_private_key_file(host.pkey_path)
                except paramiko.ssh_exception.SSHException:
                    # Try ECDSA as last fallback
                    pkey = paramiko.ECDSAKey.from_private_key_file(host.pkey_path)

        using_password = not pkey
        client.connect(
            hostname=host.address,
            port=host.port,
            username=host.username,
            password=host.password if using_password else None,
            pkey=pkey,
            timeout=self.connect_timeout,
            allow_agent=not using_password,
            look_for_keys=not using_password,
        )
        return client

    def _run(
        self,
        cli: paramiko.SSHClient,
        cmd: str,
        env: Optional[dict] = None,
        sudo: bool = True,
        host: Optional["CephHost"] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[int, str, str]:
        """
        Run a shell command on a remote host via SSH.
        - Mirrors all commands and output to both the ceph deploy log file
          and the main daalu logger (log.debug), so everything appears in
          the main daalu run log.
        - Enforces a wall-clock deadline (timeout arg, falls back to self.cmd_timeout).
        """
        import time

        deadline = time.monotonic() + (timeout if timeout is not None else self.cmd_timeout)
        log_file = Path(self._log_file)
        hostname = host.hostname if host else "unknown"

        prefix = ""
        if env:
            exports = " ".join(f'{k}={self._shq(v)}' for k, v in env.items())
            prefix += f"{exports} "
        shell_cmd = f"{prefix}{cmd}"
        final = f"sudo -S bash -lc {self._shq(shell_cmd)}" if sudo else f"bash -lc {self._shq(shell_cmd)}"

        start_ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        header = f"[{start_ts}] ({hostname}) $ {cmd}"
        log.debug(header)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\n{header}\n")

        stdin, stdout, stderr = cli.exec_command(final, timeout=self.cmd_timeout)

        # Feed sudo password via stdin when the host uses password auth
        if sudo and host and host.password:
            try:
                stdin.write(host.password + "\n")
                stdin.flush()
            except Exception:
                pass

        out_chunks, err_chunks = [], []
        while not stdout.channel.exit_status_ready():
            if time.monotonic() > deadline:
                elapsed = timeout or self.cmd_timeout
                msg = f"({hostname}) [TIMEOUT after {elapsed}s — channel closed]"
                log.debug(msg)
                stdout.channel.close()
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(msg + "\n")
                return 124, "".join(out_chunks), "".join(err_chunks) + "\n[timed out]"
            if stdout.channel.recv_ready():
                chunk = stdout.channel.recv(4096).decode("utf-8", "replace")
                out_chunks.append(chunk)
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(chunk)
            if stdout.channel.recv_stderr_ready():
                chunk = stdout.channel.recv_stderr(4096).decode("utf-8", "replace")
                err_chunks.append(chunk)
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(chunk)
            time.sleep(0.1)

        rc = stdout.channel.recv_exit_status()
        out_rem = stdout.read().decode("utf-8", "replace")
        err_rem = stderr.read().decode("utf-8", "replace")
        if out_rem:
            out_chunks.append(out_rem)
        if err_rem:
            err_chunks.append(err_rem)

        full_out = "".join(out_chunks).strip()
        full_err = "".join(err_chunks).strip()

        exit_line = f"({hostname}) [exit {rc}]"
        with open(log_file, "a", encoding="utf-8") as f:
            if out_rem.strip():
                f.write(out_rem)
            if err_rem.strip():
                f.write(err_rem)
            f.write(f"\n{exit_line}\n")

        # Mirror combined output to the main daalu logger
        if full_out:
            for line in full_out.splitlines():
                log.debug("  [stdout] %s", line)
        if full_err:
            for line in full_err.splitlines():
                log.debug("  [stderr] %s", line)
        log.debug(exit_line)

        return rc, "".join(out_chunks), "".join(err_chunks)



    def _shq(self, s: str) -> str:
        """Shell-quote helper."""
        return "'" + s.replace("'", "'\\''") + "'"
        



    def _ensure_container_engine(self, cli) -> None:
        """
        Ensures that Docker or Podman is installed on the remote host.
        Installs Docker if no container engine is present.
        """
        # Check if Docker or Podman exists
        rc, out, err = self._run(cli, "command -v docker || command -v podman", sudo=True)
        if rc == 0:
            log.debug(f"[ceph] Container engine already present: {out.strip()}")
            return

        log.debug("[ceph] No container engine found, installing Docker...")

        install_script = (
            "curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && "
            "chmod +x /tmp/get-docker.sh && "
            "sh /tmp/get-docker.sh && "
            "systemctl enable docker && "
            "systemctl start docker"
        )

        rc, out, err = self._run(cli, install_script, sudo=True)
        if rc != 0:
            raise RuntimeError(f"[ceph] Docker installation failed: {err or out}")

        # Verify installation
        rc, out, err = self._run(cli, "docker --version", sudo=True)
        if rc != 0:
            raise RuntimeError(f"[ceph] Docker verification failed: {err or out}")
        log.debug(f"[ceph] Docker installed successfully: {out.strip()}")


    def _install_cephadm(self, cli, version: str = "20.2.0") -> None:
        """
        Installs cephadm on the remote host at the given version.
        Must be run as a user with passwordless sudo privileges.
        """
        cephadm_url = f"https://download.ceph.com/rpm-{version}/el9/noarch/cephadm"

        # Download cephadm to /usr/local/bin directly (requires sudo)
        install_cmd = (
            f"curl -fsSL -o /usr/local/bin/cephadm {cephadm_url} && "
            "chmod 755 /usr/local/bin/cephadm"
        )

        rc, out, err = self._run(cli, install_cmd, sudo=True)
        if rc != 0:
            raise RuntimeError(f"[ceph] cephadm download or install failed: {err or out}")

        # Verify cephadm can run and report version
        rc, out, err = self._run(cli, "cephadm version", sudo=True)
        if rc == 0:
            log.debug(f"[ceph] cephadm installed successfully: {out.strip()}")
        else:
            raise RuntimeError(f"[ceph] cephadm installation verification failed: {err or out}")


    def deploy(self, hosts: List[CephHost], cfg: CephConfig) -> None:
        """Main orchestrator for Ceph deployment."""
        if not hosts:
            raise ValueError("No Ceph hosts provided")

        image = cfg.image or f"quay.io/ceph/ceph:v{cfg.version}"
        primary = hosts[0]
        others = hosts[1:]
        log.debug("[ceph] primary=%s  others=%s  image=%s", primary.hostname, [h.hostname for h in others], image)

        log.debug("[ceph] connecting to primary %s (%s)...", primary.hostname, primary.address)
        cli = self._connect(primary)
        log.debug("[ceph] connected to primary %s", primary.hostname)

        self.bus.emit(
            CephStarted(stage="init", message=f"Starting Ceph deployment on {primary.hostname}", **self.run_ctx)
        )

        try:
            # 1. Base prerequisites
            log.debug("[ceph] step 1/6: ensuring container engine on %s", primary.hostname)
            self._ensure_container_engine(cli, primary)
            log.debug("[ceph] step 2/6: ensuring cephadm on %s", primary.hostname)
            self._ensure_cephadm(cli, primary, cfg.version)
            log.debug("[ceph] step 3/6: pre-pulling image %s", image)
            self._prepull_image(cli, image)
            log.debug("[ceph] fixing apparmor bug")
            self._patch_cephadm_apparmor_bug_on_hosts(hosts)

            # 2. Bootstrap cluster
            log.debug("[ceph] step 4/6: bootstrapping cluster on %s", primary.hostname)
            self._bootstrap_cluster(cli, cfg, image, primary)

            # Re-patch after bootstrap: cephadm deploys a new extracted copy of
            # itself into /var/lib/ceph/<fsid>/cephadm.<hash>/__main__.py during
            # bootstrap. That copy is unpatched and must be fixed before any
            # orchestrator operations (orch daemon add, orch apply, etc.).
            log.debug("[ceph] re-patching cephadm AppArmor bug on primary after bootstrap")
            self._patch_cephadm_apparmor_bug_on_hosts([primary])

            # 3. SSH + hosts
            log.debug("[ceph] step 5/6: distributing SSH keys to %d host(s)", len(others))
            self._distribute_ssh_keys(primary, others)
            self._configure_global_image(cli, image)
            self._configure_public_network(cli, cfg, hosts)
            log.debug("[ceph] adding %d additional host(s) to cluster", len(others))
            self._add_hosts(cli, primary, others, cfg.version)

            # After host-add, cephadm deploys an extracted copy of itself into
            # /var/lib/ceph/<fsid>/cephadm.<hash>/ on every newly added host.
            # That copy is unpatched and must be fixed before OSD operations.
            log.debug("[ceph] re-patching cephadm AppArmor bug on all hosts after host add")
            self._patch_cephadm_apparmor_bug_on_hosts(hosts)

            self._restart_mgr(cli)

            # 4. Placements + OSDs
            log.debug("[ceph] step 6/6: applying placements and OSDs")
            self._apply_placements(cli, cfg, hosts)

            # Remove conflicting osd.default spec BEFORE adding OSDs, then
            # wait for all hosts (including newly-added external ones) to be
            # reachable by the orchestrator before attempting OSD deployment.
            self._remove_osd_default_spec(cli)
            if others:
                self._wait_for_hosts_ready(cli, [h.hostname for h in others])
            self._apply_osds(cli, cfg, hosts)
            self._wait_for_osds(cli, hosts)

            # 5. Post-OSD cleanup
            self._post_osd_cleanup(cli, cfg)

            # 6. Health check
            self._check_health(cli)

        finally:
            cli.close()


    # ----------------------------------------------------------------------
    def _ensure_cephadm(self, cli, host: CephHost, version: str = "20.2.0"):
        """Ensure cephadm is installed and matches the target Ceph image version.

        If cephadm is present but its major version doesn't match the target
        (e.g. tentacle binary vs reef image), it is reinstalled automatically.
        """
        self.bus.emit(CephProgress(stage="cephadm_check", message="Checking cephadm presence...", **self.run_ctx))
        rc, out, err = self._run(cli, "command -v cephadm || echo MISSING", sudo=False)
        if "MISSING" in (out + err):
            self.bus.emit(CephProgress(stage="cephadm_install", message=f"Installing cephadm {version} on {host.hostname}", **self.run_ctx))
            self._install_cephadm(cli, version)
            return

        # Verify version matches — "cephadm version" prints e.g. "cephadm version 20.2.0-0 ..."
        _, ver_out, _ = self._run(cli, "cephadm version 2>&1 || true", sudo=True, timeout=15)
        major = version.split(".")[0]  # e.g. "20" for "20.2.0"
        if major not in ver_out:
            self.bus.emit(CephProgress(
                stage="cephadm_reinstall",
                message=f"cephadm version mismatch (wanted {version}, got: {ver_out.strip()!r}); reinstalling on {host.hostname}",
                **self.run_ctx,
            ))
            self._install_cephadm(cli, version)
        else:
            self.bus.emit(CephProgress(stage="cephadm_check", message=f"cephadm {version} already installed", **self.run_ctx))

    # ----------------------------------------------------------------------
    def _ensure_container_engine(self, cli, host: CephHost):
        """Install and verify Docker or Podman."""
        self.bus.emit(CephProgress(stage="container_engine_check", message="Checking container engine...", **self.run_ctx))
        rc, out, err = self._run(cli, "command -v docker || command -v podman", sudo=True)
        if rc == 0:
            self.bus.emit(CephProgress(stage="container_engine_check", message=f"Found container engine: {out.strip()}", **self.run_ctx))
            return

        self.bus.emit(CephProgress(stage="container_engine_install", message=f"Installing Docker on {host.hostname}", **self.run_ctx))
        install_docker = (
            "export DEBIAN_FRONTEND=noninteractive && "
            "curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && "
            "chmod +x /tmp/get-docker.sh && "
            "sh /tmp/get-docker.sh || true && "
            "sudo apt-get install -y -o Dpkg::Options::='--force-confnew' "
            "containerd.io docker-ce docker-ce-cli docker-compose-plugin && "
            "sudo systemctl enable docker --now && sudo systemctl restart docker"
        )

        rc, out, err = self._run(cli, install_docker, sudo=True)
        if rc != 0:
            msg = (err or out or "").lower()
            if any(bad in msg for bad in ["release file", "duplicate", "held broken packages", "dependency problems"]):
                self._repair_apt(cli, host)
                rc2, out2, err2 = self._run(cli, install_docker, sudo=True)
                if rc2 != 0:
                    self.bus.emit(CephFailed(stage="container_engine_install", error=err2 or out2, **self.run_ctx))
                    raise RuntimeError(f"[ceph] Docker installation failed after repair: {err2 or out2}")
            else:
                raise RuntimeError(f"[ceph] Docker installation failed: {err or out}")

        self.bus.emit(CephProgress(stage="container_engine_success", message="Docker installed successfully", **self.run_ctx))

    # ----------------------------------------------------------------------
    def _repair_apt(self, cli, host: CephHost):
        """Repair broken APT environment using aptitude."""
        self.bus.emit(CephProgress(stage="apt_repair", message=f"Repairing APT environment on {host.hostname}", **self.run_ctx))
        heal_apt_env = (
            "export DEBIAN_FRONTEND=noninteractive && "
            "sudo rm -f /var/lib/apt/lists/lock /var/cache/apt/archives/lock /var/lib/dpkg/lock* || true && "
            "sudo dpkg --configure -a && "
            "sudo apt-get -f install -y || true && "
            "sudo apt-get install -y aptitude || true && "
            "sudo aptitude -f install -y || true && "
            "sudo aptitude reinstall -y apt ca-certificates curl gnupg lsb-release || true && "
            "sudo aptitude update -y && "
            "sudo aptitude full-upgrade -y && "
            "(sudo apt-mark unhold docker-ce docker-ce-cli containerd.io || true)"
        )
        self._run(cli, heal_apt_env, sudo=True)

    # ----------------------------------------------------------------------
    def _prepull_image(self, cli, image: str):
        """Pull Ceph image ahead of bootstrap, skipping if already present locally."""
        # Check whether the image is already available (avoid a potentially long pull)
        check_cmd = (
            f"(docker image inspect {image} >/dev/null 2>&1) || "
            f"(podman image inspect {image} >/dev/null 2>&1)"
        )
        rc, _, _ = self._run(cli, check_cmd, sudo=True, timeout=15)
        if rc == 0:
            self.bus.emit(CephProgress(stage="image_pull", message=f"Ceph image {image} already present, skipping pull", **self.run_ctx))
            return

        self.bus.emit(CephProgress(stage="image_pull", message=f"Pulling Ceph image {image} (this may take several minutes)...", **self.run_ctx))
        # Allow up to 20 minutes for a large image pull
        self._run(cli, f"(podman pull {image} || docker pull {image}) || true", sudo=True, timeout=1200)

    # ----------------------------------------------------------------------
    def _bootstrap_cluster(self, cli, cfg: CephConfig, image: str, host: CephHost):
        """Bootstrap Ceph cluster if not already bootstrapped."""
        mon_ip = host.address

        # --- Step 0: Check if Ceph is already running ---
        check_cmd = (
            "sudo cephadm shell -- ceph status >/dev/null 2>&1 "
            "|| test -f /etc/ceph/ceph.conf"
        )
        rc_check, _, _ = self._run(cli, check_cmd, sudo=False)
        if rc_check == 0:
            self._log(
                f"[cephadm] Detected existing Ceph cluster on {host.hostname} "
                f"({host.address}); skipping bootstrap."
            )
            return  # Skip bootstrap if cephadm already initialized

        # --- Step 1: Run bootstrap if not detected ---
        cmd = (
            f"cephadm --image {image} bootstrap --mon-ip {mon_ip} "
            f"--initial-dashboard-user {cfg.initial_dashboard_user} "
            f"--initial-dashboard-password {cfg.initial_dashboard_password} "
            "--skip-monitoring-stack --allow-overwrite --allow-mismatched-release"
        )

        rc, out, err = self._run(cli, cmd, sudo=True)

        if rc != 0:
            msg = err or out or ""
            # Handle port-in-use case gracefully
            if "Address already in use" in msg or "Cannot bind to IP" in msg:
                self._log(
                    f"[cephadm] Ceph already bootstrapped or mon active on {mon_ip}; skipping re-bootstrap."
                )
                return
            raise RuntimeError(f"cephadm bootstrap failed: {msg}")

        self._log(f"[cephadm] Ceph cluster bootstrapped successfully on {host.hostname}.")


    # ----------------------------------------------------------------------
    def _distribute_ssh_keys(self, primary: CephHost, others: List[CephHost]):
        """Copy Ceph orchestrator SSH public key to all nodes."""
        cli = self._connect(primary)
        rc, pubkey, err = self._run(cli, "cat /etc/ceph/ceph.pub", sudo=True)
        for h in others:
            c2 = self._connect(h)
            self._run(c2, f'mkdir -p /root/.ssh && echo "{pubkey.strip()}" >> /root/.ssh/authorized_keys', sudo=True)
            c2.close()

    # ----------------------------------------------------------------------
    def _configure_global_image(self, cli, image: str):
        """Set the Ceph global container image."""
        self._run(cli, f"cephadm shell -- ceph config set global container_image {image}", sudo=True)

    # ----------------------------------------------------------------------
    def _configure_public_network(self, cli, cfg: "CephConfig", hosts: list) -> None:
        """Set public_network in the Ceph config so OSD daemons on all hosts can
        select a local interface to bind to.

        Without this, cephadm derives public_network from the mon-ip subnet.
        When external OSD-only hosts (e.g. baremetal-server-02) are on a
        different subnet from the mon-ip, their OSD daemons crash with
        'Failed to pick public address'.

        If cfg.public_network is set, use it directly.
        Otherwise, auto-compute the /24 subnets of all host addresses.
        """
        import ipaddress as _ip

        if cfg.public_network:
            network = cfg.public_network
            log.debug("[ceph] Setting public_network from config: %s", network)
        else:
            subnets: list[str] = []
            for h in hosts:
                try:
                    iface = _ip.ip_interface(f"{h.address}/24")
                    subnet = str(iface.network)
                    if subnet not in subnets:
                        subnets.append(subnet)
                except ValueError:
                    pass
            if not subnets:
                log.warning("[ceph] Could not determine public_network from host addresses — skipping")
                return
            network = ",".join(subnets)
            log.debug("[ceph] Auto-computed public_network from host addresses: %s", network)

        rc, _, err = self._run(
            cli,
            f"cephadm shell -- ceph config set osd public_network {network}",
            sudo=True, timeout=30,
        )
        if rc != 0:
            log.warning("[ceph] Failed to set public_network %s: %s", network, err)
        else:
            log.info("[ceph] public_network set to %s", network)

    # ----------------------------------------------------------------------
    def _add_hosts(self, primary_cli, primary: CephHost, others: List[CephHost], version: str = "20.2.0"):
        """
        Add other Ceph hosts to the cluster.
        Ensures each has a matching container engine and cephadm version before adding.
        """
        for h in others:
            self._log(f"[cephadm] Validating container engine on {h.hostname} ({h.address})...")
            cli = self._connect(h)
            try:
                rc, _, _ = self._run(cli, "command -v docker || command -v podman", sudo=True)
                if rc != 0:
                    self._log(f"[cephadm] No container engine on {h.hostname}; installing Docker...")
                    self._ensure_container_engine(cli, h)
                else:
                    self._log(f"[cephadm] Container engine already present on {h.hostname}.")

                self._ensure_cephadm(cli, h, version)

                self._log(f"[cephadm] Adding host {h.hostname} ({h.address}) to cluster...")
                add_cmd = f"cephadm shell -- ceph orch host add {h.hostname} {h.address}"
                rc, out, err = self._run(primary_cli, add_cmd, sudo=True)
                if rc != 0:
                    self._log(f"[cephadm] Host add failed for {h.hostname}: {err or out}")
                    # continue instead of stopping entire deployment
                    continue

                self._log(f"[cephadm] Host {h.hostname} added successfully.")
            finally:
                cli.close()


    # ----------------------------------------------------------------------
    def _apply_placements(self, cli, cfg: CephConfig, hosts: List[CephHost]):
        """Apply mon and mgr placements.

        Mon and mgr daemons are only placed on hosts where ``is_mon_host=True``
        (i.e. K8s cluster nodes).  Dedicated storage hosts (``is_mon_host=False``)
        are excluded so cephadm does not attempt to deploy mon/mgr there.

        Explicit hostname lists are used instead of ``count:N`` so cephadm
        cannot pick a storage-only host when the desired count equals the
        total number of cluster members.
        """
        mon_hosts = [h for h in hosts if h.is_mon_host]
        if not mon_hosts:
            log.warning("[ceph] No mon-eligible hosts found; falling back to all hosts for placement")
            mon_hosts = hosts

        desired_mon = cfg.mon_count if cfg.mon_count is not None else min(3, len(mon_hosts))
        mon_names = " ".join(h.hostname for h in mon_hosts[:desired_mon])
        log.debug("[ceph] mon placement: %s", mon_names)
        self._run(cli, f'cephadm shell -- ceph orch apply mon --placement="{mon_names}"', sudo=True)

        desired_mgr = min(cfg.mgr_count, len(mon_hosts))
        mgr_names = " ".join(h.hostname for h in mon_hosts[:desired_mgr])
        log.debug("[ceph] mgr placement: %s", mgr_names)
        self._run(cli, f'cephadm shell -- ceph orch apply mgr --placement="{mgr_names}"', sudo=True)

    # ----------------------------------------------------------------------
    def _remove_osd_default_spec(self, cli) -> None:
        """Remove the osd.default spec before OSD provisioning.

        cephadm auto-creates this spec when ``--all-available-devices`` is
        used during bootstrap.  It conflicts with our per-device ``daemon add``
        approach and causes ``Failed to apply 1 service(s): osd.default``
        warnings.  Removing the spec does NOT affect running OSD daemons.
        """
        import json as _json

        rc, out, _ = self._run(
            cli,
            "cephadm shell -- ceph orch ls --service-type osd --format json",
            sudo=True, timeout=30,
        )
        if rc != 0:
            return
        try:
            specs = _json.loads(out.strip())
        except _json.JSONDecodeError:
            return
        for spec in specs:
            if spec.get("service_name", "") == "osd.default":
                log.debug("[ceph] Removing conflicting osd.default spec before OSD provisioning")
                # --force is required when the spec has any managed daemons
                self._run(cli, "cephadm shell -- ceph orch rm osd.default --force", sudo=True, timeout=30)

    # ----------------------------------------------------------------------
    def _wait_for_hosts_ready(self, cli, hostnames: List[str], timeout: float = 180.0) -> None:
        """Poll until all named hosts appear as non-offline in ceph orch host ls.

        The orchestrator needs to SSH to each newly-added host and deploy a
        cephadm agent before OSD operations can succeed.  Without this wait,
        ``ceph orch daemon add osd`` returns rc=0 (the request is accepted)
        but the OSD never starts because the remote host is not yet reachable
        by the orchestrator.
        """
        import time
        import json as _json

        pending = set(hostnames)
        log.debug("[ceph] Waiting up to %ds for hosts to be ready: %s", int(timeout), sorted(pending))
        deadline = time.monotonic() + timeout

        while pending and time.monotonic() < deadline:
            rc, out, _ = self._run(
                cli,
                "cephadm shell -- ceph orch host ls --format json",
                sudo=True, timeout=30,
            )
            if rc == 0:
                try:
                    for h in _json.loads(out.strip()):
                        hn = h.get("hostname", "")
                        status = h.get("status", "").lower()
                        if hn in pending and "offline" not in status:
                            log.debug("[ceph] Host %s is ready (status=%r)", hn, status)
                            pending.discard(hn)
                except _json.JSONDecodeError:
                    pass
            if pending:
                log.debug("[ceph] Still waiting for hosts: %s", sorted(pending))
                time.sleep(15)

        if pending:
            log.warning("[ceph] Host(s) still not ready after %ds: %s — proceeding anyway", int(timeout), sorted(pending))
        else:
            log.debug("[ceph] All hosts ready for OSD provisioning")

    # ----------------------------------------------------------------------
    def _wait_for_osds(self, cli, hosts: List["CephHost"], timeout: float = 60.0) -> None:
        """Final sanity check: log running OSD count after _apply_osds completes.

        _apply_osds now waits per-device, so this is just a short summary poll.
        """
        import time
        import json as _json

        expected = sum(len(h.osd_devices) for h in hosts if h.osd_devices)
        if expected == 0:
            return

        time.sleep(5)
        rc, out, _ = self._run(
            cli,
            "cephadm shell -- ceph orch ps --daemon-type osd --format json",
            sudo=True, timeout=30,
        )
        count = 0
        if rc == 0:
            try:
                daemons = _json.loads(out.strip())
                count = sum(1 for d in daemons if d.get("status_desc", "").lower() == "running")
            except _json.JSONDecodeError:
                pass

        if count >= expected:
            log.debug("[ceph] OSD summary: %d/%d daemons running", count, expected)
        else:
            log.warning("[ceph] OSD summary: only %d/%d daemons running — cluster may still be converging", count, expected)

    # ----------------------------------------------------------------------

    def _discover_available_devices(self, cli, hostname: str) -> List[str]:
        """Query ceph orch device ls for block devices on a host that can be used for OSDs.

        Returns device paths that are either:
        - ``available=True`` — ready to add directly, or
        - ``available=False`` with LVM/filesystem signatures but no active Ceph OSD — will be
          zapped by ``_apply_osds`` before adding (classified as ``needs_zap``).

        Devices that are actively used by a running Ceph OSD (bluestore/locking) are excluded.

        NOTE: ``ceph orch device ls`` uses the short hostname (e.g. ``cp01``) even when the
        node was registered with an FQDN.  We normalise both sides to the short name so that
        inventory FQDNs like ``cp01.net.daalu.io`` correctly match the Ceph-side ``cp01``.
        """
        import json as _json

        short_hostname = hostname.split(".")[0].lower()

        # Omit --hostname flag: passing the FQDN returns empty results when Ceph
        # registered the host under its short name.  Fetch all hosts and filter below.
        rc, out, err = self._run(
            cli,
            "cephadm shell -- ceph orch device ls --format json",
            sudo=True,
            timeout=60,
        )
        if rc != 0:
            log.debug("[ceph] device ls failed for %s (rc=%d): %s", hostname, rc, err or out)
            return []

        try:
            data = _json.loads(out.strip())
        except _json.JSONDecodeError:
            log.debug("[ceph] could not parse device ls JSON for %s: %r", hostname, out[:200])
            return []

        # ceph orch device ls --format json returns a list of per-host objects.
        # The hostname field may be keyed as "hostname", "name", or "addr"
        # depending on the Ceph version.  Always compare short names.
        devices = []
        for entry in data:
            host_key = entry.get("hostname") or entry.get("name") or entry.get("addr") or ""
            if host_key.split(".")[0].lower() != short_hostname:
                continue
            for dev in entry.get("devices", []):
                path = dev.get("path", "")
                available = dev.get("available", False)
                reasons = dev.get("rejected_reasons", [])
                reasons_str = " ".join(str(r) for r in reasons).lower()
                dev_type = dev.get("human_readable_type", "unknown")
                size = dev.get("sys_api", {}).get("human_readable_size", "?")
                if available:
                    log.debug(
                        "[ceph] [%s] device %s (%s, %s) — available",
                        hostname, path, dev_type, size,
                    )
                    devices.append(path)
                else:
                    # Include devices with old LVM/filesystem data that can be zapped,
                    # but exclude devices actively in use by a running Ceph OSD daemon.
                    is_active_ceph = any(kw in reasons_str for kw in ("bluestore", "locking"))
                    has_wipeable_data = any(kw in reasons_str for kw in ("lvm", "filesystem"))
                    if has_wipeable_data and not is_active_ceph:
                        log.debug(
                            "[ceph] [%s] device %s (%s, %s) — needs_zap (will be zapped): %s",
                            hostname, path, dev_type, size, reasons_str,
                        )
                        devices.append(path)
                    else:
                        log.debug(
                            "[ceph] [%s] device %s (%s, %s) — skipping: %s",
                            hostname, path, dev_type, size, reasons_str or "no reason given",
                        )

        if not devices:
            log.debug("[ceph] [%s] no usable devices discovered", hostname)
        else:
            log.debug("[ceph] [%s] discovered %d usable device(s): %s", hostname, len(devices), devices)

        return devices

    def _classify_device(self, cli, hostname: str, device: str) -> str:
        """Classify a block device for OSD provisioning.

        Returns one of:
          ``'ceph_osd'``  — device already hosts a running Ceph OSD (skip)
          ``'available'`` — Ceph reports the device as available (add directly)
          ``'needs_zap'`` — device has residual LVM / filesystem data (zap first)
          ``'unknown'``   — could not determine state (attempt add anyway)
        """
        import json as _json

        rc, out, _ = self._run(
            cli,
            f"cephadm shell -- ceph orch device ls --hostname {hostname} --format json",
            sudo=True, timeout=60,
        )
        if rc != 0:
            return "unknown"
        try:
            data = _json.loads(out.strip())
        except _json.JSONDecodeError:
            return "unknown"

        for entry in data:
            host_key = entry.get("hostname") or entry.get("name") or entry.get("addr") or ""
            if host_key != hostname:
                continue
            for dev in entry.get("devices", []):
                if dev.get("path") != device:
                    continue
                if dev.get("available", False):
                    return "available"
                reasons_str = " ".join(str(r) for r in dev.get("rejected_reasons", [])).lower()
                # Device is managed by an existing Ceph OSD daemon
                if any(kw in reasons_str for kw in ("ceph", "osd", "bluestore", "locking")):
                    return "ceph_osd"
                # Has LVM signatures, filesystems, or other non-Ceph data
                return "needs_zap"

        return "unknown"

    def _zap_device(self, cli, hostname: str, device: str, wait_available: float = 90.0) -> bool:
        """Zap a block device and wait for Ceph to report it as available.

        Returns True if the device becomes available within *wait_available* seconds.
        """
        import time as _time

        log.debug("[ceph] Zapping %s on %s to clear residual data", device, hostname)
        rc, out, err = self._run(
            cli,
            f"cephadm shell -- ceph orch device zap {hostname} {device} --force",
            sudo=True, timeout=300,
        )
        if rc != 0:
            log.warning("[ceph] Device zap failed for %s:%s (rc=%d): %s", hostname, device, rc, err or out)
            return False

        # Wait until ceph reports the device as available
        deadline = _time.monotonic() + wait_available
        while _time.monotonic() < deadline:
            state = self._classify_device(cli, hostname, device)
            if state == "available":
                log.debug("[ceph] Device %s:%s is now available after zap", hostname, device)
                return True
            _time.sleep(10)

        log.warning("[ceph] Device %s:%s not available after zap+%ds wait", hostname, device, int(wait_available))
        return False

    def _daemon_add_osd(self, cli, hostname: str, device: str) -> bool:
        """Run ``ceph orch daemon add osd hostname:device`` and return True on success."""
        rc, out, err = self._run(
            cli,
            f"cephadm shell -- ceph orch daemon add osd {hostname}:{device}",
            sudo=True,
            timeout=900,  # OSD creation (LVM setup + daemon start) can take >5 min on large HDDs
        )
        if rc == 0:
            return True
        msg = (err or out or "").lower()
        if "already" in msg or "exists" in msg:
            log.debug("[ceph] OSD already exists on %s:%s", hostname, device)
            return True
        log.debug("[ceph] daemon add osd %s:%s rc=%d: %s", hostname, device, rc, (err or out or "").strip()[:200])
        return False

    def _apply_osds(self, cli, cfg: CephConfig, hosts: list[CephHost]) -> None:
        """Apply OSDs on all Ceph hosts.

        For each device the method:
          1. Checks device state (available / already Ceph OSD / needs zap).
          2. Skips devices that already host a running OSD.
          3. Zaps devices that have residual LVM / filesystem data before adding.
          4. Submits ``ceph orch daemon add osd`` and waits for the daemon to
             appear as *running* in ``ceph orch ps``.
          5. Falls back to zap+retry for devices whose OSD daemon never starts.
        """
        import time as _time
        import json as _json

        if not cfg.apply_osds_all_devices:
            return

        total_attempted = 0
        total_ok = 0

        for host in hosts:
            if not host.osd_devices:
                log.debug("[ceph] No OSD devices configured for %s — auto-detecting...", host.hostname)
                discovered = self._discover_available_devices(cli, host.hostname)
                if not discovered:
                    log.debug("[ceph] No available devices found for %s — skipping", host.hostname)
                    continue
                log.debug("[ceph] Auto-discovered %d device(s) on %s: %s", len(discovered), host.hostname, discovered)
                host = CephHost(
                    hostname=host.hostname,
                    address=host.address,
                    username=host.username,
                    port=host.port,
                    password=host.password,
                    pkey_path=host.pkey_path,
                    osd_devices=discovered,
                )

            self.bus.emit(CephProgress(
                stage="osd_add",
                message=f"Adding {len(host.osd_devices)} OSD(s) on {host.hostname}: {', '.join(host.osd_devices)}",
                **self.run_ctx,
            ))

            # Count OSDs that are truly "up" in the cluster (reported by the mon,
            # not just by cephadm).  ceph orch ps reflects cephadm's view of the
            # container and can briefly show "running" while the activate container
            # is still executing, before the actual OSD process binds and connects
            # to the mons.  Counting "up" OSDs from `ceph osd dump` avoids this
            # false positive and ensures the OSD has actually joined the cluster.
            def _running_osd_count(host_ip: str) -> int:
                rc2, out2, _ = self._run(
                    cli,
                    "cephadm shell -- ceph osd dump --format json",
                    sudo=True, timeout=60,
                )
                if rc2 != 0:
                    return 0
                try:
                    data = _json.loads(out2.strip())
                    return sum(
                        1 for osd in data.get("osds", [])
                        if osd.get("up") == 1
                        and host_ip in osd.get("public_addr", "")
                    )
                except _json.JSONDecodeError:
                    return 0

            for device in host.osd_devices:
                total_attempted += 1
                log.debug("[ceph] Processing OSD device %s on %s", device, host.hostname)

                # ── Step 1: check device state ──────────────────────────────
                state = self._classify_device(cli, host.hostname, device)
                log.debug("[ceph] Device %s:%s state=%s", host.hostname, device, state)

                if state == "ceph_osd":
                    log.debug("[ceph] OSD already running on %s:%s — counting as success", host.hostname, device)
                    total_ok += 1
                    self.bus.emit(CephProgress(
                        stage="osd_add",
                        message=f"OSD already present: {host.hostname}:{device}",
                        **self.run_ctx,
                    ))
                    continue

                if state == "needs_zap":
                    log.debug("[ceph] Device %s:%s has residual data — zapping before add", host.hostname, device)
                    self.bus.emit(CephProgress(
                        stage="osd_add",
                        message=f"Zapping {host.hostname}:{device} (residual data)",
                        **self.run_ctx,
                    ))
                    if not self._zap_device(cli, host.hostname, device):
                        log.warning("[ceph] Zap failed for %s:%s — skipping", host.hostname, device)
                        self.bus.emit(CephProgress(
                            stage="osd_add_warn",
                            message=f"Zap failed for {host.hostname}:{device} — skipping",
                            **self.run_ctx,
                        ))
                        continue

                # ── Step 2: submit daemon add ───────────────────────────────
                before_count = _running_osd_count(host.address)
                accepted = self._daemon_add_osd(cli, host.hostname, device)
                if not accepted:
                    # Not accepted → try a zap then retry once
                    log.warning("[ceph] daemon add not accepted for %s:%s — zapping and retrying", host.hostname, device)
                    if self._zap_device(cli, host.hostname, device):
                        accepted = self._daemon_add_osd(cli, host.hostname, device)
                    if not accepted:
                        log.warning("[ceph] Giving up on OSD %s:%s after zap+retry", host.hostname, device)
                        self.bus.emit(CephProgress(
                            stage="osd_add_warn",
                            message=f"OSD {host.hostname}:{device} failed to be accepted",
                            **self.run_ctx,
                        ))
                        continue

                # ── Step 3: wait for daemon to go running (up to 90s) ───────
                _osd_up = False
                for _ in range(9):
                    _time.sleep(10)
                    if _running_osd_count(host.address) > before_count:
                        _osd_up = True
                        break

                if not _osd_up:
                    # Daemon was accepted but never started — likely the device
                    # still has invisible residual data.  Zap and retry once more.
                    log.warning(
                        "[ceph] OSD on %s:%s accepted but daemon never started — zapping and re-submitting",
                        host.hostname, device,
                    )
                    self.bus.emit(CephProgress(
                        stage="osd_add",
                        message=f"OSD {host.hostname}:{device} accepted but not running — zap+retry",
                        **self.run_ctx,
                    ))
                    if self._zap_device(cli, host.hostname, device):
                        before_count2 = _running_osd_count(host.address)
                        if self._daemon_add_osd(cli, host.hostname, device):
                            for _ in range(9):
                                _time.sleep(10)
                                if _running_osd_count(host.address) > before_count2:
                                    _osd_up = True
                                    break

                if _osd_up:
                    total_ok += 1
                    self.bus.emit(CephProgress(
                        stage="osd_add",
                        message=f"OSD running: {host.hostname}:{device}",
                        **self.run_ctx,
                    ))
                else:
                    log.warning("[ceph] OSD daemon never reached running state for %s:%s", host.hostname, device)
                    self.bus.emit(CephProgress(
                        stage="osd_add_warn",
                        message=f"OSD {host.hostname}:{device} did not reach running state",
                        **self.run_ctx,
                    ))

        summary = f"OSD provisioning: {total_ok}/{total_attempted} device(s) confirmed running"
        log.debug("[ceph] %s", summary)
        self.bus.emit(CephProgress(stage="osd_summary", message=summary, **self.run_ctx))


    # ----------------------------------------------------------------------
    def _post_osd_cleanup(self, cli, cfg: "CephConfig") -> None:
        """Fix common post-OSD health warnings.

        1. Remove OSD service specs with 0 managed OSDs (leftover ``osd.default``
           specs created by previous ``--all-available-devices`` runs that
           are no longer needed when we manage OSDs via ``daemon add``).
        2. Enable applications on pools that have none set (e.g.
           ``device_health_metrics`` needs the ``mgr`` application tag).
        """
        import json as _json

        log.debug("[ceph] post-OSD cleanup: removing empty OSD specs and fixing pool applications")

        # --- 1. Remove failing / empty OSD service specs ---
        rc, out, _ = self._run(
            cli,
            "cephadm shell -- ceph orch ls --service-type osd --format json",
            sudo=True, timeout=30,
        )
        if rc == 0:
            try:
                specs = _json.loads(out.strip())
            except _json.JSONDecodeError:
                specs = []

            for spec in specs:
                name = spec.get("service_name", "")
                # osd.default is removed pre-OSD by _remove_osd_default_spec;
                # guard here in case a subsequent run left another one behind.
                if name == "osd.default":
                    log.debug("[ceph] post-cleanup: removing stale osd.default spec")
                    self._run(
                        cli,
                        f"cephadm shell -- ceph orch rm {name} --force",
                        sudo=True, timeout=30,
                    )

        # --- 2. Enable applications on pools missing one ---
        # Known pool-name → application mappings
        _APP_HINTS = {
            "device_health_metrics": "mgr",
            ".mgr":                  "mgr",
            "cephfs":                "cephfs",
            "rbd":                   "rbd",
            "rgw":                   "rgw",
        }

        rc, out, _ = self._run(
            cli,
            "cephadm shell -- ceph osd pool ls detail --format json",
            sudo=True, timeout=30,
        )
        if rc == 0:
            try:
                pools = _json.loads(out.strip())
            except _json.JSONDecodeError:
                pools = []

            for pool in pools:
                pool_name = pool.get("pool_name", "")
                apps = pool.get("application_metadata", {})
                if apps:
                    continue  # already tagged

                app = next((v for k, v in _APP_HINTS.items() if k in pool_name), None)
                if app is None:
                    log.debug("[ceph] pool '%s' has no application tag and no known hint — skipping", pool_name)
                    continue

                log.debug("[ceph] Enabling application '%s' on pool '%s'", app, pool_name)
                self._run(
                    cli,
                    f"cephadm shell -- ceph osd pool application enable {pool_name} {app}",
                    sudo=True, timeout=30,
                )

        # --- 3. Apply pool replication size from config ---
        size = cfg.pool_replication_size
        min_size = cfg.pool_min_size
        log.debug("[ceph] Setting osd_pool_default_size=%d min_size=%d", size, min_size)
        self._run(cli, f"cephadm shell -- ceph config set global osd_pool_default_size {size}", sudo=True, timeout=30)
        self._run(cli, f"cephadm shell -- ceph config set global osd_pool_default_min_size {min_size}", sudo=True, timeout=30)

        rc, out, _ = self._run(cli, "cephadm shell -- ceph osd pool ls", sudo=True, timeout=30)
        if rc == 0:
            for pool_name in out.strip().splitlines():
                pool_name = pool_name.strip()
                if not pool_name:
                    continue
                log.debug("[ceph] Setting pool '%s' size=%d min_size=%d", pool_name, size, min_size)
                self._run(cli, f"cephadm shell -- ceph osd pool set {pool_name} size {size}", sudo=True, timeout=30)
                self._run(cli, f"cephadm shell -- ceph osd pool set {pool_name} min_size {min_size}", sudo=True, timeout=30)
        self.bus.emit(CephProgress(stage="pool_config", message=f"Pool replication size set to {size}/{min_size}", **self.run_ctx))

    # ----------------------------------------------------------------------
    def _check_health(self, cli):
        """Run final health check (soft failure — logs warning but does not abort)."""
        rc, out, err = self._run(cli, "cephadm shell -- ceph -s", sudo=True)
        if rc == 0:
            self.bus.emit(CephSucceeded(stage="completed", message="Ceph deployment completed successfully", **self.run_ctx))
            log.debug(out)
        else:
            # Non-fatal: cluster daemons may still be starting up, or the image
            # version used by the running cluster differs from the cephadm binary.
            # Emit a warning rather than aborting the deployment.
            log.warning("[ceph] health check returned non-zero (rc=%d) — cluster may still be converging: %s", rc, err or out)
            self.bus.emit(CephProgress(stage="health_check_warn", message=f"Health check warning (rc={rc}): {(err or out)[:200]}", **self.run_ctx))

    def _patch_cephadm_apparmor_bug(self, cli, hosts: List[CephHost]) -> None:
        """
        Patch cephadm AppArmor parsing bug everywhere it can execute from.

        Fixes:
            item, mode = line.split(' ')
        to:
            item, mode = line.split(None, 1)
        """

        self.bus.emit(CephProgress(
            stage="cephadm_patch",
            message="Patching cephadm AppArmor bug on all Ceph hosts",
            **self.run_ctx,
        ))

        # The patch script is base64-encoded so it survives the
        # sudo -S bash -lc '...' wrapping without any quoting issues.
        #
        # cephadm is distributed as a Python zipapp — a single zip file whose
        # tracebacks show "/__main__.py" even though it is one file on disk.
        # We must patch __main__.py *inside* the zip, preserving the shebang
        # prefix that precedes the zip magic bytes.
        _PATCH_SCRIPT = """\
import glob, io, pathlib, sys, zipfile

NEEDLE_SP  = "item, mode = line.split(' ')"
NEEDLE_DQ  = 'item, mode = line.split(" ")'
REPLACE    = 'item, mode = line.split(None, 1)'

CANDIDATES = ['/usr/local/bin/cephadm', '/usr/sbin/cephadm']
CANDIDATES += glob.glob('/var/lib/ceph/*/cephadm')

def patch_text(src):
    return src.replace(NEEDLE_SP, REPLACE).replace(NEEDLE_DQ, REPLACE)

for path in CANDIDATES:
    p = pathlib.Path(path)
    if not p.is_file():
        continue
    raw = p.read_bytes()

    if zipfile.is_zipfile(io.BytesIO(raw)):
        # --- zipapp layout: patch __main__.py inside the zip ---
        zip_start = raw.find(b'PK\\x03\\x04')
        prefix = raw[:zip_start] if zip_start >= 0 else b''
        changed = False
        buf_out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw), 'r') as zin, \\
             zipfile.ZipFile(buf_out, 'w', zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename == '__main__.py':
                    src = data.decode('utf-8')
                    new_src = patch_text(src)
                    if new_src != src:
                        data = new_src.encode('utf-8')
                        changed = True
                zout.writestr(info, data)
        if changed:
            p.write_bytes(prefix + buf_out.getvalue())
            sys.stdout.write('patched zipapp: ' + path + '\\n')
        else:
            sys.stdout.write('no match in zipapp __main__.py: ' + path + '\\n')
    else:
        # --- plain script layout ---
        src = raw.decode('utf-8')
        new_src = patch_text(src)
        if new_src != src:
            p.write_text(new_src)
            sys.stdout.write('patched plain: ' + path + '\\n')
        else:
            sys.stdout.write('no match in plain script: ' + path + '\\n')

# 2. Extracted cephadm __main__.py deployed by bootstrap into
#    /var/lib/ceph/<fsid>/cephadm.<hash>/__main__.py
#    These are plain Python files created after bootstrap runs.
main_candidates = glob.glob('/var/lib/ceph/*/cephadm/__main__.py')
main_candidates += glob.glob('/var/lib/ceph/*/cephadm.*/__main__.py')

for path in main_candidates:
    p = pathlib.Path(path)
    if not p.is_file():
        continue
    src = p.read_text('utf-8')
    new_src = patch_text(src)
    if new_src != src:
        p.write_text(new_src)
        sys.stdout.write('patched deployed __main__.py: ' + path + '\\n')
    else:
        sys.stdout.write('no match in deployed __main__.py: ' + path + '\\n')
"""
        _PATCH_B64 = base64.b64encode(_PATCH_SCRIPT.encode()).decode()

        for host in hosts:
            self._log(f"[ceph] Fixing cephadm AppArmor bug on {host.hostname}")
            host_cli = self._connect(host)
            try:
                # Base64 payload has no quotes, no special chars — zero quoting issues
                cmd = f"echo {_PATCH_B64} | base64 -d | python3"
                self._run(host_cli, cmd, sudo=True, host=host)
            finally:
                host_cli.close()




    def _restart_mgr(self, cli) -> None:
        self.bus.emit(
            CephProgress(
                stage="mgr_restart",
                message="Restarting ceph-mgr to pick up patched cephadm",
                **self.run_ctx,
            )
        )
        self._run(cli, "ceph orch restart mgr", sudo=True)


    def _patch_cephadm_apparmor_bug_on_hosts(self, hosts: List[CephHost]) -> None:
        """
        Host-scoped cephadm AppArmor fix.
        Ensures all execution paths are patched before any ceph orch commands.
        """
        self._patch_cephadm_apparmor_bug(cli=None, hosts=hosts)
