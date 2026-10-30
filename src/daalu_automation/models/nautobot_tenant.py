"""Per-tenant Nautobot instance state.

One row per Daalu tenant that opted into the hosted-Nautobot tier.
Owns the lifecycle of an isolated Nautobot stack (web + worker +
scheduler + postgres + redis) — either in the operator's cluster
(``target_cluster_tunnel_id IS NULL``) or in the customer's own
cluster reached via WireGuard (``target_cluster_tunnel_id IS NOT
NULL``).

Replaces the pre-2026 shared-Nautobot model where every tenant got
an ObjectPermission slice of one shared instance — see
[[sot_nautobot_only]] for the architectural pivot context.

The hostname + admin token written here are the same values the
tenant's ``Integration(provider="nautobot")`` row stores; the
Integration row is the customer-facing surface used by the
reconciler/executor/adapter code, and this row is the operator-side
materialisation state.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
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


class NautobotTenantState(str, enum.Enum):
    # Row created; the controller hasn't materialised K8s yet.
    pending = "pending"
    # K8s manifests applied; waiting for the web pod to report ready.
    provisioning = "provisioning"
    # Web pod is ready, admin user bootstrapped, token captured.
    active = "active"
    # Reconcile loop hit a permanent failure (e.g., kubeconfig invalid
    # for customer-cluster mode, image pull failure, schema migration
    # error). Operator action required — see ``last_error``.
    error = "error"
    # Customer asked to tear it down; manifests are being deleted.
    deleting = "deleting"
    # Manifests deleted; row kept for audit. PVC may be retained
    # depending on the destroy mode.
    destroyed = "destroyed"


class NautobotTenant(
    UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base
):
    __tablename__ = "nautobot_tenants"
    __table_args__ = (
        # One Nautobot per Daalu tenant. If a tenant ever needs two
        # (staging + prod), we'd model that as separate Daalu tenants
        # rather than complicate this one-to-one.
        UniqueConstraint("tenant_id", name="uq_nautobot_tenants_tenant_id"),
    )

    state: Mapped[NautobotTenantState] = mapped_column(
        SAEnum(NautobotTenantState, name="nautobot_tenant_state"),
        default=NautobotTenantState.pending,
        nullable=False,
    )

    # Where the Nautobot stack lives. NULL → operator cluster (Phase 1).
    # NOT NULL → customer cluster reached via the named WireGuard tunnel
    # (Phase 2). The controller picks its K8s client target from this.
    target_cluster_tunnel_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("cluster_tunnels.id", ondelete="SET NULL"),
        nullable=True,
    )

    # K8s namespace the controller stamps everything into. Deterministic
    # so the operator can find a tenant's pods without DB access:
    # ``nautobot-<daalu-tenant-slug>``.
    namespace: Mapped[str] = mapped_column(String(253), nullable=False)

    # Public hostname for the per-tenant Nautobot. Operator-cluster
    # mode: ``<slug>.sot.daalu.io`` (cert-managed). Customer-cluster
    # mode: NULL — daalu reaches the pod via the wg tunnel + in-cluster
    # Service DNS, no public ingress.
    hostname: Mapped[str | None] = mapped_column(String(253), nullable=True)

    # The Nautobot superuser's API token, encrypted with the same
    # Fernet key the rest of daalu uses for credentials at rest
    # (``core/crypto.py``). This is the cleartext token the customer's
    # ``Integration(provider="nautobot")`` row will store after the
    # provisioning route copies it across.
    admin_token_ciphertext: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )

    # The Postgres password the per-tenant Postgres pod was stamped
    # with. Kept encrypted so an operator can recover it without
    # spelunking through K8s Secrets if something goes sideways.
    postgres_password_ciphertext: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )

    # NAUTOBOT_SECRET_KEY — Django session signing key. Generated once
    # at provision time and reused on every reconcile (rotating it
    # invalidates every session).
    secret_key_ciphertext: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )

    # Whichever sub-pod readinessProbe last surfaced as ready. Used by
    # the reconcile loop's pending → active transition. NULL = never
    # observed ready.
    last_ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_error: Mapped[str | None] = mapped_column(Text)
