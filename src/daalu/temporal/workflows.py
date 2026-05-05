# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/temporal/workflows.py
"""
Daalu Temporal workflows.

DeployDaaluCloud orchestrates the deploy stages by spawning one
``deploy_stage`` activity per stage in the install plan. Each activity
returns its final log tail in ``StageResult``; the workflow folds that
into per-stage state exposed via the ``progress`` query.

For live streaming of the *currently running* stage the UI separately
polls Temporal's ``describe_workflow_execution`` and reads the pending
activity's last heartbeat details. That keeps history small (one heartbeat
detail snapshot, not one event per line) and avoids the Temporal SDK rule
that workflows cannot read their own activities' heartbeats.
"""
from __future__ import annotations

from datetime import timedelta
from typing import List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from daalu.temporal.models import (
        CleanRequest,
        DeployRequest,
        MirrorImagesRequest,
        StageInput,
        StageProgress,
        StageResult,
        WorkflowProgress,
    )


# Order in which stages run when "all" is requested.
DEPLOY_STAGE_ORDER = (
    "cluster-api",
    "nodes",
    "ceph",
    "csi",
    "infrastructure",
    "monitoring",
    "openstack",
)

ALL_STAGE_TARGETS = set(DEPLOY_STAGE_ORDER)


def _resolve_install_plan(install: Optional[str]) -> List[str]:
    """
    Translate the comma-separated --install flag into an ordered list of
    stages. Mirrors the logic in daalu.cli.app.resolve_install_plan but kept
    workflow-side so the workflow doesn't import non-determinism.
    """
    if not install or install.strip().lower() == "all":
        return list(DEPLOY_STAGE_ORDER)

    requested = {s.strip() for s in install.split(",") if s.strip()}
    return [s for s in DEPLOY_STAGE_ORDER if s in requested]


def _timeout_for_stage(stage: str) -> timedelta:
    # Bare-metal provisioning, Ceph bootstrap, and the OpenStack control plane
    # are the long-running stages. Everything else fits under ~45 min.
    long_stages = {
        "cluster-api": timedelta(hours=2),
        "ceph": timedelta(hours=2),
        "openstack": timedelta(hours=2),
    }
    return long_stages.get(stage, timedelta(minutes=45))


# ---------------------------------------------------------------------------
# Deploy workflow
# ---------------------------------------------------------------------------

@workflow.defn(name="DeployDaaluCloud")
class DeployDaaluCloudWorkflow:
    """
    Provision the workload cluster and deploy OpenStack onto it.

    Inputs : DeployRequest
    Output : WorkflowProgress (final state)
    Queries:
      - progress() -> WorkflowProgress
    """

    def __init__(self) -> None:
        self._progress = WorkflowProgress(phase="PENDING")

    # ── Queries ────────────────────────────────────────────────────────────
    @workflow.query(name="progress")
    def get_progress(self) -> WorkflowProgress:
        return self._progress

    # ── Run ────────────────────────────────────────────────────────────────
    @workflow.run
    async def run(self, req: DeployRequest) -> WorkflowProgress:
        info = workflow.info()
        self._progress.workflow_id = info.workflow_id
        self._progress.phase = "RUNNING"
        self._progress.message = "Deployment started"

        plan = _resolve_install_plan(req.install)
        self._progress.stages = [StageProgress(name=s) for s in plan]

        # Most failures need human attention, not auto-retry. The CLI itself
        # has retries embedded for transient steps, so a re-attempt of the
        # whole stage usually doesn't help.
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=10),
            maximum_interval=timedelta(minutes=5),
            maximum_attempts=1,
        )

        for idx, stage in enumerate(plan):
            self._progress.current_stage = stage
            self._progress.message = f"Running stage: {stage}"
            stage_state = self._progress.stages[idx]
            stage_state.status = "running"
            stage_state.started_at = workflow.now().isoformat()

            try:
                result: StageResult = await workflow.execute_activity(
                    "deploy_stage",
                    StageInput(stage=stage, request=req),
                    start_to_close_timeout=_timeout_for_stage(stage),
                    heartbeat_timeout=timedelta(minutes=10),
                    retry_policy=retry,
                )
            except Exception as exc:  # noqa: BLE001 — funnel into progress query
                stage_state.ended_at = workflow.now().isoformat()
                stage_state.status = "failed"
                stage_state.error = f"{type(exc).__name__}: {exc}"
                self._progress.phase = "FAILED"
                self._progress.error = (
                    f"stage '{stage}' failed: {stage_state.error}"
                )
                self._progress.message = self._progress.error
                raise

            stage_state.ended_at = workflow.now().isoformat()
            # Persist the activity's final log tail into stage state so the
            # UI can show full output for completed stages even after the
            # activity exits and its heartbeat details are gone.
            if result.log_tail:
                stage_state.log_lines = result.log_tail.split("\n")

            if result.success:
                stage_state.status = "succeeded"
            else:
                stage_state.status = "failed"
                stage_state.error = result.error or f"exit code {result.exit_code}"
                self._progress.phase = "FAILED"
                self._progress.error = (
                    f"stage '{stage}' failed: {stage_state.error}"
                )
                self._progress.message = self._progress.error
                return self._progress

        self._progress.phase = "SUCCEEDED"
        self._progress.current_stage = None
        self._progress.message = "Deployment completed"
        return self._progress


