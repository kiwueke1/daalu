# src/daalu/bootstrap/infrastructure/components/keycloak.py

from __future__ import annotations

from pathlib import Path
from typing import Dict

import time
import yaml
import pymysql
import os
import base64


from daalu.bootstrap.engine.component import InfraComponent


class KeycloakComponent(InfraComponent):
    """
    Deploy Keycloak backed by Percona XtraDB Cluster.
    Migrated from atmosphere Ansible role: roles/keycloak
    """

    def __init__(
        self,
        *,
        values_path: Path,
        kubeconfig: str,
        namespace: str = "auth-system",
    ):
        super().__init__(
            name="keycloak",
            repo_name="local",
            repo_url="",
            chart="keycloak",
            version=None,
            namespace=namespace,
            release_name="keycloak",
            local_chart_dir=values_path.parent / "charts",
            remote_chart_dir=Path("/usr/local/src/keycloak"),
            kubeconfig=kubeconfig,
            uses_helm=True,
        )

        self.values_path = values_path
        self.wait_for_pods = True
        self.min_running_pods = 1

        self._values: Dict = yaml.safe_load(values_path.read_text()) or {}

        # DB config (explicit, not magic)
        self.db_name = "keycloak"
        self.db_user = "keycloak"
        self.db_password = "admin10"
        self.enable_argocd = False

    # ------------------------------------------------------------------
    def _get_pxc_service_ip(self, kubectl) -> str:
        svc = kubectl.get(
            api_version="v1",
            kind="Service",
            name="percona-xtradb-haproxy",
            namespace="openstack",
        )
        return svc["spec"]["clusterIP"]

    def _get_mysql_root_password(self, kubectl) -> str:
        rc, out, err = kubectl.run(
            [
                "get",
                "secret",
                "percona-xtradb",
                "-n",
                "openstack",
                "-o",
                "jsonpath={.data.root}",
            ],
            capture_output=True,
        )

        if rc != 0 or not out.strip():
            raise RuntimeError(f"Failed to read MySQL root password: {err}")

        return base64.b64decode(out.strip()).decode()

    # ------------------------------------------------------------------
    def _wait_for_mysql(self, host: str) -> None:
        for _ in range(120):
            try:
                conn = pymysql.connect(
                    host=host,
                    user="root",
                    password="admin10",
                    connect_timeout=5,
                )
                conn.close()
                return
            except Exception:
                time.sleep(5)
        raise RuntimeError("MySQL never became ready")

    # ------------------------------------------------------------------
    def pre_install(self, kubectl) -> None:
        print("Running keycloak pre-install steps...")
        print("Bootstrapping database via Kubernetes Job...")

        job_name = "keycloak-mysql-bootstrap"
        pxc_ns = "openstack"
        auth_ns = self.namespace  # expected to be "auth-system"

        # ------------------------------------------------------------------
        # Ensure auth-system namespace exists (idempotent)
        # ------------------------------------------------------------------
        print(f"Ensuring namespace '{auth_ns}' exists...")

        kubectl.apply_objects(
            [
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {
                        "name": auth_ns,
                    },
                }
            ]
        )

        kubectl.run(["get", "namespace", auth_ns])
        print(f" Namespace '{auth_ns}' is present")


        sql = f"""
    CREATE DATABASE IF NOT EXISTS {self.db_name};

    CREATE USER IF NOT EXISTS '{self.db_user}'@'%' IDENTIFIED BY '{self.db_password}';
    ALTER USER '{self.db_user}'@'%' IDENTIFIED BY '{self.db_password}';

    GRANT ALL PRIVILEGES ON {self.db_name}.* TO '{self.db_user}'@'%';
    SET GLOBAL pxc_strict_mode='PERMISSIVE';

    SHOW DATABASES LIKE '{self.db_name}';
    SELECT user, host FROM mysql.user WHERE user='{self.db_user}';
    SHOW GRANTS FOR '{self.db_user}'@'%';
    """.strip()

        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": pxc_ns,
            },
            "spec": {
                # IMPORTANT: fail once, do not retry
                "backoffLimit": 0,
                "template": {
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "mysql",
                                "image": "mysql:8.0",
                                "env": [
                                    {
                                        "name": "MYSQL_PWD",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": "percona-xtradb",
                                                "key": "root",
                                            }
                                        },
                                    }
                                ],
                                "command": ["/bin/bash", "-lc"],
                                "args": [
                                    (
                                        "set -euo pipefail\n"
                                        f'printf "%s\\n" "{sql.replace(chr(34), "\\\"")}" | '
                                        "mysql "
                                        "-h percona-xtradb-haproxy.openstack.svc.cluster.local "
                                        "-P 3306 "
                                        "-u root\n"
                                    )
                                ],
                            }
                        ],
                    }
                },
            },
        }

        # ------------------------------------------------------------------
        # Ensure idempotency: delete old job first
        # ------------------------------------------------------------------
        kubectl.run(
            ["delete", "job", job_name, "-n", pxc_ns, "--ignore-not-found=true"]
        )

        kubectl.apply_objects([job])

        # ------------------------------------------------------------------
        # Wait for completion (single attempt only)
        # ------------------------------------------------------------------
        kubectl.run(
            [
                "wait",
                "--for=condition=complete",
                f"job/{job_name}",
                "-n",
                pxc_ns,
                "--timeout=300s",
            ]
        )

        # ------------------------------------------------------------------
        # Fetch logs (authoritative verification)
        # ------------------------------------------------------------------
        rc, out, err = kubectl.run(
            ["logs", f"job/{job_name}", "-n", pxc_ns],
            capture_output=True,
        )

        if rc != 0:
            raise RuntimeError(
                f"MySQL bootstrap job failed:\n{err}"
            )

        print(out.strip())
        print("✅ Database and user ensured")

        # ------------------------------------------------------------------
        # Secret for Keycloak DB password
        # ------------------------------------------------------------------
        print(
            f"Creating/ensuring secret 'keycloak-externaldb' "
            f"in namespace '{self.namespace}'..."
        )

        kubectl.apply_objects(
            [
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {
                        "name": "keycloak-externaldb",
                        "namespace": self.namespace,
                    },
                    "type": "Opaque",
                    "stringData": {
                        "db-password": self.db_password,
                    },
                }
            ]
        )

        kubectl.run(
            ["get", "secret", "keycloak-externaldb", "-n", self.namespace]
        )

        print("✅ Secret 'keycloak-externaldb' is present and ready")

    def pre_install_1(self, kubectl) -> None:
        """
        Prepare database and user (exact Ansible parity).
        """
        print("Running keycloak pre-install steps...")
        mysql_host = self._get_pxc_service_ip(kubectl)
        self._wait_for_mysql(mysql_host)

        conn = pymysql.connect(
            host=mysql_host,
            user="root",
            password="admin10",
            autocommit=True,
        )

        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {self.db_name}")
            cur.execute(
                f"CREATE USER IF NOT EXISTS '{self.db_user}'@'%' IDENTIFIED BY '{self.db_password}'"
            )
            cur.execute(
                f"GRANT ALL PRIVILEGES ON {self.db_name}.* TO '{self.db_user}'@'%'"
            )
            cur.execute("SET GLOBAL pxc_strict_mode='PERMISSIVE'")

        conn.close()

        # Secret for external DB (matches Helm values)
        kubectl.apply_objects(
            [
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {
                        "name": "keycloak-externaldb",
                        "namespace": self.namespace,
                    },
                    "type": "Opaque",
                    "stringData": {
                        "db-password": self.db_password,
                    },
                }
            ]
        )


    # ------------------------------------------------------------------
    def helm_values(self) -> Dict:
        return self.values
