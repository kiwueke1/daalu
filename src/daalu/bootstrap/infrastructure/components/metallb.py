# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

import time
from pathlib import Path
from daalu.bootstrap.engine.component import InfraComponent
import logging

log = logging.getLogger("daalu")


class MetalLBComponent(InfraComponent):
    def __init__(
        self,
        *,
        values_path: Path,
        metallb_config_path: Path,
        kubeconfig: str,
    ):
        assets_dir = values_path.parent
        super().__init__(
            name="metallb",
            repo_name="local",
            repo_url="",
            chart="metallb",
            version=None,
            namespace="metallb-system",
            release_name="metallb",
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/metallb"),
            kubeconfig=kubeconfig,
        )

        self.values_path = values_path
        self.metallb_config_path = metallb_config_path
        self.wait_for_pods = True
        self.min_running_pods = 2

    # ------------------------------------------------------------------
    # Helm values (from assets)
    # ------------------------------------------------------------------

    def values(self) -> dict:
        return self.load_values_file(self.values_path)

    # ------------------------------------------------------------------
    # Post-install: apply address pool config
    # ------------------------------------------------------------------

    def post_install(self, kubectl) -> None:
        # MetalLB CRDs (IPAddressPool, L2Advertisement) are registered by the
        # controller after the Helm chart is installed.  Wait until the API
        # server knows about them before applying the pool config.
        log.debug("[metallb] Waiting for MetalLB CRDs to be registered...")
        for attempt in range(1, 31):
            rc, _, _ = kubectl._run(
                "api-resources --api-group=metallb.io -o name"
            )
            if rc == 0:
                _, out, _ = kubectl._run(
                    "api-resources --api-group=metallb.io -o name"
                )
                if "ipaddresspools" in out:
                    log.debug("[metallb] CRDs ready.")
                    break
            log.debug(
                f"[metallb] CRDs not yet available, retrying "
                f"({attempt}/30)..."
            )
            time.sleep(10)
        else:
            raise RuntimeError(
                "Timed out waiting for MetalLB CRDs (ipaddresspools.metallb.io)"
            )

        content = self.metallb_config_path.read_text()
        kubectl.apply_content(
            content=content,
            remote_path="/tmp/metallb-config.yaml",
        )
