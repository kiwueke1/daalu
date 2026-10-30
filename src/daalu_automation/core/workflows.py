"""Pluggable, idempotent automation workflows.

A workflow is just an ``async def`` registered under a name. Modules can
implement multi-step automations (e.g. drain node → reschedule pods →
verify rollout → notify Slack) by composing primitives.

For Phase-1 we keep this in-process so the same code path runs in tests
and in the worker. A future Temporal-backed implementation can swap the
runner without changing the workflow signatures.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import structlog

from daalu_automation.config import DEFAULT_TENANT_ID
from daalu_automation.database import AsyncSessionLocal
from daalu_automation.models import WorkflowRun, WorkflowRunStatus

logger = structlog.get_logger(__name__)

WorkflowFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

_WORKFLOWS: dict[str, tuple[str, WorkflowFn]] = {}


def register_workflow(name: str, *, module: str) -> Callable[[WorkflowFn], WorkflowFn]:
    def _wrap(fn: WorkflowFn) -> WorkflowFn:
        _WORKFLOWS[name] = (module, fn)
        logger.info("workflow.registered", name=name, module=module)
        return fn

    return _wrap


def list_workflows() -> list[tuple[str, str]]:
    return [(name, module) for name, (module, _) in _WORKFLOWS.items()]


async def run_workflow(
    name: str,
    input_payload: dict[str, Any] | None = None,
    *,
    tenant_id: uuid.UUID | None = None,
) -> uuid.UUID:
    module, fn = _WORKFLOWS[name]
    run = WorkflowRun(
        tenant_id=tenant_id or DEFAULT_TENANT_ID,
        workflow_name=name,
        module=module,
        status=WorkflowRunStatus.running,
        started_at=datetime.now(tz=timezone.utc),
        input_payload=input_payload or {},
    )
    async with AsyncSessionLocal() as db:
        db.add(run)
        await db.commit()
        await db.refresh(run)

    try:
        output = await fn(input_payload or {})
        async with AsyncSessionLocal() as db:
            run_obj = await db.get(WorkflowRun, run.id)
            run_obj.status = WorkflowRunStatus.succeeded
            run_obj.finished_at = datetime.now(tz=timezone.utc)
            run_obj.output_payload = output
            await db.commit()
    except Exception as e:  # noqa: BLE001
        async with AsyncSessionLocal() as db:
            run_obj = await db.get(WorkflowRun, run.id)
            run_obj.status = WorkflowRunStatus.failed
            run_obj.finished_at = datetime.now(tz=timezone.utc)
            run_obj.error_message = str(e)
            await db.commit()
        logger.exception("workflow.failed", name=name)
        raise
    return run.id
