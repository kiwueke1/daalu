# src/daalu/bootstrap/openstack/components/openstack_cli/openstack_cli.py

from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from typing import Optional

from daalu.bootstrap.engine.component import InfraComponent
from daalu.utils.helpers import build_openstack_endpoints
import logging

log = logging.getLogger("daalu")


# Old system packages to uninstall (matching Ansible defaults/main.yml)
OPENSTACK_CLI_PACKAGES = [
    "python3-barbicanclient",
    "python3-designateclient",
    "python3-glanceclient",
    "python3-heatclient",
    "python3-magnumclient",
    "python3-manilaclient",
    "python3-neutronclient",
    "python3-novaclient",
    "python3-octaviaclient",
    "python3-openstackclient",
    "python3-osc-placement",
    "python3-swiftclient",
]

# Default container image for OpenStack CLI
DEFAULT_CLI_IMAGE = "docker.io/openstackhelm/openstack-client:2024.1-ubuntu_jammy"


class OpenStackCliComponent(InfraComponent):
    """
    Daalu OpenStack CLI component (host-level CLI configuration).

    Runs locally by default.
    If `ssh` is provided, executes remotely via SSH.

    Responsibilities:
    - Uninstall old OpenStack client system packages
    - Remove Ubuntu Cloud Archive repository and keyring
    - Generate /root/openrc with admin credentials
    - Generate /etc/profile.d/atmosphere.sh with containerized CLI aliases
    """

    def __init__(
        self,
        *,
        kubeconfig: str,
        namespace: str = "openstack",
        secrets_path: Path,
        keystone_public_host: str,
        cli_image: Optional[str] = None,
        ssh=None,  # Optional SSH runner
    ):
        super().__init__(
            name="openstack-cli",
            repo_name="local",
            repo_url="",
            chart="",
            version=None,
            namespace=namespace,
            release_name="openstack-cli",
            local_chart_dir=None,
            remote_chart_dir=None,
            kubeconfig=kubeconfig,
            uses_helm=False,
        )

        self._ssh = ssh  # If None → run locally
        self.secrets_path = secrets_path
        self.keystone_public_host = keystone_public_host
        self._cli_image = cli_image or DEFAULT_CLI_IMAGE

        self.wait_for_pods = False
        self.min_running_pods = 0
        self.enable_argocd = False

    # ================================================================
    # Unified command runner (local by default, SSH if provided)
    # ================================================================
    def _run(self, cmd: str, sudo: bool = False):
        if self._ssh:
            return self._ssh.run(cmd, sudo=sudo)

        # Local execution
        full_cmd = f"sudo {cmd}" if sudo else cmd
        result = subprocess.run(
            full_cmd,
            shell=True,
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout, result.stderr

    # ================================================================
    # Uninstall old OpenStack client packages
    # ================================================================
    def _uninstall_old_packages(self):
        log.debug("[openstack-cli] Uninstalling old OpenStack client packages...")

        packages = " ".join(OPENSTACK_CLI_PACKAGES)
        self._run(
            f"DEBIAN_FRONTEND=noninteractive apt-get remove -y {packages} 2>/dev/null || true",
            sudo=True,
        )

        log.debug("[openstack-cli] Removing Ubuntu Cloud Archive keyring...")
        self._run(
            "DEBIAN_FRONTEND=noninteractive apt-get remove -y ubuntu-cloud-keyring 2>/dev/null || true",
            sudo=True,
        )

        log.debug("[openstack-cli] Removing Ubuntu Cloud Archive repository...")
        self._run(
            "rm -f /etc/apt/sources.list.d/ubuntu-cloud-archive*.list 2>/dev/null || true",
            sudo=True,
        )

        log.debug("[openstack-cli] Old client cleanup complete")

    # ================================================================
    # Generate /root/openrc
    # ================================================================
    def _generate_openrc(self):
        log.debug("[openstack-cli] Generating /root/openrc...")

        admin = self._computed_endpoints["identity"]["auth"]["admin"]

        openrc_content = (
            "# Managed by Daalu\n"
            "\n"
            "export OS_IDENTITY_API_VERSION=3\n"
            "\n"
            f'export OS_AUTH_URL="https://{self.keystone_public_host}/v3"\n'
            "export OS_AUTH_TYPE=password\n"
            f'export OS_REGION_NAME="{admin["region_name"]}"\n'
            "export OS_USER_DOMAIN_NAME=Default\n"
            f'export OS_USERNAME="{admin["username"]}"\n'
            f'export OS_PASSWORD="{admin["password"]}"\n'
            "export OS_PROJECT_DOMAIN_NAME=Default\n"
            "export OS_PROJECT_NAME=admin\n"
        )

        cmd = f"cat > /root/openrc << 'DAALU_EOF'\n{openrc_content}DAALU_EOF"
        rc, _, err = self._run(cmd, sudo=True)

        if rc != 0:
            raise RuntimeError(f"[openstack-cli] Failed to write /root/openrc: {err}")

        self._run("chmod 600 /root/openrc", sudo=True)

        log.debug("[openstack-cli] /root/openrc generated successfully")

    # ================================================================
    # Generate /etc/profile.d/atmosphere.sh
    # ================================================================
    def _generate_aliases(self):
        log.debug("[openstack-cli] Generating /etc/profile.d/atmosphere.sh...")

        atmosphere_sh = (
            "# Managed by Daalu\n"
            "\n"
            f"alias osc='nerdctl run --rm --network host \\\n"
            f"      --volume $PWD:/opt --volume /tmp:/tmp \\\n"
            f"      --volume /etc/openstack:/etc/openstack:ro \\\n"
            f"      --env-file <(env | grep OS_) \\\n"
            f"      {self._cli_image}'\n"
            "alias openstack='osc openstack'\n"
            "alias nova='osc nova'\n"
            "alias neutron='osc neutron'\n"
            "alias cinder='osc cinder'\n"
            "alias glance='osc glance'\n"
        )

        content_b64 = base64.b64encode(atmosphere_sh.encode()).decode()
        rc, _, err = self._run(
            f"echo -n {content_b64} | base64 -d > /etc/profile.d/atmosphere.sh",
            sudo=True,
        )

        if rc != 0:
            raise RuntimeError(
                f"[openstack-cli] Failed to write /etc/profile.d/atmosphere.sh: {err}"
            )

        self._run("chmod 644 /etc/profile.d/atmosphere.sh", sudo=True)

        log.debug("[openstack-cli] CLI aliases generated successfully")

    # ================================================================
    # pre_install
    # ================================================================
    def pre_install(self, kubectl):
        log.debug("[openstack-cli] Starting pre-install...")

        # 1) Cleanup old client packages
        self._uninstall_old_packages()

        # 2) Build OpenStack endpoints (identity only)
        log.debug("[openstack-cli] Building OpenStack endpoints...")
        self._computed_endpoints = build_openstack_endpoints(
            kubectl=kubectl,
            secrets_path=self.secrets_path,
            namespace=self.namespace,
            region_name="RegionOne",
            keystone_public_host=self.keystone_public_host,
            service="keystone",
        )

        log.debug("[openstack-cli] OpenStack endpoints ready")

        # 3) Generate openrc
        self._generate_openrc()

        # 4) Generate CLI aliases
        self._generate_aliases()

        log.debug("[openstack-cli] pre-install complete")
