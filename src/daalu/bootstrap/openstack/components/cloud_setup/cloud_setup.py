# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/openstack/components/cloud_setup/cloud_setup.py

from __future__ import annotations

import shlex
import time
import logging
from pathlib import Path
from typing import Optional

from daalu.bootstrap.engine.component import InfraComponent
from daalu.utils.helpers import build_openstack_endpoints

log = logging.getLogger("daalu")


class CloudSetupComponent(InfraComponent):
    """
    Post-install cloud smoke-test component.

    Runs after the full OpenStack installation and verifies the cloud is
    functional by creating a minimal set of resources:

    1. Glance image  — Ubuntu 22.04 (downloaded from URL into pod, then uploaded)
    2. Private network + subnet  (10.0.2.0/24 by default)
    3. Public network + subnet   (flat provider, no DHCP)
    4. Router connecting private to public
    5. Security group with ICMP + SSH rules
    6. Optionally a test VM (requires vm_key_name to be set)

    All steps are idempotent: resources that already exist are silently skipped.
    """

    def __init__(
        self,
        *,
        kubeconfig: str,
        namespace: str = "openstack",
        secrets_path: Path,
        keystone_public_host: str,
        # Glance image
        image_name: str = "ubuntu-22.04",
        image_url: str = (
            "https://cloud-images.ubuntu.com/jammy/current/"
            "jammy-server-cloudimg-amd64.img"
        ),
        image_disk_format: str = "qcow2",
        # Private network
        private_network_name: str = "private-net",
        private_subnet_name: str = "private-subnet",
        private_subnet_cidr: str = "10.0.2.0/24",
        private_subnet_gateway: str = "10.0.2.1",
        dns_nameservers: Optional[list[str]] = None,
        # Public (external) network
        public_network_name: str = "public-net",
        public_subnet_name: str = "public-subnet",
        public_subnet_cidr: str = "192.168.0.0/24",
        public_subnet_gateway: str = "192.168.0.1",
        public_alloc_start: str = "192.168.0.200",
        public_alloc_end: str = "192.168.0.250",
        physical_network: str = "provider",
        # Router
        router_name: str = "router1",
        # Security group
        security_group_name: str = "vm-secgroup",
        # VM
        vm_name: str = "test-vm1",
        vm_flavor: str = "m1.large",
        vm_key_name: Optional[str] = None,
        create_vm: bool = True,
    ):
        super().__init__(
            name="cloud-setup",
            repo_name="",
            repo_url="",
            chart="",
            version=None,
            namespace=namespace,
            release_name="cloud-setup",
            local_chart_dir=None,
            remote_chart_dir=None,
            kubeconfig=kubeconfig,
            uses_helm=False,
            wait_for_pods=False,
            min_running_pods=0,
            enable_argocd=False,
            istio_enabled=False,
        )

        self.secrets_path = secrets_path
        self.keystone_public_host = keystone_public_host

        self.image_name = image_name
        self.image_url = image_url
        self.image_disk_format = image_disk_format

        self.private_network_name = private_network_name
        self.private_subnet_name = private_subnet_name
        self.private_subnet_cidr = private_subnet_cidr
        self.private_subnet_gateway = private_subnet_gateway
        self.dns_nameservers = dns_nameservers or ["8.8.8.8"]

        self.public_network_name = public_network_name
        self.public_subnet_name = public_subnet_name
        self.public_subnet_cidr = public_subnet_cidr
        self.public_subnet_gateway = public_subnet_gateway
        self.public_alloc_start = public_alloc_start
        self.public_alloc_end = public_alloc_end
        self.physical_network = physical_network

        self.router_name = router_name
        self.security_group_name = security_group_name

        self.vm_name = vm_name
        self.vm_flavor = vm_flavor
        self.vm_key_name = vm_key_name
        self.create_vm = create_vm

    # ------------------------------------------------------------------ #
    # Base class overrides                                                 #
    # ------------------------------------------------------------------ #

    def values(self) -> dict:
        return {}

    def pre_install(self, kubectl) -> None:
        log.debug("[cloud-setup] Loading OpenStack endpoints...")
        self._computed_endpoints = build_openstack_endpoints(
            kubectl=kubectl,
            secrets_path=self.secrets_path,
            namespace=self.namespace,
            region_name="RegionOne",
            keystone_public_host=self.keystone_public_host,
            service="keystone",
        )
        log.debug("[cloud-setup] Endpoints loaded")

    def post_install(self, kubectl) -> None:
        log.debug("[cloud-setup] Starting post-install smoke test...")

        pod = self._get_keystone_api_pod(kubectl)
        env_prefix = self._build_env_prefix()

        self._create_glance_image(kubectl, pod, env_prefix)
        self._create_private_network(kubectl, pod, env_prefix)
        self._create_public_network(kubectl, pod, env_prefix)
        self._create_router(kubectl, pod, env_prefix)
        self._create_security_group(kubectl, pod, env_prefix)

        if self.create_vm:
            if self.vm_key_name:
                self._create_test_vm(kubectl, pod, env_prefix)
            else:
                log.debug(
                    "[cloud-setup] Skipping VM creation: vm_key_name not configured"
                )

        log.debug("[cloud-setup] Smoke test complete")

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _build_openrc_env(self) -> dict[str, str]:
        admin = self._computed_endpoints["identity"]["auth"]["admin"]
        host = self._computed_endpoints["identity"]["hosts"]["default"]
        port = self._computed_endpoints["identity"]["port"]["api"]["default"]
        return {
            "OS_IDENTITY_API_VERSION": "3",
            "OS_AUTH_URL": f"http://{host}:{port}/v3",
            "OS_REGION_NAME": admin["region_name"],
            "OS_INTERFACE": "internal",
            "OS_PROJECT_DOMAIN_NAME": admin["project_domain_name"],
            "OS_PROJECT_NAME": admin["project_name"],
            "OS_USER_DOMAIN_NAME": admin["user_domain_name"],
            "OS_USERNAME": admin["username"],
            "OS_PASSWORD": admin["password"],
            "OS_DEFAULT_DOMAIN": admin.get("default_domain_id", "default"),
        }

    def _build_env_prefix(self) -> str:
        return " ".join(
            f"{k}={shlex.quote(v)}" for k, v in self._build_openrc_env().items()
        )

    def _get_keystone_api_pod(self, kubectl) -> str:
        pods = kubectl.get_pods(self.namespace)
        for pod in pods:
            labels = pod.get("metadata", {}).get("labels", {})
            if (
                labels.get("application") == "keystone"
                and labels.get("component") == "api"
            ):
                return pod["metadata"]["name"]
        raise RuntimeError("[cloud-setup] No keystone-api pod found")

    def _run_os(
        self,
        kubectl,
        pod: str,
        env_prefix: str,
        cmd: str,
        *,
        retries: int = 3,
        delay: int = 5,
    ) -> tuple[int, str, str]:
        """Run an openstack CLI command inside the keystone-api pod."""
        full_cmd = (
            f"exec {pod} -n {self.namespace} -c keystone-api -- "
            f"env {env_prefix} openstack {cmd}"
        )
        for attempt in range(retries):
            rc, out, err = kubectl._run(full_cmd)
            if rc == 0:
                return rc, out, err
            if attempt < retries - 1:
                time.sleep(delay)
        return rc, out, err

    def _exec_pod_shell(
        self,
        kubectl,
        pod: str,
        script: str,
    ) -> tuple[int, str, str]:
        """Run an arbitrary shell command inside the keystone-api pod."""
        full_cmd = (
            f"exec {pod} -n {self.namespace} -c keystone-api -- "
            f"bash -c {shlex.quote(script)}"
        )
        return kubectl._run(full_cmd)

    def _resource_exists(
        self,
        kubectl,
        pod: str,
        env_prefix: str,
        resource_type: str,
        name: str,
    ) -> bool:
        rc, _, _ = self._run_os(kubectl, pod, env_prefix, f"{resource_type} show {shlex.quote(name)} -f value -c id")
        return rc == 0

    # ------------------------------------------------------------------ #
    # Step 1: Glance image                                                 #
    # ------------------------------------------------------------------ #

    def _create_glance_image(self, kubectl, pod: str, env_prefix: str) -> None:
        if self._resource_exists(kubectl, pod, env_prefix, "image", self.image_name):
            log.debug(f"[cloud-setup] Image '{self.image_name}' already exists, skipping")
            return

        log.debug(f"[cloud-setup] Downloading {self.image_url} into keystone pod...")
        tmp_path = f"/tmp/daalu-{self.image_name}.img"

        script = (
            f"curl -sSL -o {shlex.quote(tmp_path)} {shlex.quote(self.image_url)} && "
            f"env {env_prefix} openstack image create {shlex.quote(self.image_name)} "
            f"--disk-format {shlex.quote(self.image_disk_format)} "
            f"--container-format bare "
            f"--public "
            f"--file {shlex.quote(tmp_path)} && "
            f"rm -f {shlex.quote(tmp_path)}"
        )

        rc, out, err = self._exec_pod_shell(kubectl, pod, script)
        if rc != 0:
            raise RuntimeError(
                f"[cloud-setup] Failed to create image '{self.image_name}': {err or out}"
            )
        log.debug(f"[cloud-setup] Image '{self.image_name}' created")

    # ------------------------------------------------------------------ #
    # Step 2: Private network + subnet                                     #
    # ------------------------------------------------------------------ #

    def _create_private_network(self, kubectl, pod: str, env_prefix: str) -> None:
        if not self._resource_exists(kubectl, pod, env_prefix, "network", self.private_network_name):
            rc, out, err = self._run_os(
                kubectl, pod, env_prefix,
                f"network create {shlex.quote(self.private_network_name)}",
            )
            if rc != 0:
                raise RuntimeError(
                    f"[cloud-setup] Failed to create private network: {err or out}"
                )
            log.debug(f"[cloud-setup] Private network '{self.private_network_name}' created")
        else:
            log.debug(f"[cloud-setup] Private network '{self.private_network_name}' already exists")

        if not self._resource_exists(kubectl, pod, env_prefix, "subnet", self.private_subnet_name):
            dns_args = " ".join(
                f"--dns-nameserver {shlex.quote(ns)}" for ns in self.dns_nameservers
            )
            rc, out, err = self._run_os(
                kubectl, pod, env_prefix,
                f"subnet create {shlex.quote(self.private_subnet_name)} "
                f"--network {shlex.quote(self.private_network_name)} "
                f"--subnet-range {shlex.quote(self.private_subnet_cidr)} "
                f"--gateway {shlex.quote(self.private_subnet_gateway)} "
                f"{dns_args}",
            )
            if rc != 0:
                raise RuntimeError(
                    f"[cloud-setup] Failed to create private subnet: {err or out}"
                )
            log.debug(f"[cloud-setup] Private subnet '{self.private_subnet_name}' created")
        else:
            log.debug(f"[cloud-setup] Private subnet '{self.private_subnet_name}' already exists")

    # ------------------------------------------------------------------ #
    # Step 3: Public (external) network + subnet                          #
    # ------------------------------------------------------------------ #

    def _create_public_network(self, kubectl, pod: str, env_prefix: str) -> None:
        if not self._resource_exists(kubectl, pod, env_prefix, "network", self.public_network_name):
            rc, out, err = self._run_os(
                kubectl, pod, env_prefix,
                f"network create {shlex.quote(self.public_network_name)} "
                f"--external "
                f"--provider-network-type flat "
                f"--provider-physical-network {shlex.quote(self.physical_network)}",
            )
            if rc != 0:
                raise RuntimeError(
                    f"[cloud-setup] Failed to create public network: {err or out}"
                )
            log.debug(f"[cloud-setup] Public network '{self.public_network_name}' created")
        else:
            log.debug(f"[cloud-setup] Public network '{self.public_network_name}' already exists")

        if not self._resource_exists(kubectl, pod, env_prefix, "subnet", self.public_subnet_name):
            rc, out, err = self._run_os(
                kubectl, pod, env_prefix,
                f"subnet create {shlex.quote(self.public_subnet_name)} "
                f"--network {shlex.quote(self.public_network_name)} "
                f"--subnet-range {shlex.quote(self.public_subnet_cidr)} "
                f"--gateway {shlex.quote(self.public_subnet_gateway)} "
                f"--no-dhcp "
                f"--allocation-pool start={self.public_alloc_start},end={self.public_alloc_end}",
            )
            if rc != 0:
                raise RuntimeError(
                    f"[cloud-setup] Failed to create public subnet: {err or out}"
                )
            log.debug(f"[cloud-setup] Public subnet '{self.public_subnet_name}' created")
        else:
            log.debug(f"[cloud-setup] Public subnet '{self.public_subnet_name}' already exists")

    # ------------------------------------------------------------------ #
    # Step 4: Router                                                       #
    # ------------------------------------------------------------------ #

    def _create_router(self, kubectl, pod: str, env_prefix: str) -> None:
        if not self._resource_exists(kubectl, pod, env_prefix, "router", self.router_name):
            rc, out, err = self._run_os(
                kubectl, pod, env_prefix,
                f"router create {shlex.quote(self.router_name)}",
            )
            if rc != 0:
                raise RuntimeError(
                    f"[cloud-setup] Failed to create router: {err or out}"
                )
            log.debug(f"[cloud-setup] Router '{self.router_name}' created")
        else:
            log.debug(f"[cloud-setup] Router '{self.router_name}' already exists")

        # Add private subnet interface (idempotent — error on duplicate is ignored)
        rc, _, err = self._run_os(
            kubectl, pod, env_prefix,
            f"router add subnet {shlex.quote(self.router_name)} "
            f"{shlex.quote(self.private_subnet_name)}",
        )
        if rc != 0 and "already exists" not in (err or "").lower():
            log.debug(
                f"[cloud-setup] router add subnet returned rc={rc}: {err} (may already be attached)"
            )

        # Set external gateway
        rc, _, err = self._run_os(
            kubectl, pod, env_prefix,
            f"router set {shlex.quote(self.router_name)} "
            f"--external-gateway {shlex.quote(self.public_network_name)}",
        )
        if rc != 0:
            log.debug(
                f"[cloud-setup] router set external-gateway returned rc={rc}: {err}"
            )

        log.debug(f"[cloud-setup] Router '{self.router_name}' configured")

    # ------------------------------------------------------------------ #
    # Step 5: Security group                                               #
    # ------------------------------------------------------------------ #

    def _create_security_group(self, kubectl, pod: str, env_prefix: str) -> None:
        if not self._resource_exists(kubectl, pod, env_prefix, "security group", self.security_group_name):
            rc, out, err = self._run_os(
                kubectl, pod, env_prefix,
                f"security group create {shlex.quote(self.security_group_name)}",
            )
            if rc != 0:
                raise RuntimeError(
                    f"[cloud-setup] Failed to create security group: {err or out}"
                )
            log.debug(f"[cloud-setup] Security group '{self.security_group_name}' created")

            # ICMP rule
            rc, _, err = self._run_os(
                kubectl, pod, env_prefix,
                f"security group rule create "
                f"--proto icmp "
                f"{shlex.quote(self.security_group_name)}",
            )
            if rc != 0:
                log.debug(f"[cloud-setup] WARNING: ICMP rule creation failed: {err}")

            # SSH rule (TCP 22)
            rc, _, err = self._run_os(
                kubectl, pod, env_prefix,
                f"security group rule create "
                f"--proto tcp --dst-port 22 "
                f"{shlex.quote(self.security_group_name)}",
            )
            if rc != 0:
                log.debug(f"[cloud-setup] WARNING: SSH rule creation failed: {err}")

            log.debug(f"[cloud-setup] Security group rules added (ICMP + SSH)")
        else:
            log.debug(f"[cloud-setup] Security group '{self.security_group_name}' already exists")

    # ------------------------------------------------------------------ #
    # Step 6: Test VM                                                      #
    # ------------------------------------------------------------------ #

    def _create_test_vm(self, kubectl, pod: str, env_prefix: str) -> None:
        if self._resource_exists(kubectl, pod, env_prefix, "server", self.vm_name):
            log.debug(f"[cloud-setup] VM '{self.vm_name}' already exists, skipping")
            return

        cmd = (
            f"server create {shlex.quote(self.vm_name)} "
            f"--flavor {shlex.quote(self.vm_flavor)} "
            f"--image {shlex.quote(self.image_name)} "
            f"--network {shlex.quote(self.private_network_name)} "
            f"--key-name {shlex.quote(self.vm_key_name)} "
            f"--security-group {shlex.quote(self.security_group_name)} "
            f"--wait"
        )
        rc, out, err = self._run_os(kubectl, pod, env_prefix, cmd, retries=1)
        if rc != 0:
            raise RuntimeError(
                f"[cloud-setup] Failed to create VM '{self.vm_name}': {err or out}"
            )
        log.debug(f"[cloud-setup] VM '{self.vm_name}' created")
