"""Edge-side agent loop for Daalu Private.

Runs inside the customer's cluster (via the ``daalu-edge`` Helm
chart with ``agents.enabled=true``). Long-polls the hub's internal
API for the tenant this pod is provisioned for, processes each
task locally, and POSTs the synthesised result back.

The agent reads telemetry from in-cluster sources (Prometheus,
Loki, kubectl) directly — that data never transits the hub. Only
the agent's *output* (English/JSON summary, suggested action) is
sent back.

Phase 1 status: the polling loop is wired correctly with the right
backoff, auth headers, and result shape; the per-task processors
that turn an `alert_explain` task into a real LLM-driven enrichment
are stubs. The processor seam (``_dispatch_task``) is the single
location to grow next.

See ``docs/design/daalu-private.md`` §5.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import httpx
import structlog

logger = structlog.get_logger(__name__)


def _hub_url() -> str:
    url = os.environ.get("DAALU_HUB_INTERNAL_API_URL", "").rstrip("/")
    if not url:
        raise RuntimeError(
            "DAALU_HUB_INTERNAL_API_URL is not set — refusing to start the "
            "edge agent loop without a configured hub URL."
        )
    return url


def _service_token() -> str:
    token = os.environ.get("DAALU_AGENT_SERVICE_TOKEN", "")
    if not token:
        raise RuntimeError(
            "DAALU_AGENT_SERVICE_TOKEN is not set — refusing to start the "
            "edge agent loop without a service token. The hub issues this "
            "at Private-tier provisioning time."
        )
    return token


async def _dispatch_task(task: dict) -> dict | None:
    """Process one agent task and return a result dict, or None to skip.

    Phase 1: returns a placeholder result so the round-trip is
    exercised end-to-end. Real processors (alert-enrichment,
    briefing-generation, RCA) hook into the existing agent code via
    ``core.agents`` — Phase 2 work.
    """
    kind = task.get("kind", "unknown")
    task_id = task.get("id", "?")
    logger.info("edge_agent.processing", kind=kind, task_id=task_id)
    return {
        "task_id": task_id,
        "summary": f"[edge-agent stub] task {kind} acknowledged",
        "artefact_kind": kind,
        "artefact_payload": {},
        "tokens_used": 0,
        "completed_at": datetime.now(tz=timezone.utc).isoformat(),
    }


async def _poll_once(
    client: httpx.AsyncClient, hub_url: str, headers: dict
) -> tuple[int, int]:
    """One poll-and-process cycle. Returns (tasks_processed, errors)."""
    try:
        resp = await client.get(f"{hub_url}/api/v1/internal/agent-tasks", headers=headers)
    except httpx.HTTPError as e:
        logger.warning("edge_agent.poll_failed", error=str(e))
        return 0, 1
    if resp.status_code >= 400:
        logger.warning("edge_agent.poll_status", status=resp.status_code)
        return 0, 1
    tasks = resp.json() or []
    processed = 0
    errors = 0
    for task in tasks:
        try:
            result = await _dispatch_task(task)
            if result is None:
                continue
            r = await client.post(
                f"{hub_url}/api/v1/internal/agent-results",
                headers=headers,
                json=result,
            )
            if r.status_code >= 400:
                logger.warning(
                    "edge_agent.result_rejected",
                    status=r.status_code,
                    task_id=task.get("id"),
                )
                errors += 1
            else:
                processed += 1
        except Exception:  # noqa: BLE001 — one bad task must not kill the loop
            logger.exception("edge_agent.task_failed", task=task)
            errors += 1
    return processed, errors


async def run_edge_loop(stop: asyncio.Event) -> None:
    """Run the edge polling loop until ``stop`` is set.

    Polling interval: 5 s on success, exponential backoff (max 60 s)
    after consecutive failures. The hub's API is the cheap path —
    no GPU there — so an aggressive poll is fine for tens of
    Private tenants.
    """
    hub_url = _hub_url()
    headers = {"authorization": f"Bearer {_service_token()}"}
    backoff = 5.0
    while not stop.is_set():
        async with httpx.AsyncClient(timeout=30.0) as client:
            processed, errors = await _poll_once(client, hub_url, headers)
        if errors > 0 and processed == 0:
            backoff = min(backoff * 2, 60.0)
        else:
            backoff = 5.0
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass


def main() -> None:
    """Standalone entrypoint for the edge-agent pod's container."""
    logger.info("edge_agent.starting")
    stop = asyncio.Event()
    try:
        asyncio.run(run_edge_loop(stop))
    except KeyboardInterrupt:
        stop.set()
