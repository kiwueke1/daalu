# src/daalu/bootstrap/openstack/rabbitmq.py

from __future__ import annotations

import time
from typing import Tuple, Optional


class RabbitMQServiceManager:
    """
    Daalu replacement for Atmosphere's `roles/rabbitmq`.

    Responsibilities:
      - Ensure a per-service RabbitMQ cluster exists
      - Wait for operator-generated default user secret
      - Return decoded credentials

    Naming matches Atmosphere:
      - Cluster name: rabbitmq-<service>
      - Secret name:  rabbitmq-<service>-default-user
    """

    def __init__(
        self,
        *,
        kubectl,
        namespace: str = "openstack",
        replicas: int = 1,
    ):
        self.kubectl = kubectl
        self.namespace = namespace
        self.replicas = replicas

    # -------------------------------------------------
    # 1) Ensure RabbitMQCluster CR exists
    # -------------------------------------------------
    def ensure_cluster(self, service: str) -> None:
        """
        Idempotently creates a RabbitMQCluster for a service.
        Safe to call multiple times.
        """
        name = f"rabbitmq-{service}"

        obj = {
            "apiVersion": "rabbitmq.com/v1beta1",
            "kind": "RabbitmqCluster",   # <-- NOTE: correct casing
            "metadata": {
                "name": name,
                "namespace": self.namespace,
            },
            "spec": {
                "replicas": self.replicas,
                "rabbitmq": {
                    # Atmosphere parity: admin default user
                    "additionalConfig": (
                        "default_user_tags.administrator = true\n"
                        "loopback_users.guest = false"
                    ),
                },
            },
        }

        self.kubectl.apply_objects([obj])

    # -------------------------------------------------
    # 2) Wait for operator-generated default user secret
    # -------------------------------------------------
    def wait_for_default_user_secret(
        self,
        service: str,
        timeout: int = 300,
        poll_interval: int = 2,
    ) -> dict:
        """
        Waits for rabbitmq-<service>-default-user Secret to appear.
        Created automatically by the RabbitMQ operator.
        """
        secret_name = f"rabbitmq-{service}-default-user"
        deadline = time.time() + timeout

        while time.time() < deadline:
            sec = self.kubectl.get_object(
                api_version="v1",
                kind="Secret",
                name=secret_name,
                namespace=self.namespace,
            )
            if sec:
                return sec

            time.sleep(poll_interval)

        raise TimeoutError(
            f"Timed out waiting for Secret/{secret_name} in namespace {self.namespace}"
        )

    # -------------------------------------------------
    # 3) Read decoded credentials
    # -------------------------------------------------
    def get_default_user_credentials(self, service: str) -> Tuple[str, str]:
        """
        Returns (username, password) for RabbitMQ service user.
        """
        sec = self.wait_for_default_user_secret(service)

        data = sec.get("data", {})
        username_b64 = data.get("username")
        password_b64 = data.get("password")

        if not username_b64 or not password_b64:
            raise RuntimeError(
                f"Secret rabbitmq-{service}-default-user missing username/password"
            )

        return (
            self.kubectl.b64decode_str(username_b64),
            self.kubectl.b64decode_str(password_b64),
        )

    # -------------------------------------------------
    # 4) High-level helper (most callers use this)
    # -------------------------------------------------
    def ensure_and_get_credentials(self, service: str) -> Tuple[str, str]:
        """
        One-call helper used by OpenStack endpoint builders.
        """
        self.ensure_cluster(service)
        return self.get_default_user_credentials(service)
