"""ConfigManagerExecutor — DeviceAdapter that drives NV-CM for a switch.

For NV-CM-enabled tenants, network-switch config changes route here
(``transport="config_manager"``) instead of the native NETCONF adapters.
The adapter reuses the existing approval gate verbatim:

* ``render(facts)`` serialises the intent to deterministic canonical text
  (the snapshot the gate stores + re-checks for drift).
* ``execute(creds, rendered)`` runs the NV-CM sequence over the tunnel:
  stage intended config → start ``DeployWorkflow`` → approve the
  ``perform_configuration_diff`` stage (the human already approved in the
  daalu UI, so the hub is the pre-approved executor) → poll to terminal.

The NV-CM connection (service URLs + Keycloak client) and the device's
Nautobot UUID don't fit the SSH-shaped ``Credentials`` fields, so
``resolve_credentials`` smuggles them through ``Credentials.extra``
(``nvcm_conn`` / ``device_uuid`` / ``filename``).

See docs/design/nv-config-manager-integration.md §9.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import ClassVar

import structlog

from daalu_automation.core.configmgr import (
    ConfigStoreClient,
    NvcmClientError,
    NvcmConn,
    TemporalWorkflowClient,
)
from daalu_automation.core.configmgr.client import DEPLOY_APPROVAL_STAGE
from daalu_automation.core.device.models import (
    ConfigDiff,
    Credentials,
    ExecutionResult,
    RenderedConfig,
)
from daalu_automation.core.device.registry import register_device_adapter
from daalu_automation.core.sot.models import DeviceFacts, NetworkFacts

logger = structlog.get_logger(__name__)

TRANSPORT = "config_manager"
RENDERER_VERSION = "config_manager.v1"
DEFAULT_FILENAME = "startup.yaml"

# Poll budget for the deploy workflow. NV-CM applies with commit-confirm +
# runs a backup child workflow, so allow generous slack on a tunnelled link.
_POLL_INTERVAL_S = 5.0
_POLL_TIMEOUT_S = 600.0


def _canonical_network_text(facts: NetworkFacts) -> str:
    """Deterministic text representation of NetworkFacts for the gate.

    Order-stable (sorted) so re-rendering identical intent yields an
    identical blob — that's what the drift re-check in
    ``change_proposals.execute`` compares.
    """
    lines: list[str] = []
    if facts.hostname:
        lines.append(f"hostname: {facts.hostname}")
    for itf in sorted(facts.interfaces, key=lambda i: i.name):
        lines.append(f"interface {itf.name}:")
        lines.append(f"  enabled: {str(itf.enabled).lower()}")
        if itf.description is not None:
            lines.append(f"  description: {itf.description}")
        if itf.mtu is not None:
            lines.append(f"  mtu: {itf.mtu}")
        if itf.ipv4_address is not None:
            lines.append(f"  ipv4: {itf.ipv4_address}")
        if itf.vlan_access is not None:
            lines.append(f"  vlan_access: {itf.vlan_access}")
    for vlan in sorted(facts.vlans, key=lambda v: v.vlan_id):
        lines.append(f"vlan {vlan.vlan_id}: {vlan.name or ''}".rstrip())
    for route in sorted(facts.static_routes, key=lambda r: (r.vrf or "", r.prefix)):
        vrf = f" vrf {route.vrf}" if route.vrf else ""
        lines.append(f"route {route.prefix} -> {route.next_hop}{vrf}")
    return "\n".join(lines) + "\n"


class ConfigManagerExecutor:
    """DeviceAdapter implementation backed by a per-tenant NV-CM stack."""

    transport: ClassVar[str] = TRANSPORT

    async def collect(
        self, creds: Credentials, intended_hint: DeviceFacts | None = None
    ) -> DeviceFacts:
        """Observed state for config_manager devices.

        NV-CM owns drift detection (its backup workflow + Config Store
        diff), and surfaces it to us over NATS (event bridge, §9.3) rather
        than us polling the device. For the synchronous reconciler path we
        therefore report the intended hint unchanged (no daalu-side drift),
        leaving NV-CM as the authority. Returns an empty NetworkFacts when
        no hint is supplied.
        """
        if isinstance(intended_hint, NetworkFacts):
            return intended_hint
        return NetworkFacts()

    async def render(self, intended: DeviceFacts) -> RenderedConfig:
        if not isinstance(intended, NetworkFacts):
            raise TypeError(
                f"ConfigManagerExecutor expects NetworkFacts, got {type(intended).__name__}"
            )
        text = _canonical_network_text(intended)
        return RenderedConfig(
            renderer_version=RENDERER_VERSION,
            files={DEFAULT_FILENAME: text},
            summary=f"{len(intended.interfaces)} interfaces, "
            f"{len(intended.vlans)} vlans, {len(intended.static_routes)} routes",
        )

    async def diff(
        self, observed: DeviceFacts, intended: DeviceFacts
    ) -> ConfigDiff:
        obs = _canonical_network_text(observed) if isinstance(observed, NetworkFacts) else ""
        new = _canonical_network_text(intended) if isinstance(intended, NetworkFacts) else ""
        return ConfigDiff(
            facts_changed=["network_config"] if obs != new else [],
            unified_diff="" if obs == new else f"--- observed\n+++ intended\n{new}",
            has_changes=obs != new,
        )

    async def execute(
        self, creds: Credentials, rendered: RenderedConfig
    ) -> ExecutionResult:
        started = datetime.now(tz=timezone.utc).isoformat()
        conn = creds.extra.get("nvcm_conn")
        device_uuid = creds.extra.get("device_uuid")
        filename = creds.extra.get("filename", DEFAULT_FILENAME)
        if not isinstance(conn, NvcmConn) or not device_uuid:
            return ExecutionResult(
                success=False,
                started_at=started,
                finished_at=datetime.now(tz=timezone.utc).isoformat(),
                error="missing nvcm_conn/device_uuid in credentials.extra",
            )

        content = rendered.files.get(filename) or next(iter(rendered.files.values()), "")
        steps: list[dict] = []
        try:
            cfg_client = ConfigStoreClient(conn)
            wf_client = TemporalWorkflowClient(conn)

            # 1. Stage intended config (versioned).
            put = await cfg_client.put_intended(
                device_uuid, filename, content,
                commit_message="daalu-automation approved change",
            )
            steps.append({"step": "stage_config", "version": put.get("version")})

            # 2. Start the deploy workflow.
            deploy = await wf_client.start_deploy(device_uuid)
            workflow_id = deploy.get("id") or deploy.get("workflow_id")
            if not workflow_id:
                raise NvcmClientError(f"deploy start returned no workflow id: {deploy}")
            steps.append({"step": "start_deploy", "workflow_id": workflow_id})

            # 3. Wait for the approval gate, then approve as pre-approved executor.
            approved = await self._await_and_approve(wf_client, workflow_id, steps)
            if not approved:
                raise NvcmClientError("deploy did not reach the approval stage in time")

            # 4. Poll to terminal.
            status = await self._poll_terminal(wf_client, workflow_id)
            steps.append({"step": "deploy_complete", "status": status})
            success = status == "COMPLETED"
            return ExecutionResult(
                success=success,
                started_at=started,
                finished_at=datetime.now(tz=timezone.utc).isoformat(),
                per_step=steps,
                error="" if success else f"workflow ended {status}",
            )
        except NvcmClientError as exc:
            return ExecutionResult(
                success=False,
                started_at=started,
                finished_at=datetime.now(tz=timezone.utc).isoformat(),
                per_step=steps,
                error=str(exc),
            )

    async def _await_and_approve(
        self, wf: TemporalWorkflowClient, workflow_id: str, steps: list[dict]
    ) -> bool:
        deadline = asyncio.get_event_loop().time() + _POLL_TIMEOUT_S
        while asyncio.get_event_loop().time() < deadline:
            detail = await wf.get_workflow(workflow_id)
            stage = self._find_stage(detail, DEPLOY_APPROVAL_STAGE)
            state = (stage or {}).get("state")
            status = detail.get("status") or detail.get("state")
            if status in {"COMPLETED", "FAILED", "TERMINATED", "CANCELED"}:
                return False
            if state == "PENDING_APPROVAL":
                await wf.approve_stage(workflow_id, DEPLOY_APPROVAL_STAGE)
                steps.append({"step": "approve_stage", "stage": DEPLOY_APPROVAL_STAGE})
                return True
            await asyncio.sleep(_POLL_INTERVAL_S)
        return False

    async def _poll_terminal(
        self, wf: TemporalWorkflowClient, workflow_id: str
    ) -> str:
        deadline = asyncio.get_event_loop().time() + _POLL_TIMEOUT_S
        while asyncio.get_event_loop().time() < deadline:
            detail = await wf.get_workflow(workflow_id)
            status = detail.get("status") or detail.get("state")
            if status in {"COMPLETED", "FAILED", "TERMINATED", "CANCELED"}:
                return str(status)
            await asyncio.sleep(_POLL_INTERVAL_S)
        return "TIMED_OUT"

    @staticmethod
    def _find_stage(detail: dict, name: str) -> dict | None:
        for stage in detail.get("stages", []) or []:
            if stage.get("name") == name:
                return stage
        return None


register_device_adapter(ConfigManagerExecutor)
