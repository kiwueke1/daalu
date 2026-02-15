# src/daalu/bootstrap/openstack/components/libvirt/libvirt.py

from __future__ import annotations

from pathlib import Path

from daalu.bootstrap.engine.component import InfraComponent
import logging

log = logging.getLogger("daalu")


class LibvirtComponent(InfraComponent):
    """
    Daalu libvirt component.

    Deploys the libvirt Helm chart (DaemonSet) which runs libvirtd
    on all compute nodes.  Provides the hypervisor layer for Nova.

    Pre-install:
    - Labels compute nodes with openstack-compute-node=enabled
    - Creates cert-manager CA Certificates and Issuers for TLS
      (libvirt-api and libvirt-vnc)
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        release_name: str = "libvirt",
        network_backend: str = "ovn",
        ssh=None,
        enable_argocd: bool = False,
    ):
        super().__init__(
            name="libvirt",
            repo_name="local",
            repo_url="",
            chart="libvirt",
            version=None,
            namespace=namespace,
            release_name=release_name,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/libvirt"),
            kubeconfig=kubeconfig,
            uses_helm=True,
            wait_for_pods=True,
            min_running_pods=1,
            enable_argocd=enable_argocd,
        )

        self.values_path = values_path
        self._assets_dir = assets_dir
        self._network_backend = network_backend
        self._ssh = ssh
        self.wait_for_pods = True
        self.min_running_pods = 1

    # -------------------------------------------------
    # Helpers
    # -------------------------------------------------
    def assets_dir(self) -> Path:
        return self._assets_dir

    def values(self) -> dict:
        base = self.load_values_file(self.values_path)
        # Inject network backend (ovn, openvswitch, linuxbridge)
        base.setdefault("network", {})
        base["network"]["backend"] = [self._network_backend]
        return base

    # -------------------------------------------------
    # pre_install
    # -------------------------------------------------
    def pre_install(self, kubectl):
        log.debug("[libvirt] Starting pre-install...")

        # 1) Label compute nodes
        self._label_compute_nodes(kubectl)

        # 2) Create cert-manager Certificates + Issuers for TLS
        self._ensure_tls_issuers(kubectl)

        log.debug("[libvirt] pre-install complete")

    def _label_compute_nodes(self, kubectl):
        """Label all nodes with openstack-compute-node=enabled."""
        log.debug("[libvirt] Labeling nodes with openstack-compute-node=enabled...")
        rc, out, err = kubectl._run(
            "get nodes -o jsonpath={.items[*].metadata.name}"
        )
        if rc != 0:
            raise RuntimeError(f"Failed to list nodes: {err}")

        nodes = (out or "").split()
        if not nodes:
            raise RuntimeError("No nodes found in cluster")

        for node in nodes:
            rc, _, err = kubectl._run(
                f"label node {node} openstack-compute-node=enabled --overwrite"
            )
            if rc != 0:
                raise RuntimeError(f"Failed to label node {node}: {err}")
            log.debug(f"[libvirt] Labeled node {node}")

    def _ensure_tls_issuers(self, kubectl):
        """Create cert-manager CA Certificates and Issuers for libvirt TLS."""
        for name in ("libvirt-api", "libvirt-vnc"):
            # CA Certificate
            ca_cert = {
                "apiVersion": "cert-manager.io/v1",
                "kind": "Certificate",
                "metadata": {
                    "name": f"{name}-ca",
                    "namespace": self.namespace,
                },
                "spec": {
                    "commonName": "libvirt",
                    "duration": "87600h0m0s",
                    "isCA": True,
                    "issuerRef": {
                        "group": "cert-manager.io",
                        "kind": "ClusterIssuer",
                        "name": "self-signed",
                    },
                    "privateKey": {
                        "algorithm": "ECDSA",
                        "size": 256,
                    },
                    "renewBefore": "720h0m0s",
                    "secretName": f"{name}-ca",
                },
            }

            # Issuer backed by the CA
            issuer = {
                "apiVersion": "cert-manager.io/v1",
                "kind": "Issuer",
                "metadata": {
                    "name": name,
                    "namespace": self.namespace,
                },
                "spec": {
                    "ca": {
                        "secretName": f"{name}-ca",
                    }
                },
            }

            log.debug(f"[libvirt] Ensuring cert-manager CA + Issuer: {name}")
            kubectl.apply_objects([ca_cert, issuer])

        log.debug("[libvirt] TLS issuers ready")
