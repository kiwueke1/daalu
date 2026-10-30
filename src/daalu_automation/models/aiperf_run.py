"""A single AIPerf load-test / benchmark run, triggered from the AI-factory UI.

AIPerf (``ai-dynamo/aiperf``, Apache-2.0) is a pure OpenAI-compatible load
generator — it drives a concurrency sweep against an endpoint and reports
TTFT / ITL / throughput vs concurrency (the SLO curve behind the pricing model;
see ``docs/plans/nvidia-ai-factory/04-aiperf.md``).

A sweep *is* load, and it is a **site-wide** measurement tool (it benchmarks the
operator's shared serving stack, not one tenant's view), so unlike the per-tenant
GPU diagnostics this is **super-admin only**: the API gates creation behind
``current_superuser`` and lists every run to any site admin. ``tenant_id`` is kept
only for provenance (which admin's tenant the run was kicked from).

Like ``gpu_diagnostic_runs``, daalu-api writes the row ``pending`` and the
**gpu-controller reconcile loop** runs the AIPerf Job on the operator cluster
(the only component with K8s access) and writes back ``output`` + ``summary`` +
``state``. The exec is gated by ``settings.gpu_aiperf_exec_enabled`` (off by
default — a full sweep against the single shared prod card spikes live tenants).
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class AiperfRunState(str, enum.Enum):
    pending = "pending"
    running = "running"
    passed = "passed"
    failed = "failed"
    error = "error"


class AiperfRun(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "aiperf_runs"

    state: Mapped[AiperfRunState] = mapped_column(
        SAEnum(AiperfRunState, name="aiperf_run_state"),
        default=AiperfRunState.pending,
        nullable=False,
    )
    # The endpoint under test (an in-cluster Service URL) + the served-model
    # name AIPerf requests. The default target is the operator's llm-classifier.
    target_url: Mapped[str] = mapped_column(String(512), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    endpoint_type: Mapped[str] = mapped_column(
        String(32), default="chat", nullable=False
    )
    # The concurrency sweep, as the comma list AIPerf consumes ("1,2,4,8,16,32").
    concurrency: Mapped[str] = mapped_column(String(255), nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    streaming: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Whether the run targeted the inference-gateway (front door) vs raw vLLM —
    # a label so the UI can compare gateway-overhead runs against direct runs.
    via_gateway: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # Parsed result: per-concurrency TTFT/ITL/throughput rows + the saturation
    # knee, or {error, code} on failure.
    summary: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # Raw stdout from the AIPerf container (truncated by the runner).
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
