from __future__ import annotations
from abc import ABC
from dataclasses import dataclass
from pathlib import Path
import yaml
from typing import Optional
import urllib.request
import time
import requests



@dataclass
class InfraComponent(ABC):
    """
    Declarative definition of an infrastructure component.
    """

    # Identity
    name: str

    # Helm repository
    repo_name: str
    repo_url: str

    # Helm chart
    chart: str
    version: Optional[str]

    # Helm release
    namespace: str
    release_name: str

    # Chart handling
    local_chart_dir: Path 
    remote_chart_dir: Path

    # Kubernetes
    kubeconfig: str

    # uses_helm boolean differentiates components that dont require Helm actions.
    uses_helm: bool = True

    # Optional hooks
    wait_for_pods: bool = True
    min_running_pods: int = 1
    enable_argocd: bool = False

    # -------------------------------------------------
    # Istio exposure (optional)
    # -------------------------------------------------
    istio_enabled: bool = False
    istio_gateway: str = "istio-ingress/gateway"
    istio_namespace: str = "istio-ingress"

    istio_host: Optional[str] = None
    istio_service: Optional[str] = None
    istio_service_namespace: Optional[str] = None
    istio_service_port: Optional[int] = None

    istio_expected_status: int = 300

    # ------------------------
    # Hooks
    # ------------------------


    def load_values_file(self, path: Path) -> dict:
        """
        Load a Helm values YAML file from disk.

        Returns an empty dict if the file does not exist.
        """
        if not path.exists():
            raise FileNotFoundError(f"Helm values file not found: {path}")

        with path.open("r") as f:
            data = yaml.safe_load(f)

        return data or {}

    def assets_dir(self) -> Path | None:
        return None 

    def _argocd_config(self) -> dict | None:
        assets = self.assets_dir
        if not assets:
            return None

        config_path = assets / "config.yaml"
        if not config_path.exists():
            return  None

        data = yaml.safe_load(config_path.read_text()) or {}

        try:
            return data["argocd"]["app"]
        except KeyError as e:
            raise RuntimeError(
                f"Invalid Argo CD config in {config_path}, expected argocd.app"
            ) from e

    def values(self) -> Dict:
        """
        Helm values.
        """
        return getattr(self, "_values", {}) or {}


    def _onboard_to_argocd(self, kubectl) -> None:
        cfg = self._argocd_config()
        if not cfg:
            return  # component does not opt into GitOps

        app_name = cfg["name"]
        namespace = cfg["namespace"]
        manifest_url = cfg["manifest_url"]

        # 1. Wait for Argo CD CRD
        kubectl.wait_for_crd("applications.argoproj.io")

        # 2. Check if app already exists
        rc, stdout, _ = kubectl.run(
            [
                "get",
                "applications.argoproj.io",
                "-n",
                namespace,
                "-o",
                "name",
            ],
            check=False,
        )

        if rc == 0:
            existing = [line.split("/")[-1].lower() for line in stdout.splitlines()]
            if app_name.lower() in existing:
                return  # already onboarded

        # 3. Download manifest
        target = Path(f"/tmp/daalu/argocd/{app_name}.yaml")
        target.parent.mkdir(parents=True, exist_ok=True)

        with urllib.request.urlopen(manifest_url) as resp:
            target.write_text(resp.read().decode())

        # 4. Apply manifest
        kubectl.apply_file(target)

    # ------------------------------------------------------------
    # Default post-install hook
    # ------------------------------------------------------------
    def post_install(self, kubectl) -> None:
        """
        Default post-install hook.
        Components may override but should call super().
        """
            # Istio exposure (if enabled)
        self._ensure_virtualservice(kubectl)
        self._validate_ingress()
        if self.enable_argocd:
            self._onboard_to_argocd(kubectl)

    def pre_install(self, kubectl):
        """Optional hook. Default: do nothing."""
        pass


    def _ensure_virtualservice(self, kubectl) -> None:
        if not self.istio_enabled:
            return

        required = [
            self.istio_host,
            self.istio_service,
            self.istio_service_namespace,
            self.istio_service_port,
        ]

        if not all(required):
            raise RuntimeError(
                f"{self.name}: Istio enabled but exposure fields not fully defined"
            )

        vs_name = f"{self.name}-vs"
        print(f"virtualservice is {vs_name}")

        if kubectl.resource_exists(
            kind="virtualservice.networking.istio.io",
            name=vs_name,
            namespace=self.istio_namespace,
        ):
            print(f"[{self.name}] Istio VirtualService already exists ✓")
            return

        print(f"[{self.name}] Creating Istio VirtualService...")

        manifest = {
            "apiVersion": "networking.istio.io/v1beta1",
            "kind": "VirtualService",
            "metadata": {
                "name": vs_name,
                "namespace": self.istio_namespace,
            },
            "spec": {
                "gateways": [self.istio_gateway],
                "hosts": [self.istio_host],
                "http": [
                    {
                        "match": [{"uri": {"prefix": "/"}}],
                        "route": [
                            {
                                "destination": {
                                    "host": (
                                        f"{self.istio_service}."
                                        f"{self.istio_service_namespace}."
                                        "svc.cluster.local"
                                    ),
                                    "port": {
                                        "number": self.istio_service_port
                                    },
                                }
                            }
                        ],
                    }
                ],
            },
        }

        kubectl.apply_objects([manifest])
        print(f"[{self.name}] Istio VirtualService created ✓")



    def _validate_ingress(self) -> None:
        """
        Validate external reachability of a component.

        - Istio mode: validate via istio_host
        - Legacy mode: subclasses may override
        """
        if not getattr(self, "istio_enabled", False):
            return

        host = self.istio_host
        url = f"https://{host}"

        expected = getattr(self, "istio_expected_status", 200)

        print(f"[{self.name}] Validating external access via Istio: {url}")

        for i in range(120):
            try:
                r = requests.get(url, verify=False, timeout=2)
                if r.status_code == expected:
                    print(f"[{self.name}] External access reachable ✓")
                    return
            except Exception:
                pass

            time.sleep(1)

        raise RuntimeError(
            f"{self.name}: external access not reachable via Istio ({url})"
        )
