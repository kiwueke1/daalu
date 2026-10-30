"""Executor service — drives ``change_proposals.execute()`` on approved rows.

The chokepoint between an approved :class:`ChangeProposal` and a live
device. Mints an :class:`Actor` with ``kind="executor"`` and the
configured ``executor_jwt_scope`` and calls
:func:`change_proposals.execute` for every approved row that hasn't
been executed yet.

Identity boundary, by design:

* The task is registered on the same Celery app as the rest of the
  workers, but it is routed via :data:`Celery.task_routes` to a
  dedicated queue (:attr:`Settings.executor_queue_name`). The main
  worker pool (``daalu worker``) consumes only the default queue and
  therefore *cannot* pick this task up, even if its image happens to
  import this module. Subscription to the executor queue is what makes
  a pod "the executor".
* In production this task runs inside the ``daalu-executor`` k8s
  Deployment, which has its own ServiceAccount and is the only pod
  whose env carries ``EXECUTOR_JWT_SCOPE``.
* Replicas stays at 1 — adding pods would create additional executor
  identities racing on the same approved rows. Throughput is scaled
  inside the process via ``--concurrency=N`` instead.

Tick budget:

* Pull up to ``executor_batch_size`` approved rows ordered by
  ``approved_at`` (FIFO).
* Group by tenant so one :class:`NautobotSoT` instance is reused per
  tenant batch.
* Per-row failures (stale, missing device, unknown transport, network
  error mid-push) are caught and logged so one bad row does not poison
  the rest of the tick.

The reconciler shares its dispose-on-tick pattern — each fire
``asyncio.run()``s a fresh loop and the async engine pool is disposed
on exit so the next tick gets fresh connections instead of dangling
ones from a dead loop.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import select

from daalu_automation.config import get_settings
from daalu_automation.core import change_proposals as cps
from daalu_automation.core.device import get_device_adapter
from daalu_automation.core.sot import NautobotSoT, NautobotUnavailable
from daalu_automation.core.sot.base import SourceOfTruth
from daalu_automation.core.sot.models import Actor
from daalu_automation.database import AsyncSessionLocal, engine
from daalu_automation.models import (
    ChangeProposal,
    ChangeProposalKind,
    ChangeProposalStatus,
)
from daalu_automation.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _execute_one(
    tenant_id: uuid.UUID,
    proposal_id: uuid.UUID,
    actor: Actor,
) -> str:
    """Execute a single approved proposal. Returns a stats bucket name."""
    async with AsyncSessionLocal() as db:
        # Look up the device on the SoT so we know which adapter to
        # dispatch to. The transport string lives on the SoT, not on
        # the proposal row — keeping the row vendor-neutral.
        proposal = (
            await db.execute(
                select(ChangeProposal).where(
                    ChangeProposal.id == proposal_id,
                    ChangeProposal.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if proposal is None:
            return "skipped"
        # Re-check status under the new session — another tick (or
        # admin action) may have mutated it since the outer batch fetch.
        if proposal.status != ChangeProposalStatus.approved:
            return "skipped"

        # The SoT is per-proposal, not per-tenant: the device id only resolves
        # in the environment the proposal was authored against, because each
        # NV-CM environment bundles its own Nautobot. A NULL slug is the
        # tenant-default resolution — every pre-multi-environment row, and
        # every BYO-Nautobot tenant.
        sot: SourceOfTruth = NautobotSoT(env=proposal.environment_slug)

        # Imperative server-lifecycle proposals take the Tinkerbell path:
        # no DeviceAdapter / SSH creds, an observed-state compare instead of
        # render-drift, CRs applied to the mgmt cluster over the tunnel.
        if proposal.kind == ChangeProposalKind.provision_op:
            try:
                result = await cps.execute_provision(
                    db, tenant_id, proposal_id, actor=actor, sot=sot
                )
            except cps.StaleProposalError:
                return "stale"
            except cps.ProposalStatusError as e:
                logger.info(
                    "executor.status_race",
                    extra={"proposal_id": str(proposal_id), "detail": str(e)},
                )
                return "skipped"
            except LookupError as e:
                logger.warning(
                    "executor.tinkerbell_target_unresolved",
                    extra={"proposal_id": str(proposal_id), "error": str(e)},
                )
                return "skipped"
            except PermissionError:
                raise
            except Exception as e:  # noqa: BLE001 — per-proposal boundary
                logger.exception(
                    "executor.provision_failed",
                    extra={
                        "proposal_id": str(proposal_id),
                        "error": f"{type(e).__name__}: {e}",
                    },
                )
                return "failed"
            if result.status == ChangeProposalStatus.executed:
                return "executed"
            return "failed"

        try:
            device = await sot.get_device(db, tenant_id, proposal.device_id)
        except NautobotUnavailable as e:
            logger.warning(
                "executor.sot_unavailable",
                extra={
                    "proposal_id": str(proposal_id),
                    "tenant_id": str(tenant_id),
                    "error": str(e),
                },
            )
            return "skipped"
        if device is None:
            logger.warning(
                "executor.device_not_found",
                extra={
                    "proposal_id": str(proposal_id),
                    "device_id": proposal.device_id,
                },
            )
            return "skipped"

        try:
            adapter = get_device_adapter(device.transport)
        except KeyError:
            logger.warning(
                "executor.unknown_transport",
                extra={
                    "proposal_id": str(proposal_id),
                    "transport": device.transport,
                },
            )
            return "skipped"

        try:
            creds = await cps.resolve_credentials(db, tenant_id, device)
        except LookupError as e:
            logger.warning(
                "executor.creds_unresolved",
                extra={
                    "proposal_id": str(proposal_id),
                    "error": str(e),
                },
            )
            return "skipped"

        try:
            result = await cps.execute(
                db,
                tenant_id,
                proposal_id,
                actor=actor,
                sot=sot,
                adapter=adapter,
                creds=creds,
            )
        except cps.StaleProposalError:
            return "stale"
        except cps.ProposalStatusError as e:
            # Lost a race with admin action between status check and
            # the lock acquire inside execute(). Not an error.
            logger.info(
                "executor.status_race",
                extra={"proposal_id": str(proposal_id), "detail": str(e)},
            )
            return "skipped"
        except PermissionError:
            # Should not happen — actor is constructed in this module
            # and execute() enforces the gate. Re-raise so it surfaces
            # in error tracking; this is a config bug, not data.
            raise
        except Exception as e:  # noqa: BLE001 — per-proposal boundary
            logger.exception(
                "executor.proposal_failed",
                extra={
                    "proposal_id": str(proposal_id),
                    "error": f"{type(e).__name__}: {e}",
                },
            )
            return "failed"

        if result.status == ChangeProposalStatus.executed:
            return "executed"
        if result.status == ChangeProposalStatus.failed:
            return "failed"
        # Unexpected terminal status — treat as skipped for stats.
        return "skipped"


async def _execute_approved_batch() -> dict[str, Any]:
    settings = get_settings()
    actor = Actor(
        kind="executor",
        scope=settings.executor_jwt_scope,
        name="executor-worker",
    )
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(
                    ChangeProposal.id,
                    ChangeProposal.tenant_id,
                )
                .where(
                    ChangeProposal.status == ChangeProposalStatus.approved,
                    ChangeProposal.executed_at.is_(None),
                )
                .order_by(ChangeProposal.approved_at)
                .limit(settings.executor_batch_size)
            )
        ).all()
    # Group by tenant purely so the log story stays tidy — the SoT itself is
    # resolved per proposal now, since two proposals in the same batch can
    # belong to different management environments.
    by_tenant: dict[uuid.UUID, list[uuid.UUID]] = {}
    for pid, tid in rows:
        by_tenant.setdefault(tid, []).append(pid)

    stats: dict[str, Any] = {
        "tenants": 0,
        "processed": 0,
        "executed": 0,
        "failed": 0,
        "stale": 0,
        "skipped": 0,
    }
    for tid, pids in by_tenant.items():
        stats["tenants"] += 1
        for pid in pids:
            stats["processed"] += 1
            bucket = await _execute_one(tid, pid, actor)
            stats[bucket] = stats.get(bucket, 0) + 1
    return stats


@celery_app.task(name="sot.execute_approved")
def execute_approved_task() -> dict[str, Any]:
    # Same dispose-on-tick pattern as reconciler: the async engine pins
    # its pool to the first event loop, but asyncio.run() creates a
    # fresh loop each tick. Dispose at end-of-tick → fresh pool next
    # tick. ~one reconnect per period — fine for 30s cadence.
    async def _wrapped() -> dict[str, Any]:
        try:
            return await _execute_approved_batch()
        finally:
            await engine.dispose()
    return asyncio.run(_wrapped())
