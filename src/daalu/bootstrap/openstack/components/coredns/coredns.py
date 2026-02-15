# src/daalu/bootstrap/openstack/components/coredns/coredns.py

from __future__ import annotations

from pathlib import Path

from daalu.bootstrap.engine.component import InfraComponent


class CoreDNSComponent(InfraComponent):
    """
    Daalu CoreDNS component.

    Deploys the CoreDNS Helm chart as a Neutron DNS resolver providing:
    - DNS forwarding on port 53 to upstream resolvers
    - Cloudflare DNS-over-TLS on port 5301
    - Google DNS-over-TLS on port 5302
    - Prometheus metrics on port 9153
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        release_name: str = "neutron-coredns",
        enable_argocd: bool = False,
    ):
        super().__init__(
            name="coredns",
            repo_name="local",
            repo_url="",
            chart="coredns",
            version=None,
            namespace=namespace,
            release_name=release_name,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/coredns"),
            kubeconfig=kubeconfig,
            uses_helm=True,
            wait_for_pods=True,
            min_running_pods=1,
            enable_argocd=enable_argocd,
        )

        self.values_path = values_path
        self._assets_dir = assets_dir

    def assets_dir(self) -> Path:
        return self._assets_dir

    def values(self) -> dict:
        return self.load_values_file(self.values_path)
