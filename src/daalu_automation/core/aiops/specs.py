"""Pydantic models behind the open AOMM v0.1 artefact schemas in ``spec/``.

These are the *interchange* shapes — what one AOMM implementation writes and
another reads — not Daalu's database rows (those carry tenant ids, internal
columns and so on). ``scripts/gen_specs.py`` turns them into the JSON Schemas
(draft 2020-12) published under ``spec/``; ``tests/test_specs.py`` checks the
files on disk match and that every ``spec/examples/*`` artefact parses.

The autonomy policy schema comes from :func:`policy.policy_json_schema` and
the eval scenario schema from ``evals.scenario.scenario_json_schema``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from daalu_automation.core.aiops.audit import KINDS

AOMM_VERSION = "0.1"
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_BASE = "https://github.com/kiwueke1/daalu/spec/"

_HEX64_OR_EMPTY = r"^([0-9a-f]{64})?$"
_KIND_PATTERN = "^(" + "|".join(k.replace(".", r"\.") for k in KINDS) + r"|x-[a-z0-9][a-z0-9._-]*)$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── Action proposal ─────────────────────────────────────────────────────────


class PolicyDecision(_Strict):
    """The autonomy policy's answer for one write."""

    mode: Literal["deny", "approve", "auto"] = Field(
        description="Effective mode after guardrails.")
    rule_id: str = Field(default="", description="Matching rule id; empty when the "
                                                 "policy defaults applied.")
    reason: str = ""
    approvers: int = Field(default=1, ge=1, le=5)
    configured_mode: Literal["deny", "approve", "auto", "auto_after_trust"] = Field(
        default="approve", description="The mode the rule (or defaults) configures, "
                                       "before trust and guardrails.")


class Approval(_Strict):
    user_id: str | None = None
    email: str
    at: datetime


class BlastRadius(_Strict):
    pods: int | None = None
    deployments: int | None = None


class Preview(BaseModel):
    """Best-effort dry run of the write, shown to approvers."""

    model_config = ConfigDict(extra="allow")
    summary: str = ""
    blast_radius: BlastRadius = Field(default_factory=BlastRadius)
    details: dict[str, Any] = Field(default_factory=dict)


class ActionProposal(_Strict):
    """A state-changing action an AI agent wants to take, as gated by policy.

    Every write an agent proposes becomes one of these; it is the unit that
    is approved, rejected, executed and verified."""

    id: uuid.UUID
    alert_id: uuid.UUID = Field(description="The incident / alert the action remediates.")
    tool: str = Field(min_length=1, description="Write tool, e.g. rollout_undo.")
    input: dict[str, Any] = Field(description="Tool arguments.")
    reason: str = Field(min_length=1, description="Why the action is needed (cite the "
                                                  "evidence) and what will happen.")
    proposed_by: str = Field(description="`agent` for the platform's own agent, "
                                         "`mcp:<email>` for an external agent over MCP.")
    proposed_at: datetime
    decision: PolicyDecision
    status: Literal["pending", "approved", "rejected", "executed", "failed"]
    approvals_required: int = Field(default=1, ge=1, le=5)
    approvals: list[Approval] = Field(default_factory=list)
    preview: Preview | None = None
    auto_executed: bool = False
    executed_at: datetime | None = None
    verified_resolved: bool | None = Field(
        default=None, description="Did post-execution verification show recovery? "
                                  "null until verified.")


# ── Audit event ─────────────────────────────────────────────────────────────


class AuditEvent(_Strict):
    """One link of a tenant's append-only, hash-chained audit log.

    ``hash = sha256_hex(prev_hash + canonical_json({seq, kind, actor, subject,
    data, created_at}))`` where canonical JSON has sorted keys, no
    whitespace (``","`` and ``":"`` separators) and ``created_at`` is
    ISO-8601 in UTC. The first event has ``seq = 1`` and ``prev_hash = ""``."""

    seq: int = Field(ge=1)
    kind: str = Field(pattern=_KIND_PATTERN, description="A core kind, or an "
                      "implementation-specific kind prefixed `x-`.")
    actor: str = Field(default="", max_length=255,
                       description="Who acted: an email, `agent`, `mcp:<email>`, `verifier`.")
    subject: str = Field(default="", max_length=255, description="Usually the alert id.")
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    prev_hash: str = Field(default="", pattern=_HEX64_OR_EMPTY)
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")


