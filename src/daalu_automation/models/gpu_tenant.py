"""Per-tenant local-GPU (vLLM) instance state.

One row per Daalu tenant that onboarded their GPU. Owns the lifecycle
of a vLLM serving stack (the manifests in ``deploy/k8s/gpu/*``) —
either in the operator's cluster (``target_cluster_tunnel_id IS NULL``)
or in the customer's own cluster reached via WireGuard
(``target_cluster_tunnel_id IS NOT NULL``).

This is the operator-side materialisation state, mirroring
``NautobotTenant``. The *customer-facing* surface is the tenant's
SOVEREIGN routing config (``Tenant.sovereign_inference_url`` + token),
which the provision route writes once this row reaches ``active`` — at
which point ``core/llm.py`` routes LLM calls to the GPU automatically.

We do NOT reimplement vLLM serving; the controller applies the existing
``deploy/k8s/gpu`` manifests onto the target cluster. See
``docs/plans/2026-06-02-gpu-onboarding.md``.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class GpuTenantState(str, enum.Enum):
    # Row created; the controller hasn't applied the GPU stack yet.
    pending = "pending"
    # Manifests applied; waiting for the vLLM Deployment to report ready
    # AND the OpenAI ``/v1/models`` endpoint to answer (first boot pulls
    # ~5 GB of weights + compiles CUDA graphs — can take ~10 min).
    provisioning = "provisioning"
    # vLLM is serving; the endpoint has been registered as the tenant's
    # SOVEREIGN tier.
    active = "active"
    # Reconcile hit a failure (bad kubeconfig, no GPU node, image pull,
    # HF gate). Operator action required — see ``last_error``.
    error = "error"
    # Teardown requested; manifests being deleted.
    deleting = "deleting"
    # Manifests deleted; row kept for audit.
    destroyed = "destroyed"


class GpuTenant(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "gpu_tenants"
    __table_args__ = (
        # One GPU stack per Daalu tenant (single shared vLLM per tenant
        # for now; multi-model is a later concern modelled separately).
        UniqueConstraint("tenant_id", name="uq_gpu_tenants_tenant_id"),
    )

    state: Mapped[GpuTenantState] = mapped_column(
        SAEnum(GpuTenantState, name="gpu_tenant_state"),
        default=GpuTenantState.pending,
        nullable=False,
    )

    # Where the vLLM stack lives. NULL → operator cluster. NOT NULL →
    # customer cluster reached via the named WireGuard tunnel; the
    # controller picks its K8s client target from this.
    target_cluster_tunnel_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("cluster_tunnels.id", ondelete="SET NULL"),
        nullable=True,
    )

    # K8s namespace the stack is stamped into. The existing manifests
    # use ``daalu``; kept configurable for isolation later.
    namespace: Mapped[str] = mapped_column(
        String(253), nullable=False, default="daalu"
    )

    # Node label the vLLM pod targets (``gpu-class=<gpu_class>``) and an
    # optional explicit node hint for diagnostics. ``ada-16`` matches the
    # label ``scripts/deploy-gpu.sh`` stamps on the GPU node.
    gpu_class: Mapped[str] = mapped_column(
        String(64), nullable=False, default="ada-16"
    )
    gpu_node: Mapped[str | None] = mapped_column(String(253), nullable=True)

    # Served model ids. ``model_classifier`` is what the router sends and
    # vLLM advertises (``--served-model-name``). ``model_quality`` is the
    # optional heavier model for the quality tier (empty = not served).
    model_classifier: Mapped[str] = mapped_column(
        String(255), nullable=False, default="meta/llama-3.1-8b-instruct"
    )
    model_quality: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # vLLM ``--gpu-memory-utilization`` override. NULL = the model's catalog
    # default (the whole card). Set it when the card is time-sliced with
    # another vLLM (e.g. the S1-01 demo workload) so the two fit side by side;
    # the controller re-applies every tick, so a hand edit on the Deployment
    # would be reverted.
    gpu_memory_utilization: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )

    # HF token for the gated Llama repo, encrypted at rest with the same
    # Fernet key the rest of daalu uses (``core/crypto.py``). The
    # controller materialises it into the ``hf-token`` Secret on apply.
    hf_token_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Shared-pool flag. When true this stack is offered to OTHER tenants via
    # the inference-gateway (a ``gpu_pools`` row points at it) rather than
    # being the owner's private SOVEREIGN endpoint. May only be true when the
    # owning tenant holds ``Tenant.is_gpu_provider`` — enforced in the
    # provision path AND by a DB trigger (Postgres CHECK can't subquery).
    # See docs/plans/nvidia-ai-factory/13-gpu-sharing-and-multi-tenant-marketplace.md.
    shared: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # The resolved endpoint the router will call once active — e.g.
    # ``http://<tunnel_ip>:8000/v1`` (daalu-edge svcproxy-vllm) or an
    # in-cluster DNS.
    # Populated by the provision route, not the controller.
    service_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    last_ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_error: Mapped[str | None] = mapped_column(Text)
