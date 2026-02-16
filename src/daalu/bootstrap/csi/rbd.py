# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/csi/rbd.py

import json
import logging
from pathlib import Path
from copy import deepcopy

from daalu.bootstrap.csi.base import CSIBase
from daalu.bootstrap.csi.helm_values import rbd_values
from daalu.bootstrap.csi.events import (
    CSIStarted, CSIProgress, CSIFailed, CSISucceeded
)

import yaml

log = logging.getLogger("daalu")

# Local assets directory: <project_root>/assets/csi/
ASSETS_DIR = Path(__file__).resolve().parents[4] / "assets" / "csi"
LOCAL_CHART_DIR = ASSETS_DIR / "charts"
REMOTE_CHART_DIR = Path("/usr/local/src/ceph-csi-rbd")


def load_yaml_file(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"CSI values file not found: {path}")
    with path.open() as f:
        return yaml.safe_load(f) or {}


def deep_merge(dst: dict, src: dict):
    """
    Recursively merge src into dst (in place).
    """
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            deep_merge(dst[key], value)
        else:
            dst[key] = value


class CephRbdCsiDriver(CSIBase):
    def __init__(
        self,
        *,
        bus,
        helm,
        ssh,
        host,
        env="workload",
        context=None,
    ):
        super().__init__(
            bus=bus,
            env=env,
            context=context,
            ssh=ssh,
            host=host,
        )
        self.helm = helm

    def _upload_chart(self):
        """
        Upload the vendored ceph-csi-rbd chart from local assets to the
        controller node (where helm runs), following the same pattern as
        other components (cinder, glance, etc.).
        """
        local_chart = LOCAL_CHART_DIR / "ceph-csi-rbd"
        if not local_chart.exists():
            raise FileNotFoundError(
                f"Vendored chart not found at {local_chart}. "
                f"Run: helm repo add ceph-csi https://ceph.github.io/csi-charts && "
                f"helm pull ceph-csi/ceph-csi-rbd --version 3.11.0 "
                f"--untar --untardir {LOCAL_CHART_DIR}"
            )

        # Upload to the controller node (self.helm.ssh), NOT the ceph host
        # (self.ssh), because helm runs on the controller.
        controller_ssh = self.helm.ssh
        log.info("[csi] Uploading ceph-csi-rbd chart to controller node...")
        controller_ssh.run(f"mkdir -p {REMOTE_CHART_DIR}", sudo=True)
        controller_ssh.put_dir(
            local_dir=LOCAL_CHART_DIR,
            remote_dir=REMOTE_CHART_DIR,
            release_name="ceph-csi-rbd",
            sudo=True,
        )
        return REMOTE_CHART_DIR / "ceph-csi-rbd"

    def deploy(self, cfg):
        self.bus.emit(CSIStarted(
            stage="init",
            message="Starting Ceph RBD CSI deployment",
            **self._ctx(),
        ))

        fsid, mons = self._get_cluster_info()
        user, key = self._ensure_user(cfg)

        # ------------------------------------------------------------------
        # Base dynamic values (generated)
        # ------------------------------------------------------------------
        values = rbd_values(
            fsid=fsid,
            monitors=mons,
            user=user,
            key=key,
            pool=cfg.ceph_pool,
        )

        # ------------------------------------------------------------------
        # Load static CSI Helm overrides (placement, tolerations, affinity)
        # ------------------------------------------------------------------
        static_values_path = ASSETS_DIR / "values.yaml"
        static_values = load_yaml_file(static_values_path)

        merged_values = deepcopy(values)
        deep_merge(merged_values, static_values)
        values = merged_values

        self.bus.emit(CSIProgress(
            stage="helm",
            message="Deploying ceph-csi-rbd Helm chart",
            **self._ctx(),
        ))

        # Upload vendored chart from local assets to remote node
        remote_chart_path = self._upload_chart()

        self.helm.install_or_upgrade(
            name="ceph-csi-rbd",
            chart=str(remote_chart_path),
            namespace="kube-system",
            values=values,
            kubeconfig=cfg.kubeconfig_path,
        )

        self.bus.emit(CSISucceeded(
            stage="completed",
            message="Ceph RBD CSI deployed successfully",
            **self._ctx(),
        ))

    def _get_cluster_info(self):
        rc, out, err = self._run(
            cli=self.ssh,
            cmd="cephadm shell -- ceph mon dump -f json",
            hostname=self.host.hostname,
            sudo=True,
        )
        if rc != 0:
            raise RuntimeError(f"failed to fetch ceph mon dump: {err or out}")

        data = json.loads(out)
        fsid = data["fsid"]
        mons = [
            m["addr"].split(":")[0]
            for m in data.get("mons", [])
        ]

        if not mons:
            raise RuntimeError("no monitors discovered from ceph mon dump")

        return fsid, mons

    def _ensure_user(self, cfg):
        # Ensure pool exists (idempotent)
        self._run(
            cli=self.ssh,
            cmd=f"cephadm shell -- ceph osd pool create {cfg.ceph_pool}",
            hostname=self.host.hostname,
            sudo=True,
        )

        # Ensure client user exists with proper caps
        self._run(
            cli=self.ssh,
            cmd=(
                "cephadm shell -- ceph auth get-or-create "
                f"client.{cfg.ceph_user} "
                "mon 'profile rbd' "
                f"mgr 'profile rbd pool={cfg.ceph_pool}' "
                f"osd 'profile rbd pool={cfg.ceph_pool}'"
            ),
            hostname=self.host.hostname,
            sudo=True,
        )

        # Fetch client key
        rc, out, err = self._run(
            cli=self.ssh,
            cmd=f"cephadm shell -- ceph auth get-key client.{cfg.ceph_user}",
            hostname=self.host.hostname,
            sudo=True,
        )
        if rc != 0:
            raise RuntimeError(f"failed to fetch ceph auth key: {err or out}")

        return cfg.ceph_user, out.strip()
