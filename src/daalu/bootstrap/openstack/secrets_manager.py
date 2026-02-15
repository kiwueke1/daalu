from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml
import logging

log = logging.getLogger("daalu")


def _b64encode_str(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _as_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", errors="replace")
    return str(v)


@dataclass(frozen=True)
class SecretRef:
    """Traceability pointer."""
    namespace: str
    name: str
    key: str
    source_key: str  # key in secrets.yaml


class SecretsManager:
    """
    Loads a secrets.yaml and provides:
      - pattern-based extraction of per-service DB and RabbitMQ passwords
      - creation of Kubernetes Secret objects (one "bundle" + optional per-chart secrets)
      - traceability mapping for debugging
    """

    DB_KEY_PATTERNS = (
        r"^(?P<svc>[a-z0-9_]+)_(database_password|db_password|mariadb_password|mysql_password|database_key|db_key)$",
    )

    RABBIT_KEY_PATTERNS = (
        r"^(?P<svc>[a-z0-9_]+)_(rabbitmq_password|rabbit_password|rabbitmq_key|rabbit_key)$",
    )


    def __init__(
        self,
        *,
        secrets_file: Path,
        default_namespace: str = "openstack",
        bundle_secret_name: str = "daalu-secrets",
    ) -> None:
        self.secrets_file = secrets_file
        self.default_namespace = default_namespace
        self.bundle_secret_name = bundle_secret_name
        self._raw: dict[str, Any] = {}

        # Discovered maps:
        self.service_db_passwords: dict[str, str] = {}
        self.service_rabbit_passwords: dict[str, str] = {}

        # Traceability:
        self._trace: list[SecretRef] = []

    def load(self) -> "SecretsManager":
        data = yaml.safe_load(self.secrets_file.read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError(f"Expected mapping in {self.secrets_file}, got {type(data)}")
        self._raw = {str(k): v for k, v in data.items()}
        self._discover_service_passwords()
        return self

    def raw(self) -> dict[str, Any]:
        return dict(self._raw)

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        if key not in self._raw:
            return default
        return _as_str(self._raw[key])

    def require(self, key: str) -> str:
        v = self.get(key)
        if not v:
            raise ValueError(f"Missing required secret key in secrets.yaml: {key}")
        return v



    def _discover_service_passwords_1(self) -> None:
        self.service_db_passwords.clear()
        self.service_rabbit_passwords.clear()

        for k, v in self._raw.items():
            sv = _as_str(v)
            if not sv:
                continue

            #  NORMALIZE  keys
            normalized = k
            if normalized.startswith("openstack_helm_endpoints_"):
                normalized = normalized[len("openstack_helm_endpoints_"):]

            # --- DB passwords ---
            for pat in self.DB_KEY_PATTERNS:
                m = re.match(pat, normalized)
                if m:
                    svc = m.group("svc")

                    log.debug("\n[DB PASSWORD MATCH]")
                    log.debug(f"  Pattern : {pat}")
                    log.debug(f"  Raw key : {normalized}")
                    log.debug(f"  Service : {svc}")
                    log.debug(f"  Value   : ***")

                    self.service_db_passwords[svc] = sv


            # --- RabbitMQ passwords ---
            for pat in self.RABBIT_KEY_PATTERNS:
                m = re.match(pat, normalized)
                if m:
                    svc = m.group("svc")

                    log.debug("\n[RABBITMQ PASSWORD MATCH]")
                    log.debug(f"  Pattern : {pat}")
                    log.debug(f"  Raw key : {normalized}")
                    log.debug(f"  Service : {svc}")
                    log.debug(f"  Value   : ***")

                    self.service_rabbit_passwords[svc] = sv


    def _discover_service_passwords(self) -> None:
        self.service_db_passwords.clear()
        self.service_rabbit_passwords.clear()

        # ------------------------------------------------------------------
        # 1. REGEX-BASED DISCOVERY (static / legacy secrets)
        # ------------------------------------------------------------------
        for k, v in self._raw.items():
            sv = _as_str(v)
            if not sv:
                continue

            normalized = k
            if normalized.startswith("openstack_helm_endpoints_"):
                normalized = normalized[len("openstack_helm_endpoints_"):]

            # --- DB passwords ---
            for pat in self.DB_KEY_PATTERNS:
                m = re.match(pat, normalized)
                if m:
                    svc = m.group("svc")

                    log.debug("\n[DB PASSWORD MATCH]")
                    log.debug(f"  Pattern : {pat}")
                    log.debug(f"  Raw key : {normalized}")
                    log.debug(f"  Service : {svc}")
                    log.debug(f"  Value   : ***")

                    self.service_db_passwords[svc] = sv

            # --- RabbitMQ passwords (static) ---
            for pat in self.RABBIT_KEY_PATTERNS:
                m = re.match(pat, normalized)
                if m:
                    svc = m.group("svc")

                    log.debug("\n[RABBITMQ PASSWORD MATCH]")
                    log.debug(f"  Pattern : {pat}")
                    log.debug(f"  Raw key : {normalized}")
                    log.debug(f"  Service : {svc}")
                    log.debug(f"  Value   : ***")

                    self.service_rabbit_passwords[svc] = sv

        # ------------------------------------------------------------------
        # 2. OPERATOR-MANAGED RABBITMQ DISCOVERY (Barbican, Octavia, etc.)
        # ------------------------------------------------------------------
        OPERATOR_RABBIT_SECRET_RE = re.compile(
            r"^rabbitmq-(?P<svc>[a-z0-9_-]+)-default-user$"
        )

        for k, v in self._raw.items():
            if not isinstance(v, dict):
                continue

            m = OPERATOR_RABBIT_SECRET_RE.match(k)
            if not m:
                continue

            svc = m.group("svc")

            username = _as_str(v.get("username"))
            password = _as_str(v.get("password"))

            if not username or not password:
                continue

            log.debug("\n[RABBITMQ OPERATOR USER DETECTED]")
            log.debug("  Mode    : operator-managed")
            log.debug(f"  Service : {svc}")
            log.debug(f"  Secret  : {k}")
            log.debug(f"  User    : {username}")
            log.debug(f"  Pass    : ***")

            # Operator-managed RabbitMQ always wins
            self.service_rabbit_passwords[svc] = password


        # -------------------------
    # Kubernetes Secret creation
    # -------------------------

    def build_bundle_secret_object(self, *, namespace: Optional[str] = None) -> dict:
        """
        Creates ONE Secret (daalu-secrets) that contains all entries from secrets.yaml
        as stringData. This is convenient for debugging + traceability.
        """
        ns = namespace or self.default_namespace
        string_data = {k: _as_str(v) for k, v in self._raw.items() if v is not None}

        # Note: stringData is nicer than data since K8s will base64 it for you.
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": self.bundle_secret_name, "namespace": ns},
            "type": "Opaque",
            "stringData": string_data,
        }

    def build_specific_secret_object(
        self,
        *,
        name: str,
        namespace: Optional[str] = None,
        key_to_value: dict[str, str],
        source_keys: dict[str, str],
        secret_type: str = "Opaque",
    ) -> dict:
        """
        Creates a targeted Secret with explicit keys, and stores traceability refs.

        key_to_value: {"password": "..."}
        source_keys:  {"password": "keystone_database_password"}  # where it came from in secrets.yaml
        """
        ns = namespace or self.default_namespace

        for key, src in source_keys.items():
            self._trace.append(
                SecretRef(namespace=ns, name=name, key=key, source_key=src)
            )

        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": ns},
            "type": secret_type,
            "data": {k: _b64encode_str(v) for k, v in key_to_value.items()},
        }

    def traceability(self) -> list[SecretRef]:
        return list(self._trace)

    def ensure_k8s_secrets(self, kubectl) -> None:
        """
        Ensure base Kubernetes secrets exist.

        Currently:
        - Applies a single bundle secret containing all secrets.yaml values
        """
        # 1) Build the bundle Secret object
        secret_obj = self.build_bundle_secret_object(
            namespace=self.default_namespace
        )

        # 2) Apply via kubectl (idempotent)
        kubectl.apply_objects([secret_obj])

    @classmethod
    def from_yaml(
        cls,
        *,
        path: Path,
        namespace: str = "openstack",
        bundle_secret_name: str = "daalu-secrets",
    ) -> "SecretsManager":
        return cls(
            secrets_file=path,
            default_namespace=namespace,
            bundle_secret_name=bundle_secret_name,
        ).load()
