from __future__ import annotations

from dataclasses import field
from pathlib import Path
from typing import Dict, Optional

from daalu.bootstrap.engine.component import InfraComponent


class RookCephComponent(InfraComponent):
    """
    Daalu Rook-Ceph component.

    Mirrors:
    roles/rook_ceph/tasks/main.yml
    roles/rook_ceph/defaults/main.yml
    roles/rook_ceph/vars/main.yml
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "rook-ceph",
        enable_argocd: bool = False,
    ):
        super().__init__(
            name="rook-ceph",
            repo_name="local",
            repo_url="",
            chart="rook-ceph",
            version=None,
            namespace=namespace,
            release_name="rook-ceph",
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/rook-ceph"),
            kubeconfig=kubeconfig,
            uses_helm=True,
            wait_for_pods=True,
            min_running_pods=1,
            enable_argocd=enable_argocd,
        )

        self.values_path = values_path
        self._assets_dir = assets_dir

        self.requires_public_ingress = True

        # Load user-supplied values (equivalent to rook_ceph_helm_values)
        self._user_values: Dict = {}
        if values_path and values_path.exists():
            self._user_values = self.load_values_file(values_path)

    def values(self) -> Dict:
        """
        Build Helm values.

        Mirrors the Ansible pattern:
        _rook_ceph_helm_values | combine(rook_ceph_helm_values, recursive=True)

        Internal defaults (from vars/main.yml) are the base;
        user values from values.yaml override them.
        """
        base: Dict = {
            "nodeSelector": {
                "openstack-control-plane": "enabled",
            },
            "resources": {
                "limits": {
                    "cpu": 1,
                },
            },
            # CSI drivers disabled — storage managed externally
            "csi": {
                "enableRbdDriver": False,
                "enableCephfsDriver": False,
            },
        }

        # Recursive merge: user values win over base
        return self._deep_merge(base, self._user_values)

    # ------------------------------------------------------------------
    # No pre_install or post_install needed.
    # The Ansible role only runs a single helm deploy task with no
    # pre/post hooks beyond chart upload (handled by the engine).
    # ------------------------------------------------------------------

    @staticmethod
    def _deep_merge(a: dict, b: dict) -> dict:
        """Recursively merge *b* into *a*; *b* wins on conflicts."""
        out = dict(a)
        for k, v in b.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = RookCephComponent._deep_merge(out[k], v)
            else:
                out[k] = v
        return out