# ── Investigation ───────────────────────────────────────────────────────────


class Investigation(_Strict):
    """The learning-loop record of one AI investigation of one alert."""

    id: uuid.UUID | None = None
    alert_id: uuid.UUID
    started_at: datetime
    triaged_at: datetime | None = Field(
        default=None, description="First answer that named a root cause.")
    root_cause: str = ""
    actions_proposed: int = Field(default=0, ge=0)
    actions_approved: int = Field(default=0, ge=0)
    actions_rejected: int = Field(default=0, ge=0)
    actions_auto: int = Field(default=0, ge=0)
    resolution: Literal["open", "auto", "approved", "manual", "unresolved"] = "open"
    resolved_at: datetime | None = None
    verified: bool | None = None
    runbook_ids: list[str] = Field(default_factory=list,
                                   description="Runbooks injected into the investigation.")
    verdict: Literal["correct", "partial", "wrong"] | None = None
    verdict_by: str = ""
    verdict_at: datetime | None = None
    verdict_note: str = ""
    model: str = Field(default="", description="Model that ran the investigation.")


# ── Runbook frontmatter ─────────────────────────────────────────────────────


class RunbookMatch(_Strict):
    alertname: list[str] = Field(default_factory=list,
                                 description="Alert names (or titles) this runbook covers.")
    labels: dict[str, str] = Field(default_factory=dict,
                                   description="Every label must equal the alert's.")


class RunbookFrontmatter(_Strict):
    """YAML frontmatter of a runbook file. The markdown body follows it and
    should have ``## Symptoms``, ``## Diagnosis`` and ``## Fix`` sections."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,127}$", description="Stable slug.")
    title: str = Field(min_length=1, max_length=255)
    match: RunbookMatch = Field(
        description="At least one alertname or label is required.")
    allowed_actions: list[str] = Field(default_factory=list,
                                       description="Write tools the fix may use.")
    description: str = ""
    status: Literal["draft", "published"] | None = Field(
        default=None, description="Ask the importer to publish (or keep as draft).")


# ── Schema generation ───────────────────────────────────────────────────────


def _finish(schema: dict[str, Any], name: str, title: str) -> dict[str, Any]:
    out = {
        "$schema": SCHEMA_DIALECT,
        "$id": f"{SCHEMA_BASE}{name}.schema.json",
        "title": title,
        "x-aomm-version": AOMM_VERSION,
    }
    out.update({k: v for k, v in schema.items() if k not in out})
    return out


def _model_schema(model: type[BaseModel], name: str, title: str) -> dict[str, Any]:
    return _finish(model.model_json_schema(), name, title)


def eval_scenario_schema() -> dict[str, Any]:
    from daalu_automation.evals.scenario import scenario_json_schema

    return _finish(scenario_json_schema(), "eval-scenario", "AOMM eval scenario v0.1")


def all_schemas() -> dict[str, dict[str, Any]]:
    """``{file name: schema}`` for every schema published under ``spec/``."""
    from daalu_automation.core.aiops.policy import policy_json_schema

    return {
        "autonomy-policy.schema.json": _finish(
            policy_json_schema(), "autonomy-policy", "AOMM autonomy policy v0.1"),
        "action-proposal.schema.json": _model_schema(
            ActionProposal, "action-proposal", "AOMM action proposal v0.1"),
        "audit-event.schema.json": _model_schema(
            AuditEvent, "audit-event", "AOMM audit event v0.1"),
        "investigation.schema.json": _model_schema(
            Investigation, "investigation", "AOMM investigation record v0.1"),
        "runbook.schema.json": _model_schema(
            RunbookFrontmatter, "runbook", "AOMM runbook frontmatter v0.1"),
        "eval-scenario.schema.json": eval_scenario_schema(),
    }
