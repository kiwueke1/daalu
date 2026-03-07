# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/mgmt/metal3_installer.py

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import subprocess
import tempfile
from pathlib import Path

import yaml

from daalu.bootstrap.mgmt.models import MgmtClusterConfig

log = logging.getLogger("daalu")

# ------------------------------------------------------------------
# Well-known release URLs / references
# ------------------------------------------------------------------

CERT_MANAGER_URL = (
    "https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml"
)
IRSO_URL = (
    "https://github.com/metal3-io/ironic-standalone-operator/releases/latest/download/install.yaml"
)
# BMO applied via kustomize remote URL (no git clone needed).
# Must use main branch because:
#  - the image quay.io/metal3-io/baremetal-operator has no tag → pulls latest (main HEAD)
#  - HostClaim and other newer CRDs are only in main, not in any stable release yet
#  - v0.8.0 kustomize does not include hostclaims.yaml → BMO crashes on start
# Pin to a specific commit once a stable release includes HostClaim.
BMO_KUSTOMIZE_URL = (
    "https://github.com/metal3-io/baremetal-operator/config/default?ref=main"
)


class Metal3Installer:
    """
    Installs the Metal3 stack on an existing Kubernetes cluster:

      1. cert-manager
      2. Cluster API core (+ kubeadm bootstrap / control-plane providers)
      3. Ironic Standalone Operator (IrSO)
      4. Ironic CR  →  IrSO deploys Ironic
      5. Baremetal Operator (BMO) wired to Ironic
      6. Cluster API Provider Metal3 (CAPM3) + IPAM

    All operations run locally via subprocess using a kubeconfig that
    points at the remote mgmt cluster.
    """

    def __init__(self, kubeconfig_path: str, cfg: MgmtClusterConfig):
        self._kc = kubeconfig_path
        self._cfg = cfg

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def install(self) -> None:
        self._install_cert_manager()
        self._install_capi()
        self._install_irso()
        self._deploy_ironic()
        self._setup_bmo()
        self._install_capm3()
        log.info("[mgmt/metal3] Metal3 stack installed successfully")

    def _deployment_ready(self, namespace: str, deploy: str) -> bool:
        """Return True if a Deployment exists and has at least one ready replica."""
        r = subprocess.run(
            [
                "kubectl", "--kubeconfig", self._kc,
                "get", f"deploy/{deploy}", "-n", namespace,
                "-o", "jsonpath={.status.readyReplicas}",
            ],
            capture_output=True, text=True,
        )
        return r.returncode == 0 and r.stdout.strip() not in ("", "0")

    # ------------------------------------------------------------------
    # Step 1 — cert-manager
    # ------------------------------------------------------------------

    def _install_cert_manager(self) -> None:
        if self._deployment_ready("cert-manager", "cert-manager"):
            log.info("[mgmt/metal3] cert-manager already ready — skipping")
            return
        log.info("[mgmt/metal3] Installing cert-manager...")
        self._kubectl("apply", "-f", CERT_MANAGER_URL)
        for deploy in ["cert-manager", "cert-manager-webhook", "cert-manager-cainjector"]:
            self._kubectl(
                "-n", "cert-manager",
                "rollout", "status", f"deploy/{deploy}",
                "--timeout=5m",
            )
        log.info("[mgmt/metal3] cert-manager ready")

    # ------------------------------------------------------------------
    # Step 2 — Cluster API core providers
    # ------------------------------------------------------------------

    def _install_capi(self) -> None:
        if self._deployment_ready("capi-system", "capi-controller-manager"):
            log.info("[mgmt/metal3] CAPI already installed — skipping")
            return
        ver = self._cfg.capi_version
        log.info("[mgmt/metal3] Initialising Cluster API core (%s)...", ver)
        subprocess.run(
            [
                "clusterctl", "--kubeconfig", self._kc,
                "init",
                "--core", f"cluster-api:{ver}",
                "--bootstrap", f"kubeadm:{ver}",
                "--control-plane", f"kubeadm:{ver}",
                "-v5",
            ],
            check=True,
        )
        log.info("[mgmt/metal3] CAPI core installed")

    # ------------------------------------------------------------------
    # Step 3 — Ironic Standalone Operator
    # ------------------------------------------------------------------

    def _install_irso(self) -> None:
        if self._deployment_ready(
            "ironic-standalone-operator-system",
            "ironic-standalone-operator-controller-manager",
        ):
            log.info("[mgmt/metal3] IrSO already ready — skipping")
            return
        log.info("[mgmt/metal3] Installing Ironic Standalone Operator...")
        self._kubectl("apply", "-f", IRSO_URL)
        self._kubectl(
            "-n", "ironic-standalone-operator-system",
            "wait", "--for=condition=Available",
            "deploy/ironic-standalone-operator-controller-manager",
            "--timeout=5m",
        )
        log.info("[mgmt/metal3] IrSO ready")

    # ------------------------------------------------------------------
    # Step 4 — Ironic CR  (IrSO creates the Ironic deployment)
    # ------------------------------------------------------------------

    def _deploy_ironic(self) -> None:
        ns = self._cfg.ironic_namespace
        name = self._cfg.ironic_name
        log.info("[mgmt/metal3] Deploying Ironic '%s' in namespace '%s'...", name, ns)

        # Ensure namespace
        self._kubectl("create", "namespace", ns, check=False)

        # Use the provisioning_ip when configured (static provisioning NIC IP).
        # Fall back to the management host IP only if no provisioning IP is set.
        external_ip = self._cfg.provisioning_ip or self._cfg.host

        networking: dict = {
            "externalIP": external_ip,
            "interface": self._cfg.provisioning_interface,
        }
        if self._cfg.provisioning_ip:
            networking["ipAddress"] = self._cfg.provisioning_ip
        if self._cfg.dhcp_range_begin and self._cfg.dhcp_range_end:
            # Compute network CIDR from provisioning_ip/prefix (e.g. 10.10.0.0/16)
            net = ipaddress.ip_network(
                f"{external_ip}/{self._cfg.provisioning_prefix}", strict=False
            )
            networking["dhcp"] = {
                "networkCIDR": str(net),
                "rangeBegin": self._cfg.dhcp_range_begin,
                "rangeEnd": self._cfg.dhcp_range_end,
                "gatewayAddress": self._cfg.dhcp_gateway or self._cfg.host,
                "dnsAddress": self._cfg.dhcp_dns,
            }

        ironic_cr = {
            "apiVersion": "ironic.metal3.io/v1alpha1",
            "kind": "Ironic",
            "metadata": {"name": name, "namespace": ns},
            "spec": {"networking": networking},
        }

        self._kubectl_stdin("apply", "-f", "-", input=yaml.dump(ironic_cr))

        log.info("[mgmt/metal3] Waiting for Ironic to be Ready (timeout 10m)...")
        self._kubectl(
            "-n", ns,
            "wait", "--for=condition=Ready",
            f"ironic/{name}",
            "--timeout=10m",
        )
        log.info("[mgmt/metal3] Ironic is Ready")

    # ------------------------------------------------------------------
    # Step 5 — Baremetal Operator, wired to Ironic
    # ------------------------------------------------------------------

    def _setup_bmo(self) -> None:
        ns = self._cfg.ironic_namespace
        name = self._cfg.ironic_name
        bmo_ns = "baremetal-operator-system"

        if self._deployment_ready(bmo_ns, "baremetal-operator-controller-manager"):
            log.info("[mgmt/metal3] BMO already ready — skipping")
            return

        log.info("[mgmt/metal3] Extracting Ironic API credentials...")
        secret_name, username, password = self._get_ironic_credentials(ns, name)

        log.info("[mgmt/metal3] Creating daalu-ironic-auth secret for BMO...")
        self._kubectl(
            "-n", ns,
            "create", "secret", "generic", "daalu-ironic-auth",
            f"--from-literal=username={username}",
            f"--from-literal=password={password}",
            check=False,  # idempotent: ignore AlreadyExists
        )

        log.info("[mgmt/metal3] Applying Baremetal Operator CRDs and manifests...")
        # Apply CRDs first — kustomize config/default includes config/base/crds but
        # some CRDs (hostclaims, hostupdatepolicies, hostdeploypolicies) are only on
        # the main branch.  Apply them explicitly before the kustomize so the manager
        # does not crash on startup or get stuck with missing-kind reconcile errors.
        _BMO_CRD_BASE = "https://raw.githubusercontent.com/metal3-io/baremetal-operator/main/config/base/crds/bases"
        for crd_file in [
            "metal3.io_hostclaims.yaml",
            "metal3.io_hostupdatepolicies.yaml",
            "metal3.io_hostdeploypolicies.yaml",
        ]:
            self._kubectl("apply", "-f", f"{_BMO_CRD_BASE}/{crd_file}")

        self._kubectl("apply", "-k", BMO_KUSTOMIZE_URL)

        # The BMO kustomize config/default generates an 'ironic' ConfigMap from
        # ironic.env with hardcoded default IPs (172.22.0.2).  IrSO uses the
        # mgmt node's real externalIP, so patch the configmap immediately after
        # apply to avoid Ironic trying to validate images against a non-existent IP.
        host = self._cfg.provisioning_ip or self._cfg.host
        log.info("[mgmt/metal3] Patching 'ironic' configmap with provisioning IP %s...", host)
        self._kubectl(
            "patch", "configmap", "ironic",
            "-n", bmo_ns,
            "--type=merge",
            "-p", (
                f'{{"data":{{'
                f'"DEPLOY_KERNEL_URL":"http://{host}:6180/images/ironic-python-agent.kernel",'
                f'"DEPLOY_RAMDISK_URL":"http://{host}:6180/images/ironic-python-agent.initramfs",'
                f'"CACHEURL":"http://{host}/images"'
                f'}}}}'
            ),
        )

        # Wait for BMO to be available
        self._kubectl(
            "-n", bmo_ns,
            "rollout", "status",
            "deploy/baremetal-operator-controller-manager",
            "--timeout=5m",
        )

        # IrSO exposes the Ironic API on port 80 (not 6385).
        # BMO reads credentials from files at /opt/metal3/auth/ironic/{username,password}
        # OR from credentials embedded in the endpoint URL — IRONIC_USERNAME/PASSWORD
        # env vars are NOT read by BMO.  Embed credentials in the URL so BMO's
        # ConfigFromEndpointURL() parses them as http_basic auth automatically.
        ironic_endpoint = (
            f"http://{username}:{password}"
            f"@{name}.{ns}.svc.cluster.local:80/v1/"
        )
        log.info("[mgmt/metal3] Patching BMO with Ironic endpoint (credentials embedded)")
        self._kubectl(
            "-n", bmo_ns,
            "set", "env",
            "deploy/baremetal-operator-controller-manager",
            f"IRONIC_ENDPOINT={ironic_endpoint}",
        )


        # Wait for the patched rollout to complete
        self._kubectl(
            "-n", bmo_ns,
            "rollout", "status",
            "deploy/baremetal-operator-controller-manager",
            "--timeout=5m",
        )
        log.info("[mgmt/metal3] BMO ready")

    # ------------------------------------------------------------------
    # Step 6 — CAPM3 + IPAM
    # ------------------------------------------------------------------

    def _install_capm3(self) -> None:
        if self._deployment_ready("capm3-system", "capm3-controller-manager"):
            log.info("[mgmt/metal3] CAPM3 already installed — skipping")
            return
        ver = self._cfg.capm3_version
        log.info("[mgmt/metal3] Installing CAPM3 (%s) + IPAM...", ver)
        subprocess.run(
            [
                "clusterctl", "--kubeconfig", self._kc,
                "init",
                "--infrastructure", f"metal3:{ver}",
                "--ipam", "metal3",
            ],
            check=True,
        )
        log.info("[mgmt/metal3] CAPM3 + IPAM installed")

    # ------------------------------------------------------------------
    # Ironic credential helpers
    # ------------------------------------------------------------------

    def _get_ironic_credentials(
        self, ns: str, ironic_name: str
    ) -> tuple[str, str, str]:
        """
        Returns (secret_name, username, password).

        IrSO sets spec.apiCredentialsName on the Ironic CR to point at the
        secret it created.  That secret holds plaintext 'username' and
        'password' fields (plus 'htpasswd' for the httpd Basic Auth).
        Ironic runs on plain HTTP so no CA cert is needed.
        """
        result = subprocess.run(
            [
                "kubectl", "--kubeconfig", self._kc,
                "get", f"ironic/{ironic_name}", "-n", ns,
                "-o", "jsonpath={.spec.apiCredentialsName}",
            ],
            capture_output=True, text=True, check=True,
        )
        secret_name = result.stdout.strip()
        if not secret_name:
            raise RuntimeError(
                f"Ironic CR '{ironic_name}' has no spec.apiCredentialsName — "
                "IrSO may not have reconciled yet. Re-run after a few seconds."
            )
        username = self._get_secret_field(ns, secret_name, "username")
        password = self._get_secret_field(ns, secret_name, "password")
        return secret_name, username, password

    def _get_secret_field(self, ns: str, secret: str, field: str) -> str:
        result = subprocess.run(
            [
                "kubectl", "--kubeconfig", self._kc,
                "get", f"secret/{secret}", "-n", ns,
                "-o", f"jsonpath={{.data.{field}}}",
            ],
            capture_output=True, text=True, check=True,
        )
        return base64.b64decode(result.stdout.strip()).decode()

    def _get_secret_field_raw(self, ns: str, secret: str, jsonpath_field: str) -> str:
        """Return raw base64 value (not decoded) for a secret field."""
        result = subprocess.run(
            [
                "kubectl", "--kubeconfig", self._kc,
                "get", f"secret/{secret}", "-n", ns,
                "-o", f"jsonpath={{.data.{jsonpath_field}}}",
            ],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    # ------------------------------------------------------------------
    # kubectl / subprocess helpers
    # ------------------------------------------------------------------

    def _kubectl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["kubectl", "--kubeconfig", self._kc, *args],
            check=check,
        )

    def _kubectl_stdin(self, *args: str, input: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["kubectl", "--kubeconfig", self._kc, *args],
            input=input, text=True, check=check,
        )
