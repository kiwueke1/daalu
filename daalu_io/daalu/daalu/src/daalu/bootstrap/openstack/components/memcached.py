# src/daalu/bootstrap/openstack/components/memcached.py

from pathlib import Path

from daalu.bootstrap.engine.component import InfraComponent
from daalu.utils.helpers import load_yaml_file
from daalu.utils.helpers import kubectl
import logging

log = logging.getLogger("daalu")


class MemcachedComponent(InfraComponent):
    def __init__(
        self,
        *,
        kubeconfig: str,
        values_path: Path,
        assets_dir: Path,
        namespace: str = "openstack",
        enable_argocd: bool = True,
    ):
        super().__init__(
            name="memcached",
            repo_name="local",
            repo_url="",
            chart="memcached",
            version=None,
            namespace=namespace,
            release_name="memcached",
            kubeconfig=kubeconfig,
            uses_helm=True,
            wait_for_pods=True,
            min_running_pods=1,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src"),
            enable_argocd=enable_argocd,
        )

        self.values_path = values_path
        self.assets_dir = assets_dir

        self._values = (
            load_yaml_file(values_path)
            if values_path and values_path.exists()
            else {}
        )

    def post_deploy(self):
        """
        Mirrors:
        - Apply Service memcached-metrics
        """
        metrics_manifest = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": "memcached-metrics",
                "namespace": self.namespace,
                "labels": {
                    "application": "memcached",
                    "component": "server",
                },
            },
            "spec": {
                "ports": [
                    {
                        "name": "metrics",
                        "protocol": "TCP",
                        "port": 9150,
                        "targetPort": 9150,
                    }
                ],
                "selector": {
                    "application": "memcached",
                    "component": "server",
                },
            },
        }

        kubectl.apply_object(
            metrics_manifest,
            kubeconfig=self.kubeconfig,
        )

    def values(self) -> dict:
        log.debug(f"values are {self._values}")
        return self._values