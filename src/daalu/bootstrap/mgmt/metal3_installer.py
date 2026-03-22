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
import time
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
        # Nginx image server must be running before Ironic starts — the
        # ramdisk-downloader init container fetches the IPA tarball from it.
        self._deploy_image_server()
        # Create the IPA tarball at /srv/images/ from the host kernel/initrd
        # (or from a user-configured mirror).  Nginx serves it on port 80.
        self._ensure_ipa_available()
        # Deploy a MutatingAdmissionWebhook that injects IPA_BASEURI into the
        # ramdisk-downloader init container at pod-create time.  IrSO's
        # reconciliation loop continuously reverts any manual Deployment patches,
        # so the webhook is the only reliable injection point.
        self._deploy_ipa_webhook()
        self._deploy_ironic()
        # Ironic is Ready — ramdisk-downloader has already completed.
        # The webhook is no longer needed.
        self._teardown_ipa_webhook()
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
            "spec": {
                "networking": networking,
            },
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
    # Step 4b — nginx image server (serves node OS image on port 80)
    # ------------------------------------------------------------------

    def _deploy_image_server(self) -> None:
        """
        Deploy a hostNetwork nginx pod that serves /srv/images/ on port 80.

        Ironic validates and downloads the node OS image (qcow2) via HTTP before
        writing it to bare-metal disks.  The IrSO httpd runs on port 6180, so a
        separate server is needed for the image URL (ironic_http_base port 80).

        The image file is expected at /srv/images/<filename> on the mgmt node,
        placed there during image download (daalu mgmt or manual copy).
        """
        ns = self._cfg.ironic_namespace
        pod_name = "ironic-image-server"

        # Namespace may not exist yet if called before _deploy_ironic
        self._kubectl("create", "namespace", ns, check=False)

        # Idempotent: skip if already running
        r = subprocess.run(
            ["kubectl", "--kubeconfig", self._kc,
             "get", "pod", pod_name, "-n", ns],
            capture_output=True, check=False,
        )
        if r.returncode == 0:
            log.info("[mgmt/metal3] Image server pod already exists — skipping")
            return

        log.info("[mgmt/metal3] Deploying nginx image server on port 80...")
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": ns,
                         "labels": {"app": pod_name}},
            "spec": {
                "hostNetwork": True,
                "tolerations": [{"operator": "Exists"}],
                "restartPolicy": "Always",
                "containers": [{
                    "name": "nginx",
                    "image": "nginx:alpine",
                    "ports": [{"containerPort": 80, "hostPort": 80}],
                    "volumeMounts": [
                        {"name": "images", "mountPath": "/usr/share/nginx/html",
                         "readOnly": True},
                    ],
                }],
                "volumes": [{
                    "name": "images",
                    "hostPath": {"path": "/srv/images", "type": "DirectoryOrCreate"},
                }],
            },
        }
        self._kubectl_stdin("apply", "-f", "-", input=yaml.dump(pod))
        log.info("[mgmt/metal3] Image server pod deployed at http://<provisioning-ip>/")

        provisioning_ip = self._cfg.provisioning_ip or self._cfg.host
        log.warning(
            "\n"
            "============================================================\n"
            "  ACTION REQUIRED: Place the node OS image on the mgmt node\n"
            "============================================================\n"
            "  The nginx image server is ready at http://%s/\n"
            "  Ironic will download the OS image from this server during\n"
            "  bare-metal node provisioning (when BareMetalHost objects\n"
            "  are applied). You must place the image file BEFORE running\n"
            "  'daalu deploy-infra'.\n"
            "\n"
            "  Steps:\n"
            "    1. Download a CAPI-compatible node image (.qcow2), e.g.:\n"
            "       https://image-builder.sigs.k8s.io/\n"
            "    2. Copy it to the mgmt node:\n"
            "       scp <image>.qcow2 %s@%s:/srv/images/\n"
            "    3. Verify it is served:\n"
            "       curl -I http://%s/<image>.qcow2  (expect HTTP 200)\n"
            "    4. Set 'ironic_http_base' in cluster.yaml to:\n"
            "       http://%s\n"
            "    5. Set the image URL in your BareMetalHost spec to:\n"
            "       http://%s/<image>.qcow2\n"
            "============================================================",
            provisioning_ip,
            self._cfg.ssh_username,
            self._cfg.host,
            provisioning_ip,
            provisioning_ip,
            provisioning_ip,
        )

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
                f'"DEPLOY_KERNEL_URL":"http://{host}/ironic-python-agent.kernel",'
                f'"DEPLOY_RAMDISK_URL":"http://{host}/ironic-python-agent.initramfs",'
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
    # IPA download via ironic-ipa-downloader
    # ------------------------------------------------------------------

    _IPA_JOB_NAME = "daalu-ipa-downloader"
    _IPA_TARBALL = "ipa-centos9-master.tar.gz"

    def _get_ramdisk_downloader_image(self) -> str:
        """
        Read the ramdisk-downloader image from IrSO's operator deployment
        (RELATED_IMAGE_RAMDISK_DOWNLOADER env var) so we use the exact same
        version IrSO will use.  Falls back to the well-known latest tag.
        """
        r = subprocess.run(
            [
                "kubectl", "--kubeconfig", self._kc,
                "get", "deploy", "ironic-standalone-operator-controller-manager",
                "-n", "ironic-standalone-operator-system",
                "-o",
                "jsonpath={.spec.template.spec.containers[0].env"
                "[?(@.name=='RELATED_IMAGE_RAMDISK_DOWNLOADER')].value}",
            ],
            capture_output=True, text=True,
        )
        image = r.stdout.strip() if r.returncode == 0 else ""
        if image:
            log.info("[mgmt/metal3] IrSO ramdisk-downloader image: %s", image)
            return image
        fallback = "quay.io/metal3-io/ironic-ipa-downloader:latest"
        log.info(
            "[mgmt/metal3] Could not read ramdisk-downloader image from IrSO — using %s",
            fallback,
        )
        return fallback

    def _ensure_ipa_available(self) -> None:
        """
        Place the IPA tarball at /srv/images/ on the mgmt node so that the
        ramdisk-downloader init container (patched in _patch_ipa_baseuri) can
        fetch it from the local nginx rather than tarballs.opendev.org.

        Download strategy (in order):
          1. User-configured ipa_baseuri (cluster.yaml mgmt_cluster.ipa_baseuri)
          2. HTTP fallback: http://tarballs.opendev.org/... (port 80 may work
             even when port 443 is refused)
          3. Placeholder: copy host kernel + initrd into a tarball so Ironic
             can start.  Bare-metal provisioning will need real IPA files
             before any BareMetalHost is created; Ironic itself does not
             validate the IPA at startup.
        """
        ns = "kube-system"
        tarball = self._IPA_TARBALL
        filename_no_ext = tarball.replace(".tar.gz", "")

        if self._cfg.ipa_baseuri:
            # User supplied a working mirror — try it first, then fall through
            # to the placeholder if it also fails.
            download_clause = (
                f'try_download "{self._cfg.ipa_baseuri.rstrip("/")}/{tarball}" && exit 0\n'
                f'echo "Custom ipa_baseuri download failed — falling back to placeholder"\n'
            )
        else:
            # tarballs.opendev.org is known to be unreachable on some networks
            # (both port 80 and 443 refuse connections).  Skip the attempt and
            # go straight to the placeholder so the job completes quickly.
            download_clause = (
                f'echo "No ipa_baseuri configured — using placeholder IPA from host kernel"\n'
            )

        script = f"""\
#!/bin/sh
DEST=/srv/images/{tarball}
test -f "$DEST" && echo "IPA tarball already present" && exit 0

try_download() {{
  URL="$1"
  echo "Trying $URL ..."
  curl -fsSL --retry 1 --retry-delay 2 --connect-timeout 10 "$URL" -o "$DEST" && return 0
  echo "Failed: $URL"
  rm -f "$DEST"
  return 1
}}

{download_clause}
echo "Creating placeholder IPA from host kernel/initrd."
echo "Ironic will start normally. Replace with real IPA before provisioning bare-metal nodes:"
echo "  scp ipa-centos9-master.tar.gz {self._cfg.ssh_username}@{self._cfg.host}:/srv/images/"
echo ""

KERNEL=$(ls /boot/vmlinuz-* 2>/dev/null | sort -V | tail -1)
INITRD=$(ls /boot/initrd.img-* /boot/initramfs-* 2>/dev/null | sort -V | tail -1)
if [ -z "$KERNEL" ]; then echo "ERROR: no kernel found in /boot"; exit 1; fi
if [ -z "$INITRD" ];  then echo "ERROR: no initrd found in /boot";  exit 1; fi
echo "kernel: $KERNEL"
echo "initrd:  $INITRD"

TMP=$(mktemp -d)
cp "$KERNEL" "$TMP/{filename_no_ext}.kernel"
cp "$INITRD"  "$TMP/{filename_no_ext}.initramfs"
tar -czf "$DEST" -C "$TMP" {filename_no_ext}.kernel {filename_no_ext}.initramfs
rm -rf "$TMP"
echo "Placeholder IPA created at $DEST"

# Extract individual kernel/initramfs files so nginx can serve them directly.
# BMO's DEPLOY_KERNEL_URL and DEPLOY_RAMDISK_URL point to these files.
tar -xzf "$DEST" -C /srv/images/ 2>/dev/null || true
mv /srv/images/{filename_no_ext}.kernel    /srv/images/ironic-python-agent.kernel    2>/dev/null || true
mv /srv/images/{filename_no_ext}.initramfs /srv/images/ironic-python-agent.initramfs 2>/dev/null || true
echo "Extracted ironic-python-agent.kernel and ironic-python-agent.initramfs to /srv/images/"
"""

        downloader_image = self._get_ramdisk_downloader_image()

        # Delete any stale job from a previous run
        self._kubectl("delete", "job", self._IPA_JOB_NAME, "-n", ns, check=False)

        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": self._IPA_JOB_NAME, "namespace": ns},
            "spec": {
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": 300,
                "template": {
                    "spec": {
                        "hostNetwork": True,
                        "dnsPolicy": "Default",
                        "tolerations": [{"operator": "Exists"}],
                        "restartPolicy": "Never",
                        "containers": [{
                            "name": "downloader",
                            "image": downloader_image,
                            "command": ["sh", "-c", script],
                            "volumeMounts": [
                                {"name": "srv",  "mountPath": "/srv/images"},
                                {"name": "boot", "mountPath": "/boot", "readOnly": True},
                            ],
                        }],
                        "volumes": [
                            {
                                "name": "srv",
                                "hostPath": {"path": "/srv/images", "type": "DirectoryOrCreate"},
                            },
                            {
                                "name": "boot",
                                "hostPath": {"path": "/boot", "type": "Directory"},
                            },
                        ],
                    },
                },
            },
        }

        log.info("[mgmt/metal3] Ensuring IPA ramdisk is available...")
        self._kubectl_stdin("apply", "-f", "-", input=yaml.dump(job))

        r = subprocess.run(
            [
                "kubectl", "--kubeconfig", self._kc,
                "wait", "job", self._IPA_JOB_NAME, "-n", ns,
                "--for=condition=Complete",
                "--timeout=10m",
            ],
            capture_output=True,
        )
        if r.returncode != 0:
            logs = subprocess.run(
                ["kubectl", "--kubeconfig", self._kc,
                 "logs", "-n", ns, f"job/{self._IPA_JOB_NAME}"],
                capture_output=True, text=True,
            )
            raise RuntimeError(
                f"IPA job failed — no kernel/initrd found in /boot on the mgmt node?\n"
                f"Job logs:\n{logs.stdout}"
            )
        log.info("[mgmt/metal3] IPA tarball ready at /srv/images/%s", tarball)

    # ------------------------------------------------------------------
    # IPA MutatingAdmissionWebhook
    # ------------------------------------------------------------------

    _WEBHOOK_NAME = "daalu-ipa-webhook"
    _WEBHOOK_NS   = "kube-system"

    # Minimal Python stdlib webhook server stored in a ConfigMap and run
    # inside a python:3-alpine container.  IPA_BASEURI is injected as an
    # env var so the script works without modification for any mirror URL.
    _WEBHOOK_SCRIPT = """\
#!/usr/bin/env python3
import base64, json, os, ssl, http.server

IPA_BASEURI = os.environ["IPA_BASEURI"]

class _H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        review = json.loads(self.rfile.read(n))
        uid = review["request"]["uid"]
        pod = review["request"].get("object", {})
        patch = []
        for i, c in enumerate(pod.get("spec", {}).get("initContainers", [])):
            if c.get("name") == "ramdisk-downloader":
                if not any(e.get("name") == "IPA_BASEURI" for e in c.get("env", [])):
                    if c.get("env"):
                        patch.append({
                            "op": "add",
                            "path": "/spec/initContainers/%d/env/-" % i,
                            "value": {"name": "IPA_BASEURI", "value": IPA_BASEURI},
                        })
                    else:
                        patch.append({
                            "op": "add",
                            "path": "/spec/initContainers/%d/env" % i,
                            "value": [{"name": "IPA_BASEURI", "value": IPA_BASEURI}],
                        })
        resp = {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"uid": uid, "allowed": True},
        }
        if patch:
            resp["response"]["patchType"] = "JSONPatch"
            resp["response"]["patch"] = base64.b64encode(
                json.dumps(patch).encode()
            ).decode()
        body = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)

ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain("/tls/tls.crt", "/tls/tls.key")
server = http.server.HTTPServer(("0.0.0.0", 8443), _H)
server.socket = ctx.wrap_socket(server.socket, server_side=True)
print("IPA webhook :8443", flush=True)
server.serve_forever()
"""

    def _generate_webhook_certs(self) -> tuple[bytes, bytes, bytes]:
        """
        Generate a self-signed CA and a server TLS cert for the webhook service.
        Returns (ca_cert_pem, server_cert_pem, server_key_pem).
        """
        svc = f"{self._WEBHOOK_NAME}.{self._WEBHOOK_NS}.svc"
        with tempfile.TemporaryDirectory() as d:
            # CA key + self-signed cert
            subprocess.run(
                ["openssl", "genrsa", "-out", f"{d}/ca.key", "2048"],
                check=True, capture_output=True,
            )
            subprocess.run(
                [
                    "openssl", "req", "-x509", "-new", "-nodes",
                    "-key", f"{d}/ca.key", "-sha256", "-days", "1",
                    "-subj", "/CN=daalu-ipa-webhook-ca",
                    "-out", f"{d}/ca.crt",
                ],
                check=True, capture_output=True,
            )
            # Server key
            subprocess.run(
                ["openssl", "genrsa", "-out", f"{d}/srv.key", "2048"],
                check=True, capture_output=True,
            )
            # SAN config for CSR + signing
            ext_cfg = (
                "[req]\nreq_extensions = v3_req\ndistinguished_name = dn\n"
                "[dn]\n"
                "[v3_req]\n"
                "basicConstraints = CA:FALSE\n"
                "keyUsage = digitalSignature, keyEncipherment\n"
                "extendedKeyUsage = serverAuth\n"
                f"subjectAltName = DNS:{svc},DNS:{svc}.cluster.local\n"
            )
            Path(f"{d}/ext.cnf").write_text(ext_cfg)
            subprocess.run(
                [
                    "openssl", "req", "-new",
                    "-key", f"{d}/srv.key",
                    "-subj", f"/CN={svc}",
                    "-config", f"{d}/ext.cnf",
                    "-out", f"{d}/srv.csr",
                ],
                check=True, capture_output=True,
            )
            subprocess.run(
                [
                    "openssl", "x509", "-req",
                    "-in", f"{d}/srv.csr",
                    "-CA", f"{d}/ca.crt",
                    "-CAkey", f"{d}/ca.key",
                    "-CAcreateserial",
                    "-out", f"{d}/srv.crt",
                    "-days", "1", "-sha256",
                    "-extensions", "v3_req",
                    "-extfile", f"{d}/ext.cnf",
                ],
                check=True, capture_output=True,
            )
            return (
                Path(f"{d}/ca.crt").read_bytes(),
                Path(f"{d}/srv.crt").read_bytes(),
                Path(f"{d}/srv.key").read_bytes(),
            )

    def _deploy_ipa_webhook(self) -> None:
        """
        Deploy a MutatingAdmissionWebhook that injects IPA_BASEURI into the
        ramdisk-downloader init container so it fetches the IPA tarball from
        the local nginx image server instead of tarballs.opendev.org.

        Resources deployed in kube-system:
          - Secret  daalu-ipa-webhook-tls  (TLS cert + key)
          - ConfigMap  daalu-ipa-webhook   (Python webhook server script)
          - Deployment daalu-ipa-webhook   (python:3-alpine running the script)
          - Service    daalu-ipa-webhook   (port 443 → 8443)
          - MutatingWebhookConfiguration  daalu-ipa-webhook
        """
        ns    = self._WEBHOOK_NS
        name  = self._WEBHOOK_NAME
        ironic_ns = self._cfg.ironic_namespace

        ipa_baseuri = (
            self._cfg.ipa_baseuri.rstrip("/")
            if self._cfg.ipa_baseuri
            else f"http://{self._cfg.provisioning_ip or self._cfg.host}"
        )

        # Idempotent: skip if webhook deployment already exists
        r = subprocess.run(
            ["kubectl", "--kubeconfig", self._kc, "get", "deploy", name, "-n", ns],
            capture_output=True,
        )
        if r.returncode == 0:
            log.info("[mgmt/metal3] IPA webhook already deployed — skipping")
            return

        log.info("[mgmt/metal3] Generating webhook TLS certificates...")
        ca_cert, srv_cert, srv_key = self._generate_webhook_certs()
        ca_b64  = base64.b64encode(ca_cert).decode()
        crt_b64 = base64.b64encode(srv_cert).decode()
        key_b64 = base64.b64encode(srv_key).decode()

        secret = {
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": name + "-tls", "namespace": ns},
            "type": "kubernetes.io/tls",
            "data": {"tls.crt": crt_b64, "tls.key": key_b64},
        }
        cm = {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": ns},
            "data": {"webhook.py": self._WEBHOOK_SCRIPT},
        }
        deploy = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": ns},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {
                        "containers": [{
                            "name": "webhook",
                            "image": "python:3-alpine",
                            "command": ["python3", "/webhook/webhook.py"],
                            "env": [{"name": "IPA_BASEURI", "value": ipa_baseuri}],
                            "ports": [{"containerPort": 8443}],
                            "volumeMounts": [
                                {"name": "tls",    "mountPath": "/tls",     "readOnly": True},
                                {"name": "script", "mountPath": "/webhook", "readOnly": True},
                            ],
                        }],
                        "volumes": [
                            {"name": "tls",    "secret":    {"secretName": name + "-tls"}},
                            {"name": "script", "configMap": {"name": name}},
                        ],
                    },
                },
            },
        }
        svc = {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": name, "namespace": ns},
            "spec": {
                "selector": {"app": name},
                "ports": [{"port": 443, "targetPort": 8443}],
            },
        }
        mwc = {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "MutatingWebhookConfiguration",
            "metadata": {"name": name},
            "webhooks": [{
                "name": "ipa-baseuri.daalu.io",
                "admissionReviewVersions": ["v1"],
                "sideEffects": "None",
                # Ignore failures — if the webhook pod is unhealthy, pods
                # still get created (without the env var injection).
                "failurePolicy": "Ignore",
                "namespaceSelector": {
                    "matchLabels": {
                        # kubernetes.io/metadata.name is auto-applied to every
                        # namespace since K8s 1.21 — ensures we only intercept
                        # pods in the Ironic namespace.
                        "kubernetes.io/metadata.name": ironic_ns,
                    },
                },
                "rules": [{
                    "operations": ["CREATE"],
                    "apiGroups": [""],
                    "apiVersions": ["v1"],
                    "resources": ["pods"],
                }],
                "clientConfig": {
                    "service": {
                        "name": name, "namespace": ns,
                        "path": "/mutate", "port": 443,
                    },
                    "caBundle": ca_b64,
                },
            }],
        }

        log.info(
            "[mgmt/metal3] Deploying IPA webhook (IPA_BASEURI=%s)...", ipa_baseuri
        )
        for obj in [secret, cm, deploy, svc, mwc]:
            self._kubectl_stdin("apply", "-f", "-", input=yaml.dump(obj))

        log.info("[mgmt/metal3] Waiting for IPA webhook pod to be ready (timeout 3m)...")
        self._kubectl(
            "-n", ns, "rollout", "status", f"deploy/{name}", "--timeout=3m",
        )
        log.info("[mgmt/metal3] IPA webhook ready")

    def _teardown_ipa_webhook(self) -> None:
        """Remove the IPA webhook and all associated resources (idempotent)."""
        ns   = self._WEBHOOK_NS
        name = self._WEBHOOK_NAME
        log.info("[mgmt/metal3] Removing IPA webhook resources...")
        self._kubectl("delete", "mutatingwebhookconfiguration", name, check=False)
        self._kubectl("delete", "svc",       name,            "-n", ns, check=False)
        self._kubectl("delete", "deploy",    name,            "-n", ns, check=False)
        self._kubectl("delete", "configmap", name,            "-n", ns, check=False)
        self._kubectl("delete", "secret",    name + "-tls",   "-n", ns, check=False)
        log.info("[mgmt/metal3] IPA webhook removed")

    # ------------------------------------------------------------------
    # IPA local-mirror helpers
    # ------------------------------------------------------------------

    def _wait_for_deploy(self, ns: str, deploy: str, timeout: int = 60) -> bool:
        """Poll until a Deployment exists in the cluster or timeout is reached."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = subprocess.run(
                ["kubectl", "--kubeconfig", self._kc,
                 "get", "deploy", deploy, "-n", ns],
                capture_output=True,
            )
            if r.returncode == 0:
                return True
            time.sleep(3)
        return False

    def _patch_ipa_baseuri(self, ns: str, deploy_name: str, base_uri: str) -> None:
        """
        Set IPA_BASEURI on the ramdisk-downloader init container so it fetches
        the IPA tarball from the local nginx image server rather than from
        tarballs.opendev.org.  Called immediately after the Ironic CR is applied
        so the patch lands before the pod is scheduled.
        """
        log.info(
            "[mgmt/metal3] Waiting for IrSO to create deployment %s/%s...",
            ns, deploy_name,
        )
        if not self._wait_for_deploy(ns, deploy_name):
            log.warning(
                "[mgmt/metal3] Deployment %s not found after 60s — "
                "skipping IPA_BASEURI patch; ramdisk-downloader may fail",
                deploy_name,
            )
            return

        log.info(
            "[mgmt/metal3] Patching ramdisk-downloader IPA_BASEURI → %s", base_uri
        )
        patch = json.dumps({
            "spec": {
                "template": {
                    "spec": {
                        "initContainers": [{
                            "name": "ramdisk-downloader",
                            "env": [{"name": "IPA_BASEURI", "value": base_uri}],
                        }],
                    },
                },
            },
        })
        self._kubectl(
            "patch", "deploy", deploy_name, "-n", ns,
            "--type=strategic", "-p", patch,
        )
        log.info("[mgmt/metal3] IPA_BASEURI patch applied to %s", deploy_name)

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
