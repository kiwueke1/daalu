# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/openstack/secret_registry.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from daalu.bootstrap.openstack.secrets_manager import SecretsManager


@dataclass(frozen=True)
class SecretSpec:
    """
    How to create a Kubernetes secret from a secrets.yaml entry.
    """
    k8s_name: str
    k8s_key: str
    source_key: str
    namespace: Optional[str] = None
    secret_type: str = "Opaque"


class SecretRegistry:
    """
    Defines per-chart secrets and can materialize them as Kubernetes Secret objects.
    """

    def __init__(self, *, secrets: SecretsManager) -> None:
        self.secrets = secrets

    def keystone_specs(self) -> list[SecretSpec]:
        # You can extend this over time, without changing your secrets.yaml structure.
        return [
            # OIDC crypto passphrase used by keystone apache OIDC
            SecretSpec(
                k8s_name="keystone-oidc-crypto-passphrase",
                k8s_key="passphrase",
                source_key="keystone_oidc_crypto_passphrase",
            ),
            # Keycloak client secret if you wire Keystone to Keycloak (your environment)
            SecretSpec(
                k8s_name="keystone-keycloak-client-secret",
                k8s_key="client_secret",
                source_key="keystone_keycloak_client_secret",
            ),
        ]

    def materialize(self, kubectl, specs: list[SecretSpec], default_ns: str) -> None:
        objs = []
        for s in specs:
            ns = s.namespace or default_ns
            value = self.secrets.require(s.source_key)
            objs.append(
                self.secrets.build_specific_secret_object(
                    name=s.k8s_name,
                    namespace=ns,
                    key_to_value={s.k8s_key: value},
                    source_keys={s.k8s_key: s.source_key},
                    secret_type=s.secret_type,
                )
            )
        if objs:
            kubectl.apply_objects(objs)
