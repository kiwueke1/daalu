# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/temporal/worker.py
"""
Daalu Temporal worker.

A long-running process that connects to the Temporal frontend, registers
the daalu workflows + activities, and pumps the task queue.

Run inside a pod on the management cluster, with the daalu CLI on PATH and
``cluster-defs/`` + ``cloud-config/secrets.yaml`` mounted into ``/workspace``.

Activities are blocking (they shell out to ``daalu``), so we use a
ThreadPoolExecutor sized for the maximum number of concurrent stages we
expect — currently 4 (one in-flight workflow has at most one stage running,
but several workflows may run in parallel).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import signal

from temporalio.client import Client
from temporalio.worker import Worker

from daalu.temporal.activities import ALL_ACTIVITIES
from daalu.temporal.settings import load_temporal_settings
from daalu.temporal.workflows import ALL_WORKFLOWS

log = logging.getLogger("daalu.worker")


async def _run() -> None:
    settings = load_temporal_settings()
    log.info(
        "[daalu-worker] connecting address=%s namespace=%s task_queue=%s",
        settings.address, settings.namespace, settings.task_queue,
    )

    client = await Client.connect(settings.address, namespace=settings.namespace)

    # ThreadPool for blocking activities (subprocess + SSH).
    max_workers = int(os.getenv("DAALU_WORKER_THREADS", "4"))
    activity_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix="daalu-act-",
    )

    worker = Worker(
        client,
        task_queue=settings.task_queue,
        workflows=ALL_WORKFLOWS,
        activities=ALL_ACTIVITIES,
        activity_executor=activity_executor,
    )

    log.info(
        "[daalu-worker] ready task_queue=%s workflows=%s activities=%s threads=%d",
        settings.task_queue,
        [w.__name__ for w in ALL_WORKFLOWS],
        [getattr(a, "__name__", str(a)) for a in ALL_ACTIVITIES],
        max_workers,
    )

    # Graceful shutdown on SIGTERM (Kubernetes pod stop) / SIGINT.
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _stop(*_args: object) -> None:
        log.info("[daalu-worker] received stop signal")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            # add_signal_handler is unavailable on Windows / inside some
            # test runners — those don't matter for prod use.
            pass

    worker_task = asyncio.create_task(worker.run())
    stopped = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        {worker_task, stopped}, return_when=asyncio.FIRST_COMPLETED,
    )

    if stopped in done:
        log.info("[daalu-worker] shutting down...")
        await worker.shutdown()
    for t in pending:
        t.cancel()
    activity_executor.shutdown(wait=True)


def main() -> None:
    """``daalu-worker`` console entry point."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
