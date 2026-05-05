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
   namespace ``temporal``. The bundled Postgres backend is good enough for a
   single-operator mgmt cluster; switch to the production chart settings later
   if you need HA.
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
from typing import Optional

from daalu.bootstrap.mgmt.models import MgmtClusterConfig, TemporalConfig

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
    """

    HELM_REPO_NAME = "temporal"
    HELM_REPO_URL = "https://go.temporal.io/helm-charts"

    def __init__(
        self,
        kubeconfig_path: str,
        cfg: MgmtClusterConfig,
        workspace_root: Path,
    ) -> None:
        self._kubeconfig = kubeconfig_path
        self._cfg = cfg
        self._tcfg: TemporalConfig = cfg.temporal
        self._workspace_root = workspace_root

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
        # Add/update the official Temporal helm repo.
        self._run(["helm", "repo", "add", self.HELM_REPO_NAME, self.HELM_REPO_URL,
                   "--force-update"], check=False)
        self._run(["helm", "repo", "update", self.HELM_REPO_NAME])

        ns = self._tcfg.namespace
        # Minimal, single-instance config — bundled Postgres + ephemeral storage.
        # For production, set persistence + replicas in a values file.
        sets = [
            f"server.replicaCount=1",
            f"server.image.tag={self._tcfg.server_image_tag}",
            f"cassandra.enabled=false",
            f"mysql.enabled=false",
            f"prometheus.enabled=false",
            f"grafana.enabled=false",
            f"elasticsearch.enabled=false",
        ]
        if self._tcfg.storage == "postgresql":
            sets.append("postgresql.enabled=true")
        elif self._tcfg.storage == "mysql":
            sets += ["postgresql.enabled=false", "mysql.enabled=true"]
        elif self._tcfg.storage == "cassandra":
            sets += ["postgresql.enabled=false", "cassandra.enabled=true"]
        # Drop the elasticsearch dependency in favour of the basic visibility
        # store backed by the chosen SQL backend.
        sets.append("server.config.persistence.default.driver=sql")
        sets.append("server.config.persistence.visibility.driver=sql")

        cmd = [
            "helm", "upgrade", "--install", "temporal",
            f"{self.HELM_REPO_NAME}/temporal",
            "--version", self._tcfg.chart_version,
            "--namespace", ns,
            "--create-namespace",
            "--wait", "--timeout", "20m",
        ]
        for s in sets:
            cmd += ["--set", s]
        self._run(cmd)

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
        """Locate the temporal-console helm chart, trying a few common spots."""
        candidates = [
            Path(self._tcfg.console_chart_path),
            self._workspace_root / self._tcfg.console_chart_path,
            self._workspace_root.parent / "temporal-console" / "chart",
            self._workspace_root.parent / "daalu_private" / "temporal-console" / "chart",
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
