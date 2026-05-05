# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/mgmt/temporal_installer.py
"""
Install Temporal + the daalu worker + the temporal-console UI onto the
management cluster.

This runs after the bare-metal provisioning stack (CAPI/CAPT, Tinkerbell,
Harbor) so the operator can immediately drive workload-cluster deployments
from the UI rather than the daalu CLI.

What we install
---------------
1. **Temporal server** — official ``temporalio/temporal`` Helm chart in
   namespace ``temporal``. The chart bundles a single-replica Cassandra
   subchart (default) which is good enough for a single-operator mgmt
   cluster; switch to MySQL or external Postgres for production HA.
2. **daalu-worker** — Deployment in namespace ``daalu`` running the
   ``daalu-worker`` entrypoint. Polls the ``daalu.deployments`` task queue
   and shells out to the daalu CLI to execute each stage. Mounts the daalu
   workspace, mgmt kubeconfig, and SSH keys via hostPath volumes so it has
   exactly what the CLI on the mgmt node has.
3. **temporal-console** — UI in namespace ``daalu``. Talks to the Temporal
   frontend over the in-cluster service, renders typed forms for daalu
   workflows, and shows the per-stage progress view.

Idempotent: each helm release is upgraded if it already exists.
"""
from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from daalu.bootstrap.mgmt.models import MgmtClusterConfig, TemporalConfig

if TYPE_CHECKING:
    from daalu.bootstrap.registry.manager import RegistryManager

log = logging.getLogger("daalu")


