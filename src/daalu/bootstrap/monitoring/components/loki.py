# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/monitoring/components/loki.py

from pathlib import Path

from daalu.bootstrap.engine.component import InfraComponent


class LokiComponent(InfraComponent):
    def __init__(
        self,
        *,
        assets_dir: Path,
        values_path: Path,
        kubeconfig: str,
        namespace: str = "monitoring",
        enable_argocd: bool = False,
    ):
        super().__init__(
            name="loki",
            repo_name="local",
            repo_url="",
            chart="loki",
            version=None,
            namespace=namespace,
            release_name="loki",
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src"),
            kubeconfig=kubeconfig,
        )

        self._assets_dir = assets_dir
        self.values_path = values_path
        self.enable_argocd = enable_argocd

        self.wait_for_pods = True
        self.min_running_pods = 1

    def assets_dir(self) -> Path:
        return self._assets_dir

    def values(self) -> dict:
        return self.load_values_file(self.values_path)

    def post_install(self, kubectl):
        kubectl.apply_objects([
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": "loki-alerting-rules",
                    "namespace": self.namespace,
                    "labels": {"loki_rule": "atmosphere"},
                },
                "data": {
                    "loki-alerting-rules.yaml": """
    groups:
    - name: additional-loki-rules
        rules:
        - alert: NovaCellNotResponding
            expr: 'count_over_time({pod_label_component="compute"} |= "not responding and hence is being omitted from the results" [1m]) > 0'
            labels:
            severity: critical
            annotations:
            summary: Nova Cell is not responding. It can cause port deletion in CAPI.
    """
                },
            }
        ])

        super().post_install(kubectl)
