# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/openstack/components/rook_ceph/ceph_provisioners.py

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

from daalu.bootstrap.engine.component import InfraComponent
import logging

log = logging.getLogger("daalu")


class CephProvisionersComponent(InfraComponent):
    """
    Daalu Ceph Provisioners component.

    Creates Kubernetes Service/Endpoints for Ceph monitors,
    stores the client.admin keyring as a Secret, and deploys
    the ceph-provisioners Helm chart.

    Mirrors:
    roles/ceph_provisioners/tasks/main.yml
    roles/ceph_provisioners/defaults/main.yml
    roles/ceph_provisioners/vars/main.yml
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        ssh,
        enable_argocd: bool = False,
    ):
        super().__init__(
            name="ceph-provisioners",
            repo_name="local",
            repo_url="",
            chart="ceph-provisioners",
            version=None,
            namespace=namespace,
            release_name="ceph-provisioners",
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/ceph-provisioners"),
            kubeconfig=kubeconfig,
            uses_helm=True,
            wait_for_pods=True,
            min_running_pods=1,
            enable_argocd=enable_argocd,
        )

        self.values_path = values_path
        self._assets_dir = assets_dir
        self._ssh = ssh

        self.requires_public_ingress = False

        #self.ceph_public_network = ceph_public_network
        #self.ceph_cluster_network = ceph_cluster_network or ceph_public_network

        # Populated during pre_install
        self._ceph_fsid: Optional[str] = None

        # User-supplied values from file
        self._user_values: Dict = {}
        if values_path and values_path.exists():
            self._user_values = self.load_values_file(values_path)

    # -------------------------------------------------
    # pre_install
    # -------------------------------------------------
    def pre_install(self, kubectl):
        """
        1. Collect ceph mon dump (FSID + monitor addresses)
        2. Create headless Service 'ceph-mon'
        3. Create Endpoints 'ceph-mon' with monitor IPs
        4. Retrieve client.admin keyring
        5. Create Secret 'pvc-ceph-client-key'
        """
        log.debug("[ceph-provisioners] Starting pre-install...")

        # Resolve cephadm from known paths since sudo may strip PATH
        _CEPHADM = (
            'CEPHADM=$(for p in /usr/local/bin/cephadm /usr/sbin/cephadm /usr/bin/cephadm; '
            'do [ -x "$p" ] && echo "$p" && break; done)'
        )

        # 1. Collect mon dump
        log.debug("[ceph-provisioners] Collecting ceph mon dump...")
        rc, out, err = self._ssh.run(
            f"{_CEPHADM} && $CEPHADM shell -- ceph mon dump -f json",
            sudo=True,
        )
        if rc != 0:
            raise RuntimeError(f"Failed to get ceph mon dump: {err}")
        mon_dump = json.loads(out)

        self._ceph_fsid = mon_dump["fsid"]
        monitor_ips = [
            mon["addr"].split(":")[0]
            for mon in mon_dump["mons"]
        ]

        # 2. Create headless Service for ceph-mon
        log.debug("[ceph-provisioners] Creating ceph-mon Service...")
        kubectl.apply_objects([
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": "ceph-mon",
                    "namespace": self.namespace,
                    "labels": {"application": "ceph"},
                },
                "spec": {
                    "clusterIP": "None",
                    "ports": [
                        {"name": "mon", "port": 6789, "targetPort": 6789},
                        {"name": "mon-msgr2", "port": 3300, "targetPort": 3300},
                        {"name": "metrics", "port": 9283, "targetPort": 9283},
                    ],
                },
            },
        ])

        # 3. Create Endpoints with monitor IPs
        log.debug("[ceph-provisioners] Creating ceph-mon Endpoints...")
        kubectl.apply_objects([
            {
                "apiVersion": "v1",
                "kind": "Endpoints",
                "metadata": {
                    "name": "ceph-mon",
                    "namespace": self.namespace,
                    "labels": {"application": "ceph"},
                },
                "subsets": [
                    {
                        "addresses": [{"ip": ip} for ip in monitor_ips],
                        "ports": [
                            {"name": "mon", "port": 6789, "protocol": "TCP"},
                            {"name": "mon-msgr2", "port": 3300, "protocol": "TCP"},
                            {"name": "metrics", "port": 9283, "protocol": "TCP"},
                        ],
                    }
                ],
            },
        ])

        # 4. Retrieve client.admin keyring
        log.debug("[ceph-provisioners] Retrieving client.admin keyring...")
        rc, out, err = self._ssh.run(
            f"{_CEPHADM} && $CEPHADM shell -- ceph auth get client.admin -f json",
            sudo=True,
        )
        if rc != 0:
            raise RuntimeError(f"Failed to get admin keyring: {err}")
        admin_key = json.loads(out)[0]["key"]

        # 5. Create pvc-ceph-client-key Secret
        log.debug("[ceph-provisioners] Creating pvc-ceph-client-key Secret...")
        kubectl.apply_objects([
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "kubernetes.io/rbd",
                "metadata": {
                    "name": "pvc-ceph-client-key",
                    "namespace": self.namespace,
                    "labels": {"application": "ceph"},
                },
                "stringData": {
                    "key": admin_key,
                },
            },
        ])

        log.debug("[ceph-provisioners] pre-install complete")

    # -------------------------------------------------
    # values
    # -------------------------------------------------
    def values(self) -> Dict:
        """
        Load values from file, inject only the runtime-discovered FSID.
        """
        if self._ceph_fsid is None:
            raise RuntimeError("pre_install must run before values()")

        base = self.load_values_file(self.values_path)

        # Only FSID is runtime-discovered — everything else is in the values file
        base.setdefault("conf", {})
        base["conf"].setdefault("ceph", {})
        base["conf"]["ceph"].setdefault("global", {})
        base["conf"]["ceph"]["global"]["fsid"] = self._ceph_fsid

        return base