# ---------------------------------------------------------------------------
# Clean workflow
# ---------------------------------------------------------------------------

@workflow.defn(name="CleanDaaluCloud")
class CleanDaaluCloudWorkflow:
    def __init__(self) -> None:
        self._progress = WorkflowProgress(phase="PENDING")

    @workflow.query(name="progress")
    def get_progress(self) -> WorkflowProgress:
        return self._progress

    @workflow.run
    async def run(self, req: CleanRequest) -> WorkflowProgress:
        info = workflow.info()
        self._progress.workflow_id = info.workflow_id
        self._progress.phase = "RUNNING"
        self._progress.message = "Cleanup started"
        self._progress.stages = [StageProgress(name="clean")]
        stage_state = self._progress.stages[0]
        stage_state.status = "running"
        stage_state.started_at = workflow.now().isoformat()

        try:
            result: StageResult = await workflow.execute_activity(
                "clean_stage",
                req,
                start_to_close_timeout=timedelta(hours=1),
                heartbeat_timeout=timedelta(minutes=10),
            )
        except Exception as exc:  # noqa: BLE001
            stage_state.ended_at = workflow.now().isoformat()
            stage_state.status = "failed"
            stage_state.error = f"{type(exc).__name__}: {exc}"
            self._progress.phase = "FAILED"
            self._progress.error = stage_state.error
            self._progress.message = stage_state.error or "Cleanup failed"
            raise

        stage_state.ended_at = workflow.now().isoformat()
        if result.log_tail:
            stage_state.log_lines = result.log_tail.split("\n")

        if result.success:
            stage_state.status = "succeeded"
            self._progress.phase = "SUCCEEDED"
            self._progress.message = "Cleanup completed"
        else:
            stage_state.status = "failed"
            stage_state.error = result.error or f"exit code {result.exit_code}"
            self._progress.phase = "FAILED"
            self._progress.error = stage_state.error
            self._progress.message = stage_state.error or "Cleanup failed"
        return self._progress


# ---------------------------------------------------------------------------
# Mirror-images workflow
# ---------------------------------------------------------------------------

@workflow.defn(name="MirrorImages")
class MirrorImagesWorkflow:
    def __init__(self) -> None:
        self._progress = WorkflowProgress(phase="PENDING")

    @workflow.query(name="progress")
    def get_progress(self) -> WorkflowProgress:
        return self._progress

    @workflow.run
    async def run(self, req: MirrorImagesRequest) -> WorkflowProgress:
        info = workflow.info()
        self._progress.workflow_id = info.workflow_id
        self._progress.phase = "RUNNING"
        self._progress.message = "Mirroring images"
        self._progress.stages = [StageProgress(name="mirror-images")]
        stage_state = self._progress.stages[0]
        stage_state.status = "running"
        stage_state.started_at = workflow.now().isoformat()

        try:
            result: StageResult = await workflow.execute_activity(
                "mirror_images",
                req,
                start_to_close_timeout=timedelta(hours=4),
                heartbeat_timeout=timedelta(minutes=10),
            )
        except Exception as exc:  # noqa: BLE001
            stage_state.ended_at = workflow.now().isoformat()
            stage_state.status = "failed"
            stage_state.error = f"{type(exc).__name__}: {exc}"
            self._progress.phase = "FAILED"
            self._progress.error = stage_state.error
            self._progress.message = stage_state.error or "Mirror failed"
            raise

        stage_state.ended_at = workflow.now().isoformat()
        if result.log_tail:
            stage_state.log_lines = result.log_tail.split("\n")

        if result.success:
            stage_state.status = "succeeded"
            self._progress.phase = "SUCCEEDED"
            self._progress.message = "Mirror complete"
        else:
            stage_state.status = "failed"
            stage_state.error = result.error or f"exit code {result.exit_code}"
            self._progress.phase = "FAILED"
            self._progress.error = stage_state.error
            self._progress.message = stage_state.error or "Mirror failed"
        return self._progress


ALL_WORKFLOWS = [
    DeployDaaluCloudWorkflow,
    CleanDaaluCloudWorkflow,
    MirrorImagesWorkflow,
]
