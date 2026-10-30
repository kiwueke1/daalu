"""Long-running agent host.

Started as its own k8s Deployment (``deploy/k8s/workers/agents.yaml``).
One process per replica subscribes every registered agent to the event
stream; horizontal scale just adds more consumers to the same group.
"""

from __future__ import annotations

import asyncio
import signal

import structlog

import daalu_automation.modules  # noqa: F401  — registers agents
from daalu_automation.core.agents import (
    _AGENT_FACTORIES,  # noqa: F401  reuse private registry
    get_agent,
)
from daalu_automation.observability import init_observability

logger = structlog.get_logger(__name__)


async def _main(*, mode: str = "hub") -> None:
    init_observability(component="agents")
    # Edge mode runs a completely different loop: long-poll the hub's
    # internal API and process per-tenant tasks. Returns when the
    # loop exits.
    if mode == "edge":
        from daalu_automation.workers.edge_agent_runner import run_edge_loop

        logger.info("agent_runner.edge_mode_starting")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        # Readiness sentinel for the daalu-edge agents chart probe.
        try:
            with open("/tmp/daalu-edge-agent-ready", "w") as f:  # noqa: PTH123
                f.write("ok\n")
        except OSError:
            pass
        await run_edge_loop(stop)
        return
    tasks: list[asyncio.Task] = []
    for name in _AGENT_FACTORIES:
        agent = get_agent(name)
        logger.info("agent_runner.starting", agent=name, mode=mode)
        tasks.append(asyncio.create_task(agent.run_forever(), name=name))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    # Readiness sentinel — the daalu-edge agents chart's readiness
    # probe checks for this file. Writing it once we're past startup
    # work means the probe accurately reflects "ready to do work."
    try:
        with open("/tmp/daalu-edge-agent-ready", "w") as f:  # noqa: PTH123
            f.write("ok\n")
    except OSError:
        pass  # not all environments have writable /tmp; ignore
    await stop.wait()
    logger.info("agent_runner.shutting_down")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def main(*, mode: str = "hub") -> None:
    asyncio.run(_main(mode=mode))


if __name__ == "__main__":
    main()
