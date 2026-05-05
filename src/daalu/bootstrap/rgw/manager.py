# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/rgw/manager.py
from __future__ import annotations

import json
import logging
import time

from daalu.bootstrap.ceph.models import CephHost
from daalu.bootstrap.rgw.models import RGWConfig
from daalu.utils.ssh import open_ssh

log = logging.getLogger("daalu")


class RGWManager:
    """
    Deploy a Ceph RGW (S3-compatible object gateway) via cephadm on the
    primary Ceph host. Idempotent: re-running against an already-deployed
    RGW is a no-op aside from the readiness poll.
    """

    def __init__(
        self,
        *,
        bus,
        ceph_hosts: list[CephHost],
    ):
        self.bus = bus
        self.ceph_hosts = ceph_hosts

        if not ceph_hosts:
            raise RuntimeError("RGW requires at least one Ceph host")
        self.primary_host = ceph_hosts[0]

    # ------------------------------------------------------------------
    def _cephadm(self, ssh, subcmd: str, timeout: int = 120) -> tuple[int, str, str]:
        """Run `cephadm shell -- <subcmd>` on the primary host."""
        cmd = f"cephadm shell -- {subcmd}"
        return ssh.run(cmd, sudo=True, timeout=timeout)

    # ------------------------------------------------------------------
    def _ensure_rgw_service(self, ssh, cfg: RGWConfig) -> None:
        """Declare the RGW service to `ceph orch`. Idempotent."""
        log.info(
            "[rgw] applying rgw.%s (placement=%d, port=%d)",
            cfg.realm, cfg.placement_count, cfg.port,
        )
        rc, out, err = self._cephadm(
            ssh,
            f"ceph orch apply rgw {cfg.realm} "
            f"--placement='count:{cfg.placement_count}' --port={cfg.port}",
        )
        if rc != 0:
            raise RuntimeError(f"ceph orch apply rgw failed: {err or out}")

    # ------------------------------------------------------------------
    def _wait_running(self, ssh, cfg: RGWConfig) -> dict:
        """Poll `ceph orch ps --daemon-type rgw` until ≥1 daemon is running."""
        deadline = time.monotonic() + cfg.ready_timeout_secs
        last_status = "unknown"
        while time.monotonic() < deadline:
            rc, out, _ = self._cephadm(
                ssh, "ceph orch ps --daemon-type rgw --format json"
            )
            if rc == 0 and out.strip():
                try:
                    daemons = json.loads(out)
                except json.JSONDecodeError:
                    daemons = []
                for d in daemons:
                    last_status = d.get("status_desc", "unknown")
                    # cephadm uses status_desc="running" or status=1 for healthy
                    if last_status.lower().startswith("running") or d.get("status") == 1:
                        log.info(
                            "[rgw] daemon %s is running on %s:%s",
                            d.get("daemon_name"), d.get("hostname"), d.get("ports"),
                        )
                        return d
            time.sleep(5)
        raise RuntimeError(
            f"RGW did not reach Running state within {cfg.ready_timeout_secs}s "
            f"(last status: {last_status})"
        )

    # ------------------------------------------------------------------
    def _maybe_create_user(self, ssh, cfg: RGWConfig) -> None:
        if not cfg.default_user_id:
            return
        log.info("[rgw] creating radosgw-admin user %s (idempotent)", cfg.default_user_id)
        # `user create` is NOT idempotent — it errors if the user exists.
        # Check first.
        rc, out, _ = self._cephadm(
            ssh,
            f"radosgw-admin user info --uid={cfg.default_user_id} --format=json",
        )
        if rc == 0 and out.strip():
            log.info("[rgw] user %s already exists — skipping create", cfg.default_user_id)
            return

        display = cfg.default_user_display_name or cfg.default_user_id
        rc, out, err = self._cephadm(
            ssh,
            f"radosgw-admin user create "
            f"--uid={cfg.default_user_id} "
            f"--display-name='{display}' --format=json",
        )
        if rc != 0:
            raise RuntimeError(f"radosgw-admin user create failed: {err or out}")

        # Emit the generated keys so the operator can pick them up. Never logs
        # them; they go to stdout of the daalu run which is already an
        # authenticated context.
        try:
            info = json.loads(out)
            keys = info.get("keys", [])
            if keys:
                k0 = keys[0]
                print(
                    f"[rgw] user={cfg.default_user_id} "
                    f"access_key={k0.get('access_key')} "
                    f"secret_key={k0.get('secret_key')}"
                )
        except json.JSONDecodeError:
            log.warning("[rgw] user created but could not parse returned keys")

    # ------------------------------------------------------------------
    def deploy(self, cfg: RGWConfig) -> None:
        log.debug("[rgw] primary host is %s", self.primary_host)
        ssh = open_ssh(self.primary_host)
        try:
            self._ensure_rgw_service(ssh, cfg)
            self._wait_running(ssh, cfg)
            self._maybe_create_user(ssh, cfg)
            log.info("[rgw] deployment complete")
        finally:
            ssh.close()
