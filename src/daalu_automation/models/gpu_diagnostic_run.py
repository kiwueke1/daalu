"""A single GPU diagnostic / validation run, triggered from the AI-factory UI.

Three kinds:

* ``dcgmi_diag``           — ``dcgmi diag -r {level}`` (NVVS) on the card.
* ``nccl_test``            — collective bandwidth/latency (multi-GPU only).
* ``observability_validate`` — the doc 02 §4A read-only checklist (run inline
  by daalu-api against Prometheus; no on-GPU exec).

``dcgmi_diag`` / ``nccl_test`` need to exec on the GPU node, so daalu-api writes
the row ``pending`` and the **gpu-controller reconcile loop** runs it over the
tunnel and writes back ``output`` + ``state`` (the only component that touches
the K8s/GPU side). ``observability_validate`` is resolved synchronously by the
API and lands ``passed``/``failed`` immediately.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class GpuDiagnosticKind(str, enum.Enum):
    dcgmi_diag = "dcgmi_diag"
    nccl_test = "nccl_test"
    observability_validate = "observability_validate"


class GpuDiagnosticState(str, enum.Enum):
    pending = "pending"
    running = "running"
    passed = "passed"
    failed = "failed"
    error = "error"


class GpuDiagnosticRun(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "gpu_diagnostic_runs"

    gpu_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("gpu_tenants.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[GpuDiagnosticKind] = mapped_column(
        SAEnum(GpuDiagnosticKind, name="gpu_diagnostic_kind"), nullable=False
    )
    # dcgmi diag run-level (1/2/3); null for nccl/validate.
    level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[GpuDiagnosticState] = mapped_column(
        SAEnum(GpuDiagnosticState, name="gpu_diagnostic_state"),
        default=GpuDiagnosticState.pending,
        nullable=False,
    )
    # Structured result (per-check pass/fail for validate; subtest map for diag).
    summary: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # Raw stdout/stderr from the diagnostic command (truncated by the runner).
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
