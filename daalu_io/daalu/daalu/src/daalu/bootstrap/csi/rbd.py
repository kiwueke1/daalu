# src/daalu/bootstrap/csi/rbd.py

import json
from pathlib import Path
from daalu.bootstrap.csi.base import CSIBase
from daalu.bootstrap.csi.helm_values import rbd_values
from daalu.bootstrap.csi.events import (
    CSIStarted, CSIProgress, CSIFailed, CSISucceeded
)
from daalu.config.models import RepoSpec
from daalu.helm.charts import ensure_chart

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



    def deploy(self, cfg):
        self.bus.emit(CSIStarted(
            stage="init",
            message="Starting Ceph RBD CSI deployment",
            **self._ctx(),
        ))

        fsid, mons = self._get_cluster_info()
        user, key = self._ensure_user(cfg)

        self.helm.add_repo(
            RepoSpec(
                name="ceph-csi",
                url="https://ceph.github.io/csi-charts",
            )
        )
        self.helm.update_repos()

        values = rbd_values(
            fsid=fsid,
            monitors=mons,
            user=user,
            key=key,
            pool=cfg.ceph_pool,
        )

        self.bus.emit(CSIProgress(
            stage="helm",
            message="Deploying ceph-csi-rbd Helm chart",
            **self._ctx(),
        ))

        charts_base = Path.home() / ".daalu" / "helm" / "charts"

        chart_path = ensure_chart(
            repo="ceph-csi",
            chart="ceph-csi-rbd",
            version="3.11.0",
            target_dir=charts_base,
        )

        self.helm.install_or_upgrade(
            name="ceph-csi-rbd",
            chart=str(chart_path),
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
