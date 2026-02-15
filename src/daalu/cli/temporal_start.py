# src/daalu/cli/temporal_start.py

from __future__ import annotations

from daalu.temporal.client import get_temporal_client
from daalu.temporal.settings import load_temporal_settings
from daalu.temporal.models import DeployRequest


async def start_deploy_workflow(req: DeployRequest) -> str:
    from daalu.temporal.workflows import DaaluDeployWorkflow
    client = await get_temporal_client()
    settings = load_temporal_settings()

    workflow_id = f"daalu-deploy:{req.cluster_name}"

    handle = await client.start_workflow(
        DaaluDeployWorkflow.run,
        req,
        id=workflow_id,
        task_queue=settings.task_queue,
    )

    return f"{handle.id} / {handle.run_id}"
