"""Resolve the (kubeconfig, namespace) for a tenant's Tinkerbell cluster.

One source of truth shared by the executor (``change_proposals.
execute_provision``) and the health precheck (``tinkerbell.health``), so
both reach the same mgmt cluster the same way.

Reads ``Integration(provider="tinkerbell")``. The kubeconfig comes from a
referenced ``cluster_tunnel_id`` (sibling ``Integration(provider=
"kubernetes")``) or an inline ``kubeconfig`` blob; absent both, ``None``
means in-cluster (controller co-located with Tinkerbell).
"""

from __future__ import annotations

import uuid

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.models import Integration


async def resolve_tinkerbell_target(
    db: AsyncSession, tenant_id: uuid.UUID
) -> tuple[dict | None, str]:
    """Return ``(kubeconfig_dict | None, namespace)`` for the tenant.

    Raises ``LookupError`` when no tinkerbell integration exists or its
    referenced kubeconfig can't be resolved.
    """
    row = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == tenant_id,
                Integration.provider == "tinkerbell",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(f"no tinkerbell integration for tenant {tenant_id}")
    cfg = row.config or {}
    namespace = cfg.get("namespace", "tink-system")

    if cfg.get("kubeconfig"):
        parsed = yaml.safe_load(cfg["kubeconfig"])
        if not isinstance(parsed, dict):
            raise LookupError("tinkerbell integration kubeconfig is not valid YAML")
        return parsed, namespace

    tunnel_id = cfg.get("cluster_tunnel_id")
    if tunnel_id:
        # Sibling Integration(provider="kubernetes") holds the kubeconfig
        # for the tunnel's cluster — same convention the controllers use.
        kube_row = (
            await db.execute(
                select(Integration).where(
                    Integration.tenant_id == tenant_id,
                    Integration.provider == "kubernetes",
                )
            )
        ).scalar_one_or_none()
        blob = (kube_row.config or {}).get("kubeconfig") if kube_row else None
        if not blob:
            raise LookupError(
                "tinkerbell integration references a cluster_tunnel but no "
                "Integration(provider='kubernetes') kubeconfig was found"
            )
        parsed = yaml.safe_load(blob)
        if not isinstance(parsed, dict):
            raise LookupError("kubernetes integration kubeconfig is not valid YAML")
        return parsed, namespace

    return None, namespace
