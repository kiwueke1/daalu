# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/temporal/worker.py

# src/daalu/temporal/worker.py
from __future__ import annotations

import asyncio
import concurrent.futures

from temporalio.worker import Worker

from .client import get_temporal_client
from .settings import load_temporal_settings
from .workflows import DaaluDeployWorkflow
from .activities import (
    activity_deploy_cluster_api,
    activity_deploy_nodes,
    activity_deploy_ceph,
    activity_deploy_csi,
    activity_deploy_infrastructure,
)


async def main() -> None:
    settings = load_temporal_settings()
    client = await get_temporal_client()

    # Thread pool for BLOCKING activities (SSH, Helm, Ceph, Metal3, etc.)
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=8  # tune later
    ) as activity_executor:

        worker = Worker(
            client,
            task_queue=settings.task_queue,
            workflows=[DaaluDeployWorkflow],
            activities=[
                activity_deploy_cluster_api,
                activity_deploy_nodes,
                activity_deploy_ceph,
                activity_deploy_csi,
                activity_deploy_infrastructure,
            ],
            activity_executor=activity_executor,
        )

        print(
            "[daalu-worker] starting. "
            f"address={settings.address} "
            f"ns={settings.namespace} "
            f"tq={settings.task_queue}"
        )

        # Blocks forever until SIGINT / SIGTERM
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
