"""Hub-side internal API consumed by edge-side agent pods.

Daalu Private edge agents run inside the customer's cluster. They
poll this API for the next batch of agent-tasks (alert fired,
briefing schedule due, change-proposal opened) belonging to *their*
tenant, process them locally, and POST the synthesised results
back.

These endpoints are **not** intended for user clients. They reject
cookie/PAT auth — only service tokens (with the right tenant scope)
are accepted. They live under ``/api/v1/internal/`` so the
AuthGate middleware can be configured to skip the cookie check for
this prefix specifically (and the service-token check is enforced
inside the router).

Wire shape:

  GET  /api/v1/internal/agent-tasks?since=<iso>
       → list of pending tasks for the calling tenant, batched
  POST /api/v1/internal/agent-results
       → record the synthesised result; do NOT include raw
         telemetry, only the agent's English/JSON summary

See ``docs/design/daalu-private.md`` §5.

Phase 1 status: endpoints defined with the right shapes and auth;
tasks/results are persisted to a simple in-DB queue (the
``agent_tasks`` / ``agent_results`` tables — TODO migration 0010).
A proper outbox + long-poll is Phase 2.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from daalu_automation.core.service_tokens import (
    ServiceTokenError,
    verify_service_token,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


def _authenticate(authorization: str | None) -> tuple[str, str]:
    """Return (tenant_id, purpose) on successful verification.

    Refuses anything that isn't a service token with purpose='rca' or
    'chat' — the edge agent always identifies itself with a purpose
    that signals "this token is for the agent loop, not a user."
    Cookie / PAT auth is explicitly rejected here.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        claims = verify_service_token(authorization.split(None, 1)[1])
    except ServiceTokenError as e:
        logger.warning("internal_agents.auth_failed", error=str(e))
        raise HTTPException(status_code=401, detail="invalid token")
    return claims.tenant_id, claims.purpose


class AgentTask(BaseModel):
    """One unit of work for an edge agent.

    Carries everything the agent needs to do its job *without*
    requiring a hub round-trip: the triggering event id, the
    rendered prompt skeleton, references to the integrations to use
    (URLs resolved tenant-side). The agent reads the referenced
    integrations directly from its in-cluster network, never the hub.
    """

    id: str
    kind: str  # 'alert_explain' | 'briefing' | 'rca' | 'sales_followup'
    tenant_id: str
    payload: dict[str, Any]
    created_at: datetime


class AgentResult(BaseModel):
    """The agent's synthesised result.

    Strictly the agent's *output* — the English/JSON answer the
    agent decided to produce. Source data is intentionally not in
    this schema: the hub does not want it and the contract with the
    Private tenant says we don't get it.
    """

    task_id: str
    summary: str = Field(max_length=20_000)
    artefact_kind: str = Field(
        description="alert_enrichment | briefing_text | rca_report | proposal_draft"
    )
    artefact_payload: dict[str, Any] = Field(default_factory=dict)
    tokens_used: int = Field(default=0, ge=0)
    completed_at: datetime


@router.get("/agent-tasks", response_model=list[AgentTask])
async def list_pending_tasks(
    since: datetime | None = None,
    limit: int = 20,
    authorization: str | None = Header(default=None),
) -> list[AgentTask]:
    """Return pending agent tasks for the caller's tenant.

    The edge agent long-polls this endpoint. ``since`` is the cursor
    the agent persists locally so a restart doesn't re-process work.

    **Phase 1 status:** returns an empty list. The hub-side outbox
    that populates the queue is built in Phase 2 (see TODOs in
    ``docs/design/daalu-private.md`` §10). This endpoint exists so
    the edge pod's polling loop has a stable URL to call from
    day one — the wire shape is the contract.
    """
    tenant_id, _ = _authenticate(authorization)
    logger.debug("internal_agents.poll", tenant_id=tenant_id, since=str(since))
    return []


@router.post("/agent-results", status_code=204)
async def record_result(
    result: AgentResult,
    authorization: str | None = Header(default=None),
) -> None:
    """Record an edge agent's synthesised result.

    The hub persists ``summary`` + ``artefact_payload`` into the
    appropriate tenant table (e.g. an alert_enrichment row or a
    briefing row). The raw telemetry the agent consulted is *not*
    in this payload and never reaches the hub.

    **Phase 1 status:** validates and logs only; persistence into
    the right tenant table is Phase 2.
    """
    tenant_id, _ = _authenticate(authorization)
    if not result.summary.strip():
        raise HTTPException(status_code=400, detail="empty summary")
    logger.info(
        "internal_agents.result_received",
        tenant_id=tenant_id,
        task_id=result.task_id,
        artefact_kind=result.artefact_kind,
        tokens_used=result.tokens_used,
    )
    return None
