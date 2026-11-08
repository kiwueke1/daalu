"""AI-Ops Maturity Model (AOMM) tables.

See ``docs/design/ai-ops-maturity.md`` for the contract these back:

* :class:`AutonomyPolicy` — versioned policy-as-code YAML per tenant.
* :class:`AIOpsTenantState` — kill switch + circuit-breaker state.
* :class:`Investigation` — one AI investigation per alert; the learning-loop
  record (time to triage, outcome, human verdict).
* :class:`AuditEvent` — append-only, per-tenant hash chain.
* :class:`Runbook` — runbook-as-code the agent reads and drafts.
* :class:`EvalRun` — one eval-harness scenario run.
* :class:`MaturityAssessment` — self-assessment answers.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class AutonomyPolicy(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "autonomy_policies"
    __table_args__ = (UniqueConstraint("tenant_id", "version", name="uq_autonomy_policy_version"),)

    version: Mapped[int] = mapped_column(Integer, nullable=False)
    yaml: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class AIOpsTenantState(TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "aiops_tenant_state"

    # One row per tenant: tenant_id is the primary key.
    tenant_id: Mapped[uuid.UUID] = mapped_column(  # type: ignore[assignment]
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    kill_switch_by: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    kill_switch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # rule_id -> ISO-8601 time the breaker stays tripped until.
    breaker: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class Investigation(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "investigations"

    alert_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("alerts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    triaged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    root_cause: Mapped[str] = mapped_column(Text, default="", nullable=False)
    actions_proposed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actions_approved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actions_rejected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actions_auto: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # open | auto | approved | manual | unresolved
    resolution: Mapped[str] = mapped_column(String(16), default="open", nullable=False, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified: Mapped[bool | None] = mapped_column(Boolean)
    runbook_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # correct | partial | wrong
    verdict: Mapped[str | None] = mapped_column(String(16), index=True)
    verdict_by: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    verdict_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verdict_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    model: Mapped[str] = mapped_column(String(128), default="", nullable=False)


class AuditEvent(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (UniqueConstraint("tenant_id", "seq", name="uq_audit_event_seq"),)

    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    subject: Mapped[str] = mapped_column(String(255), default="", nullable=False, index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)


class Runbook(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "runbooks"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_runbook_slug"),)

    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # {"alertname": [...], "labels": {...}}
    match: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    allowed_actions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # draft | published
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False, index=True)
    # manual | generated | imported
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    from_investigation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investigations.id", ondelete="SET NULL")
    )
    published_by: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvalRun(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "eval_runs"

    scenario_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    alert_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("alerts.id", ondelete="SET NULL")
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class MaturityAssessment(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "maturity_assessments"

    answers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), default="", nullable=False)
