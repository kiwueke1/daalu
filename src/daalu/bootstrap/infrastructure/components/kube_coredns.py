# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/infrastructure/components/kube_coredns.py

from __future__ import annotations

import re
import logging
from pathlib import Path
from typing import Optional

from daalu.bootstrap.engine.component import InfraComponent

log = logging.getLogger("daalu")


class KubeCoreDNSPatchComponent(InfraComponent):
    """
    Patches the Kubernetes system CoreDNS (kube-system/coredns ConfigMap) to add
    split-horizon DNS overrides that map cluster-hosted service FQDNs to the
    internal Istio ingress gateway IP.

    WHY THIS IS NEEDED — SPLIT-HORIZON DNS / HAIRPIN NAT LOOPBACK
    =============================================================
    All public-facing services (Keycloak, Keystone, Horizon, etc.) share a single
    external DNS record that resolves to the public IP of the network edge device
    (e.g. a pfSense router/firewall). For example:

        auth.daalu.io  →  69.153.232.26  (pfSense WAN IP)

    From a browser on the internet this works fine: pfSense port-forwards 443 to
    the Istio ingress gateway inside the cluster, which serves the correct TLS cert
    and routes traffic to Keycloak.

    HOWEVER, pods running INSIDE the cluster use the same external DNS record. When
    a pod (e.g. keystone-api) resolves auth.daalu.io it gets the pfSense WAN IP.
    pfSense receives the connection but — because the source is already inside the
    LAN — it typically serves its own management UI on port 443 with a self-signed
    certificate rather than forwarding the request to the cluster. This is known as
    the "hairpin NAT" or "NAT loopback" problem.

    CONCRETE FAILURE THIS FIXES
    ---------------------------
    The Keystone Apache module mod_auth_openidc makes outbound HTTPS calls to fetch
    the Keycloak OIDC discovery document:

        https://auth.daalu.io/realms/daalu/.well-known/openid-configuration

    Without this fix, that request hits pfSense and Apache sees:

        SSL certificate problem: self-signed certificate
        (O=Netgate pfSense Plus GUI default Self-Signed Certificate)

    mod_auth_openidc rejects the untrusted cert and returns HTTP 500 to the
    browser, showing "Internal Server Error" on the WebSSO login page.

    THE FIX — SPLIT-HORIZON DNS VIA COREDNS HOSTS PLUGIN
    -----------------------------------------------------
    CoreDNS (kube-system/coredns) is the cluster-internal DNS resolver used by
    all pods. We inject a `hosts` plugin block that overrides resolution for all
    cluster-hosted FQDNs so they resolve to the Istio ingress gateway's internal
    LoadBalancer IP (e.g. 10.10.1.101) instead of the pfSense public IP:

        auth.daalu.io       →  10.10.1.101  (Istio gateway, serves LE cert)
        identity.daalu.io   →  10.10.1.101
        dashboard.daalu.io  →  10.10.1.101
        ...

    From inside a pod, traffic now goes directly to the Istio gateway, which has a
    valid Let's Encrypt certificate for each FQDN. mod_auth_openidc verifies the
    cert successfully, retrieves the OIDC metadata, and the WebSSO flow completes.

    The `fallthrough` directive in the hosts block tells CoreDNS to continue with
    the normal upstream lookup for any hostname NOT listed (i.e. everything else on
    the internet), so no existing DNS resolution is broken.

    IDEMPOTENCY
    -----------
    The component is safe to re-run. It reads the live Corefile, strips any
    pre-existing `hosts { ... }` block written by a previous run, inserts the
    freshly-computed block, and restarts CoreDNS. Running it again after adding a
    new OpenStack service (and its VirtualService) will pick up the new hostname.
    """

    def __init__(
        self,
        *,
        kubeconfig: str,
        # Namespace and service name of the Istio ingress gateway whose
        # LoadBalancer IP all the service FQDNs should resolve to internally.
        # These match the default Daalu Istio installation.
        istio_gateway_namespace: str = "istio-ingress",
        istio_gateway_service: str = "istio-ingressgateway",
    ):
        super().__init__(
            name="kube-coredns-patch",
            repo_name="none",
            repo_url="",
            chart="",
            version=None,
            namespace="kube-system",
            release_name="",
            local_chart_dir=Path("/tmp"),
            remote_chart_dir=Path("/tmp"),
            kubeconfig=kubeconfig,
            uses_helm=False,
        )

        self.istio_gateway_namespace = istio_gateway_namespace
        self.istio_gateway_service = istio_gateway_service
        # Disable the default pod-readiness wait; CoreDNS rollout is handled
        # explicitly inside post_install via `rollout status`.
        self.wait_for_pods = False

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    def post_install(self, kubectl) -> None:
        log.info("[kube-coredns-patch] Resolving Istio ingress gateway IP...")
        gateway_ip = self._get_gateway_ip(kubectl)
        log.info("[kube-coredns-patch] Gateway IP: %s", gateway_ip)

        log.info("[kube-coredns-patch] Discovering VirtualService hostnames...")
        hostnames = self._collect_vs_hostnames(kubectl)

        if not hostnames:
            log.warning(
                "[kube-coredns-patch] No VirtualService hosts found; "
                "skipping CoreDNS patch. Re-run after VirtualServices are created."
            )
            return

        log.info(
            "[kube-coredns-patch] Patching CoreDNS hosts block for %d FQDNs → %s",
            len(hostnames),
            gateway_ip,
        )
        for h in hostnames:
            log.debug("[kube-coredns-patch]   %s → %s", h, gateway_ip)

        self._patch_coredns(kubectl, gateway_ip=gateway_ip, hostnames=hostnames)

        log.info("[kube-coredns-patch] Restarting CoreDNS to apply new config...")
        self._restart_coredns(kubectl)

        log.info("[kube-coredns-patch] CoreDNS split-horizon DNS patch applied ✓")

    # -----------------------------------------------------------------
    # Step 1 — Discover the Istio gateway's internal LoadBalancer IP
    # -----------------------------------------------------------------

    def _get_gateway_ip(self, kubectl) -> str:
        """
        Read the LoadBalancer IP assigned to the Istio ingress gateway service.

        This is the internal cluster IP that all service FQDNs should resolve to
        from inside pods, bypassing the external pfSense hairpin NAT problem.
        """
        rc, out, err = kubectl._run(
            f"get svc {self.istio_gateway_service}"
            f" -n {self.istio_gateway_namespace}"
            f" -o jsonpath='{{.status.loadBalancer.ingress[0].ip}}'"
        )

        ip = out.strip().strip("'\"")
        if rc != 0 or not ip:
            raise RuntimeError(
                f"[kube-coredns-patch] Could not determine Istio gateway IP.\n"
                f"Command exit code: {rc}\n"
                f"Stderr: {err}\n"
                f"Ensure the '{self.istio_gateway_service}' service in namespace "
                f"'{self.istio_gateway_namespace}' has a LoadBalancer IP assigned."
            )

        return ip

    # -----------------------------------------------------------------
    # Step 2 — Collect all cluster-hosted FQDNs from Istio VirtualServices
    # -----------------------------------------------------------------

    def _collect_vs_hostnames(self, kubectl) -> list[str]:
        """
        Query all Istio VirtualService objects across every namespace and extract
        their `.spec.hosts[]` entries.

        We only include hostnames that look like real public FQDNs (contain a dot
        and do not end with `.cluster.local`) because internal service names like
        `keystone-api` or `keycloak.svc.cluster.local` are already handled by the
        Kubernetes cluster DNS and do not need overriding.

        The returned list is sorted for reproducible Corefile output so that
        successive runs produce identical diffs.
        """
        rc, out, err = kubectl._run(
            "get virtualservices -A"
            " -o jsonpath='{range .items[*]}{.spec.hosts[*]}{\"\\n\"}{end}'"
        )

        if rc != 0:
            log.warning(
                "[kube-coredns-patch] Failed to list VirtualServices: %s", err
            )
            return []

        hostnames: set[str] = set()
        for line in out.splitlines():
            for token in line.split():
                host = token.strip("'\"")
                # Skip internal Kubernetes service names and wildcards
                if (
                    host
                    and "." in host
                    and not host.endswith(".cluster.local")
                    and not host.startswith("*")
                ):
                    hostnames.add(host)

        return sorted(hostnames)

    # -----------------------------------------------------------------
    # Step 3 — Build and apply the patched Corefile
    # -----------------------------------------------------------------

    def _patch_coredns(
        self, kubectl, *, gateway_ip: str, hostnames: list[str]
    ) -> None:
        """
        Read the live Corefile from the kube-system/coredns ConfigMap, replace any
        pre-existing `hosts { ... }` block produced by a previous run of this
        component, insert a fresh block with the current hostnames, and write the
        result back via a kubectl patch.
        """
        # Read the live Corefile
        rc, out, err = kubectl._run(
            "get cm coredns -n kube-system -o jsonpath='{.data.Corefile}'"
        )
        if rc != 0:
            raise RuntimeError(
                f"[kube-coredns-patch] Failed to read CoreDNS Corefile: {err}"
            )

        existing_corefile = out.strip().strip("'\"")
        new_corefile = self._build_corefile(
            existing_corefile, gateway_ip=gateway_ip, hostnames=hostnames
        )

        # Patch the ConfigMap. kubectl.patch() serialises the dict to JSON and
        # runs: kubectl patch cm coredns -n kube-system --type merge -p '<json>'
        # json.dumps correctly escapes newlines inside the Corefile string so the
        # shell command is valid.
        kubectl.patch(
            api_version="v1",
            kind="ConfigMap",
            name="coredns",
            namespace="kube-system",
            patch={"data": {"Corefile": new_corefile}},
        )

    def _build_corefile(
        self, existing: str, *, gateway_ip: str, hostnames: list[str]
    ) -> str:
        """
        Produce the desired Corefile by:
          1. Stripping any existing `hosts { ... }` block (idempotency).
          2. Building a new `hosts { ... }` block mapping every FQDN to the
             Istio gateway IP.
          3. Inserting it immediately before the `prometheus` plugin so it takes
             effect before the upstream forwarder is consulted.

        The `fallthrough` directive at the end of the hosts block is mandatory —
        without it CoreDNS would return NXDOMAIN for every hostname NOT listed
        in the block, breaking normal internet DNS resolution for all pods.
        """
        # Build the new hosts block with consistent 4-space indentation that
        # matches the rest of the Corefile produced by kubeadm.
        host_lines = "\n".join(f"       {gateway_ip} {h}" for h in hostnames)
        hosts_block = (
            "    hosts {\n"
            f"{host_lines}\n"
            "       # fallthrough passes any hostname NOT listed above to the\n"
            "       # upstream forwarder, preserving normal internet DNS.\n"
            "       fallthrough\n"
            "    }"
        )

        # Remove any previously-injected hosts block so the patch is idempotent.
        cleaned = _strip_hosts_block(existing)

        # Insert before the `prometheus` plugin line.  If prometheus is not
        # present fall back to inserting before `forward`.
        for marker in ["    prometheus ", "    forward "]:
            if marker in cleaned:
                return cleaned.replace(marker, f"{hosts_block}\n{marker}", 1)

        # Last resort: append inside the outer server block.
        return cleaned.rstrip().rstrip("}").rstrip() + f"\n{hosts_block}\n}}\n"

    # -----------------------------------------------------------------
    # Step 4 — Rolling restart so pods pick up the new Corefile
    # -----------------------------------------------------------------

    def _restart_coredns(self, kubectl) -> None:
        """
        CoreDNS watches its ConfigMap and reloads automatically (the `reload`
        plugin), but the reload can take up to 30 seconds. A rolling restart
        guarantees the new config is active immediately and lets us wait for
        readiness before continuing with the rest of the bootstrap.
        """
        kubectl._run("rollout restart deployment/coredns -n kube-system")
        rc, out, err = kubectl._run(
            "rollout status deployment/coredns -n kube-system --timeout=120s"
        )
        if rc != 0:
            raise RuntimeError(
                f"[kube-coredns-patch] CoreDNS rollout did not complete: {err or out}"
            )


