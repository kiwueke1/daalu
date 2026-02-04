# src/daalu/bootstrap/monitoring/components/thanos.py

from pathlib import Path
from typing import Dict

from daalu.bootstrap.engine.component import InfraComponent
from daalu.utils.helpers import load_yaml_file


class ThanosComponent(InfraComponent):
    def __init__(
        self,
        *,
        kubeconfig: str,
        values_path: Path,
        assets_dir: Path,
        namespace: str = "monitoring",
        s3_bucket: str,
        s3_endpoint: str,
        s3_access_key: str,
        s3_secret_key: str,
        enable_argocd: bool = False,
        **kwargs,
    ):
        super().__init__(
            name="thanos",
            repo_name="local",
            repo_url="",
            chart="thanos",
            version=None,
            namespace=namespace,
            release_name="thanos",
            kubeconfig=kubeconfig,
            uses_helm=True,
            wait_for_pods=True,
            min_running_pods=1,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src"),
            enable_argocd=enable_argocd,
        )

        # -------------------------------
        # Helm values
        # -------------------------------
        self.values_path = values_path
        self._values = (
            load_yaml_file(values_path)
            if values_path and values_path.exists()
            else {}
        )
        self.assets_dir = assets_dir

        # -------------------------------
        # S3 / MinIO config
        # -------------------------------
        self.s3_bucket = s3_bucket
        self.s3_endpoint = s3_endpoint
        self.s3_access_key = s3_access_key
        self.s3_secret_key = s3_secret_key

    # ---------------------------------------------------------
    # Pre-install hook (exact Ansible behavior)
    # ---------------------------------------------------------
    def pre_install(self, kubectl):
        # Create object store secret
        kubectl.apply_objects(
            [
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {
                        "name": "thanos-objstore",
                        "namespace": self.namespace,
                    },
                    "stringData": {
                        "objstore.yml": (
                            "type: s3\n"
                            "config:\n"
                            f"  bucket: {self.s3_bucket}\n"
                            f"  endpoint: {self.s3_endpoint}\n"
                            f"  access_key: {self.s3_access_key}\n"
                            f"  secret_key: {self.s3_secret_key}\n"
                            "  insecure: true\n"
                        )
                    },
                }
            ]
        )

        # Wait for secret (same semantics as Ansible retries)
        kubectl.wait_for(
            kind="Secret",
            name="thanos-objstore",
            namespace=self.namespace,
            timeout_seconds=60,
        )
        thanos_bucket_job_path = self.assets_dir/"thanos-bucket.yaml"
        kubectl.apply_file(thanos_bucket_job_path)

    # ---------------------------------------------------------
    # Helm values (used by HelmInfraEngine)
    # ---------------------------------------------------------
    def values(self) -> Dict:
        return self._values
