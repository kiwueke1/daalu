"""One-click install of an observability store into the tenant's cluster.

``POST /observability/{provider}/install`` applies the vendored manifests
(``deploy_assets/observability/*.yaml``) over the tenant's registered
kubeconfig — the same per-tenant credential the kubectl tools and the
gpu-controller use, which points at the cluster's API server over the
WireGuard tunnel. After the apply, the matching ``Integration`` row is
upserted with the store's deterministic in-cluster URL (+ the cluster's
tunnel id) so the daalu-native console lights up as soon as the pods are
Ready.

Grafana needs one non-deterministic extra — a service-account API token for
``/dashboards`` — so its install spawns a background finalizer that polls
``/api/health`` through the edge proxy and mints the token with the admin
credential the manifest provisions.

Apply semantics: create, and on 409 merge-patch — idempotent re-install,
no server-side-apply version requirements on the kubernetes client.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import structlog
import yaml
from sqlalchemy import select

from daalu_automation.core.cluster_proxy import get_proxy_url
from daalu_automation.core.kube_tools import KubeUnavailable, _tenant_kube_or_default
from daalu_automation.database import AsyncSessionLocal
from daalu_automation.models import Integration, IntegrationStatus

logger = structlog.get_logger(__name__)

_ASSETS = Path(__file__).resolve().parent.parent / "deploy_assets" / "observability"

# provider → (manifest files, deterministic in-cluster URL, display name)
INSTALLABLE: dict[str, dict[str, Any]] = {
    "grafana": {
        "files": ["grafana.yaml"],
        "url": "http://grafana.monitoring.svc.cluster.local",
        "name": "Grafana",
    },
    "opensearch": {
        "files": ["opensearch.yaml", "fluent-bit.yaml"],
        "url": "http://opensearch.opensearch.svc.cluster.local:9200",
        "name": "OpenSearch",
    },
}

# The admin credential grafana.yaml provisions (used once, by the finalizer,
# to mint the service-account token the integration row stores).
_GRAFANA_ADMIN = ("admin", "daalu-grafana-lab")

_FINALIZE_TIMEOUT_S = 300
_FINALIZE_POLL_S = 10


class ObsInstallError(RuntimeError):
    """The install could not be performed."""


def load_manifests(provider: str) -> list[dict[str, Any]]:
    spec = INSTALLABLE.get(provider)
    if spec is None:
        raise ObsInstallError(f"provider {provider!r} is not installable")
    docs: list[dict[str, Any]] = []
    for fname in spec["files"]:
        path = _ASSETS / fname
        if not path.is_file():
            raise ObsInstallError(f"vendored manifest missing: {fname}")
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict) and doc.get("kind"):
                docs.append(doc)
    return docs


def _apply_docs_sync(api_client: Any, docs: list[dict[str, Any]]) -> list[str]:
    """Create-or-merge-patch every doc. Returns 'Kind/name' per applied doc."""
    from kubernetes import client as k8s_client  # type: ignore
    from kubernetes.client.rest import ApiException  # type: ignore
    from kubernetes.dynamic import DynamicClient  # type: ignore

    dyn = DynamicClient(api_client)
    core = k8s_client.CoreV1Api(api_client=api_client)

    # Namespaces first — both the explicit Namespace docs and any namespace a
    # namespaced doc references (grafana rides `monitoring`, which this
    # cluster may not have).
    needed_ns = {d["metadata"]["namespace"] for d in docs
                 if d.get("metadata", {}).get("namespace")}
    declared_ns = {d["metadata"]["name"] for d in docs
                   if d.get("kind") == "Namespace"}
    for ns in sorted(needed_ns - declared_ns):
        try:
            core.create_namespace({"metadata": {"name": ns}})
        except ApiException as e:
            if e.status != 409:
                raise

    applied: list[str] = []
    for doc in docs:
        kind = doc["kind"]
        name = doc["metadata"]["name"]
        ns = doc["metadata"].get("namespace")
        resource = dyn.resources.get(
            api_version=doc["apiVersion"], kind=kind
        )
        try:
            resource.create(body=doc, namespace=ns)
        except ApiException as e:
            if e.status != 409:
                raise
            resource.patch(
                body=doc, name=name, namespace=ns,
                content_type="application/merge-patch+json",
            )
        applied.append(f"{kind}/{name}")
    return applied


async def _kubernetes_row(
    db: Any, tenant_id: uuid.UUID
) -> Integration | None:
    return (
        await db.execute(
            select(Integration)
            .where(
                Integration.tenant_id == tenant_id,
                Integration.provider == "kubernetes",
            )
            .order_by(Integration.created_at)
        )
    ).scalars().first()


async def install(
    db: Any, tenant_id: uuid.UUID, provider: str
) -> dict[str, Any]:
    """Apply the provider's manifests + upsert its integration row."""
    spec = INSTALLABLE.get(provider)
    if spec is None:
        raise ObsInstallError(
            f"provider {provider!r} is not installable "
            f"(supported: {', '.join(sorted(INSTALLABLE))})"
        )
    docs = load_manifests(provider)

    kube_row = await _kubernetes_row(db, tenant_id)
    if kube_row is None:
        raise ObsInstallError(
            "no Kubernetes cluster is registered for this tenant — "
            "onboard the cluster first (Managed infra → Onboarding)"
        )

    try:
        core_v1, _apps, _mod = await _tenant_kube_or_default(tenant_id)
    except KubeUnavailable as e:
        raise ObsInstallError(str(e)) from e

    applied = await asyncio.to_thread(
        _apply_docs_sync, core_v1.api_client, docs
    )
    logger.info("obs_install.applied", provider=provider,
                tenant_id=str(tenant_id), applied=applied)

    # Upsert the integration row so the console lights up once pods are Ready.
    row = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == tenant_id,
                Integration.provider == provider,
            )
        )
    ).scalar_one_or_none()
    cfg: dict[str, Any] = {"url": spec["url"]}
    if row is None:
        row = Integration(
            tenant_id=tenant_id,
            provider=provider,
            module="infra",
            name=f"{spec['name']} (installed by Daalu)",
            status=IntegrationStatus.connected,
            config=cfg,
            cluster_tunnel_id=kube_row.cluster_tunnel_id,
        )
        db.add(row)
    else:
        # Keep any existing credential fields (e.g. a grafana api_token from
        # a previous finalize) — only refresh the URL + tunnel binding.
        row.config = {**(row.config or {}), **cfg}
        row.cluster_tunnel_id = (
            row.cluster_tunnel_id or kube_row.cluster_tunnel_id
        )
        row.status = IntegrationStatus.connected
    await db.commit()

    finalizing = False
    if provider == "grafana" and not (row.config or {}).get("api_token"):
        # Mint the /dashboards token once grafana answers. Fire-and-forget:
        # the row flips to carrying api_token when it lands.
        asyncio.get_running_loop().create_task(
            _finalize_grafana(tenant_id)
        )
        finalizing = True

    return {"applied": applied, "finalizing": finalizing}


