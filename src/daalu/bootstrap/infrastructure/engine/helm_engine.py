# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu.bootstrap.engine.helm_engine.py

from __future__ import annotations

from daalu.bootstrap.engine.chart_manager import prepare_chart
from daalu.bootstrap.engine.values import deep_merge
from daalu.kube.kubectl import KubectlRunner
from daalu.config.models import RepoSpec
from daalu.bootstrap.engine.infra_logging import InfraJsonlLogger


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

    def deploy_1(self, component):
        # ---------------- Context ----------------
        if self.logger:
            self.logger.set_component(component.name)
            self.logger.set_stage("init")
            self.logger.log_event(
                "infra.component.deploy.start",
                component=component.name,
                namespace=component.namespace,
                release=component.release_name,
            )

        kubectl = KubectlRunner(
            ssh=self.ssh,
            kubeconfig=component.kubeconfig,
        )

        try:
            # ============================================================
            # Helm-backed components
            # ============================================================
            if component.uses_helm:
                # ---------------- 1. Helm repo ----------------
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

                # ---------------- 2. Chart prep ----------------
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

                # ---------------- 3. Values layering ----------------
                if self.logger:
                    self.logger.set_stage("values.merge")

                values = deep_merge(
                    self.base_values(component),
                    component.values(),
                )

                # ---------------- 3.5. Pre-install ----------------
                if self.logger:
                    self.logger.set_stage("pre_install")
                    self.logger.log_event(
                        "infra.component.pre_install.start",
                        component=component.name,
                    )

                component.pre_install(kubectl)

                if self.logger:
                    self.logger.log_event(
                        "infra.component.pre_install.success",
                        component=component.name,
                    )

                # ---------------- 4. Install / upgrade ----------------
                if self.logger:
                    self.logger.set_stage("helm.install_or_upgrade")

                self.helm.install_or_upgrade(
                    name=component.release_name,
                    chart=str(chart_path),
                    namespace=component.namespace,
                    values=values,
                    kubeconfig=component.kubeconfig,
                )

                # ---------------- 5. Wait ----------------
                if component.wait_for_pods:
                    if self.logger:
                        self.logger.set_stage("kubectl.wait")

                    kubectl.wait_for_pods_running(
                        namespace=component.namespace,
                        min_running=component.min_running_pods,
                    )

            # ============================================================
            # Kubectl-only components (no Helm)
            # ============================================================
            else:
                if self.logger:
                    self.logger.set_stage("kubectl.only")
                    self.logger.log_event(
                        "infra.component.kubectl_only",
                        component=component.name,
                    )

            # ---------------- 6. Post-install (always) ----------------
            if self.logger:
                self.logger.set_stage("post_install")

            component.post_install(kubectl)

            if self.logger:
                self.logger.log_event(
                    "infra.component.deploy.success",
                    component=component.name,
                )

        except Exception as e:
            if self.logger:
                self.logger.log_event(
                    "infra.component.deploy.failed",
                    component=component.name,
                    error=str(e),
                )
            raise

    def deploy(self, component):
        # ---------------- Context ----------------
        if self.logger:
            self.logger.set_component(component.name)
            self.logger.set_stage("init")
            self.logger.log_event(
                "infra.component.deploy.start",
                component=component.name,
                namespace=component.namespace,
                release=component.release_name,
            )

        kubectl = KubectlRunner(
            ssh=self.ssh,
            kubeconfig=component.kubeconfig,
        )

        try:
            # ============================================================
            # 1. Pre-install (ALWAYS runs)
            # ============================================================
            if self.logger:
                self.logger.set_stage("pre_install")
                self.logger.log_event(
                    "infra.component.pre_install.start",
                    component=component.name,
                )

            component.pre_install(kubectl)

            if self.logger:
                self.logger.log_event(
                    "infra.component.pre_install.success",
                    component=component.name,
                )

            # ============================================================
            # 2. Helm-backed components
            # ============================================================
            if component.uses_helm:
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

                # ---------------- Install / upgrade ----------------
                if self.logger:
                    self.logger.set_stage("helm.install_or_upgrade")

                self.helm.install_or_upgrade(
                    name=component.release_name,
                    chart=str(chart_path),
                    namespace=component.namespace,
                    values=values,
                    kubeconfig=component.kubeconfig,
                )

                # ---------------- Wait ----------------
                if component.wait_for_pods:
                    if self.logger:
                        self.logger.set_stage("kubectl.wait")

                    kubectl.wait_for_pods_running(
                        namespace=component.namespace,
                        min_running=component.min_running_pods,
                    )

            # ============================================================
            # 3. Kubectl-only components (no Helm)
            # ============================================================
            else:
                if self.logger:
                    self.logger.set_stage("kubectl.only")
                    self.logger.log_event(
                        "infra.component.kubectl_only",
                        component=component.name,
                    )

            # ============================================================
            # 4. Post-install (ALWAYS runs)
            # ============================================================
            if self.logger:
                self.logger.set_stage("post_install")

            component.post_install(kubectl)

            if self.logger:
                self.logger.log_event(
                    "infra.component.deploy.success",
                    component=component.name,
                )

        except Exception as e:
            if self.logger:
                self.logger.log_event(
                    "infra.component.deploy.failed",
                    component=component.name,
                    error=str(e),
                )
            raise
