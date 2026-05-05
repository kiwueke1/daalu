# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/infrastructure/components/cert_manager.py

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from daalu.bootstrap.engine.component import InfraComponent
import logging

log = logging.getLogger("daalu")


@dataclass(frozen=True)
class CertManagerIssuer:
    name: str
    server: str


@dataclass(frozen=True)
class CertManagerCertIssuerRef:
    kind: str = "ClusterIssuer"
    name: str = "letsencrypt-prod"


@dataclass(frozen=True)
class CertManagerCertificate:
    name: str
    namespace: str
    secret_name: str
    dns_names: List[str]
    issuer: CertManagerCertIssuerRef


@dataclass(frozen=True)
class CertManagerArgoCDOnboard:
    enabled: bool = False
    local_manifest: str = ""
    github_raw_url: str = ""
    github_token_env: str = "GITHUB_TOKEN"


@dataclass(frozen=True)
class CertManagerConfig:
    cloudflare_api_token: str
    email: str
    dns_zones: List[str]
    cluster_issuers: List[CertManagerIssuer]
    certificates: List[CertManagerCertificate]
    argocd_onboard: CertManagerArgoCDOnboard


class CertManagerComponent(InfraComponent):
    """
    Installs cert-manager + configures:
      - Cloudflare token Secret
      - ClusterIssuers (staging + prod)
      - Namespaces for Certificates
      - Certificates

    Optionally onboards an ArgoCD Application (kept as a toggle).
    """

    def __init__(
        self,
        *,
        values_path: Path,
        config_path: Path,
        assets_dir: Path,
        kubeconfig: str,
    ):
        super().__init__(
            name="cert-manager",
            repo_name="jetstack",
            repo_url="https://charts.jetstack.io",
            chart="cert-manager",
            version=None,
            namespace="cert-manager",
            release_name="cert-manager",
            local_chart_dir=Path.home() / ".daalu/helm/charts",
            remote_chart_dir=Path("/usr/local/src"),
            kubeconfig=kubeconfig,
        )
        self.values_path = values_path
        self.config_path = config_path
        self.assets_dir = assets_dir

        # cert-manager is a Jetstack chart, not OpenStack-Helm.
        # Its JSON schema rejects the OSH-style top-level `images.tags` block.
        self.inject_base_images = False

        # cert-manager typically runs multiple pods
        self.min_running_pods = 2

    def values(self) -> dict:
        return self.load_values_file(self.values_path)

    # -------------------------
    # Config loading
    # -------------------------

    def _load_config(self) -> CertManagerConfig:
        raw = yaml.safe_load(self.config_path.read_text()) or {}

        cloudflare = raw.get("cloudflare", {}) or {}
        token_from_file = (cloudflare.get("api_token") or "").strip()
        token_from_env = (os.environ.get("CERT_MANAGER_CLOUDFLARE_API_TOKEN") or "").strip()
        cloudflare_token = token_from_file or token_from_env

        if not cloudflare_token:
            raise RuntimeError(
                "cert-manager Cloudflare API token is required. "
                "Set it in assets config.yaml (cloudflare.api_token) or export CERT_MANAGER_CLOUDFLARE_API_TOKEN."
            )

        email = (raw.get("email") or "").strip()
        if not email:
            raise RuntimeError("cert-manager email is required (config.yaml: email).")

        dns_zones = raw.get("dns_zones") or []
        if not isinstance(dns_zones, list) or not dns_zones:
            raise RuntimeError("cert-manager dns_zones must be a non-empty list (config.yaml: dns_zones).")

        issuers_raw = raw.get("cluster_issuers") or []
        issuers: List[CertManagerIssuer] = []
        for i in issuers_raw:
            issuers.append(CertManagerIssuer(name=i["name"], server=i["server"]))

        if not issuers:
            raise RuntimeError("cert-manager cluster_issuers must be non-empty (config.yaml: cluster_issuers).")

        certs_raw = raw.get("certificates") or []
        certs: List[CertManagerCertificate] = []
        for c in certs_raw:
            issuer_raw = c.get("issuer") or {}
            issuer = CertManagerCertIssuerRef(
                kind=issuer_raw.get("kind") or "ClusterIssuer",
                name=issuer_raw.get("name") or "letsencrypt-prod",
            )
            certs.append(
                CertManagerCertificate(
                    name=c["name"],
                    namespace=c["namespace"],
                    secret_name=c["secret_name"],
                    dns_names=list(c.get("dns_names") or []),
                    issuer=issuer,
                )
            )

        argocd_raw = raw.get("argocd_onboard") or {}
        argocd = CertManagerArgoCDOnboard(
            enabled=bool(argocd_raw.get("enabled", False)),
            local_manifest=str(argocd_raw.get("local_manifest") or ""),
            github_raw_url=str(argocd_raw.get("github_raw_url") or ""),
            github_token_env=str(argocd_raw.get("github_token_env") or "GITHUB_TOKEN"),
        )

        return CertManagerConfig(
            cloudflare_api_token=cloudflare_token,
            email=email,
            dns_zones=dns_zones,
            cluster_issuers=issuers,
            certificates=certs,
            argocd_onboard=argocd,
        )

    # -------------------------
    # K8s resource builders
    # -------------------------

    def _cloudflare_secret(self, *, token: str) -> Dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": "cloudflare-api-token-secret",
                "namespace": "cert-manager",
            },
            "type": "Opaque",
            # Use stringData so kubectl applies it easily
            "stringData": {"api-token": token},
        }

    def _cluster_issuer(
        self,
        *,
        name: str,
        server: str,
        email: str,
        dns_zones: List[str],
    ) -> Dict[str, Any]:
        return {
            "apiVersion": "cert-manager.io/v1",
            "kind": "ClusterIssuer",
            "metadata": {"name": name},
            "spec": {
                "acme": {
                    "email": email,
                    "server": server,
                    "privateKeySecretRef": {
                        "name": name,
                        "key": "tls.key",
                    },
                    "solvers": [
                        {
                            "dns01": {
                                "cloudflare": {
                                    "apiTokenSecretRef": {
                                        "name": "cloudflare-api-token-secret",
                                        "key": "api-token",
                                    }
                                }
                            },
                            "selector": {"dnsZones": dns_zones},
                        }
                    ],
                }
            },
        }

    def _namespace(self, name: str) -> Dict[str, Any]:
        return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}

    def _certificate(self, cert: CertManagerCertificate) -> Dict[str, Any]:
        return {
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "metadata": {"name": cert.name, "namespace": cert.namespace},
            "spec": {
                "secretName": cert.secret_name,
                "dnsNames": cert.dns_names,
                "issuerRef": {"kind": cert.issuer.kind, "name": cert.issuer.name},
            },
        }

    def _dump_multi(self, objs: List[Dict[str, Any]]) -> str:
        # YAML multi-doc output
        return "\n---\n".join(yaml.safe_dump(o, sort_keys=False) for o in objs if o)

    # -------------------------
    # Hooks
    # -------------------------

    def post_install(self, kubectl) -> None:
        import time

        cfg = self._load_config()

        # The webhook pod reports Ready before its HTTPS server is actually
        # listening. Retry applying resources until the webhook accepts connections.
        def _apply_with_retry(content: str, remote_path: str, retries: int = 10, delay: int = 10) -> None:
            for attempt in range(1, retries + 1):
                try:
                    kubectl.apply_content(content=content, remote_path=remote_path)
                    return
                except Exception as e:
                    if "connection refused" in str(e) and attempt < retries:
                        log.info(
                            "[cert-manager] Webhook not ready yet (attempt %d/%d), retrying in %ds...",
                            attempt, retries, delay,
                        )
                        time.sleep(delay)
                    else:
                        raise

        # 1) Cloudflare token secret (cert-manager namespace)
        _apply_with_retry(
            content=self._dump_multi([self._cloudflare_secret(token=cfg.cloudflare_api_token)]),
            remote_path="/tmp/cert-manager-cloudflare-secret.yaml",
        )

        # 1b) Drop any stale ACME account keys from a previous cluster install.
        # cert-manager (re)creates these automatically on first ClusterIssuer sync.
        # The key secret is named after the issuer (privateKeySecretRef.name = issuer name).
        # If a leftover key exists, Let's Encrypt may reject it with
        # "Account ID doesn't match ID for authorization", breaking cert issuance.
        for issuer in cfg.cluster_issuers:
            kubectl._run(
                f"delete secret {issuer.name}"
                " -n cert-manager --ignore-not-found"
            )

        # 2) ClusterIssuers
        issuer_docs = [
            self._cluster_issuer(
                name=i.name,
                server=i.server,
                email=cfg.email,
                dns_zones=cfg.dns_zones,
            )
            for i in cfg.cluster_issuers
        ]
        _apply_with_retry(
            content=self._dump_multi(issuer_docs),
            remote_path="/tmp/cert-manager-cluster-issuers.yaml",
        )

        # 3) Ensure namespaces for certificates exist
        ns_names = sorted({c.namespace for c in cfg.certificates if c.namespace})
        ns_docs = [self._namespace(n) for n in ns_names]
        if ns_docs:
            _apply_with_retry(
                content=self._dump_multi(ns_docs),
                remote_path="/tmp/cert-manager-namespaces.yaml",
            )

        # 4) Certificates
        cert_docs = [self._certificate(c) for c in cfg.certificates]
        if cert_docs:
            _apply_with_retry(
                content=self._dump_multi(cert_docs),
                remote_path="/tmp/cert-manager-certificates.yaml",
            )

        # 5) Optional: Argo CD onboarding (kept, but off by default)
        if cfg.argocd_onboard.enabled:
            self._maybe_onboard_argocd_app(kubectl, cfg)

    def teardown_extra(self, kubectl) -> None:
        """Delete cert-manager CRD objects created in post_install."""
        for kind in ("certificate", "clusterissuer"):
            rc, out, _ = kubectl._run(
                f"get {kind}.cert-manager.io -A -o name --ignore-not-found"
            )
            if rc == 0 and out.strip():
                for resource in out.strip().splitlines():
                    log.debug("[cert-manager] Deleting %s", resource)
                    kubectl._run(f"delete {resource} -A --ignore-not-found")

        kubectl._run(
            "delete secret cloudflare-api-token-secret"
            " -n cert-manager --ignore-not-found"
        )

    def _maybe_onboard_argocd_app(self, kubectl, cfg: CertManagerConfig) -> None:
        """
          - wait for applications.argoproj.io CRD
          - apply an Application manifest (local file or GitHub raw)
        """
        # If your kubectl wrapper already has a wait_for_crd method, use it.
        # Otherwise, this can be implemented in your kubectl wrapper.
        if hasattr(kubectl, "wait_for_crd"):
            kubectl.wait_for_crd("applications.argoproj.io")

        onboard = cfg.argocd_onboard

        if onboard.local_manifest:
            path = self.assets_dir / onboard.local_manifest
            if not path.exists():
                raise RuntimeError(f"ArgoCD onboard manifest not found: {path}")
            kubectl.apply_file(path)
            return

        if onboard.github_raw_url:
            # Use kubectl wrapper's ability to apply remote content if available.
            # Otherwise, just curl via your ssh runner in InfraComponent (if you add that helper).
            token = (os.environ.get(onboard.github_token_env) or "").strip()
            if not token:
                raise RuntimeError(
                    f"ArgoCD onboarding requires GitHub token in env var {onboard.github_token_env}."
                )

            if not hasattr(kubectl, "apply_url"):
                raise RuntimeError(
                    "kubectl.apply_url() not available. "
                    "Either add apply_url to your kubectl wrapper or use local_manifest."
                )
            log.debug(f"cert-manager github url is {onboard.github_raw_url}")

            kubectl.apply_url(
                onboard.github_raw_url,
                headers={
                    "Accept": "application/vnd.github.v3.raw",
                    "Authorization": f"token {token}",
                },
            )
            return

        raise RuntimeError(
            "argocd_onboard.enabled=true but neither local_manifest nor github_raw_url provided."
        )
