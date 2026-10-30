"""ChangeProposal — the approval-gated record of a proposed device change.

This is the central artifact of the SoT + device-management pipeline:
the AI engine, a human, or the drift reconciler authors a proposal;
a human approves it in the UI; the executor service (a separate
identity, not the engine) is the only thing that can call
:func:`daalu_automation.core.change_proposals.execute` to actually push
config at a real device.

The ``intended_config`` and ``observed_config`` columns store the
*rendered* canonical text snapshotted at proposal time. The executor
re-renders fresh from the SoT before commit; if the freshly-rendered
text differs from the stored intended_config the row is flipped to
``stale`` and re-approval is required.

The ``evidence`` JSONB column follows this shape (documented in the
design doc):

.. code-block:: json

    {
      "triggered_by": "engine" | "user" | "reconciler" | "importer",
      "llm_reasoning": "...",
      "llm_model": "claude-sonnet-4-6",
      "evidence_events": ["<event_uuid>"],
      "evidence_alerts": ["<alert_uuid>"],
      "evidence_metrics": [{"name": "...", "value": "...", "ts": "..."}],
      "confidence": 0.0
    }

``device_id`` is intentionally a free string (the SoT-native ID — for
NautobotSoT it is the Nautobot Device UUID). We do **not** carry an FK
because device rows live in the SoT, not in this database.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class ChangeProposalKind(str, enum.Enum):
    intended_change = "intended_change"  # SoT user edited the intent
    drift = "drift"                       # observed != intended
    manual = "manual"                     # AI / user proposed directly
    # Imperative server lifecycle op (provision / power / reprovision)
    # executed via Tinkerbell. Unlike the declarative kinds above, the
    # gate does not render-and-drift-check these — it does an
    # observed-state compare. See change_proposals.execute_provision.
    provision_op = "provision_op"


class ChangeProposalStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    executed = "executed"
    failed = "failed"
    stale = "stale"


class ChangeProposal(
    UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base
):
    __tablename__ = "change_proposals"

    device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Which NV-CM management environment this device lives in. Each environment
    # bundles its own Nautobot, so the slug is what lets the executor re-resolve
    # the device in the same source of truth the proposal was authored against —
    # device ids are only unique *within* an environment. NULL means the
    # tenant's default resolution, which is what every row written before
    # multi-environment support meant, and still what a BYO-Nautobot tenant
    # means today.
    environment_slug: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    kind: Mapped[ChangeProposalKind] = mapped_column(
        SAEnum(ChangeProposalKind, name="change_proposal_kind"),
        nullable=False,
    )
    status: Mapped[ChangeProposalStatus] = mapped_column(
        SAEnum(ChangeProposalStatus, name="change_proposal_status"),
        default=ChangeProposalStatus.pending,
        nullable=False,
    )
    intended_config: Mapped[str] = mapped_column(Text, default="", nullable=False)
    observed_config: Mapped[str] = mapped_column(Text, default="", nullable=False)
    diff: Mapped[str] = mapped_column(Text, default="", nullable=False)
    renderer_version: Mapped[str] = mapped_column(
        String(32), default="", nullable=False
    )
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    executor_result: Mapped[dict] = mapped_column(
        JSONB, default=dict, nullable=False
    )

    __table_args__ = (
        Index(
            "ix_change_proposals_tenant_status",
            "tenant_id",
            "status",
        ),
        Index(
            "ix_change_proposals_tenant_device_status",
            "tenant_id",
            "device_id",
            "status",
        ),
    )
