"""Per-tenant NVIDIA Config Manager (NV-CM) stack state.

One row per Daalu tenant that opted into the network config-management
plane. Owns the lifecycle of an isolated NV-CM Helm release (Render +
Config Store + Temporal + optional ZTP/DHCP/UI + bundled Nautobot) —
either in the operator's cluster (``target_cluster_tunnel_id IS NULL``)
or in the customer's own cluster reached via WireGuard.

Mirrors :class:`NautobotTenant` in shape and intent. The customer-facing
surface (service URLs + Keycloak client) is the tenant's
``Integration(provider="config_manager")`` row, written from this row
once provisioning reaches ``active``; this table is the operator-side
materialisation state driven by ``config_manager_controller``.

See docs/design/nv-config-manager-integration.md §6, §10.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
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


class ConfigManagerTenantState(str, enum.Enum):
    pending = "pending"
    provisioning = "provisioning"
    active = "active"
    error = "error"
    deleting = "deleting"
    destroyed = "destroyed"


class ConfigManagerTenant(
    UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base
):
    __tablename__ = "config_manager_tenants"
    __table_args__ = (
        # One row per (customer, environment). Was one row per customer; a
        # customer may now run several independently-named environments.
        UniqueConstraint(
            "tenant_id", "slug", name="uq_config_manager_tenants_tenant_slug"
        ),
        # At most one primary per customer — enforced by a partial unique
        # index created in migration 0030 (see `is_primary` below).
        Index(
            "uq_config_manager_tenants_one_primary",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
    )

    # Operator-supplied display name, e.g. "Production".
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    # DNS-safe identity derived from `name` at create time and **immutable**
    # afterwards: it is baked into the namespace and into public hostnames,
    # so renaming would be a rebuild, not a rename. Rules (single DNS label,
    # no tool-prefix collisions) live in config_manager_controller.env_slug.
    slug: Mapped[str] = mapped_column(String(24), nullable=False, default="")

    # The environment every existing consumer resolves when it asks for "the
    # tenant's config manager" (tool proxy, SoT client, NATS bridge,
    # onboarding writer). Keeps those paths working unchanged now that a
    # customer can have more than one environment.
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    state: Mapped[ConfigManagerTenantState] = mapped_column(
        SAEnum(ConfigManagerTenantState, name="config_manager_tenant_state"),
        default=ConfigManagerTenantState.pending,
        nullable=False,
    )

    # NULL → operator/local cluster. NOT NULL → customer cluster reached
    # via the named WireGuard tunnel. The controller picks its Helm/kube
    # target from this (reusing _load_customer_kubeconfig).
    target_cluster_tunnel_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("cluster_tunnels.id", ondelete="SET NULL"),
        nullable=True,
    )

    # K8s namespace the Helm release is installed into: ``cm-<slug>``.
    namespace: Mapped[str] = mapped_column(String(253), nullable=False)

    # Base hostname the chart derives per-component URLs from, e.g.
    # ``<slug>.cm.daalu.io``. Unique per tenant so HTTPRoutes/listeners on
    # the shared GatewayClass don't collide.
    base_hostname: Mapped[str] = mapped_column(String(253), nullable=False)

    # Chosen NV-CM components (chart ``services.*`` toggles), e.g.
    # {"render": true, "configStore": true, "temporal": true,
    #  "nautobot": true, "ztp": false, "dhcp": false, "ui": false}.
    components: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # Sizing profile (small/medium/large) → CNPG instance count + resource
    # overrides at values-render time.
    size_profile: Mapped[str] = mapped_column(
        String(16), default="small", nullable=False
    )

    # Pinned vendored chart version the controller installs/upgrades to.
    chart_version: Mapped[str] = mapped_column(
        String(64), default="", nullable=False
    )

    # Resolved per-component URLs (svc-* machine + human), populated when
    # state == active. Copied into the Integration(provider="config_manager")
    # row by the onboarding route.
    urls: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # In-cluster address of this environment's Envoy Gateway proxy Service,
    # resolved by the controller once the gateway exists. The hub tool proxy
    # dials it. It lives on the row rather than only on the tenant's
    # ``config_manager`` Integration because there is exactly one Integration
    # per tenant, tracking the primary — a non-primary environment would
    # otherwise have nowhere to publish its gateway and be unreachable.
    gateway_internal_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )

    # Keycloak service client the hub uses against NV-CM svc-* endpoints.
    keycloak_client_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    keycloak_client_secret_ciphertext: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )

    # Encrypted JSON bag of generated per-tenant secrets (e.g. CNPG/NATS
    # passwords) the controller stamps into the release — kept so an
    # operator can recover them without spelunking K8s Secrets.
    secrets_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_error: Mapped[str | None] = mapped_column(Text)
