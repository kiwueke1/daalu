# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

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
import logging

log = logging.getLogger("daalu")


class KeycloakComponent(InfraComponent):
    """
    Deploy Keycloak backed by Percona XtraDB Cluster.
    Migrated from atmosphere Ansible role: roles/keycloak
    """

    ADMIN_SECRET_NAME = "keycloak-admin-credentials"

    def __init__(
        self,
        *,
        values_path: Path,
        kubeconfig: str,
        namespace: str = "auth-system",
        admin_password: str = "",
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
        self.db_password = os.environ.get("DAALU_KEYCLOAK_DB_PASSWORD", "")
        self.admin_password = admin_password or os.environ.get("DAALU_KEYCLOAK_ADMIN_PASSWORD", "")
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

    def _get_mysql_root_password_env(self) -> str:
        return os.environ.get("DAALU_MYSQL_ROOT_PASSWORD", "")

    # ------------------------------------------------------------------
    def _wait_for_mysql(self, host: str) -> None:
        for _ in range(120):
            try:
                conn = pymysql.connect(
                    host=host,
                    user="root",
                    password=self._get_mysql_root_password_env(),
                    connect_timeout=5,
                )
                conn.close()
                return
            except Exception:
                time.sleep(5)
        raise RuntimeError("MySQL never became ready")

    # ------------------------------------------------------------------
    def pre_install(self, kubectl) -> None:
        log.debug("Running keycloak pre-install steps...")
        log.debug("Bootstrapping database via Kubernetes Job...")

        job_name = "keycloak-mysql-bootstrap"
        pxc_ns = "openstack"
        auth_ns = self.namespace  # expected to be "auth-system"

        # ------------------------------------------------------------------
        # Ensure auth-system namespace exists (idempotent)
        # ------------------------------------------------------------------
        log.debug(f"Ensuring namespace '{auth_ns}' exists...")

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
        log.debug(f" Namespace '{auth_ns}' is present")


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
                                        'printf "%s\\n" "' + sql.replace('"', '\\"') + '" | '
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
        )

        if rc != 0:
            raise RuntimeError(
                f"MySQL bootstrap job failed:\n{err}"
            )

        log.debug(out.strip())
        log.debug("✅ Database and user ensured")

        # ------------------------------------------------------------------
        # Secret for Keycloak DB password
        # ------------------------------------------------------------------
        log.debug(
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
                        "db-username": self.db_user,
                    },
                }
            ]
        )

        kubectl.run(
            ["get", "secret", "keycloak-externaldb", "-n", self.namespace]
        )

        log.debug("✅ Secret 'keycloak-externaldb' is present and ready")

        # ------------------------------------------------------------------
        # Admin credentials secret (used via existingSecret in Helm values)
        # ------------------------------------------------------------------
        if self.admin_password:
            # Create secret in auth-system (for Keycloak Helm chart)
            # AND in openstack (for the DB reset job which runs there)
            for ns in [self.namespace, "openstack"]:
                log.debug(
                    f"Creating/ensuring secret '{self.ADMIN_SECRET_NAME}' "
                    f"in namespace '{ns}'..."
                )
                kubectl.apply_objects(
                    [
                        {
                            "apiVersion": "v1",
                            "kind": "Secret",
                            "metadata": {
                                "name": self.ADMIN_SECRET_NAME,
                                "namespace": ns,
                            },
                            "type": "Opaque",
                            "stringData": {
                                "admin-password": self.admin_password,
                            },
                        }
                    ]
                )
            log.debug(f"✅ Secret '{self.ADMIN_SECRET_NAME}' is present")

            # Reset admin password in the DB so it matches the secret
            # (KC_BOOTSTRAP_ADMIN_PASSWORD only works on first boot)
            self._reset_admin_password_in_db(kubectl)

    def _reset_admin_password_in_db(self, kubectl) -> None:
        """
        Ensure the admin user exists in the Keycloak DB with the correct
        bcrypt password hash.

        KC_BOOTSTRAP_ADMIN_PASSWORD only works when the master realm
        doesn't exist yet. For existing databases we must directly
        insert/update the user and credential rows via SQL.

        We use a Python 3 container (which has bcrypt-compatible
        hashlib/crypt) to generate the hash and run the SQL.
        """
        import uuid

        job_name = "keycloak-reset-admin-pw"
        pxc_ns = "openstack"

        cred_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())

        # Use Python image with mysql client to hash the password and run SQL
        # The password is injected via env var from the K8s secret
        script = r"""
set -euo pipefail

# Install mysql client and bcrypt
apt-get update -qq && apt-get install -yqq default-mysql-client > /dev/null 2>&1
pip install -q bcrypt 2>/dev/null

# Generate bcrypt hash
BCRYPT_HASH=$(python3 -c "
import bcrypt, os
pw = os.environ['ADMIN_PASSWORD'].encode()
hashed = bcrypt.hashpw(pw, bcrypt.gensalt(rounds=12)).decode()
print(hashed)
")

echo "Generated bcrypt hash successfully"

MYSQL_CMD="mysql -h percona-xtradb-haproxy.openstack.svc.cluster.local -P 3306 -u root -D keycloak -N -B"

echo "=== Ensuring admin user in keycloak DB ==="

# Check if admin user exists
ADMIN_ID=$($MYSQL_CMD -e "SELECT ID FROM USER_ENTITY WHERE USERNAME='admin' AND REALM_ID='master' LIMIT 1")

if [ -z "$ADMIN_ID" ]; then
    echo "Admin user does not exist — creating..."
    ADMIN_ID='""" + user_id + r"""'
    $MYSQL_CMD -e "INSERT INTO USER_ENTITY (ID, USERNAME, EMAIL_CONSTRAINT, ENABLED, REALM_ID, CREATED_TIMESTAMP, NOT_BEFORE, EMAIL_VERIFIED) VALUES ('$ADMIN_ID', 'admin', 'admin', 1, 'master', UNIX_TIMESTAMP() * 1000, 0, 0)"
    echo "Admin user created with ID: $ADMIN_ID"

    # Map all master realm admin roles
    ADMIN_ROLE_ID=$($MYSQL_CMD -e "SELECT ID FROM KEYCLOAK_ROLE WHERE NAME='admin' AND REALM_ID='master' LIMIT 1")
    if [ -n "$ADMIN_ROLE_ID" ]; then
        $MYSQL_CMD -e "INSERT IGNORE INTO USER_ROLE_MAPPING (ROLE_ID, USER_ID) VALUES ('$ADMIN_ROLE_ID', '$ADMIN_ID')"
        echo "Admin role mapped"
    fi
else
    echo "Admin user exists with ID: $ADMIN_ID"
fi

# Delete existing credentials
echo "Deleting old credentials..."
$MYSQL_CMD -e "DELETE FROM CREDENTIAL WHERE USER_ID='$ADMIN_ID'"

# Escape the hash for SQL ($ signs)
ESCAPED_HASH=$(echo "$BCRYPT_HASH" | sed 's/\$/\\$/g')

echo "Inserting new credential..."
CRED_ID='""" + cred_id + r"""'
$MYSQL_CMD -e "INSERT INTO CREDENTIAL (ID, TYPE, USER_ID, CREATED_DATE, SECRET_DATA, CREDENTIAL_DATA, PRIORITY) VALUES ('$CRED_ID', 'password', '$ADMIN_ID', UNIX_TIMESTAMP() * 1000, CONCAT('{\"value\":\"', '$BCRYPT_HASH', '\",\"salt\":\"\"}'), '{\"hashIterations\":1,\"algorithm\":\"bcrypt\"}', 10)"

echo "=== Verifying ==="
$MYSQL_CMD -e "SELECT ID, USERNAME, REALM_ID FROM USER_ENTITY WHERE USERNAME='admin' AND REALM_ID='master'"
$MYSQL_CMD -e "SELECT ID, TYPE FROM CREDENTIAL WHERE USER_ID='$ADMIN_ID'"

echo "DONE — admin password reset in DB"
"""

        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": job_name, "namespace": pxc_ns},
            "spec": {
                "backoffLimit": 0,
                "template": {
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "keycloak-pw-reset",
                                "image": "python:3.11-slim",
                                "env": [
                                    {
                                        "name": "MYSQL_PWD",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": "percona-xtradb",
                                                "key": "root",
                                            }
                                        },
                                    },
                                    {
                                        "name": "ADMIN_PASSWORD",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": self.ADMIN_SECRET_NAME,
                                                "key": "admin-password",
                                            }
                                        },
                                    },
                                ],
                                "command": ["/bin/bash", "-c", script],
                            }
                        ],
                    }
                },
            },
        }

        kubectl.run(
            ["delete", "job", job_name, "-n", pxc_ns, "--ignore-not-found=true"]
        )
        kubectl.apply_objects([job])
        kubectl.run(
            [
                "wait", "--for=condition=complete",
                f"job/{job_name}", "-n", pxc_ns,
                "--timeout=120s",
            ]
        )
        log.debug("✅ Admin credentials cleared from DB — will be re-created on pod restart")

    # ------------------------------------------------------------------
    def helm_values(self) -> Dict:
        return self.values
