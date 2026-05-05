# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/mgmt/tinkerbell_installer.py

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from daalu.bootstrap.mgmt.models import MgmtClusterConfig

log = logging.getLogger("daalu")


class TinkerbellInstaller:
    """
    Installs the Tinkerbell bare-metal provisioning stack on an existing
    management Kubernetes cluster.

    Installation sequence:
      1. cert-manager              — TLS certificate management (Helm, local chart)
      2. CAPI core                 — Cluster API + kubeadm bootstrap/control-plane providers
      3. Tinkerbell stack (Helm)   — Tink Server, Hegel (metadata), SMEE (DHCP/iPXE), Rufio CRDs
                                     MUST precede CAPT: CAPT controller watches Workflow/Job CRDs
                                     on startup; without them it crashes into CrashLoopBackOff
      4. SMEE DHCP config          — Patch SMEE with provisioning IP and DHCP range
      5. CAPT                      — Cluster API Provider Tinkerbell (bundles Rufio for BMC)
      6. Image server              — nginx pod serving OS images over the provisioning IP
      7. Hegel externalIPs         — Expose Hegel on provisioning IP so bare-metal HookOS nodes
                                     can reach it (ClusterIPs are not reachable from bare metal)
      8. Hardware registration     — Apply Hardware CRs for each bare-metal node
      9. OS provisioning template  — Apply Tinkerbell Template CR (image2disk + cexec + reboot)
     10. Workflow CRs              — Apply per-node Workflow CRs
     11. Node power-on             — Rufio Job CRs: set PXE boot once, then power on via BMC
     12. Post-provision reboot     — Wait for STATE_SUCCESS, disable PXE, Rufio power-cycle into OS

    All operations run locally via subprocess using a kubeconfig that
    points at the remote mgmt cluster.
    """

    def __init__(self, kubeconfig_path: str, cfg: MgmtClusterConfig, workspace_root: Path):
        self._kc = kubeconfig_path
        self._cfg = cfg
        self._workspace_root = workspace_root

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def install(self) -> None:
        """Run the full Tinkerbell stack installation.

        Steps 1-7 set up the Tinkerbell infrastructure and register Hardware CRs
        with allowPXE=true.  Standalone OS provisioning (Template/Workflow CRs,
        Rufio power-on, post-provision reboot) is intentionally omitted because
        CAPT handles all of that automatically via its templateOverride when
        ``daalu deploy --install cluster-api`` applies the CAPI manifests.

        Sequence:
          1. cert-manager
          2. CAPI core
          3. Tinkerbell stack (Tink, Hegel, SMEE) — must precede CAPT so that
             Workflow/Job CRDs exist before the CAPT controller starts
          4. SMEE DHCP config
          5. CAPT (bundles Rufio) — installed after Tinkerbell CRDs are present
          6. Image server (nginx serving OS images over provisioning IP)
          7. Hegel externalIPs patch — makes Hegel reachable from bare-metal nodes
          8. Hardware CRs + Rufio Machine CRs + BMC Secrets
        """
        self._install_cert_manager()
        self._install_capi()
        self._install_tinkerbell_stack()
        self._configure_smee()
        self._install_capt()
        self._deploy_image_server()
        self._patch_hegel_service()
        self._register_hardware()
        log.info(
            "[mgmt/tinkerbell] Tinkerbell stack installed and hardware registered. "
            "Run 'daalu deploy --install cluster-api' to provision workload nodes via CAPT."
        )

    # ------------------------------------------------------------------
    # Step 1 — cert-manager (Helm, local chart)
    # ------------------------------------------------------------------

    def _install_cert_manager(self) -> None:
        """
        Install cert-manager from the local Helm chart at
        assets/cert-manager/charts/.

        Skipped if the cert-manager deployment is already ready (idempotent).
        CRDs are installed via the chart's installCRDs value.
        """
        if self._deployment_ready("cert-manager", "cert-manager"):
            log.info("[mgmt/tinkerbell] cert-manager already ready — skipping")
            return

        chart = self._local_chart("cert-manager")
        log.info("[mgmt/tinkerbell] Installing cert-manager from %s...", chart)

        self._helm(
            "upgrade", "--install", "cert-manager", chart,
            "--namespace", "cert-manager",
            "--create-namespace",
            "--set", "installCRDs=true",
        )

        for deploy in ["cert-manager", "cert-manager-webhook", "cert-manager-cainjector"]:
            self._kubectl(
                "-n", "cert-manager",
                "rollout", "status", f"deploy/{deploy}",
                "--timeout=5m",
            )

        # rollout status only confirms the pod is Running; the webhook TLS
        # endpoint takes a few extra seconds to register.  Poll until a
        # dry-run Issuer create succeeds, which proves the webhook is
        # accepting cert-manager API requests.  clusterctl init does the
        # same check internally but loops forever — doing it here lets us
        # block cleanly before handing off to clusterctl.
        log.info("[mgmt/tinkerbell] Waiting for cert-manager webhook to be ready...")
        import time as _time
        _probe_manifest = (
            "apiVersion: cert-manager.io/v1\n"
            "kind: Issuer\n"
            "metadata:\n"
            "  name: webhook-readiness-probe\n"
            "  namespace: cert-manager\n"
            "spec:\n"
            "  selfSigned: {}\n"
        )
        deadline = _time.time() + 120
        while _time.time() < deadline:
            r = subprocess.run(
                ["kubectl", "--kubeconfig", self._kc,
                 "create", "--dry-run=server", "-f", "-"],
                input=_probe_manifest,
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                break
            log.debug("[mgmt/tinkerbell] cert-manager webhook not ready yet — retrying...")
            _time.sleep(5)
        else:
            log.warning("[mgmt/tinkerbell] cert-manager webhook did not become ready in 120s — proceeding anyway")

        log.info("[mgmt/tinkerbell] cert-manager ready")

    # ------------------------------------------------------------------
    # Step 2 — Cluster API core providers
    # ------------------------------------------------------------------

    def _install_capi(self) -> None:
        """
        Initialise Cluster API core components (kubeadm bootstrap +
        control-plane providers) via clusterctl.

        Skipped if the CAPI controller manager is already running.
        """
        if self._deployment_ready("capi-system", "capi-controller-manager"):
            log.info("[mgmt/tinkerbell] CAPI already installed — skipping")
            return

        ver = self._cfg.capi_version
        log.info("[mgmt/tinkerbell] Initialising Cluster API core (%s)...", ver)

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

        log.info("[mgmt/tinkerbell] CAPI core installed")

    # ------------------------------------------------------------------
    # Step 3 — Cluster API Provider Tinkerbell (CAPT)
    # ------------------------------------------------------------------

    def _install_capt(self) -> None:
        """
        Install the Cluster API Provider Tinkerbell (CAPT) via clusterctl.

        CAPT is a community provider — not built-in to clusterctl — so its
        release URL must be registered in ~/.config/cluster-api/clusterctl.yaml
        before clusterctl init is called.  This method writes that entry
        automatically if it is not already present.

        CAPT bundles Rufio (BMC controller) so no separate install is needed.
        Skipped if the CAPT controller manager is already running.
        """
        if self._deployment_ready("capt-system", "capt-controller-manager"):
            log.info("[mgmt/tinkerbell] CAPT already installed — skipping")
            return

        ver = self._cfg.capt_version
        self._register_capt_in_clusterctl_config(ver)

        # CAPT's infrastructure-components.yaml substitutes TINKERBELL_IP at
        # install time.  Pass it via env so the clusterctl process picks it up
        # without permanently polluting the clusterctl config file.
        import os
        ip = self._cfg.provisioning_ip or self._cfg.host
        env = {**os.environ, "TINKERBELL_IP": ip}

        log.info("[mgmt/tinkerbell] Installing CAPT %s (TINKERBELL_IP=%s)...", ver, ip)
        subprocess.run(
            [
                "clusterctl", "--kubeconfig", self._kc,
                "init",
                "--infrastructure", f"tinkerbell:{ver}",
                "-v5",
            ],
            env=env,
            check=True,
        )

        log.info("[mgmt/tinkerbell] CAPT installed")

    # ------------------------------------------------------------------
    # Steps 4–7 — stubs (implemented in later steps)
    # ------------------------------------------------------------------

    def _install_tinkerbell_stack(self) -> None:
        """
        Deploy the Tinkerbell stack (Tink, Hegel, SMEE, Rufio) via the local
        Helm chart at assets/tinkerbell/charts/stack-<version>.tgz.

        Chart  : assets/tinkerbell/charts/stack-0.6.3.tgz  (built from tinkerbell/charts repo)
        NS     : tinkerbell

        Key values (global.*):
          global.publicIP       — provisioning IP reachable by bare-metal nodes
          global.trustedProxies — pod CIDR list for the nginx reverse proxy
        """
        ns = "tinkerbell"
        r = subprocess.run(
            ["helm", "--kubeconfig", self._kc, "status", "tinkerbell", "-n", ns],
            capture_output=True,
        )
        if r.returncode == 0:
            log.info("[mgmt/tinkerbell] Tinkerbell stack already installed — skipping")
            return

        ip = self._cfg.provisioning_ip or self._cfg.host
        chart = self._local_chart("tinkerbell")

        log.info(
            "[mgmt/tinkerbell] Installing Tinkerbell stack from %s (publicIP=%s)...",
            chart, ip,
        )

        self._helm(
            "upgrade", "--install", "tinkerbell", chart,
            "--namespace", ns,
            "--create-namespace",
            "--set", f"global.publicIP={ip}",
            "--set", f"global.trustedProxies={{{self._cfg.pod_cidr}}}",
            # Use ClusterRole so tink-controller and tink-server watch ALL namespaces.
            # CAPT creates Workflow/Template CRs in the cluster namespace (e.g. "default"),
            # not in "tinkerbell".  Without ClusterRole, tink-controller ignores those
            # CRs and the tink-worker in HookOS never receives workflow actions.
            "--set", "global.rbac.type=ClusterRole",
            "--wait",
            "--timeout", "10m",
        )

        log.info("[mgmt/tinkerbell] Tinkerbell stack ready")

    def _configure_smee(self) -> None:
        """
        Patch the SMEE deployment with DHCP range and gateway environment
        variables so it serves iPXE and DHCP leases to bare-metal nodes.
        """
        cfg = self._cfg
        if not all([cfg.dhcp_range_begin, cfg.dhcp_range_end]):
            log.info(
                "[mgmt/tinkerbell] dhcp_range_begin/end not set — skipping SMEE DHCP patch"
            )
            return

        ip = cfg.provisioning_ip or cfg.host
        log.info("[mgmt/tinkerbell] Patching SMEE DHCP config...")

        env_patch: dict = {
            "env": [
                {"name": "SMEE_DHCP_IP_FOR_PACKET", "value": ip},
                {"name": "SMEE_DHCP_RANGE_START",   "value": cfg.dhcp_range_begin},
                {"name": "SMEE_DHCP_RANGE_END",     "value": cfg.dhcp_range_end},
                {"name": "SMEE_DHCP_GATEWAY",       "value": cfg.dhcp_gateway or ip},
                {"name": "SMEE_DHCP_DNS",            "value": cfg.dhcp_dns},
            ]
        }
        import json
        patch = json.dumps({
            "spec": {
                "template": {
                    "spec": {
                        # hostNetwork is required so SMEE binds directly to the
                        # host's provisioning interface (ens19) and can receive
                        # DHCP broadcasts from bare-metal nodes.  Without this,
                        # SMEE runs in the pod network and never sees L2 broadcasts.
                        "hostNetwork": True,
                        "dnsPolicy": "ClusterFirstWithHostNet",
                        "containers": [
                            {"name": "smee", **env_patch}
                        ]
                    }
                }
            }
        })

        self._kubectl(
            "-n", "tinkerbell",
            "patch", "deployment/smee",
            "--type=strategic",
            f"--patch={patch}",
        )

        self._kubectl(
            "-n", "tinkerbell",
            "rollout", "status", "deployment/smee",
            "--timeout=3m",
        )

        log.info("[mgmt/tinkerbell] SMEE DHCP config applied")

    def _patch_hegel_service(self) -> None:
        """
        Patch the Hegel Service to add the provisioning IP as an ExternalIP.

        Hegel's Service is ClusterIP-only by default. Bare-metal nodes running
        HookOS are outside the Kubernetes cluster and cannot reach ClusterIPs.
        The configure-node workflow action fetches kubeadm user-data from Hegel
        via HTTP — without this patch the wget hangs until timeout and the
        workflow fails.

        This patch must be reapplied after every Tinkerbell stack reinstall
        because Helm recreates the Service without the ExternalIP.
        """
        ip = self._cfg.provisioning_ip or self._cfg.host
        log.info("[mgmt/tinkerbell] Patching Hegel service with externalIPs=[%s]...", ip)

        import json as _json
        patch = _json.dumps({"spec": {"externalIPs": [ip]}})

        r = subprocess.run(
            ["kubectl", "--kubeconfig", self._kc,
             "-n", "tinkerbell",
             "patch", "svc", "hegel",
             "--type=strategic",
             f"--patch={patch}"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            log.warning(
                "[mgmt/tinkerbell] Failed to patch Hegel service: %s", r.stderr.strip()
            )
        else:
            log.info("[mgmt/tinkerbell] Hegel reachable at http://%s:50061", ip)

    def _register_hardware(self) -> None:
        """
        Apply a Tinkerbell Hardware CR for each bare-metal node in cfg.hardware.

        Hardware CRs are placed in cfg.cluster_namespace (default: "default") so that
        CAPT-created Workflow/Template CRs in the same namespace can reference them.
        Rufio Machine (BMC) CRs and their Secrets are placed in the same namespace.

        tink-controller/tink-server must have ClusterRole RBAC to watch these CRs
        (set via ``global.rbac.type=ClusterRole`` in the Tinkerbell Helm chart).

        The apply is idempotent — existing CRs are updated in-place.
        """
        if not self._cfg.hardware:
            log.info("[mgmt/tinkerbell] No hardware entries configured — skipping registration")
            return

        import yaml as _yaml

        hw_ns = self._cfg.cluster_namespace

        for hw in self._cfg.hardware:
            log.info("[mgmt/tinkerbell] Registering hardware: %s (%s) in ns=%s", hw.name, hw.mac, hw_ns)

            cr = {
                "apiVersion": "tinkerbell.org/v1alpha1",
                "kind": "Hardware",
                "metadata": {
                    "name": hw.name,
                    "namespace": hw_ns,
                },
                "spec": {
                    "bmcRef": {
                        "apiGroup": "bmc.tinkerbell.org",
                        "kind": "Machine",
                        "name": hw.name,
                    },
                    "disks": [{"device": hw.disk}],
                    "metadata": {
                        "instance": {
                            "id": hw.mac,
                            "hostname": hw.name,
                        }
                    },
                    "interfaces": [
                        {
                            "dhcp": {
                                "arch": "x86_64",
                                "hostname": hw.name,
                                "ip": {
                                    "address": hw.ip,
                                    "family": 4,
                                    "gateway": self._cfg.dhcp_gateway or "",
                                    "netmask": self._prefix_to_netmask(
                                        self._cfg.provisioning_prefix
                                    ),
                                },
                                "mac": hw.mac,
                                "uefi": hw.uefi if hasattr(hw, "uefi") else True,
                            },
                            "netboot": {"allowPXE": True, "allowWorkflow": True},
                        }
                    ],
                },
            }

            # Rufio Machine CR for BMC access
            bmc_cr = {
                "apiVersion": "bmc.tinkerbell.org/v1alpha1",
                "kind": "Machine",
                "metadata": {
                    "name": hw.name,
                    "namespace": hw_ns,
                },
                "spec": {
                    "connection": {
                        "host": hw.bmc_endpoint,
                        "authSecretRef": {
                            "name": f"{hw.name}-bmc-secret",
                            "namespace": hw_ns,
                        },
                        "insecureTLS": True,
                    }
                },
            }

            bmc_secret = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": f"{hw.name}-bmc-secret",
                    "namespace": hw_ns,
                },
                "stringData": {
                    "username": hw.bmc_username,
                    "password": hw.bmc_password,
                },
            }

            manifest = _yaml.dump_all([bmc_secret, bmc_cr, cr])
            subprocess.run(
                ["kubectl", "--kubeconfig", self._kc, "apply", "-f", "-"],
                input=manifest,
                text=True,
                check=True,
            )

        log.info("[mgmt/tinkerbell] Hardware registration complete")

    @staticmethod
    def _prefix_to_netmask(prefix: str) -> str:
        """Convert a CIDR prefix length string (e.g. '24') to a dotted netmask."""
        import ipaddress
        return str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)

    def _deploy_image_server(self) -> None:
        """
        Deploy an nginx pod on the mgmt node that serves OS images over the
        provisioning IP so bare-metal nodes can fetch them during PXE boot.

        Images are expected at /var/www/images on the mgmt node host.
        The Service binds to the provisioning IP on port 80, making images
        reachable at http://<provisioning_ip>/<image_name>.

        Skipped if the image-server Deployment already exists.
        """
        if self._deployment_ready("tinkerbell", "image-server"):
            log.info("[mgmt/tinkerbell] image-server already deployed — skipping")
            return

        ip = self._cfg.provisioning_ip or self._cfg.host
        log.info("[mgmt/tinkerbell] Deploying image server on %s:80...", ip)

        import json as _json

        manifest = [
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": "tinkerbell"},
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {
                    "name": "image-server",
                    "namespace": "tinkerbell",
                },
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": "image-server"}},
                    "template": {
                        "metadata": {"labels": {"app": "image-server"}},
                        "spec": {
                            # hostNetwork so nginx binds directly to 10.10.0.9:80
                            # on the provisioning interface — externalIPs alone is
                            # not reliably reachable from bare-metal nodes outside
                            # the cluster network.
                            "hostNetwork": True,
                            "dnsPolicy": "ClusterFirstWithHostNet",
                            "containers": [
                                {
                                    "name": "nginx",
                                    "image": "nginx:stable-alpine",
                                    "ports": [{"containerPort": 80}],
                                    "volumeMounts": [
                                        {
                                            "name": "images",
                                            "mountPath": "/usr/share/nginx/html",
                                        }
                                    ],
                                }
                            ],
                            "volumes": [
                                {
                                    "name": "images",
                                    "hostPath": {
                                        "path": "/var/www/images",
                                        "type": "DirectoryOrCreate",
                                    },
                                }
                            ],
                            "nodeSelector": {"node-role.kubernetes.io/control-plane": ""},
                            "tolerations": [
                                {
                                    "key": "node-role.kubernetes.io/control-plane",
                                    "operator": "Exists",
                                    "effect": "NoSchedule",
                                }
                            ],
                        },
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": "image-server",
                    "namespace": "tinkerbell",
                },
                "spec": {
                    "type": "NodePort",
                    "selector": {"app": "image-server"},
                    "ports": [
                        {
                            "port": 80,
                            "targetPort": 80,
                            "nodePort": 30080,
                            "protocol": "TCP",
                        }
                    ],
                    "externalIPs": [ip],
                },
            },
        ]

        import yaml as _yaml
        manifest_yaml = _yaml.dump_all(manifest)
        subprocess.run(
            ["kubectl", "--kubeconfig", self._kc, "apply", "-f", "-"],
            input=manifest_yaml,
            text=True,
            check=True,
        )

        self._kubectl(
            "-n", "tinkerbell",
            "rollout", "status", "deploy/image-server",
            "--timeout=3m",
        )
        log.info(
            "[mgmt/tinkerbell] Image server ready — place images in "
            "/var/www/images on the mgmt node and access via http://%s/<image>",
            ip,
        )

    def _create_os_template(self) -> None:
        """
        Apply the Tinkerbell Template CR from
        assets/tinkerbell/templates/ubuntu-kubeadm.yaml.

        The YAML is applied as-is — image_url and other per-node values are
        substituted at Workflow creation time (not in the Template itself),
        so no rendering is needed here.
        """
        template_path = (
            self._workspace_root
            / "assets"
            / "tinkerbell"
            / "templates"
            / "ubuntu-kubeadm.yaml"
        )

        if not template_path.is_file():
            raise FileNotFoundError(
                f"Tinkerbell OS template not found: {template_path}"
            )

        log.info("[mgmt/tinkerbell] Applying OS provisioning template...")
        self._kubectl("apply", "-f", str(template_path))
        log.info("[mgmt/tinkerbell] OS template applied")

    def _create_workflows(self) -> None:
        """
        Apply all Workflow CRs found under assets/tinkerbell/workflows/.

        Each YAML file is applied idempotently (kubectl apply).  Files are
        applied in sorted order so cp01 always precedes cp02.
        """
        workflows_dir = self._workspace_root / "assets" / "tinkerbell" / "workflows"
        if not workflows_dir.is_dir():
            log.info("[mgmt/tinkerbell] No workflows directory found — skipping")
            return

        yamls = sorted(workflows_dir.glob("*.yaml"))
        if not yamls:
            log.info("[mgmt/tinkerbell] No workflow files found — skipping")
            return

        for wf_path in yamls:
            log.info("[mgmt/tinkerbell] Applying workflow: %s", wf_path.name)
            # Delete first so stale STATE_PENDING/FAILED workflows from aborted
            # runs don't survive — apply would leave them unchanged and the
            # node would never PXE boot.
            self._kubectl(
                "delete", "-f", str(wf_path),
                "--ignore-not-found",
            )
            self._kubectl("apply", "-f", str(wf_path))

        log.info("[mgmt/tinkerbell] %d workflow(s) applied", len(yamls))

    # ------------------------------------------------------------------
    # Step 10 — Power on bare-metal nodes via Rufio BMC Jobs
    # ------------------------------------------------------------------

    def _power_on_nodes(self) -> None:
        """
        For each hardware node create a Rufio Job CR that:
          1. Sets the one-time boot device to PXE (ephemeral — reverts to disk
             after the first boot so the OS boots normally thereafter).
          2. Powers the machine on via its BMC.

        Rufio (bundled with CAPT) translates these tasks into Redfish/IPMI
        calls using the credentials stored in each {name}-bmc-secret.

        Idempotent: if a Job with the same name already exists it is left in
        place (the node has already been told to power on).

        Waits up to 5 minutes per node for the Job to reach a terminal state
        (Completed or Failed) so callers know the BMC command was accepted
        before returning.
        """
        if not self._cfg.hardware:
            return

        import yaml as _yaml

        hw_ns = self._cfg.cluster_namespace

        for hw in self._cfg.hardware:
            job_name = f"{hw.name}-pxe-poweron"

            # Delete any existing Job so we always send a fresh power-on —
            # a stale completed Job means the node may not have actually booted.
            subprocess.run(
                ["kubectl", "--kubeconfig", self._kc,
                 "delete", "job.bmc.tinkerbell.org", job_name,
                 "-n", hw_ns, "--ignore-not-found"],
                capture_output=True,
            )

            log.info(
                "[mgmt/tinkerbell] Creating Rufio Job %s — PXE boot %s via %s",
                job_name, hw.name, hw.bmc_endpoint,
            )

            job = {
                "apiVersion": "bmc.tinkerbell.org/v1alpha1",
                "kind": "Job",
                "metadata": {
                    "name": job_name,
                    "namespace": hw_ns,
                },
                "spec": {
                    "machineRef": {
                        "name": hw.name,
                        "namespace": hw_ns,
                    },
                    "tasks": [
                        {"powerAction": "off"},
                        {
                            "oneTimeBootDeviceAction": {
                                "device": ["pxe"],
                                "efiBoot": hw.uefi,
                            }
                        },
                        {"powerAction": "on"},
                    ],
                },
            }

            subprocess.run(
                ["kubectl", "--kubeconfig", self._kc, "apply", "-f", "-"],
                input=_yaml.dump(job),
                text=True,
                check=True,
            )

        # Wait for each Job to reach a terminal state
        for hw in self._cfg.hardware:
            self._wait_for_rufio_job(f"{hw.name}-pxe-poweron", hw.name)

        log.info("[mgmt/tinkerbell] Node power-on sequence complete")

    # ------------------------------------------------------------------
    # Step 11 — Reboot provisioned nodes into the installed OS
    # ------------------------------------------------------------------

    def _reboot_provisioned_nodes(self) -> None:
        """
        After provisioning workflows complete, boot nodes into the installed OS.

        The HookOS workflow template includes a kernel sysrq reboot action, but
        because the Hardware CR still has ``netboot.allowPXE: true``, SMEE
        responds to the node's next DHCP/PXE request and loads HookOS again —
        the node loops back into the provisioner instead of booting Ubuntu.

        This method fixes that by:
          1. Waiting for each Workflow CR to reach STATE_SUCCESS (up to 45 min
             to allow for large image downloads and disk writes).
          2. Patching the corresponding Hardware CR to set allowPXE=false so
             SMEE stops serving iPXE to that node.
          3. Creating a Rufio Job with ``powerAction: cycle`` to hard-reboot
             the node via BMC — since the one-time PXE override from power-on
             was already consumed, the node boots from disk (Ubuntu).

        Idempotent: existing reboot Jobs are left in place.
        """
        if not self._cfg.hardware:
            return

        import time as _time
        import json as _json
        import yaml as _yaml

        provisioning_timeout = 45 * 60  # 45 minutes — image download + dd can be slow

        for hw in self._cfg.hardware:
            workflow_name = f"{hw.name}-provision"
            log.info(
                "[mgmt/tinkerbell] Waiting for workflow %s to reach STATE_SUCCESS "
                "(timeout %ds)...",
                workflow_name, provisioning_timeout,
            )

            deadline = _time.time() + provisioning_timeout
            succeeded = False

            while _time.time() < deadline:
                r = subprocess.run(
                    ["kubectl", "--kubeconfig", self._kc,
                     "get", "workflow", workflow_name,
                     "-n", "tinkerbell",
                     "-o", "jsonpath={.status.state}"],
                    capture_output=True, text=True,
                )
                if r.returncode == 0:
                    state = r.stdout.strip()
                    if state == "STATE_SUCCESS":
                        log.info(
                            "[mgmt/tinkerbell] Workflow %s completed successfully",
                            workflow_name,
                        )
                        succeeded = True
                        break
                    if state == "STATE_FAILED":
                        log.warning(
                            "[mgmt/tinkerbell] Workflow %s reached STATE_FAILED — "
                            "skipping reboot for %s",
                            workflow_name, hw.name,
                        )
                        break
                    log.debug(
                        "[mgmt/tinkerbell] Workflow %s state=%s — waiting...",
                        workflow_name, state or "(pending)",
                    )
                _time.sleep(15)
            else:
                log.warning(
                    "[mgmt/tinkerbell] Timed out waiting for workflow %s — "
                    "skipping reboot for %s",
                    workflow_name, hw.name,
                )

            if not succeeded:
                continue

            # Patch Hardware CR: disable PXE so SMEE no longer intercepts boots.
            # Must use --type=json with targeted path operations — --type=merge on
            # an array field replaces the entire interfaces[0] element, wiping the
            # dhcp section and breaking subsequent CAPT reconciliation.
            log.info(
                "[mgmt/tinkerbell] Disabling PXE on Hardware CR %s ...", hw.name,
            )
            pxe_patch = _json.dumps([
                {"op": "replace", "path": "/spec/interfaces/0/netboot/allowPXE",      "value": False},
                {"op": "replace", "path": "/spec/interfaces/0/netboot/allowWorkflow", "value": False},
            ])
            r = subprocess.run(
                ["kubectl", "--kubeconfig", self._kc,
                 "-n", "tinkerbell",
                 "patch", "hardware", hw.name,
                 "--type=json",
                 f"--patch={pxe_patch}"],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                log.warning(
                    "[mgmt/tinkerbell] Failed to patch Hardware CR %s: %s",
                    hw.name, r.stderr.strip(),
                )

            # BMC power cycle — boots into Ubuntu (disk) since one-time PXE override
            # from the initial power-on was already consumed
            job_name = f"{hw.name}-post-provision-reboot"

            existing = subprocess.run(
                ["kubectl", "--kubeconfig", self._kc,
                 "get", "job.bmc.tinkerbell.org", job_name,
                 "-n", "tinkerbell"],
                capture_output=True,
            )
            if existing.returncode == 0:
                log.info(
                    "[mgmt/tinkerbell] Rufio reboot Job %s already exists — skipping",
                    job_name,
                )
                continue

            log.info(
                "[mgmt/tinkerbell] Creating Rufio Job %s — power-cycling %s into Ubuntu",
                job_name, hw.name,
            )
            reboot_job = {
                "apiVersion": "bmc.tinkerbell.org/v1alpha1",
                "kind": "Job",
                "metadata": {
                    "name": job_name,
                    "namespace": "tinkerbell",
                },
                "spec": {
                    "machineRef": {
                        "name": hw.name,
                        "namespace": "tinkerbell",
                    },
                    "tasks": [
                        {"powerAction": "cycle"},
                    ],
                },
            }
            subprocess.run(
                ["kubectl", "--kubeconfig", self._kc, "apply", "-f", "-"],
                input=_yaml.dump(reboot_job),
                text=True,
                check=True,
            )
            self._wait_for_rufio_job(job_name, hw.name)

        log.info("[mgmt/tinkerbell] Post-provisioning reboot sequence complete")

    def _wait_for_rufio_job(self, job_name: str, node_name: str, timeout: int = 300) -> None:
        """
        Poll a Rufio Job until it reaches Completed or Failed, or until
        *timeout* seconds elapse.  Logs a warning (does not raise) on
        failure or timeout so the install can continue — the node may
        still boot if the BMC command was sent before the error.
        """
        import time as _time
        import json as _json

        log.info("[mgmt/tinkerbell] Waiting for Rufio Job %s ...", job_name)
        deadline = _time.time() + timeout

        while _time.time() < deadline:
            r = subprocess.run(
                ["kubectl", "--kubeconfig", self._kc,
                 "get", "job.bmc.tinkerbell.org", job_name,
                 "-n", "tinkerbell",
                 "-o", "jsonpath={.status.conditions}"],
                capture_output=True, text=True,
            )

            if r.returncode == 0 and r.stdout.strip():
                try:
                    for cond in _json.loads(r.stdout):
                        if cond.get("type") == "Completed" and cond.get("status") == "True":
                            log.info(
                                "[mgmt/tinkerbell] Rufio Job %s completed — %s is powering on",
                                job_name, node_name,
                            )
                            return
                        if cond.get("type") == "Failed" and cond.get("status") == "True":
                            log.warning(
                                "[mgmt/tinkerbell] Rufio Job %s FAILED for %s: %s",
                                job_name, node_name, cond.get("message", "(no message)"),
                            )
                            return
                except Exception:
                    log.exception("[mgmt/tinkerbell] Error parsing Rufio Job status for %s", job_name)

            _time.sleep(5)

        log.warning(
            "[mgmt/tinkerbell] Rufio Job %s did not reach a terminal state within %ds — "
            "check BMC connectivity for %s",
            job_name, timeout, node_name,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _register_capt_in_clusterctl_config(self, ver: str) -> None:
        """
        Ensure the Tinkerbell infrastructure provider is registered in the
        clusterctl config file (~/.config/cluster-api/clusterctl.yaml).

        clusterctl only knows about built-in providers by default.  Community
        providers like CAPT must be added under the `providers:` key with their
        GitHub release URL before `clusterctl init --infrastructure tinkerbell`
        will work.
        """
        import yaml as _yaml

        config_path = Path.home() / ".config" / "cluster-api" / "clusterctl.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)

        # Read existing config, preserving all existing content
        existing: dict = {}
        if config_path.is_file():
            raw = config_path.read_text()
            # The file may mix YAML key:value lines with non-YAML comment blocks.
            # Parse only what yaml.safe_load can handle; ignore parse errors on
            # comment-heavy files by falling back to an empty dict.
            try:
                existing = _yaml.safe_load(raw) or {}
            except _yaml.YAMLError:
                existing = {}

        providers: list = existing.get("providers", [])

        # Check if Tinkerbell is already registered (any version)
        already_registered = any(
            p.get("name") == "tinkerbell" and p.get("type") == "InfrastructureProvider"
            for p in providers
            if isinstance(p, dict)
        )
        if already_registered:
            log.info("[mgmt/tinkerbell] Tinkerbell provider already in clusterctl config")
            return

        capt_url = (
            f"https://github.com/tinkerbell/cluster-api-provider-tinkerbell"
            f"/releases/{ver}/infrastructure-components.yaml"
        )

        providers.append({
            "name": "tinkerbell",
            "url": capt_url,
            "type": "InfrastructureProvider",
        })

        existing["providers"] = providers

        # Write providers block as clean YAML appended after the existing file
        # content so we don't disturb the hand-written variable lines above.
        providers_yaml = _yaml.dump({"providers": providers}, default_flow_style=False)

        # Remove any old providers block from the file, then append the fresh one
        import re
        raw_text = config_path.read_text() if config_path.is_file() else ""
        raw_text = re.sub(
            r"^providers:.*?(?=\n\S|\Z)", "", raw_text,
            flags=re.DOTALL | re.MULTILINE,
        ).rstrip()

        config_path.write_text(raw_text + "\n\n" + providers_yaml)
        log.info(
            "[mgmt/tinkerbell] Registered Tinkerbell provider in %s (url: %s)",
            config_path, capt_url,
        )

    def _local_chart(self, asset_name: str) -> str:
        """
        Return the path to a local Helm chart .tgz (or extracted directory)
        under assets/<asset_name>/charts/.

        Prefers an extracted directory if present (faster); falls back to the
        first .tgz found.  Raises FileNotFoundError if neither exists.
        """
        charts_dir = self._workspace_root / "assets" / asset_name / "charts"

        # Prefer an extracted directory (e.g. charts/cert-manager/)
        for entry in sorted(charts_dir.iterdir()) if charts_dir.is_dir() else []:
            if entry.is_dir():
                return str(entry)

        # Fall back to the first .tgz
        if charts_dir.is_dir():
            for entry in sorted(charts_dir.iterdir()):
                if entry.suffix == ".tgz" or entry.name.endswith(".tar.gz"):
                    return str(entry)

        raise FileNotFoundError(
            f"No Helm chart found under {charts_dir}. "
            f"Run: helm pull <repo>/{asset_name} --destination assets/{asset_name}/charts/"
        )

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

    def _kubectl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["kubectl", "--kubeconfig", self._kc, *args],
            check=check,
        )

    def _helm(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["helm", "--kubeconfig", self._kc, *args],
            check=check,
        )