class TemporalInstaller:
    """
    Install the Temporal control plane + daalu worker + console.

    Args:
        kubeconfig_path: path to the mgmt cluster kubeconfig.
        cfg:             mgmt cluster config block — drives namespaces,
                         chart versions, and toggles.
        workspace_root:  workspace root containing ``deployments/daalu-worker/``
                         and (optionally) ``../temporal-console/chart``.
        registry_mgr:    optional RegistryManager used to pre-create Harbor
                         projects referenced by the worker/console images.
                         Without it, the operator must create those projects
                         manually before pushing images.
    """

    def __init__(
        self,
        kubeconfig_path: str,
        cfg: MgmtClusterConfig,
        workspace_root: Path,
        registry_mgr: Optional["RegistryManager"] = None,
    ) -> None:
        self._kubeconfig = kubeconfig_path
        self._cfg = cfg
        self._tcfg: TemporalConfig = cfg.temporal
        self._workspace_root = workspace_root
        self._registry_mgr = registry_mgr

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def install(self) -> None:
        if not self._tcfg.enabled:
            log.info("[temporal] temporal.enabled=False — skipping Temporal install")
            return

        self._require_tools()
        self._ensure_namespace(self._tcfg.namespace)
        self._ensure_namespace(self._tcfg.worker_namespace)
        self._ensure_harbor_projects()

        log.info("[temporal] installing Temporal server in ns=%s", self._tcfg.namespace)
        self._install_temporal_server()
        self._wait_temporal_ready()

        log.info("[temporal] installing daalu-worker in ns=%s", self._tcfg.worker_namespace)
        self._install_daalu_worker()

        if self._tcfg.console_enabled:
            log.info(
                "[temporal] installing temporal-console in ns=%s",
                self._tcfg.console_namespace,
            )
            self._install_temporal_console()
        else:
            log.info("[temporal] console_enabled=False — skipping temporal-console")

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _install_temporal_server(self) -> None:
        chart = self._resolve_server_chart()
        ns = self._tcfg.namespace
        storage = self._tcfg.storage

        # The official temporalio/temporal chart only bundles a `cassandra`
        # subchart for persistence (plus elasticsearch/prometheus/grafana for
        # observability). The `mysql.enabled` / `postgresql.enabled` flags do
        # NOT install a database — they only configure the sql driver. So for
        # storage=mysql we deploy our own standalone MySQL into the temporal
        # namespace before installing the Temporal release.
        if storage not in ("cassandra", "mysql", "external"):
            raise ValueError(
                f"Unsupported temporal.storage={storage!r}. "
                "Valid options: 'mysql' (default — installer deploys a "
                "standalone MySQL 8 alongside Temporal), 'cassandra' (chart's "
                "bundled 3-node Cassandra; visibility broken on admin-tools "
                ">=1.21 — pin server_image_tag<=1.20.x or pair with an "
                "external visibility store), or 'external' (you deploy the DB "
                "yourself and override server.config.persistence.* via a "
                "custom values file)."
            )

        if storage == "mysql":
            self._install_mysql_for_temporal(ns)

        sets = [
            "server.replicaCount=1",
            f"server.image.tag={self._tcfg.server_image_tag}",
            "prometheus.enabled=false",
            "grafana.enabled=false",
            "elasticsearch.enabled=false",
            "cassandra.enabled=false",
            "mysql.enabled=false",
        ]
        if storage == "cassandra":
            # Chart's default persistence driver is `cassandra`; just enable
            # the bundled subchart.
            sets[-2] = "cassandra.enabled=true"
            log.warning(
                "[temporal] storage=cassandra: visibility schema was dropped "
                "from temporalio/admin-tools >=1.21. If "
                "update-visibility-store fails, pin server_image_tag<=1.20.x "
                "or switch to storage=mysql."
            )
        elif storage == "mysql":
            db_default = self._tcfg.mysql_default_database
            db_vis = self._tcfg.mysql_visibility_database
            pw = self._tcfg.mysql_root_password
            sets += [
                "server.config.persistence.default.driver=sql",
                "server.config.persistence.visibility.driver=sql",
                "server.config.persistence.default.sql.driver=mysql8",
                "server.config.persistence.default.sql.host=mysql",
                "server.config.persistence.default.sql.port=3306",
                f"server.config.persistence.default.sql.database={db_default}",
                "server.config.persistence.default.sql.user=root",
                f"server.config.persistence.default.sql.password={pw}",
                "server.config.persistence.visibility.sql.driver=mysql8",
                "server.config.persistence.visibility.sql.host=mysql",
                "server.config.persistence.visibility.sql.port=3306",
                f"server.config.persistence.visibility.sql.database={db_vis}",
                "server.config.persistence.visibility.sql.user=root",
                f"server.config.persistence.visibility.sql.password={pw}",
            ]
        # storage == "external": no overrides — operator's values file wins.

        cmd = [
            "helm", "upgrade", "--install", "temporal",
            str(chart),
            "--namespace", ns,
            "--create-namespace",
            "--wait", "--timeout", "20m",
        ]
        for s in sets:
            cmd += ["--set", s]
        self._run(cmd)

    def _install_mysql_for_temporal(self, ns: str) -> None:
        """
        Deploy a single-pod MySQL 8 StatefulSet into the temporal namespace.

        The temporal helm chart does not bundle a MySQL database (despite
        having a ``mysql.enabled`` flag that only flips driver config), so we
        ship our own. Idempotent — re-running ``daalu mgmt`` is safe; existing
        data on the PVC is preserved across helm upgrades.

        The init ConfigMap pre-creates the visibility database so the chart's
        ``schema.createDatabase`` step doesn't need elevated SQL permissions.
        """
        log.info("[temporal] Deploying standalone MySQL into ns=%s", ns)

        pw = self._tcfg.mysql_root_password
        db_default = self._tcfg.mysql_default_database
        db_vis = self._tcfg.mysql_visibility_database
        image = self._tcfg.mysql_image
        sc = self._tcfg.mysql_storage_class
        size = self._tcfg.mysql_storage_size

        manifest = f"""---
apiVersion: v1
kind: ConfigMap
metadata:
  name: mysql-init
  namespace: {ns}
data:
  init.sql: |
    CREATE DATABASE IF NOT EXISTS {db_vis} CHARACTER SET utf8mb4;
    GRANT ALL PRIVILEGES ON {db_default}.* TO 'root'@'%';
    GRANT ALL PRIVILEGES ON {db_vis}.* TO 'root'@'%';
    FLUSH PRIVILEGES;
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data-mysql-0
  namespace: {ns}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: {sc}
  resources:
    requests:
      storage: {size}
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: mysql
  namespace: {ns}
spec:
  serviceName: mysql
  replicas: 1
  selector:
    matchLabels: {{app: mysql}}
  template:
    metadata:
      labels: {{app: mysql}}
    spec:
      containers:
      - name: mysql
        image: {image}
        env:
        - {{name: MYSQL_ROOT_PASSWORD, value: "{pw}"}}
        - {{name: MYSQL_DATABASE,      value: "{db_default}"}}
        ports:
        - {{containerPort: 3306, name: mysql}}
        volumeMounts:
        - {{name: data, mountPath: /var/lib/mysql}}
        - {{name: init, mountPath: /docker-entrypoint-initdb.d}}
        readinessProbe:
          exec: {{command: [mysqladmin, ping, -h, localhost, -uroot, -p{pw}]}}
          initialDelaySeconds: 20
          periodSeconds: 5
        resources:
          requests: {{cpu: 100m, memory: 256Mi}}
      volumes:
      - {{name: data, persistentVolumeClaim: {{claimName: data-mysql-0}}}}
      - {{name: init, configMap: {{name: mysql-init}}}}
---
apiVersion: v1
kind: Service
metadata:
  name: mysql
  namespace: {ns}
spec:
  selector: {{app: mysql}}
  ports: [{{port: 3306, targetPort: 3306}}]
  clusterIP: None
"""

        self._run(
            ["kubectl", "--kubeconfig", self._kubeconfig, "apply", "-f", "-"],
            input_text=manifest,
        )
        # Wait for mysql-0 to be Ready before the helm install kicks off the
        # chart's schema-init job (which fails with "no usable database
        # connection found" if MySQL is not yet listening on 3306).
        self._run([
            "kubectl", "--kubeconfig", self._kubeconfig,
            "-n", ns,
            "rollout", "status", "statefulset/mysql",
            "--timeout=5m",
        ])
        log.info("[temporal] MySQL ready at mysql.%s.svc.cluster.local:3306", ns)

    def _resolve_server_chart(self) -> Path:
        """
        Locate the user-pulled Temporal helm chart.

        We use the same pattern as the rest of daalu's third-party charts —
        the user runs `helm pull temporal/temporal --untar --untardir
        assets/temporal/charts/` once before `daalu mgmt`. See README's
        "Helm Charts" section for the exact command.
        """
        candidates = [
            Path(self._tcfg.server_chart_path),
            self._workspace_root / self._tcfg.server_chart_path,
        ]
        for c in candidates:
            try:
                if (c / "Chart.yaml").is_file():
                    return c.resolve()
            except OSError:
                continue
        raise FileNotFoundError(
            f"Temporal helm chart not found at "
            f"{self._workspace_root / self._tcfg.server_chart_path}.\n"
            f"Run this once before `daalu mgmt`:\n\n"
            f"  helm repo add temporal https://go.temporal.io/helm-charts\n"
            f"  helm pull temporal/temporal --untar --untardir assets/temporal/charts/\n"
        )

    def _wait_temporal_ready(self) -> None:
        ns = self._tcfg.namespace
        log.info("[temporal] waiting for temporal-frontend to be Ready...")
        # The frontend Service may exist before the pods are Ready; the helm
        # --wait above handles most of this, but we double-check.
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            rc, out, _ = self._run_capture([
                "kubectl", "--kubeconfig", self._kubeconfig,
                "-n", ns,
                "get", "deploy", "temporal-frontend",
                "-o", "jsonpath={.status.readyReplicas}",
            ])
            if rc == 0 and out.strip().isdigit() and int(out.strip()) >= 1:
                log.info("[temporal] temporal-frontend is Ready.")
                return
            time.sleep(5)
        raise RuntimeError(
            f"temporal-frontend in ns={ns} did not become Ready within 10 minutes"
        )

    def _install_daalu_worker(self) -> None:
        chart = (self._workspace_root / self._tcfg.worker_chart_path).resolve()
        if not (chart / "Chart.yaml").is_file():
            raise FileNotFoundError(
                f"daalu-worker chart not found at {chart}. "
                f"Set mgmt_cluster.temporal.worker_chart_path or run from the daalu repo."
            )

        # The worker hostPath-mounts the workspace + kubeconfig + SSH keys.
        # Resolve those to absolute paths on the mgmt node — these match the
        # paths the operator already uses for `daalu mgmt`.
        workspace_hp = str(self._workspace_root.resolve())
        kc_hp = str(Path(self._kubeconfig).expanduser().resolve())
        ssh_hp = str(Path("~/.ssh").expanduser().resolve())

        sets = [
            f"replicaCount={self._tcfg.worker_replicas}",
            f"image.repository={self._tcfg.worker_image.rsplit(':', 1)[0]}",
            f"image.tag={self._tcfg.worker_image.rsplit(':', 1)[-1]}",
            f"temporal.address=temporal-frontend.{self._tcfg.namespace}.svc.cluster.local:7233",
            f"temporal.namespace=default",
            f"temporal.taskQueue=daalu.deployments",
            f"threads={self._tcfg.worker_threads}",
            f"workspace.hostPath={workspace_hp}",
            f"mgmtKubeconfig.hostPath={kc_hp}",
            f"sshKeys.hostPath={ssh_hp}",
        ]

        cmd = [
            "helm", "upgrade", "--install", "daalu-worker",
            str(chart),
            "--namespace", self._tcfg.worker_namespace,
            "--create-namespace",
            "--wait", "--timeout", "5m",
        ]
        for s in sets:
            cmd += ["--set", s]
        self._run(cmd)

    def _install_temporal_console(self) -> None:
        chart = self._resolve_console_chart()
        if not chart:
            log.warning(
                "[temporal] temporal-console chart not found at %s — skipping console. "
                "Set mgmt_cluster.temporal.console_chart_path to a valid path to enable.",
                self._tcfg.console_chart_path,
            )
            return

        sets = [
            f"image.repository={self._tcfg.console_image.rsplit(':', 1)[0]}",
            f"image.tag={self._tcfg.console_image.rsplit(':', 1)[-1]}",
            f"temporal.host=temporal-frontend.{self._tcfg.namespace}.svc.cluster.local:7233",
            f"temporal.namespaces=default",
            f"brand.name={self._tcfg.console_brand_name}",
            f"brand.subtitle={self._tcfg.console_brand_subtitle}",
            f"oidc.issuer={self._tcfg.console_oidc_issuer}",
            f"oidc.clientId={self._tcfg.console_oidc_client_id}",
            f"istio.host={self._tcfg.console_host}",
            f"ingress.host={self._tcfg.console_host}",
        ]

        cmd = [
            "helm", "upgrade", "--install", "temporal-console",
            str(chart),
            "--namespace", self._tcfg.console_namespace,
            "--create-namespace",
            "--wait", "--timeout", "5m",
        ]
        for s in sets:
            cmd += ["--set", s]
        self._run(cmd)

    def _resolve_console_chart(self) -> Optional[Path]:
        """
        Locate the temporal-console helm chart.

        Default location is ``external/temporal-console/chart`` inside this
        repo — the console source ships alongside daalu so a single clone has
        everything. ``console_chart_path`` can override.
        """
        candidates = [
            Path(self._tcfg.console_chart_path),
            self._workspace_root / self._tcfg.console_chart_path,
        ]
        for c in candidates:
            try:
                if (c / "Chart.yaml").is_file():
                    return c.resolve()
            except OSError:
                continue
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_harbor_projects(self) -> None:
        """
        Pre-create Harbor projects referenced by the Temporal images.

        Harbor's deployer only auto-creates the default ``openstack`` project,
        so a fresh registry rejects pushes/pulls for any other path with
        ``project ... not found``. Derive the project from each image URL
        (``host:port/<project>/<image>:tag``) and ensure it exists.
        """
        if self._registry_mgr is None:
            log.info(
                "[temporal] No RegistryManager available — skipping Harbor "
                "project pre-creation. If image pulls fail with 'project not "
                "found', create the project manually via the Harbor UI."
            )
            return

        projects: set[str] = set()
        for image in (self._tcfg.worker_image, self._tcfg.console_image):
            project = self._project_from_image(image)
            if project:
                projects.add(project)

        for project in sorted(projects):
            try:
                self._registry_mgr.ensure_project(project)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "[temporal] Could not ensure Harbor project '%s': %s. "
                    "If image pulls fail, create the project manually in Harbor.",
                    project, exc,
                )

    @staticmethod
    def _project_from_image(image: str) -> Optional[str]:
        """
        Extract the Harbor project from an image reference.

        ``10.10.0.9:30003/daalu/daalu-worker:latest`` → ``"daalu"``.
        Returns None for refs without a host/project structure (e.g. bare
        ``nginx:latest``) — those don't target a Harbor project.
        """
        if not image:
            return None
        ref = image.split("@", 1)[0]
        parts = ref.split("/")
        if len(parts) < 3:
            return None
        return parts[1]

    def _ensure_namespace(self, ns: str) -> None:
        # `kubectl get ns X --ignore-not-found` returns rc=0 whether or not the
        # ns exists, so check for empty stdout to decide whether to create it.
        rc, out, _ = self._run_capture([
            "kubectl", "--kubeconfig", self._kubeconfig,
            "get", "namespace", ns, "--ignore-not-found",
            "-o", "name",
        ])
        if rc == 0 and out.strip():
            return
        self._run([
            "kubectl", "--kubeconfig", self._kubeconfig,
            "create", "namespace", ns,
        ])

    def _require_tools(self) -> None:
        for tool in ("helm", "kubectl"):
            if shutil.which(tool) is None:
                raise RuntimeError(
                    f"{tool} not found on PATH. The TemporalInstaller runs on the "
                    f"mgmt operator workstation and needs both helm and kubectl."
                )

    def _run(
        self,
        cmd: list[str],
        *,
        check: bool = True,
        capture: bool = False,
        pipe_to: Optional[list[str]] = None,
        input_text: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        log.debug("[temporal] $ %s", shlex.join(cmd))
        if pipe_to is not None:
            p1 = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                  env=self._env())
            p2 = subprocess.Popen(pipe_to, stdin=p1.stdout, env=self._env())
            assert p1.stdout is not None
            p1.stdout.close()
            p2.communicate()
            return subprocess.CompletedProcess(cmd, p2.returncode)
        return subprocess.run(
            cmd,
            check=check,
            capture_output=capture,
            text=True,
            input=input_text,
            env=self._env(),
        )

    def _run_capture(self, cmd: list[str]) -> tuple[int, str, str]:
        p = subprocess.run(
            cmd, check=False, capture_output=True, text=True, env=self._env()
        )
        return p.returncode, p.stdout, p.stderr

    def _env(self) -> dict:
        env = os.environ.copy()
        env["KUBECONFIG"] = self._kubeconfig
        return env
