"""Pydantic response/request models — flat so the frontend types match 1:1."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class EventOut(_ORM):
    id: uuid.UUID
    type: str
    module: str
    source: str
    severity: str
    summary: str
    occurred_at: datetime
    payload: dict[str, Any]


class AlertOut(_ORM):
    id: uuid.UUID
    module: str
    severity: str
    status: str
    title: str
    body: str
    ai_confidence: float
    metadata_json: dict[str, Any]
    fingerprint: str | None = None
    occurrence_count: int = 1
    last_seen_at: datetime | None = None
    created_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None


class AlertOccurrenceOut(_ORM):
    id: uuid.UUID
    alert_id: uuid.UUID
    occurred_at: datetime
    source_event_id: uuid.UUID | None
    metadata_json: dict[str, Any]
    created_at: datetime


class RecommendationOut(_ORM):
    id: uuid.UUID
    module: str
    status: str
    title: str
    rationale: str
    suggested_action: str
    confidence: float
    payload: dict[str, Any]
    created_at: datetime


class BriefingOut(_ORM):
    id: uuid.UUID
    channel: str
    status: str
    coverage_date: date
    title: str
    summary: str
    body_markdown: str
    metrics: dict[str, Any]
    source_event_ids: list[Any]
    created_at: datetime


class AgentDescriptorOut(BaseModel):
    name: str
    module: str
    description: str
    subscribed_event_types: list[str]


class AgentRunOut(_ORM):
    id: uuid.UUID
    agent_name: str
    module: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    activity: str
    metrics: dict[str, Any]
    error_message: str | None


class WorkflowRunOut(_ORM):
    id: uuid.UUID
    workflow_name: str
    module: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    input_payload: dict[str, Any]
    output_payload: dict[str, Any]
    error_message: str | None
    # Remediation runs link back to their source alert; ``alert_title`` is
    # populated by the router for the Workflows list/detail views.
    alert_id: uuid.UUID | None = None
    alert_title: str | None = None
    steps: list[dict[str, Any]] = []


class WorkflowDescriptorOut(BaseModel):
    name: str
    module: str


class IntegrationDescriptorOut(BaseModel):
    provider: str
    module: str
    display_name: str
    description: str
    required_settings: list[str]
    configured: bool


class LeadOut(_ORM):
    id: uuid.UUID
    full_name: str
    email: str | None
    company: str | None
    title: str | None
    status: str
    score: int
    score_reasoning: str
    next_action: str | None
    source: str
    signals: dict[str, Any]
    last_outreach_at: datetime | None
    created_at: datetime


class IncidentOut(_ORM):
    id: uuid.UUID
    title: str
    summary: str
    severity: str
    status: str
    started_at: datetime
    resolved_at: datetime | None
    ai_root_cause: str
    ai_remediation: str
    evidence: list[Any]


class IncidentFromAlertRequest(BaseModel):
    title: str
    severity: str | None = None
    summary: str | None = None


class CommandRequest(BaseModel):
    query: str
    # Slug of the cluster whose page the user is on, if any: the copilot's
    # default target for Kubernetes / metrics / logs questions.
    cluster: str | None = Field(default=None, max_length=64)


class CommandResponse(BaseModel):
    answer: str
    references: list[dict[str, Any]] = []


# ── Billing / SKU / usage ────────────────────────────────────────────────


class SkuOut(_ORM):
    id: uuid.UUID
    slug: str
    name: str
    tagline: str
    description: str
    routing_policy: str
    monthly_base_usd: float
    included_events_per_month: int
    price_local_in_per_mtok: float
    price_local_out_per_mtok: float
    price_external_classifier_in_per_mtok: float
    price_external_classifier_out_per_mtok: float
    price_external_quality_in_per_mtok: float
    price_external_quality_out_per_mtok: float
    monthly_soft_cap_usd: float
    is_active: bool
    display_order: int


class TenantSkuOut(_ORM):
    id: uuid.UUID
    sku_id: uuid.UUID
    current: bool
    started_at: datetime
    ended_at: datetime | None


class TenantSkuChangeRequest(BaseModel):
    sku_slug: str


class PeriodTotalOut(BaseModel):
    period_start: datetime
    period_end: datetime
    events: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    base_usd: float
    included_events: int
    included_events_used: int


class BreakdownRowOut(BaseModel):
    key: str
    events: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


class DailyPointOut(BaseModel):
    day: str
    events: int
    cost_usd: float


class LocalGpuStatusOut(BaseModel):
    configured: bool
    healthy: bool
    base_url: str
    model_classifier: str
    model_quality: str
    # Where the configured GPU comes from: "sovereign" (per-tenant GPU
    # onboarded over the tunnel), "local" (operator-wide env LOCAL tier),
    # or "none". Lets the UI label the banner accurately.
    source: str = "none"
    # gpu_tenants lifecycle state when source == "sovereign"
    # (pending/provisioning/active/error/…); None otherwise.
    state: str | None = None


class AlertActionOut(_ORM):
    id: uuid.UUID
    message_id: uuid.UUID
    tool_call_id: str
    tool_name: str
    tool_input: dict[str, Any]
    requires_approval: bool
    status: str
    result_output: str
    result_error: str
    approved_at: datetime | None
    executed_at: datetime | None
    created_at: datetime
    # AI-Ops maturity: autonomy-policy decision, approvals, preview, outcome.
    policy_mode: str = ""
    policy_rule: str = ""
    policy_reason: str = ""
    approvals_required: int = 1
    approvals: list[dict[str, Any]] = []
    preview: dict[str, Any] = {}
    auto_executed: bool = False
    verified_resolved: bool | None = None
    actor: str = "agent"


class AlertChatMessageOut(_ORM):
    id: uuid.UUID
    role: str
    content: str
    tool_calls_json: list[Any]
    tool_call_id: str | None
    created_at: datetime
    # Actions whose ``message_id`` matches this message (assistant only).
    actions: list[AlertActionOut] = []


class AlertChatPostRequest(BaseModel):
    content: str


class ChangeProposalOut(_ORM):
    id: uuid.UUID
    device_id: str
    # NV-CM environment the device lives in; null for the tenant's default
    # source of truth. Device ids repeat across environments, so the approval
    # queue needs this to say which fabric a row is about.
    environment_slug: str | None = None
    kind: str
    status: str
    intended_config: str
    observed_config: str
    diff: str
    renderer_version: str
    evidence: dict[str, Any]
    created_by: uuid.UUID | None
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    executed_at: datetime | None
    executor_result: dict[str, Any]
    created_at: datetime
