from __future__ import annotations

import os
import paramiko
from typing import List, Optional, Tuple

from .models import CephHost, CephConfig


class CephManager:
    """
    Bootstraps a Ceph cluster via cephadm, mirroring the Atmosphere playbook intent:
      - Use CEPHADM_IMAGE (derived from version unless given)
      - Deploy mon/mgr (from 'cephs' group)
      - Deploy OSDs (all-available-devices by default)
    All cephadm orchestration commands are executed on the PRIMARY host.
    """

    def __init__(self, connect_timeout: float = 20.0, cmd_timeout: float = 300.0):
        self.connect_timeout = connect_timeout
        self.cmd_timeout = cmd_timeout

    # ------------- SSH helpers -------------

    def _connect(self, host: CephHost) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        pkey = None
        if host.pkey_path:
            pkey = paramiko.RSAKey.from_private_key_file(host.pkey_path)

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

    def _run(self, cli: paramiko.SSHClient, cmd: str, env: Optional[dict] = None, sudo: bool = True) -> Tuple[int, str, str]:
        """
        Run a shell command (optionally with environment) on a remote host.
        If sudo=True, use 'sudo -S bash -lc'.
        """
        prefix = ""
        if env:
            # export inline; keep simple and explicit
            exports = " ".join(f'{k}={self._shq(v)}' for k, v in env.items())
            prefix += f"{exports} "
        shell_cmd = f"{prefix}{cmd}"

        if sudo:
            final = f"sudo -S bash -lc {self._shq(shell_cmd)}"
        else:
            final = f"bash -lc {self._shq(shell_cmd)}"

        stdin, stdout, stderr = cli.exec_command(final, timeout=self.cmd_timeout)
        # If sudo prompts for password and NOPASSWD isn't configured, you can write it here:
        # stdin.write(password + "\n"); stdin.flush()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out, err

    def _shq(self, s: str) -> str:
        return "'" + str(s).replace("'", "'\"'\"'") + "'"

    # ------------- cephadm orchestration -------------

    def deploy(self, hosts: List[CephHost], cfg: CephConfig) -> None:
        """
        Entry point: perform cephadm bootstrap on primary host, then add rest.
        """
        if not hosts:
            raise ValueError("No Ceph hosts provided")

        image = cfg.image or f"quay.io/ceph/ceph:v{cfg.version}"
        primary = hosts[0]
        others = hosts[1:]

        cli = self._connect(primary)
        try:
            # 0) sanity: cephadm present?
            rc, out, err = self._run(cli, "command -v cephadm || echo MISSING", sudo=False)
            if "MISSING" in (out + err):
                raise RuntimeError("cephadm not found on primary host; please pre-install cephadm or extend CephManager to install it.")

            # 1) (optional) pull the image using podman/docker to speed up bootstrap
            self._run(cli, f"(command -v podman && podman pull {image}) || (command -v docker && docker pull {image}) || true")

            # 2) bootstrap on primary
            mon_ip = primary.address  # you can switch to another IP if needed
            bootstrap_cmd = (
                f"cephadm --image {image} "
                f"bootstrap --mon-ip {mon_ip} "
                f"--initial-dashboard-user {cfg.initial_dashboard_user} "
                f"--initial-dashboard-password {cfg.initial_dashboard_password}"
            )
            rc, out, err = self._run(cli, bootstrap_cmd)
            if rc != 0:
                raise RuntimeError(f"cephadm bootstrap failed: {err or out}")

            # 3) set global container image (like env CEPHADM_IMAGE)
            self._run(cli, f"cephadm shell -- ceph config set global container_image {image}")

            # 4) add remaining hosts to orchestrator
            for h in others:
                add_cmd = f"cephadm shell -- ceph orch host add {h.hostname} {h.address}"
                self._run(cli, add_cmd)

            # 5) apply mon & mgr placements
            desired_mon = cfg.mon_count if cfg.mon_count is not None else min(3, len(hosts))
            self._run(cli, f'cephadm shell -- ceph orch apply mon --placement="count:{desired_mon}"')
            self._run(cli, f'cephadm shell -- ceph orch apply mgr --placement="count:{cfg.mgr_count}"')

            # 6) apply OSDs
            if cfg.apply_osds_all_devices:
                self._run(cli, "cephadm shell -- ceph orch apply osd --all-available-devices")
            # else: future: drivegroups per-host

            # 7) basic health check
            rc, out, err = self._run(cli, "cephadm shell -- ceph -s")
            # not strictly failing here; operator can review output
            if rc == 0:
                print(out)
            else:
                print(err)

        finally:
            cli.close()
