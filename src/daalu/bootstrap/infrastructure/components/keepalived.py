# src/daalu/bootstrap/infrastructure/components/keepalived.py

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import json
import yaml

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from daalu.bootstrap.engine.component import InfraComponent
from daalu.utils.helpers import wait_for_node_interface_ipv4
import logging

log = logging.getLogger("daalu")


def _render_jinja_dir(*, root: Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(root)),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )

    # Needed for manifests.yaml.j2
    env.filters["tojson"] = lambda v: json.dumps(v)
    env.filters["indent"] = lambda s, n=2: "\n".join((" " * n + line) for line in str(s).splitlines())

    return env


class KeepalivedComponent(InfraComponent):
    """
    Keepalived VIP DaemonSet (no Helm).
    All Kubernetes resources live in assets/keepalived/manifests.yaml.j2
    """

    def __init__(
        self,
        *,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
    ):
        super().__init__(
            name="keepalived",
            repo_name="local",
            repo_url="",
            chart="",
            version=None,
            namespace=namespace,
            release_name="keepalived",
            local_chart_dir=None,
            remote_chart_dir=None,
            kubeconfig=kubeconfig,
            uses_helm=False,
        )

        self.assets_dir = assets_dir
        self.values_path = assets_dir / "values.yaml"
        self.namespace = namespace

        self.wait_for_pods = True
        self.min_running_pods = 1

        self._values: Dict[str, Any] = yaml.safe_load(self.values_path.read_text()) or {}
        self._jinja = _render_jinja_dir(root=self.assets_dir)
        self.enable_argocd = False

    def _required(self, key: str) -> Any:
        v = self._values.get(key)
        if v in (None, "", []):
            raise ValueError(f"[keepalived] Missing required '{key}' in {self.values_path}")
        return v

    def pre_install(self, kubectl) -> None:
        if not self._values.get("keepalived_enabled", True):
            log.debug("[keepalived] Disabled, skipping")
            return

        # Required config
        self._required("keepalived_password")
        self._required("keepalived_vip")
        self._required("keepalived_interface")
        self._required("dep_check_image")
        self._required("keepalived_image")

        backend = self._values.get("network_backend", "ovn")
        dep_map = self._values.get("keepalived_pod_dependency") or {}
        dependency = dep_map.get(backend)
        if dependency is None:
            raise ValueError(
                f"[keepalived] keepalived_pod_dependency missing backend '{backend}'"
            )

        # Render keepalived.conf
        keepalived_conf = self._jinja.get_template("keepalived.conf.j2").render(
            **self._values
        )

        # Render full manifests
        manifests = self._jinja.get_template("manifests.yaml.j2").render(
            namespace=self.namespace,
            keepalived_conf=keepalived_conf,
            dependency_pod_json=dependency,
            **self._values,
        )

        # Apply (your kubectl wrapper should support applying raw YAML)
        # If you don't have apply_yaml, use apply_file with a temp file.
        kubectl.apply_content(content=manifests, remote_path="/tmp/keepalived.yaml")

        # Replace the old wait-for-ip initContainer with a Daalu-level wait
        wait_cfg = self._values.get("wait_for_interface_ipv4", {}) or {}


        # Basic sanity
        kubectl.run(["get", "ds", "keepalived", "-n", self.namespace])

    def helm_values(self) -> Dict[str, Any]:
        return self._values