# -----------------------------------------------------------------
# Module-level helper — CoreDNS Corefile parser
# -----------------------------------------------------------------

def _strip_hosts_block(corefile: str) -> str:
    """
    Remove existing `hosts { ... }` and `template IN A <zone> { ... }` plugin
    blocks from a CoreDNS Corefile, preserving everything else exactly as-is.

    Both block types are used by this component (hosts for explicit entries,
    template for wildcard-domain overrides applied as emergency fixes).  Stripping
    both ensures idempotent re-runs regardless of which approach was used
    previously.

    Uses a simple brace-counting state machine rather than a regex so it handles
    nested braces correctly.

    The function is idempotent: calling it on a Corefile that has neither block
    returns the input unchanged.
    """
    lines = corefile.splitlines(keepends=True)
    result: list[str] = []
    depth = 0
    in_block = False

    for line in lines:
        if not in_block:
            # Match `hosts {` or `template IN A <zone> {` blocks.
            if re.match(r"^\s*hosts\s*\{", line) or re.match(
                r"^\s*template\s+IN\s+A\s+\S+\s*\{", line
            ):
                in_block = True
                depth = line.count("{") - line.count("}")
                if depth <= 0:
                    # Degenerate single-line block — already closed.
                    in_block = False
                # Either way, do NOT append this line.
                continue
            result.append(line)
        else:
            # We're inside the block — track brace depth.
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                # The closing `}` of the block.
                in_block = False
            # Don't append any line that belongs to the old block.

    return "".join(result)
