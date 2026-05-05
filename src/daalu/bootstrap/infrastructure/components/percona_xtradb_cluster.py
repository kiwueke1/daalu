# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/infrastructure/components/percona_xtradb_cluster.py

from __future__ import annotations

import json
import secrets
import string
import time
import logging
from pathlib import Path
from typing import Optional

import yaml

from daalu.bootstrap.engine.component import InfraComponent

log = logging.getLogger("daalu")


def _gen_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class PerconaXtraDBClusterComponent(InfraComponent):
    """
    Deploy Percona XtraDB Cluster CR, secrets, and HAProxy metrics service.
    Mirrors percona_xtradb_cluster Ansible role.
    """

    def __init__(
        self,
        *,
        spec_path: Path,
        kubeconfig: str,
        registry_url: str | None = None,
        registry_project: str = "openstack",
    ):
        super().__init__(
            name="percona-xtradb-cluster",
            repo_name="none",
            repo_url="",
            chart="",
            version=None,
            namespace="openstack",
            release_name="percona-xtradb",
            local_chart_dir=Path("/tmp"),
            remote_chart_dir=Path("/tmp"),
            kubeconfig=kubeconfig,
            uses_helm=False,
        )

        self.spec_path = spec_path
        self.wait_for_pods = False
        self._registry_url = registry_url
        self._registry_project = registry_project

        raw = yaml.safe_load(spec_path.read_text())

        if not raw:
            raise ValueError(
                f"{spec_path} is empty. "
                "You must define _percona_xtradb_cluster_spec."
            )

        if "_percona_xtradb_cluster_spec" not in raw:
            raise ValueError(
                f"{spec_path} must contain a top-level "
                "'_percona_xtradb_cluster_spec' key."
            )

        self.cluster_spec = raw["_percona_xtradb_cluster_spec"]

    def _resolved_spec(self) -> dict:
        """Return cluster_spec with REGISTRY_URL placeholder substituted."""
        if not self._registry_url:
            return self.cluster_spec
        # Re-serialise to string, substitute, then parse back — handles
        # nested fields like initImage without hardcoding key names.
        raw_str = yaml.dump(self.cluster_spec)
        raw_str = raw_str.replace("REGISTRY_URL", self._registry_url)
        return yaml.safe_load(raw_str)


    # ------------------------------------------------------------------
    def pre_install(self, kubectl) -> None:
        """
        Create secret if it doesn't exist (exact Ansible parity).
        """
        secret_name = self.cluster_spec.get("secretsName", "percona-xtradb")

        try:
            if kubectl.get(
                api_version="v1",
                kind="Secret",
                name=secret_name,
                namespace=self.namespace,
            ):
                return
        except RuntimeError:
            pass  # Secret doesn't exist yet — create it below

        kubectl.apply_objects([
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": secret_name,
                    "namespace": self.namespace,
                },
                "type": "Opaque",
                "stringData": {
                    "clustercheck": _gen_password(),
                    "monitor": _gen_password(),
                    "operator": _gen_password(),
                    "proxyadmin": _gen_password(),
                    "replication": _gen_password(),
                    "root": _gen_password(),
                    "xtrabackup": _gen_password(),
                },
            }
        ])

    # ------------------------------------------------------------------
    def post_install(self, kubectl) -> None:
        """
        Apply PerconaXtraDBCluster CR and HAProxy metrics service.
        """

        kubectl.apply_objects([
            {
                "apiVersion": "pxc.percona.com/v1",
                "kind": "PerconaXtraDBCluster",
                "metadata": {
                    "name": "percona-xtradb",
                    "namespace": self.namespace,
                },
                "spec": self._resolved_spec(),
            }
        ])

        self._wait_for_pxc_ready(kubectl)
        self._patch_fsgroupchangepolicy(kubectl)

        # HAProxy metrics service — only reached after cluster is ready
        kubectl.apply_objects([
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": "percona-xtradb-haproxy-metrics",
                    "namespace": self.namespace,
                    "labels": {
                        "name": "percona-xtradb-haproxy-metrics",
                    },
                },
                "spec": {
                    "type": "ClusterIP",
                    "ports": [
                        {
                            "name": "metrics",
                            "port": 8404,
                            "protocol": "TCP",
                            "targetPort": 8404,
                        }
                    ],
                    "selector": {
                        "app.kubernetes.io/component": "haproxy",
                        "app.kubernetes.io/instance": "percona-xtradb",
                    },
                },
            }
        ])

    # ------------------------------------------------------------------
    def _patch_fsgroupchangepolicy(self, kubectl) -> None:
        """
        Patch the PXC StatefulSet to use fsGroupChangePolicy: OnRootMismatch.

        The PXC operator default (omitting this field) means Kubernetes uses
        'Always', which recursively chowns the entire 160Gi RBD volume on every
        pod start — this stalls startup for minutes and can cause liveness kills
        during crash recovery.  OnRootMismatch skips the chown when the root
        dir already has the correct ownership, cutting restart time to seconds.

        The operator overwrites the StatefulSet on every reconciliation, so this
        patch must be re-applied after each deploy rather than done once manually.
        """
        sts_name = "percona-xtradb-pxc"
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "securityContext": {
                            "fsGroupChangePolicy": "OnRootMismatch",
                        }
                    }
                }
            }
        }
        rc, out, err = kubectl.run([
            "patch", "statefulset", sts_name,
            "-n", self.namespace,
            "--type=merge",
            "-p", json.dumps(patch),
        ])
        if rc != 0:
            log.warning(
                "[percona-xtradb-cluster] Could not patch fsGroupChangePolicy "
                "on StatefulSet %s: %s", sts_name, err or out,
            )
        else:
            log.info(
                "[percona-xtradb-cluster] Patched StatefulSet %s: "
                "fsGroupChangePolicy=OnRootMismatch", sts_name,
            )

    # ------------------------------------------------------------------
    def _wait_for_pxc_ready(
        self,
        kubectl,
        timeout_seconds: int = 1200,
        poll_interval: int = 15,
    ) -> None:
        """
        Block until the PerconaXtraDBCluster CR reports status.state == 'ready'.

        The PXC operator sets this only when all members are synced and
        HAProxy is serving — safe for downstream consumers (e.g. Keycloak
        bootstrap job) to connect immediately after this returns.
        """
        cluster_name = self.cluster_spec.get("clusterName", "percona-xtradb")
        log.info(
            "[percona-xtradb-cluster] Waiting for PXC cluster '%s' to be ready "
            "(timeout %ds)...",
            cluster_name, timeout_seconds,
        )

        deadline = time.monotonic() + timeout_seconds
        attempt = 0

        while time.monotonic() < deadline:
            rc, out, _ = kubectl.run(
                [
                    "get", "perconaxtradbcluster", cluster_name,
                    "-n", self.namespace,
                    "-o", "jsonpath={.status.state}",
                ]
            )
            state = out.strip() if rc == 0 else ""

            if state == "ready":
                log.info(
                    "[percona-xtradb-cluster] PXC cluster '%s' is ready", cluster_name
                )
                return

            if attempt % 4 == 0:  # log every ~60s
                log.info(
                    "[percona-xtradb-cluster] PXC cluster '%s' state='%s', "
                    "still waiting...",
                    cluster_name, state or "<unknown>",
                )

            time.sleep(poll_interval)
            attempt += 1

        raise RuntimeError(
            f"PXC cluster '{cluster_name}' did not reach 'ready' state "
            f"within {timeout_seconds}s (last state: '{state}')"
        )
