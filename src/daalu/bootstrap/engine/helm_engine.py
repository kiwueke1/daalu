# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu.bootstrap.engine.helm_engine.py

from __future__ import annotations

import logging

from daalu.bootstrap.engine.chart_manager import prepare_chart
from daalu.bootstrap.engine.values import deep_merge
from daalu.kube.kubectl import KubectlRunner
from daalu.config.models import RepoSpec
from daalu.bootstrap.engine.infra_logging import InfraJsonlLogger

log = logging.getLogger("daalu")


class HelmInfraEngine:
    def __init__(self, *, helm, ssh, logger: InfraJsonlLogger | None = None):
        self.helm = helm
        self.ssh = ssh
        self.logger = logger

    def base_values(self, component) -> dict:
        """
        Engine-wide defaults applied to all components.
        """
        return {}


    # Valid phases for --phase filtering
    VALID_PHASES = {"pre_install", "helm", "post_install"}

    def deploy(self, component, *, phase: str | None = None):
        """
        Deploy a component through its lifecycle phases.

        Args:
            component: The InfraComponent to deploy.
            phase: Optional phase to run in isolation.
                   One of "pre_install", "helm", "post_install".
                   If None, all phases run (default behaviour).
        """
        if phase and phase not in self.VALID_PHASES:
            raise ValueError(
                f"Invalid phase '{phase}'. "
                f"Valid phases: {', '.join(sorted(self.VALID_PHASES))}"
            )

        run_pre = phase in (None, "pre_install")
        run_helm = phase in (None, "helm")
        run_post = phase in (None, "post_install")

        log.info("[%s] Starting deployment...", component.name)

        # ---------------- Context ----------------
        if self.logger:
            self.logger.set_component(component.name)
            self.logger.set_stage("init")
            self.logger.log_event(
                "infra.component.deploy.start",
                component=component.name,
                namespace=component.namespace,
                release=component.release_name,
                phase=phase or "all",
            )

        kubectl = KubectlRunner(
            ssh=self.ssh,
            kubeconfig=component.kubeconfig,
            logger=self.logger,
        )

        try:
            # ============================================================
            # 1. Pre-install
            # ============================================================
            if run_pre:
                log.info("[%s] Running pre-install...", component.name)
                if self.logger:
                    self.logger.set_stage("pre_install")
                    self.logger.log_event(
                        "infra.component.pre_install.start",
                        component=component.name,
                    )

                component.pre_install(kubectl)

                log.info("[%s] Pre-install complete", component.name)
                if self.logger:
                    self.logger.log_event(
                        "infra.component.pre_install.success",
                        component=component.name,
                    )

            # ============================================================
            # 2. Helm-backed components
            # ============================================================
            if run_helm and component.uses_helm:
                # ---------------- Helm repo ----------------
                if component.local_chart_dir is None:
                    if self.logger:
                        self.logger.set_stage("helm.repo")

                    if not component.repo_name or not component.repo_url:
                        raise ValueError(
                            f"Component {component.name} is marked uses_helm=True "
                            f"but repo_name/repo_url is missing"
                        )

                    self.helm.add_repo(
                        RepoSpec(
                            name=component.repo_name,
                            url=component.repo_url,
                        )
                    )
                    self.helm.update_repos()

                # ---------------- Chart prep ----------------
                if self.logger:
                    self.logger.set_stage("chart.prepare")

                chart_path = prepare_chart(
                    ssh=self.ssh,
                    component=component,
                )

                if self.logger:
                    self.logger.log_event(
                        "infra.chart.ready",
                        chart=str(chart_path),
                    )

                # ---------------- Values layering ----------------
                if self.logger:
                    self.logger.set_stage("values.merge")

                values = deep_merge(
                    self.base_values(component),
                    component.values(),
                )

                # Dump merged values for debugging
                #import json
                #log.info(
                #    "[%s] === Merged Helm values ===\n%s",
                #    component.name,
                #    json.dumps(values, indent=2, default=str),
                #)

                # ---------------- Install / upgrade ----------------
                if self.logger:
                    self.logger.set_stage("helm.install_or_upgrade")

                if self.helm.release_is_deployed(
                    component.release_name, component.namespace,
                ):
                    log.info(
                        "[%s] Helm release '%s' already deployed in '%s' -- skipping",
                        component.name, component.release_name, component.namespace,
                    )
                else:
                    log.info("[%s] Installing helm chart...", component.name)
                    self.helm.install_or_upgrade(
                        name=component.release_name,
                        chart=str(chart_path),
                        namespace=component.namespace,
                        values=values,
                        kubeconfig=component.kubeconfig,
                        wait=False,
                        atomic=False

                    )
                    log.info("[%s] Helm install command completed", component.name)

                # ---------------- Wait ----------------
                if component.wait_for_pods:
                    log.info(
                        "[%s] Waiting for pods to be ready in namespace '%s'...",
                        component.name, component.namespace,
                    )
                    if self.logger:
                        self.logger.set_stage("kubectl.wait")

                    kubectl.wait_for_pods_running(
                        namespace=component.namespace,
                        min_running=component.min_running_pods,
                    )
                    log.info("[%s] Pods are ready", component.name)

            elif run_helm and not component.uses_helm:
                # ============================================================
                # 3. Kubectl-only components (no Helm)
                # ============================================================
                log.debug("[%s] Component does not use helm", component.name)
                if self.logger:
                    self.logger.set_stage("kubectl.only")
                    self.logger.log_event(
                        "infra.component.kubectl_only",
                        component=component.name,
                    )

            # ============================================================
            # 4. Post-install
            # ============================================================
            if run_post:
                log.info("[%s] Running post-install...", component.name)
                if self.logger:
                    self.logger.set_stage("post_install")

                component.post_install(kubectl)

            log.info("[%s] Deployed successfully", component.name)
            if self.logger:
                self.logger.log_event(
                    "infra.component.deploy.success",
                    component=component.name,
                )

        except Exception as e:
            log.error("[%s] Deployment failed: %s", component.name, e)
            if self.logger:
                self.logger.log_event(
                    "infra.component.deploy.failed",
                    component=component.name,
                    error=str(e),
                )
            raise
