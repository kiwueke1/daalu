"""Tinkerbell server provisioning — apply CRs + watch to completion.

This is the *imperative* server-lifecycle executor (kind ``provision_op``),
distinct from the declarative ``DeviceAdapter`` family: it doesn't render
config text or drift-check, it applies a set of Tinkerbell/Rufio CRs to the
mgmt cluster and watches them to a terminal state.

The spec carries explicit CR bodies (built from the shared Nautobot SoT in
Increment 6, or by an agent/wizard). The executor applies them in order:
BMC Secret → Rufio Machine → Hardware → Template → Workflow, then a Rufio
power Job (PXE-once + power-on), and watches the Workflow to STATE_SUCCESS.

See docs/design/nv-config-manager-integration.md §17, §18.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog
from pydantic import BaseModel, Field

from daalu_automation.core.device.models import ExecutionResult
from daalu_automation.core.tinkerbell import TinkerbellClient, TinkerbellError

logger = structlog.get_logger(__name__)

_POLL_INTERVAL_S = 10.0
_POLL_TIMEOUT_S = 1800.0  # bare-metal image + reboot can take many minutes


class ServerProvisionSpec(BaseModel):
    """Everything needed to provision one bare-metal host via Tinkerbell.

    CR bodies are explicit dicts so the construction logic (from Nautobot)
    lives in the SoT layer, not here. ``power_job_tasks`` is the ordered
    Rufio task list (set PXE boot + power on); the executor wraps it in a
    Job CR. Names are used for status polling.
    """

    hardware_name: str
    workflow_name: str
    # Optional CR bodies — omit any the caller pre-created out of band.
    bmc_secret: dict[str, Any] | None = None
    rufio_machine: dict[str, Any] | None = None
    hardware: dict[str, Any] | None = None
    template: dict[str, Any] | None = None
    workflow: dict[str, Any] | None = None
    rufio_machine_name: str | None = None
    power_job_tasks: list[dict[str, Any]] = Field(default_factory=list)


async def provision(
    spec: ServerProvisionSpec,
    *,
    kubeconfig: dict[str, Any] | None,
    namespace: str,
) -> ExecutionResult:
    """Apply the spec's CRs over the tunnel and watch to completion."""
    started = datetime.now(tz=timezone.utc).isoformat()
    steps: list[dict[str, Any]] = []
    try:
        async with TinkerbellClient(kubeconfig=kubeconfig, namespace=namespace) as tk:
            # Apply in dependency order. Each is optional so a caller can
            # re-provision an already-registered host (Hardware/Machine
            # already exist) by sending only the Workflow + power Job.
            #
            # NB: ``bmc_secret`` (a core v1 Secret holding BMC creds) is
            # out of this CR client's scope — the caller pre-creates it (or
            # the SoT layer applies it via the generic kube helper) before
            # provisioning. We only drive the Tinkerbell/Rufio CRs here.
            if spec.rufio_machine is not None:
                await tk.apply("Machine", spec.rufio_machine)
                steps.append({"step": "apply_rufio_machine"})
            if spec.hardware is not None:
                await tk.apply("Hardware", spec.hardware)
                steps.append({"step": "apply_hardware"})
            if spec.template is not None:
                await tk.apply("Template", spec.template)
                steps.append({"step": "apply_template"})
            if spec.workflow is not None:
                await tk.apply("Workflow", spec.workflow)
                steps.append({"step": "apply_workflow"})

            # Power the node into PXE to start the workflow.
            if spec.power_job_tasks:
                job_name = f"{spec.hardware_name}-provision-poweron"
                machine_ref = spec.rufio_machine_name or spec.hardware_name
                job = tk.build_power_job(
                    name=job_name,
                    machine_ref=machine_ref,
                    tasks=spec.power_job_tasks,
                    namespace=namespace,
                )
                await tk.apply("Job", job)
                steps.append({"step": "apply_power_job", "job": job_name})
                job_state = await _watch(
                    lambda: tk.rufio_job_state(job_name),
                    terminal={"Completed", "Failed"},
                )
                steps.append({"step": "power_job", "state": job_state})
                if job_state != "Completed":
                    return _result(started, False, steps, f"power job {job_state}")

            # Watch the provisioning workflow to success.
            wf_state = await _watch(
                lambda: tk.workflow_state(spec.workflow_name),
                terminal={"STATE_SUCCESS", "STATE_FAILED", "STATE_TIMEOUT"},
            )
            steps.append({"step": "workflow", "state": wf_state})
            success = wf_state == "STATE_SUCCESS"
            return _result(
                started, success, steps,
                "" if success else f"workflow {wf_state}",
            )
    except TinkerbellError as exc:
        return _result(started, False, steps, str(exc))


async def _watch(getter, *, terminal: set[str]) -> str | None:
    """Poll ``getter()`` until it returns a value in ``terminal`` or timeout."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + _POLL_TIMEOUT_S
    last: str | None = None
    while loop.time() < deadline:
        last = await getter()
        if last in terminal:
            return last
        await asyncio.sleep(_POLL_INTERVAL_S)
    return last or "TIMED_OUT"


def _result(
    started: str, success: bool, steps: list[dict[str, Any]], error: str
) -> ExecutionResult:
    return ExecutionResult(
        success=success,
        started_at=started,
        finished_at=datetime.now(tz=timezone.utc).isoformat(),
        per_step=steps,
        error=error,
    )
