from __future__ import annotations

import os
import paramiko
from typing import List, Optional, Tuple
import sys
import datetime
from pathlib import Path
from .models import CephHost, CephConfig
from datetime import datetime


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
        print(line, flush=True)

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

        client.connect(
            hostname=host.address,
            port=host.port,
            username=host.username,
            password=host.password if not pkey else None,
            pkey=pkey,
            timeout=self.connect_timeout,
            allow_agent=True,
            look_for_keys=True,
        )
        return client

    def _run(
        self,
        cli: paramiko.SSHClient,
        cmd: str,
        env: Optional[dict] = None,
        sudo: bool = True,
        host: Optional["CephHost"] = None,
    ) -> Tuple[int, str, str]:
        """
        Run a shell command on a remote host via SSH.
        - Writes all commands/output into the main ceph-deploy log file.
        - Each command block is tagged with the hostname.
        - No output printed to CLI.
        """
        import time

        log_file = Path(self._log_file)  # central log file
        hostname = host.hostname if host else "unknown"

        prefix = ""
        if env:
            exports = " ".join(f'{k}={self._shq(v)}' for k, v in env.items())
            prefix += f"{exports} "
        shell_cmd = f"{prefix}{cmd}"
        final = f"sudo -S bash -lc {self._shq(shell_cmd)}" if sudo else f"bash -lc {self._shq(shell_cmd)}"

        # Write command header
        with open(log_file, "a", encoding="utf-8") as f:
            start_ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            f.write(f"\n[{start_ts}] ({hostname}) $ {final}\n")

        stdin, stdout, stderr = cli.exec_command(final, timeout=self.cmd_timeout)

        out_chunks, err_chunks = [], []
        while not stdout.channel.exit_status_ready():
            if stdout.channel.recv_ready():
                chunk = stdout.channel.recv(1024).decode("utf-8", "replace")
                out_chunks.append(chunk)
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(f"({hostname}) [stdout] {chunk}")
            if stdout.channel.recv_stderr_ready():
                chunk = stdout.channel.recv_stderr(1024).decode("utf-8", "replace")
                err_chunks.append(chunk)
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(f"({hostname}) [stderr] {chunk}")
            time.sleep(0.2)

        rc = stdout.channel.recv_exit_status()
        out_rem = stdout.read().decode("utf-8", "replace")
        err_rem = stderr.read().decode("utf-8", "replace")
        if out_rem:
            out_chunks.append(out_rem)
        if err_rem:
            err_chunks.append(err_rem)

        with open(log_file, "a", encoding="utf-8") as f:
            if out_rem.strip():
                f.write(f"({hostname}) [stdout]\n{out_rem}\n")
            if err_rem.strip():
                f.write(f"({hostname}) [stderr]\n{err_rem}\n")
            f.write(f"({hostname}) [exit {rc}]\n")

        return rc, "".join(out_chunks), "".join(err_chunks)


    def _run_1(
        self,
        cli: paramiko.SSHClient,
        cmd: str,
        env: Optional[dict] = None,
        sudo: bool = True,
        host: Optional["CephHost"] = None,
    ) -> Tuple[int, str, str]:
        """
        Run a shell command on a remote host via SSH.
        - Writes full command + output to logs/{hostname}.log
        - No output is printed to CLI.
        """
        import time

        log_dir = Path(self._log_file).parent  # keep in same dir as main log
        log_dir.mkdir(exist_ok=True)
        hostname = host.hostname if host else "unknown"
        log_file = log_dir / f"{hostname}.log"

        prefix = ""
        if env:
            exports = " ".join(f'{k}={self._shq(v)}' for k, v in env.items())
            prefix += f"{exports} "
        shell_cmd = f"{prefix}{cmd}"
        final = f"sudo -S bash -lc {self._shq(shell_cmd)}" if sudo else f"bash -lc {self._shq(shell_cmd)}"

        with open(log_file, "a", encoding="utf-8") as f:
            start_ts = datetime.utcnow().isoformat()
            f.write(f"\n[{start_ts}Z] ({hostname}) $ {final}\n")

        stdin, stdout, stderr = cli.exec_command(final, timeout=self.cmd_timeout)

        out_chunks, err_chunks = [], []
        while not stdout.channel.exit_status_ready():
            if stdout.channel.recv_ready():
                chunk = stdout.channel.recv(1024).decode("utf-8", "replace")
                out_chunks.append(chunk)
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(chunk)
            if stdout.channel.recv_stderr_ready():
                chunk = stdout.channel.recv_stderr(1024).decode("utf-8", "replace")
                err_chunks.append(chunk)
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write("[stderr] " + chunk)
            time.sleep(0.2)

        rc = stdout.channel.recv_exit_status()
        out_rem = stdout.read().decode("utf-8", "replace")
        err_rem = stderr.read().decode("utf-8", "replace")
        if out_rem:
            out_chunks.append(out_rem)
        if err_rem:
            err_chunks.append(err_rem)

        with open(log_file, "a", encoding="utf-8") as f:
            if out_rem.strip():
                f.write(f"[stdout]\n{out_rem}\n")
            if err_rem.strip():
                f.write(f"[stderr]\n{err_rem}\n")
            f.write(f"[exit {rc}]\n")

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
            print(f"[ceph] Container engine already present: {out.strip()}")
            return

        print("[ceph] No container engine found, installing Docker...")

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
        print(f"[ceph] Docker installed successfully: {out.strip()}")


    def _install_cephadm(self, cli) -> None:
        """
        Installs cephadm on the remote host if missing.
        Must be run as a user with passwordless sudo privileges.
        """
        cephadm_url = "https://github.com/ceph/ceph/raw/quincy/src/cephadm/cephadm"

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
            print(f"[ceph] cephadm installed successfully: {out.strip()}")
        else:
            raise RuntimeError(f"[ceph] cephadm installation verification failed: {err or out}")

    def deploy_1(self, hosts: List[CephHost], cfg: CephConfig) -> None:
        """
        Perform Ceph deployment via cephadm.
        - Ensures cephadm and a container engine (Docker/Podman) are installed.
        - Automatically heals APT issues using aptitude for better dependency repair.
        """
        if not hosts:
            raise ValueError("No Ceph hosts provided")

        image = cfg.image or f"quay.io/ceph/ceph:v{cfg.version}"
        primary = hosts[0]
        others = hosts[1:]

        cli = self._connect(primary)
        try:

            # ------------------------------------------------------------------
            # 0) Ensure container engine (Docker or Podman) is installed
            # ------------------------------------------------------------------
            rc, out, err = self._run(cli, "command -v docker || command -v podman", sudo=True)
            if rc != 0:
                print(f"[ceph] No container engine found on {primary.hostname}, installing Docker...")

                install_docker = (
                    "curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && "
                    "chmod +x /tmp/get-docker.sh && "
                    "sh /tmp/get-docker.sh && "
                    "systemctl enable docker && "
                    "systemctl start docker"
                )

                rc, out, err = self._run(cli, install_docker, sudo=True)
                if rc != 0:
                    msg = (err or out or "").lower()
                    if any(
                        bad in msg
                        for bad in [
                            "release file",
                            "$(lsb_release",
                            "duplicate",
                            "held broken packages",
                            "dependency problems",
                        ]
                    ):
                        print(f"[ceph] Detected APT issues on {primary.hostname}, auto-repairing with aptitude...")

                        # --- Self-healing using aptitude ---
                        heal_apt_env = (
                            "sudo rm -f /var/lib/apt/lists/lock /var/cache/apt/archives/lock "
                            "/var/lib/dpkg/lock-frontend /var/lib/dpkg/lock || true && "
                            "sudo dpkg --configure -a && "
                            "sudo apt-get -f install -y || true && "
                            # install aptitude (for advanced resolution)
                            "sudo apt-get install -y aptitude || true && "
                            # use aptitude to fix and reinstall essentials
                            "sudo aptitude -f install -y || true && "
                            "sudo aptitude reinstall -y apt ca-certificates curl gnupg lsb-release || true && "
                            "sudo aptitude update -y && "
                            "sudo aptitude full-upgrade -y && "
                            "(sudo apt-mark unhold docker-ce docker-ce-cli containerd.io || true)"
                        )
                        self._run(cli, heal_apt_env, sudo=True)

                        print(f"[ceph] Retrying Docker installation on {primary.hostname} after aptitude repair...")
                        rc2, out2, err2 = self._run(cli, install_docker, sudo=True)
                        if rc2 != 0:
                            raise RuntimeError(
                                f"[ceph] Docker installation still failed after aptitude repair: {err2 or out2}"
                            )
                        else:
                            print(f"[ceph] Docker installed successfully after aptitude repair.")
                    else:
                        raise RuntimeError(f"[ceph] Docker installation failed: {err or out}")
                else:
                    print(f"[ceph] Docker installed successfully on {primary.hostname}")
            else:
                print(f"[ceph] Container engine already present on {primary.hostname}: {out.strip()}")

            # ------------------------------------------------------------------
            # 0.5) Ensure cephadm is installed
            # ------------------------------------------------------------------
            rc, out, err = self._run(cli, "command -v cephadm || echo MISSING", sudo=False)
            if "MISSING" in (out + err):
                print(f"[ceph] cephadm not found on {primary.hostname}, installing...")
                self._install_cephadm(cli)
            else:
                print(f"[ceph] cephadm already installed on {primary.hostname}")

            # ------------------------------------------------------------------
            # 1) Pre-pull Ceph image (optional optimization)
            # ------------------------------------------------------------------
            self._run(
                cli,
                f"(command -v podman && podman pull {image}) || "
                f"(command -v docker && docker pull {image}) || true",
                sudo=True,
            )

            # ------------------------------------------------------------------
            # 2) Bootstrap Ceph cluster
            # ------------------------------------------------------------------
            mon_ip = primary.address
            bootstrap_cmd = (
                f"cephadm --image {image} "
                f"bootstrap --mon-ip {mon_ip} "
                f"--initial-dashboard-user {cfg.initial_dashboard_user} "
                f"--initial-dashboard-password {cfg.initial_dashboard_password} "
                "--skip-monitoring-stack"
            )
            rc, out, err = self._run(cli, bootstrap_cmd, sudo=True)
            if rc != 0:
                raise RuntimeError(f"cephadm bootstrap failed: {err or out}")

            print("[ceph] Distributing Ceph orchestrator SSH key...")
            rc, pubkey, err = self._run(cli, "cat /etc/ceph/ceph.pub", sudo=True)
            for h in others:
                cmd = f'mkdir -p /root/.ssh && echo "{pubkey.strip()}" >> /root/.ssh/authorized_keys'
                c2 = self._connect(h)
                try:
                    self._run(c2, cmd, sudo=True)
                    print(f"[ceph] Copied orchestrator SSH key to {h.hostname}")
                finally:
                    c2.close()

            # ------------------------------------------------------------------
            # 3) Set global container image
            # ------------------------------------------------------------------
            self._run(cli, f"cephadm shell -- ceph config set global container_image {image}", sudo=True)

            # ------------------------------------------------------------------
            # 4) Add remaining hosts
            # ------------------------------------------------------------------
            for h in others:
                self._run(cli, f"cephadm shell -- ceph orch host add {h.hostname} {h.address}", sudo=True)

            # ------------------------------------------------------------------
            # 5) Apply mon & mgr placements
            # ------------------------------------------------------------------
            desired_mon = cfg.mon_count if cfg.mon_count is not None else min(3, len(hosts))
            self._run(cli, f'cephadm shell -- ceph orch apply mon --placement="count:{desired_mon}"', sudo=True)
            self._run(cli, f'cephadm shell -- ceph orch apply mgr --placement="count:{cfg.mgr_count}"', sudo=True)

            # ------------------------------------------------------------------
            # 6) Apply OSDs
            # ------------------------------------------------------------------
            if cfg.apply_osds_all_devices:
                self._run(cli, "cephadm shell -- ceph orch apply osd --all-available-devices", sudo=True)

            # ------------------------------------------------------------------
            # 7) Health check
            # ------------------------------------------------------------------
            rc, out, err = self._run(cli, "cephadm shell -- ceph -s", sudo=True)
            print(out if rc == 0 else err)

        finally:
            cli.close()

    # ------------- cephadm orchestration -------------

    def deploy_2(self, hosts: List[CephHost], cfg: CephConfig) -> None:
        """
        Perform Ceph deployment via cephadm.
        - Ensures cephadm and a container engine (Docker/Podman) are installed.
        - Automatically heals APT issues using aptitude for better dependency repair.
        """
        if not hosts:
            raise ValueError("No Ceph hosts provided")

        image = cfg.image or f"quay.io/ceph/ceph:v{cfg.version}"
        primary = hosts[0]
        others = hosts[1:]

        # 🔹 Emit: start of deployment
        self.bus.emit(
            CephStarted(stage="init", message=f"Starting Ceph deployment on {primary.hostname}", **self.run_ctx)
        )

        cli = self._connect(primary)
        try:
            # ------------------------------------------------------------------
            # 0) Ensure cephadm is installed
            # ------------------------------------------------------------------
            self.bus.emit(CephProgress(stage="cephadm_check", message="Checking cephadm presence...", **self.run_ctx))
            rc, out, err = self._run(cli, "command -v cephadm || echo MISSING", sudo=False)
            if "MISSING" in (out + err):
                self.bus.emit(CephProgress(stage="cephadm_install", message=f"Installing cephadm on {primary.hostname}", **self.run_ctx))
                self._install_cephadm(cli)
            else:
                self.bus.emit(CephProgress(stage="cephadm_check", message=f"cephadm already installed on {primary.hostname}", **self.run_ctx))

            # ------------------------------------------------------------------
            # 0.5) Ensure container engine (Docker or Podman) is installed
            # ------------------------------------------------------------------
            self.bus.emit(CephProgress(stage="container_engine_check", message="Checking container engine...", **self.run_ctx))
            rc, out, err = self._run(cli, "command -v docker || command -v podman", sudo=True)
            if rc != 0:
                self.bus.emit(CephProgress(stage="container_engine_install", message=f"Installing Docker on {primary.hostname}", **self.run_ctx))
                install_docker = (
                    "curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && "
                    "chmod +x /tmp/get-docker.sh && "
                    "sh /tmp/get-docker.sh && "
                    "systemctl enable docker && "
                    "systemctl start docker"
                )

                rc, out, err = self._run(cli, install_docker, sudo=True)
                if rc != 0:
                    msg = (err or out or "").lower()
                    if any(
                        bad in msg
                        for bad in [
                            "release file",
                            "$(lsb_release",
                            "duplicate",
                            "held broken packages",
                            "dependency problems",
                        ]
                    ):
                        self.bus.emit(CephProgress(stage="apt_repair", message=f"Auto-repairing APT on {primary.hostname}", **self.run_ctx))
                        heal_apt_env = (
                            "sudo rm -f /var/lib/apt/lists/lock /var/cache/apt/archives/lock "
                            "/var/lib/dpkg/lock-frontend /var/lib/dpkg/lock || true && "
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

                        self.bus.emit(CephProgress(stage="container_engine_retry", message=f"Retrying Docker install on {primary.hostname}", **self.run_ctx))
                        rc2, out2, err2 = self._run(cli, install_docker, sudo=True)
                        if rc2 != 0:
                            self.bus.emit(CephFailed(stage="container_engine_install", error=err2 or out2, **self.run_ctx))
                            raise RuntimeError(
                                f"[ceph] Docker installation still failed after aptitude repair: {err2 or out2}"
                            )
                        else:
                            self.bus.emit(CephProgress(stage="container_engine_success", message="Docker installed successfully after aptitude repair", **self.run_ctx))
                    else:
                        self.bus.emit(CephFailed(stage="container_engine_install", error=err or out, **self.run_ctx))
                        raise RuntimeError(f"[ceph] Docker installation failed: {err or out}")
                else:
                    self.bus.emit(CephProgress(stage="container_engine_success", message=f"Docker installed successfully on {primary.hostname}", **self.run_ctx))
            else:
                self.bus.emit(CephProgress(stage="container_engine_check", message=f"Container engine already present: {out.strip()}", **self.run_ctx))

            # ------------------------------------------------------------------
            # 1) Pre-pull Ceph image
            # ------------------------------------------------------------------
            self.bus.emit(CephProgress(stage="image_pull", message=f"Pulling Ceph image {image}", **self.run_ctx))
            self._run(
                cli,
                f"(command -v podman && podman pull {image}) || "
                f"(command -v docker && docker pull {image}) || true",
                sudo=True,
            )

            # ------------------------------------------------------------------
            # 2) Bootstrap Ceph cluster
            # ------------------------------------------------------------------
            mon_ip = primary.address
            bootstrap_cmd = (
                f"cephadm --image {image} "
                f"bootstrap --mon-ip {mon_ip} "
                f"--initial-dashboard-user {cfg.initial_dashboard_user} "
                f"--initial-dashboard-password {cfg.initial_dashboard_password} "
                "--skip-monitoring-stack --allow-overwrite"
            )
            self.bus.emit(CephProgress(stage="bootstrap", message="Bootstrapping Ceph cluster", **self.run_ctx))
            rc, out, err = self._run(cli, bootstrap_cmd, sudo=True)
            if rc != 0:
                self.bus.emit(CephFailed(stage="bootstrap", error=err or out, **self.run_ctx))
                raise RuntimeError(f"cephadm bootstrap failed: {err or out}")

            # ------------------------------------------------------------------
            # 2.5) Distribute Ceph SSH key
            # ------------------------------------------------------------------
            self.bus.emit(CephProgress(stage="ssh_key_distribute", message="Distributing Ceph orchestrator SSH key", **self.run_ctx))
            rc, pubkey, err = self._run(cli, "cat /etc/ceph/ceph.pub", sudo=True)
            for h in others:
                cmd = f'mkdir -p /root/.ssh && echo "{pubkey.strip()}" >> /root/.ssh/authorized_keys'
                c2 = self._connect(h)
                try:
                    self._run(c2, cmd, sudo=True)
                    self.bus.emit(CephProgress(stage="ssh_key_distribute", message=f"Copied SSH key to {h.hostname}", **self.run_ctx))
                finally:
                    c2.close()

            # ------------------------------------------------------------------
            # 3) Set global container image
            # ------------------------------------------------------------------
            self.bus.emit(CephProgress(stage="config_image", message="Setting global Ceph container image", **self.run_ctx))
            self._run(cli, f"cephadm shell -- ceph config set global container_image {image}", sudo=True)

            # ------------------------------------------------------------------
            # 4) Add remaining hosts
            # ------------------------------------------------------------------
            for h in others:
                self.bus.emit(CephProgress(stage="add_host", message=f"Adding host {h.hostname} ({h.address})", **self.run_ctx))
                self._run(cli, f"cephadm shell -- ceph orch host add {h.hostname} {h.address}", sudo=True)

            # ------------------------------------------------------------------
            # 5) Apply mon & mgr placements
            # ------------------------------------------------------------------
            self.bus.emit(CephProgress(stage="placements", message="Applying mon/mgr placements", **self.run_ctx))
            desired_mon = cfg.mon_count if cfg.mon_count is not None else min(3, len(hosts))
            self._run(cli, f'cephadm shell -- ceph orch apply mon --placement="count:{desired_mon}"', sudo=True)
            self._run(cli, f'cephadm shell -- ceph orch apply mgr --placement="count:{cfg.mgr_count}"', sudo=True)

            # ------------------------------------------------------------------
            # 6) Apply OSDs
            # ------------------------------------------------------------------
            if cfg.apply_osds_all_devices:
                self.bus.emit(CephProgress(stage="osd_apply", message="Applying OSDs to all available devices", **self.run_ctx))
                self._run(cli, "cephadm shell -- ceph orch apply osd --all-available-devices", sudo=True)

            # ------------------------------------------------------------------
            # 7) Health check
            # ------------------------------------------------------------------
            self.bus.emit(CephProgress(stage="health_check", message="Checking Ceph cluster health", **self.run_ctx))
            rc, out, err = self._run(cli, "cephadm shell -- ceph -s", sudo=True)
            if rc == 0:
                self.bus.emit(CephSucceeded(stage="completed", message="Ceph deployment completed successfully", **self.run_ctx))
                print(out)
            else:
                self.bus.emit(CephFailed(stage="health_check", error=err or out, **self.run_ctx))

        finally:
            cli.close()


    def deploy(self, hosts: List[CephHost], cfg: CephConfig) -> None:
        """Main orchestrator for Ceph deployment."""
        if not hosts:
            raise ValueError("No Ceph hosts provided")

        image = cfg.image or f"quay.io/ceph/ceph:v{cfg.version}"
        primary = hosts[0]
        others = hosts[1:]
        cli = self._connect(primary)

        self.bus.emit(
            CephStarted(stage="init", message=f"Starting Ceph deployment on {primary.hostname}", **self.run_ctx)
        )

        try:
            # ✅ Correct order: container engine first
            self._ensure_container_engine(cli, primary)
            self._ensure_cephadm(cli, primary)
            self._prepull_image(cli, image)
            self._bootstrap_cluster(cli, cfg, image, primary)
            self._distribute_ssh_keys(primary, others)
            self._configure_global_image(cli, image)
            self._add_hosts(cli, primary, others)
            self._apply_placements(cli, cfg, hosts)
            self._apply_osds(cli, cfg)
            self._check_health(cli)
        finally:
            cli.close()


    # ----------------------------------------------------------------------
    def _ensure_cephadm(self, cli, host: CephHost):
        """Ensure cephadm is installed."""
        self.bus.emit(CephProgress(stage="cephadm_check", message="Checking cephadm presence...", **self.run_ctx))
        rc, out, err = self._run(cli, "command -v cephadm || echo MISSING", sudo=False)
        if "MISSING" in (out + err):
            self.bus.emit(CephProgress(stage="cephadm_install", message=f"Installing cephadm on {host.hostname}", **self.run_ctx))
            self._install_cephadm(cli)
        else:
            self.bus.emit(CephProgress(stage="cephadm_check", message="cephadm already installed", **self.run_ctx))

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
        """Pull Ceph image ahead of bootstrap."""
        self.bus.emit(CephProgress(stage="image_pull", message=f"Pulling Ceph image {image}", **self.run_ctx))
        self._run(cli, f"(podman pull {image} || docker pull {image}) || true", sudo=True)

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
            "--skip-monitoring-stack --allow-overwrite"
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


    def _bootstrap_cluster_1(self, cli, cfg: CephConfig, image: str, host: CephHost):
        """Run cephadm bootstrap."""
        mon_ip = host.address
        cmd = (
            f"cephadm --image {image} bootstrap --mon-ip {mon_ip} "
            f"--initial-dashboard-user {cfg.initial_dashboard_user} "
            f"--initial-dashboard-password {cfg.initial_dashboard_password} "
            "--skip-monitoring-stack --allow-overwrite"
        )
        rc, out, err = self._run(cli, cmd, sudo=True)
        if rc != 0:
            raise RuntimeError(f"cephadm bootstrap failed: {err or out}")

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
    def _add_hosts(self, primary_cli, primary: CephHost, others: List[CephHost]):
        """
        Add other Ceph hosts to the cluster.
        Ensures each has a container engine before adding.
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
        """Apply mon and mgr placements."""
        desired_mon = cfg.mon_count if cfg.mon_count is not None else min(3, len(hosts))
        self._run(cli, f'cephadm shell -- ceph orch apply mon --placement="count:{desired_mon}"', sudo=True)
        self._run(cli, f'cephadm shell -- ceph orch apply mgr --placement="count:{cfg.mgr_count}"', sudo=True)

    # ----------------------------------------------------------------------
    def _apply_osds(self, cli, cfg: CephConfig):
        """Apply OSD configuration."""
        if cfg.apply_osds_all_devices:
            self._run(cli, "cephadm shell -- ceph orch apply osd --all-available-devices", sudo=True)

    # ----------------------------------------------------------------------
    def _check_health(self, cli):
        """Run final health check."""
        rc, out, err = self._run(cli, "cephadm shell -- ceph -s", sudo=True)
        if rc == 0:
            self.bus.emit(CephSucceeded(stage="completed", message="Ceph deployment completed successfully", **self.run_ctx))
            print(out)
        else:
            self.bus.emit(CephFailed(stage="health_check", error=err or out, **self.run_ctx))