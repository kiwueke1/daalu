# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/temporal/workflows.py

from __future__ import annotations

from datetime import timedelta
from typing import List, Optional, Set

from temporalio import workflow
from temporalio.common import RetryPolicy

from .models import DeployRequest, DeployStatus

# activities are imported via workflow.unsafe.imports_passed_through
# to avoid workflow sandbox issues
with workflow.unsafe.imports_passed_through():
    from .activities import (
        activity_deploy_cluster_api,
        activity_deploy_nodes,
        activity_deploy_ceph,
        activity_deploy_csi,
        activity_deploy_infrastructure,
    )
    from daalu.cli.app import resolve_install_plan


@workflow.defn
class DaaluDeployWorkflow:
    def __init__(self) -> None:
        self._status = DeployStatus(
            phase="PENDING",
            message="Waiting to start",
            current_stage=None,
            completed_stages=[],
        )

    @workflow.query
    def status(self) -> DeployStatus:
        return self._status

    @workflow.run
    async def run(self, req: DeployRequest) -> DeployStatus:
        self._status.phase = "RUNNING"
        self._status.message = "Deployment started"

        install_plan: Set[str] = resolve_install_plan(req.install)

        # sane defaults for infra automation
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=5),
            maximum_interval=timedelta(minutes=5),
            maximum_attempts=5,
        )

        async def do_stage(name: str, coro):
            self._status.current_stage = name
            self._status.message = f"Running stage: {name}"
            try:
                await coro
                self._status.completed_stages = (self._status.completed_stages or []) + [name]
            except Exception as e:
                self._status.phase = "FAILED"
                self._status.error = f"{e}"
                self._status.message = f"Stage failed: {name}"
                raise

        # 1) cluster-api
        if "cluster-api" in install_plan:
            await do_stage(
                "cluster-api",
                workflow.execute_activity(
                    activity_deploy_cluster_api,
                    req,
                    start_to_close_timeout=timedelta(minutes=60),
                    retry_policy=retry,
                ),
            )

        # 2) nodes
        if "nodes" in install_plan:
            await do_stage(
                "nodes",
                workflow.execute_activity(
                    activity_deploy_nodes,
                    req,
                    start_to_close_timeout=timedelta(minutes=60),
                    retry_policy=retry,
                ),
            )

        ceph_hosts = []
        # 3) ceph
        if "ceph" in install_plan:
            ceph_hosts = await workflow.execute_activity(
                activity_deploy_ceph,
                req,
                start_to_close_timeout=timedelta(hours=3),
                retry_policy=retry,
            )
            (self._status.completed_stages or []).append("ceph")

        # 4) csi
        if "csi" in install_plan:
            await do_stage(
                "csi",
                workflow.execute_activity(
                    activity_deploy_csi,
                    req,
                    ceph_hosts,
                    start_to_close_timeout=timedelta(minutes=60),
                    retry_policy=retry,
                ),
            )

        # 5) infrastructure
        if "infrastructure" in install_plan:
            await do_stage(
                "infrastructure",
                workflow.execute_activity(
                    activity_deploy_infrastructure,
                    req,
                    start_to_close_timeout=timedelta(minutes=60),
                    retry_policy=retry,
                ),
            )

        # 6) openstack (later)
        if "openstack" in install_plan:
            # add activities later: keystone/nova/glance/etc
            # or one activity that triggers your OpenStackManager
            pass

        self._status.phase = "SUCCEEDED"
        self._status.current_stage = None
        self._status.message = "Deployment completed"
        return self._status
