"""Build a Tinkerbell provisioning spec from a shared-Nautobot server Device.

The shared Nautobot (bundled in the NV-CM stack) models servers as
``Device`` rows (role=server) carrying MAC, BMC, and disk facts in
``Device.extra`` (Nautobot custom fields), plus day-1 OS intent in a
``daalu_intent`` Config Context. This module turns one such Device into a
:class:`ServerProvisionSpec` (Hardware + Rufio Machine + Workflow + power
tasks) the Tinkerbell executor applies.

CR shapes mirror the lower-level ``daalu`` project's
``tinkerbell_installer`` (``tinkerbell.org/v1alpha1`` Hardware,
``bmc.tinkerbell.org/v1alpha1`` Machine/Job). See
docs/design/nv-config-manager-integration.md §17.
"""

from __future__ import annotations

from typing import Any

from daalu_automation.core.device.tinkerbell_provision import ServerProvisionSpec
from daalu_automation.core.sot.models import Device

TINK_API = "tinkerbell.org/v1alpha1"
BMC_API = "bmc.tinkerbell.org/v1alpha1"


def build_provision_spec(
    device: Device,
    *,
    namespace: str = "tink-system",
    os_image_url: str,
    template_ref: str | None = None,
    cloud_init_userdata: str = "",
    disk: str = "/dev/sda",
) -> tuple[ServerProvisionSpec, dict[str, Any]]:
    """Build (spec, bmc_secret) for provisioning ``device`` via Tinkerbell.

    Reads BMC + NIC facts from ``device.extra``:
      ``mac`` (provisioning NIC MAC), ``bmc_ip``, ``bmc_user``,
      ``bmc_password`` (the caller is expected to have decrypted it),
      optional ``disk``.

    Returns the spec plus a core v1 BMC ``Secret`` body the caller applies
    out of band (the Tinkerbell executor only drives CRs). The Hardware CR
    references the Hegel/SMEE-served metadata; ``cloud_init_userdata`` is
    the day-1 OS config (users/keys/hostname) the host applies at first boot.
    """
    extra = device.extra or {}
    name = _safe_name(device.name or device.id)
    mac = extra.get("mac", "")
    bmc_ip = extra.get("bmc_ip", "")
    bmc_user = extra.get("bmc_user", "admin")
    bmc_password = extra.get("bmc_password", "")
    disk = extra.get("disk", disk)
    workflow_name = f"{name}-provision"
    machine_name = f"{name}-bmc"
    secret_name = f"{name}-bmc-creds"

    bmc_secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": secret_name, "namespace": namespace},
        "type": "Opaque",
        "stringData": {"username": bmc_user, "password": bmc_password},
    }

    rufio_machine = {
        "apiVersion": BMC_API,
        "kind": "Machine",
        "metadata": {"name": machine_name, "namespace": namespace},
        "spec": {
            "connection": {
                "host": bmc_ip,
                "authSecretRef": {"name": secret_name, "namespace": namespace},
                "insecureTLS": True,
                "providerOptions": {"redfish": {"port": 443}},
            }
        },
    }

    hardware = {
        "apiVersion": TINK_API,
        "kind": "Hardware",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "disks": [{"device": disk}],
            "metadata": {
                "facility": {"facility_code": "onprem"},
                "instance": {
                    "hostname": device.name or name,
                    "id": device.id,
                    "operating_system": {"version": "ubuntu"},
                },
            },
            "interfaces": [
                {
                    "dhcp": {
                        "mac": mac,
                        "hostname": device.name or name,
                        "ip": (
                            {"address": device.primary_ip}
                            if device.primary_ip
                            else {}
                        ),
                        "arch": "x86_64",
                        "uefi": True,
                    },
                    "netboot": {"allowPXE": True, "allowWorkflow": True},
                }
            ],
        },
    }

    workflow = {
        "apiVersion": TINK_API,
        "kind": "Workflow",
        "metadata": {"name": workflow_name, "namespace": namespace},
        "spec": {
            "templateRef": template_ref or f"{name}-template",
            "hardwareRef": name,
            "hardwareMap": {"device": mac},
        },
    }

    # Rufio power sequence: set one-time PXE boot (UEFI) then power on.
    power_job_tasks = [
        {"powerAction": "off"},
        {"oneTimeBootDeviceAction": {"device": ["pxe"], "efiBoot": True}},
        {"powerAction": "on"},
    ]

    spec = ServerProvisionSpec(
        hardware_name=name,
        workflow_name=workflow_name,
        rufio_machine_name=machine_name,
        bmc_secret=bmc_secret,
        rufio_machine=rufio_machine,
        hardware=hardware,
        template=None if template_ref else _default_template(name, namespace, os_image_url, disk, cloud_init_userdata),
        workflow=workflow,
        power_job_tasks=power_job_tasks,
    )
    return spec, bmc_secret


def _default_template(
    name: str, namespace: str, os_image_url: str, disk: str, userdata: str
) -> dict[str, Any]:
    """A minimal stream-image + cloud-init Tinkerbell Template.

    Mirrors the common HookOS action set: stream the OS image to disk and
    write the cloud-init user-data so the host boots daalu-ready (day-1).
    Day-2 OS config is then owned by daalu's ``linux_ssh`` adapter.
    """
    tasks_yaml = f"""version: "0.1"
name: {name}
global_timeout: 1800
tasks:
  - name: "os-install"
    worker: "{{{{.device_1}}}}"
    volumes:
      - /dev:/dev
      - /statedir:/statedir
    actions:
      - name: "stream-image"
        image: quay.io/tinkerbell/actions/image2disk:latest
        timeout: 600
        environment:
          DEST_DISK: {disk}
          IMG_URL: "{os_image_url}"
          COMPRESSED: true
      - name: "write-cloud-init"
        image: quay.io/tinkerbell/actions/writefile:latest
        timeout: 90
        environment:
          DEST_DISK: {disk}1
          DEST_PATH: /var/lib/cloud/seed/nocloud/user-data
          CONTENTS: |
{_indent(userdata, 12)}
          MODE: 0644
"""
    return {
        "apiVersion": TINK_API,
        "kind": "Template",
        "metadata": {"name": f"{name}-template", "namespace": namespace},
        "spec": {"data": tasks_yaml},
    }


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in (text or "").splitlines()) or (pad + "#cloud-config")


def _safe_name(raw: str) -> str:
    """K8s-safe object name from a device name/UUID."""
    out = "".join(c if c.isalnum() or c == "-" else "-" for c in raw.lower())
    return out.strip("-")[:253] or "host"
