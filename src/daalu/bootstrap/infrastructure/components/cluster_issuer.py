# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
import yaml

from daalu.bootstrap.engine.component import InfraComponent


@dataclass
class ClusterIssuerACMESolver:
    type: str
    config: dict


@dataclass
class ClusterIssuerConfig:
    name: str
    issuer_type: str
    namespace: str = "cert-manager"

    # Self-signed / CA
    self_signed_secret_name: Optional[str] = None
    ca_secret_name: Optional[str] = None
    ca_cert: Optional[str] = None
    ca_key: Optional[str] = None

    # ACME
    acme_email: Optional[str] = None
    acme_server: Optional[str] = None
    acme_private_key_secret_name: Optional[str] = None
    acme_solvers: Optional[List[ClusterIssuerACMESolver]] = None

    # Venafi
    venafi_secret_name: Optional[str] = None
    venafi_spec: Optional[dict] = None


class ClusterIssuerComponent(InfraComponent):
    def __init__(self, *, config_path: Path, kubeconfig: str):
        super().__init__(
            name="cluster-issuer",
            repo_name="none",
            repo_url="",
            chart="",
            version=None,
            namespace="cert-manager",
            release_name="cluster-issuer",
            local_chart_dir=Path("/tmp"),
            remote_chart_dir=Path("/tmp"),
            kubeconfig=kubeconfig,
            uses_helm=False,
        )

        self.config_path = config_path
        self.wait_for_pods = False
        self.cfg = self._load_config()

    # --------------------------------------------------
    def _load_config(self) -> ClusterIssuerConfig:
        raw = yaml.safe_load(self.config_path.read_text()) or {}

        issuer_type = raw["issuer_type"]
        name = raw["name"]
        namespace = raw.get("namespace", "cert-manager")

        # -----------------------------
        # Self-signed / CA
        # -----------------------------
        ca = raw.get("ca") or {}
        self_signed_secret = raw.get("self_signed_secret_name")

        # -----------------------------
        # ACME
        # -----------------------------
        acme = raw.get("acme") or {}
        solvers = []
        for s in acme.get("solvers", []) or []:
            solvers.append(ClusterIssuerACMESolver(
                type=s["type"],
                config=s["config"],
            ))

        # -----------------------------
        # Venafi
        # -----------------------------
        venafi = raw.get("venafi") or {}

        return ClusterIssuerConfig(
            name=name,
            issuer_type=issuer_type,
            namespace=namespace,
            self_signed_secret_name=self_signed_secret,
            ca_secret_name=ca.get("secret_name"),
            ca_cert=ca.get("certificate"),
            ca_key=ca.get("private_key"),
            acme_email=acme.get("email"),
            acme_server=acme.get("server"),
            acme_private_key_secret_name=acme.get("private_key_secret_name"),
            acme_solvers=solvers or None,
            venafi_secret_name=venafi.get("secret_name"),
            venafi_spec=venafi.get("spec"),
        )

    # --------------------------------------------------
    def post_install(self, kubectl) -> None:
        cfg = self.cfg

        # Always ensure bootstrap self-signed issuer exists
        kubectl.apply_objects([{
            "apiVersion": "cert-manager.io/v1",
            "kind": "ClusterIssuer",
            "metadata": {"name": "self-signed"},
            "spec": {"selfSigned": {}},
        }])

        if cfg.issuer_type == "self-signed":
            self._apply_self_signed(kubectl)
        elif cfg.issuer_type == "ca":
            self._apply_ca(kubectl)
        elif cfg.issuer_type == "acme":
            self._apply_acme(kubectl)
        elif cfg.issuer_type == "venafi":
            self._apply_venafi(kubectl)
        else:
            raise ValueError(f"Unknown issuer_type: {cfg.issuer_type}")

    # --------------------------------------------------
    def _apply_self_signed(self, kubectl):
        kubectl.apply_objects([
            {
                "apiVersion": "cert-manager.io/v1",
                "kind": "Certificate",
                "metadata": {
                    "name": "self-signed-ca",
                    "namespace": self.cfg.namespace,
                },
                "spec": {
                    "isCA": True,
                    "commonName": "selfsigned-ca",
                    "secretName": self.cfg.self_signed_secret_name,
                    "privateKey": {"algorithm": "ECDSA", "size": 256},
                    "issuerRef": {"name": "self-signed", "kind": "ClusterIssuer"},
                },
            },
            {
                "apiVersion": "cert-manager.io/v1",
                "kind": "ClusterIssuer",
                "metadata": {"name": self.cfg.name},
                "spec": {"ca": {"secretName": self.cfg.self_signed_secret_name}},
            },
        ])

    def _apply_ca(self, kubectl):
        kubectl.apply_objects([
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": self.cfg.ca_secret_name,
                    "namespace": self.cfg.namespace,
                },
                "type": "kubernetes.io/tls",
                "stringData": {
                    "tls.crt": self.cfg.ca_cert,
                    "tls.key": self.cfg.ca_key,
                },
            },
            {
                "apiVersion": "cert-manager.io/v1",
                "kind": "ClusterIssuer",
                "metadata": {"name": self.cfg.name},
                "spec": {"ca": {"secretName": self.cfg.ca_secret_name}},
            },
        ])

    def _apply_acme(self, kubectl):
        solvers = [{s.type: s.config} for s in self.cfg.acme_solvers or []]

        kubectl.apply_objects([{
            "apiVersion": "cert-manager.io/v1",
            "kind": "ClusterIssuer",
            "metadata": {"name": self.cfg.name},
            "spec": {
                "acme": {
                    "email": self.cfg.acme_email,
                    "server": self.cfg.acme_server,
                    "privateKeySecretRef": {
                        "name": self.cfg.acme_private_key_secret_name,
                    },
                    "solvers": solvers,
                }
            },
        }])

    def _apply_venafi(self, kubectl):
        kubectl.apply_objects([{
            "apiVersion": "cert-manager.io/v1",
            "kind": "ClusterIssuer",
            "metadata": {"name": self.cfg.name},
            "spec": {"venafi": self.cfg.venafi_spec},
        }])