async def _finalize_grafana(tenant_id: uuid.UUID) -> None:
    """Poll grafana through the tunnel; mint + stamp the SA API token."""
    import httpx

    user, password = _GRAFANA_ADMIN
    deadline = asyncio.get_running_loop().time() + _FINALIZE_TIMEOUT_S
    while True:
        try:
            async with AsyncSessionLocal() as db:
                row = (
                    await db.execute(
                        select(Integration).where(
                            Integration.tenant_id == tenant_id,
                            Integration.provider == "grafana",
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    return
                if (row.config or {}).get("api_token"):
                    return  # already finalized (e.g. concurrent install)
                base = (row.config or {}).get("url", "").rstrip("/")
                proxy = await get_proxy_url(db, row.cluster_tunnel_id)
            async with httpx.AsyncClient(
                proxy=proxy, timeout=15, auth=(user, password)
            ) as c:
                h = await c.get(f"{base}/api/health")
                if h.status_code == 200:
                    sa = await c.post(
                        f"{base}/api/serviceaccounts",
                        json={"name": "daalu-hub", "role": "Admin"},
                    )
                    if sa.status_code == 400 and "already exists" in sa.text:
                        # Look the existing SA up by name.
                        q = await c.get(
                            f"{base}/api/serviceaccounts/search",
                            params={"query": "daalu-hub"},
                        )
                        sa_id = (q.json().get("serviceAccounts") or [{}])[0].get("id")
                    else:
                        sa.raise_for_status()
                        sa_id = sa.json()["id"]
                    tok = await c.post(
                        f"{base}/api/serviceaccounts/{sa_id}/tokens",
                        json={"name": f"daalu-hub-{uuid.uuid4().hex[:6]}"},
                    )
                    tok.raise_for_status()
                    key = tok.json()["key"]
                    async with AsyncSessionLocal() as db:
                        row = (
                            await db.execute(
                                select(Integration).where(
                                    Integration.tenant_id == tenant_id,
                                    Integration.provider == "grafana",
                                )
                            )
                        ).scalar_one_or_none()
                        if row is not None:
                            row.config = {**(row.config or {}), "api_token": key}
                            await db.commit()
                    logger.info("obs_install.grafana_finalized",
                                tenant_id=str(tenant_id))
                    return
        except Exception as e:  # noqa: BLE001 — retry until deadline
            logger.debug("obs_install.grafana_finalize_retry", error=str(e)[:120])
        if asyncio.get_running_loop().time() > deadline:
            logger.warning("obs_install.grafana_finalize_timeout",
                           tenant_id=str(tenant_id))
            return
        await asyncio.sleep(_FINALIZE_POLL_S)
