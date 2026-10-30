from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    # Timezone the daily briefing should be rendered in (IANA name).
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    # sha256 hex of the per-tenant ingest API key. Lookup is by hash so
    # the cleartext key never lands in the database; rotated by issuing
    # a new key + overwriting this column.
    ingest_api_key_hash: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True
    )
    # Soft-delete — DELETE /tenants/{id} flips this rather than dropping
    # rows so audit / billing reconstruction stays possible.
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # GPU-provider grant — when true, this tenant may mark a GpuTenant
    # ``shared`` and register a ``gpu_pools`` row, i.e. sell its card's
    # capacity to other tenants through the inference-gateway. Granted by a
    # superuser only. The schema allows N providers; policy grants exactly
    # one today (the operator). Non-providers' GPUs stay private (SOVEREIGN)
    # and unshareable. See
    # docs/plans/nvidia-ai-factory/13-gpu-sharing-and-multi-tenant-marketplace.md.
    is_gpu_provider: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # Per-tenant feature toggles. Currently recognised keys:
    #   daalu_hosted_enabled : bool — opt into the operator-owned GPU pool
    #   sovereign_enabled    : bool — route to the tenant's own GPU via tunnel
    #   workspaces_enabled   : bool — show the /workspace IDE entry point
    # Treat as a read cache; the authoritative state for daalu-hosted
    # lives in ``daalu_hosted_quotas``.
    feature_flags: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # SOVEREIGN tier (customer's own GPU): URL reachable via the tenant's
    # federation tunnel, e.g. http://llm-classifier.daalu.svc.cluster.local:8000.
    # The bearer token is sha256-hashed for storage (same pattern as
    # ingest_api_key_hash) so a DB leak doesn't reveal credentials. The
    # cleartext is shown to the admin once at configure-time.
    sovereign_inference_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sovereign_inference_token_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    # Fernet-encrypted cleartext of the SOVEREIGN bearer token. Unlike
    # the hash above (verification only), the router decrypts this to
    # actually call the tenant's vLLM endpoint. Written by the GPU
    # onboarding route; NULL when the endpoint needs no auth (common for
    # in-cluster vLLM). See core/crypto.py + core/llm._load_tenant_routing_config.
    sovereign_inference_token_enc: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    # Served model ids on the tenant's SOVEREIGN endpoint. classifier =
    # what the router sends by default; quality = optional heavier model.
    sovereign_model_classifier: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    sovereign_model_quality: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    # Daalu Private tier — see docs/design/daalu-private.md.
    # ``is_private`` is the top-level toggle every consumer should
    # branch on; the other two columns are only consulted when this
    # is true. ``edge_agents_enabled`` shifts the agent host into the
    # customer's cluster; ``private_db_url`` points tenant-scoped
    # reads/writes at a Postgres the customer operates (the hub DB
    # only keeps account/billing tables for Private tenants).
    is_private: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    edge_agents_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    private_db_url: Mapped[str | None] = mapped_column(String, nullable=True)
    private_db_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Edge data plane (Daalu Private full-sovereignty mode). When set,
    # the hub's EdgeForwardMiddleware proxies all tenant-scoped requests
    # to this URL via the WireGuard tunnel; rows never touch the hub
    # DB. The token cleartext lives in a K8s Secret on the edge — only
    # its sha256 is stored on the hub. See
    # docs/design/edge-data-plane.md.
    edge_data_url: Mapped[str | None] = mapped_column(String, nullable=True)
    edge_data_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
