"""Managed-cluster onboarding API.

A "cluster" here is a customer-owned Kubernetes cluster that we reach
over a WireGuard tunnel. The flow:

1. Operator POSTs /clusters {slug, name}. We generate an operator-side
   WG keypair, allocate a tunnel IP from 10.200.0.0/16, mint a one-shot
   invite token, and write the row. The response includes the
   plaintext invite token + an install snippet — neither is ever shown
   again.
2. Operator shares the snippet with the customer. They run
   `helm install daalu-edge ... --set bootstrap.token=...`. The edge
   container brings WireGuard up against the hub, then POSTs back to
   /clusters/{slug}/callback with its own pubkey + endpoint, presenting
   the invite token as a bearer credential.
3. The callback handler verifies the token, stores the customer pubkey,
   moves the row to ``awaiting_handshake``, and patches the hub Secret
   so the new Peer block becomes live. Once the hub observes a real
   handshake, the beat task flips the row to ``connected``.

Token discipline. ``invite_token_hash`` is cleared on first successful
callback, so a leaked snippet can only be redeemed once.
"""

from __future__ import annotations

import re
import uuid
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_admin, current_tenant_id, current_user
from daalu_automation.config import get_settings
from daalu_automation.core import kube_console
from daalu_automation.core.kube_console import KubeConsoleError
from daalu_automation.core.wireguard import (
    HubSecretUnavailable,
    allocate_tunnel_ip,
    encrypt_keypair,
    generate_invite_token,
    generate_keypair,
    hash_invite_token,
    render_edge_install_snippet,
    sync_hub_secret,
)
from daalu_automation.database import get_db
from daalu_automation.models import (
    ClusterTunnel,
    ClusterTunnelStatus,
    User,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/clusters", tags=["clusters"])


_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


# ── schemas ─────────────────────────────────────────────────────────────


class ClusterCreate(BaseModel):
    slug: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description=(
            "DNS-style identifier the customer types when running the edge "
            "(lowercase letters, digits, hyphens — no leading/trailing dash)."
        ),
    )
    name: str = Field(..., min_length=1, max_length=255)


class ClusterOut(BaseModel):
    id: str
    slug: str
    name: str
    status: str
    tunnel_ip: str
    operator_pubkey: str
    customer_pubkey: str | None
    customer_endpoint: str | None
    last_handshake_at: str | None
    last_error: str | None


class ClusterCreateOut(ClusterOut):
    # Shown exactly once. The plaintext token never persists; subsequent
    # GETs return None here so a stale browser tab can't re-leak it.
    invite_token: str
    install_snippet: str


class ClusterCallbackIn(BaseModel):
    invite_token: str = Field(..., min_length=8)
    customer_pubkey: str = Field(
        ..., min_length=44, max_length=44,
        description="WireGuard pubkey (raw 32-byte X25519, base64 = 44 chars).",
    )
    customer_endpoint: str | None = Field(
        default=None, max_length=255,
        description=(
            "host:port the hub can reach the edge on, if the edge has a "
            "stable public endpoint. Optional — usually NAT-traversed."
        ),
    )
    kube_token: str | None = Field(
        default=None, max_length=8192,
        description=(
            "Long-lived ServiceAccount token for this cluster's apiserver. "
            "When present the hub assembles the kubeconfig itself and stores "
            "it as the cluster's Integration(provider='kubernetes'), so no "
            "one has to paste one — and a rebuilt cluster replaces its own "
            "stale credential instead of failing a later provision with a "
            "bare 401. Optional: omit to manage that Integration by hand."
        ),
    )


# ── helpers ─────────────────────────────────────────────────────────────


def _to_out(row: ClusterTunnel) -> ClusterOut:
    return ClusterOut(
        id=str(row.id),
        slug=row.slug,
        name=row.name,
        status=row.status.value,
        tunnel_ip=str(row.tunnel_ip),
        operator_pubkey=row.operator_pubkey,
        customer_pubkey=row.customer_pubkey,
        customer_endpoint=row.customer_endpoint,
        last_handshake_at=row.last_handshake_at.isoformat()
        if row.last_handshake_at
        else None,
        last_error=row.last_error,
    )


def _require_endpoint(settings) -> str:
    if not settings.wireguard_public_endpoint:
        raise HTTPException(
            503,
            "wireguard_public_endpoint is not configured — set it in the "
            "daalu-api Secret before onboarding clusters",
        )
    return settings.wireguard_public_endpoint


def _require_api_public_url(settings) -> str:
    if not settings.operator_api_public_url:
        raise HTTPException(
            503,
            "operator_api_public_url is not configured — the install "
            "snippet would point the customer edge at an unresolvable "
            "host. Set OPERATOR_API_PUBLIC_URL in the daalu-api Secret "
            "(e.g. https://ops.daalu.io) before onboarding clusters",
        )
    return settings.operator_api_public_url.rstrip("/")


async def _load_hub_pubkey() -> str | None:
    """Best-effort read of the hub's current PublicKey from the Secret.

    Used to build the install snippet. Returns None if we can't reach
    the Secret (local dev, RBAC missing, hub not enabled) — the snippet
    still renders, with a placeholder, so the operator gets actionable
    output instead of a 500.

    Order of attempts:

    1. Read the WireGuard hub Secret in-cluster and derive the public
       key from its private key (canonical source of truth — survives
       hub keypair rotation).
    2. Fall back to ``settings.wireguard_hub_public_key`` if that
       failed. Set this in the daalu-api Secret as a belt-and-braces
       so the install snippet still works when the in-cluster Secret
       read fails (e.g. the kubernetes-client lib's intermittent 401s).
    """
    import asyncio
    import base64

    from daalu_automation.core.wireguard import (
        HUB_SECRET_KEY,
        HUB_SECRET_NAME,
        _ensure_hub_config_file,
        _load_in_cluster_core_v1,
        _resolve_namespace,
        hub_config_file,
    )

    def _read_conf() -> str | None:
        conf_path = hub_config_file()
        if conf_path:
            # File-backed hub (Docker Compose): seed the keypair on first
            # onboard so the snippet carries a real public key.
            settings = get_settings()
            try:
                return _ensure_hub_config_file(
                    conf_path,
                    listen_port=settings.wireguard_listen_port,
                    hub_address=settings.wireguard_hub_address,
                )
            except OSError:
                return None
        try:
            ns = _resolve_namespace()
            core_v1, _ = _load_in_cluster_core_v1()
            sec = core_v1.read_namespaced_secret(HUB_SECRET_NAME, ns)
        except HubSecretUnavailable:
            return None
        except Exception:  # noqa: BLE001
            return None
        data = (sec.data or {}).get(HUB_SECRET_KEY)
        if not data:
            return None
        return base64.b64decode(data).decode("utf-8")

    def _read() -> str | None:
        conf = _read_conf()
        if not conf:
            return None
        # The Secret holds the hub's PRIVATE key; we derive the public
        # half here so the operator never copy-pastes the private key
        # into a customer-bound install snippet.
        m = re.search(r"^PrivateKey\s*=\s*(\S+)", conf, flags=re.MULTILINE)
        if not m:
            return None
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.x25519 import (
                X25519PrivateKey,
            )

            sk_bytes = base64.b64decode(m.group(1))
            sk = X25519PrivateKey.from_private_bytes(sk_bytes)
            pk_bytes = sk.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            return base64.b64encode(pk_bytes).decode("ascii")
        except Exception:  # noqa: BLE001
            return None

    pubkey = await asyncio.to_thread(_read)
    if pubkey:
        return pubkey

    # Fallback to the env-configured pubkey. Lets onboarding work even
    # when the in-cluster Secret read fails (the kubernetes-client lib
    # has a known case-sensitivity bug on the Bearer scheme that produces
    # 401s against newer apiservers — see workload_cluster_gpu_path memory
    # note). The env value must be kept in sync with the hub's keypair —
    # rotate both together.
    settings = get_settings()
    fallback = getattr(settings, "wireguard_hub_public_key", "") or ""
    return fallback or None


# ── routes ──────────────────────────────────────────────────────────────


@router.get("", response_model=list[ClusterOut])
async def list_clusters(
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    rows = (
        await db.execute(
            select(ClusterTunnel)
            .where(ClusterTunnel.tenant_id == tenant_id)
            .order_by(ClusterTunnel.created_at.desc())
        )
    ).scalars().all()
    return [_to_out(r) for r in rows]


@router.get("/{slug}", response_model=ClusterOut)
async def get_cluster(
    slug: str,
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    row = (
        await db.execute(
            select(ClusterTunnel).where(
                ClusterTunnel.tenant_id == tenant_id,
                ClusterTunnel.slug == slug,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "cluster not found")
    return _to_out(row)


# ── console: overview + read-only kubectl runner ────────────────────────


class NodeSummaryOut(BaseModel):
    name: str
    status: str
    roles: list[str]
    version: str
    internal_ip: str | None
    os_image: str | None
    cpu: str | None
    memory: str | None
    created_at: str | None


class SymptomaticPodOut(BaseModel):
    namespace: str
    name: str
    phase: str
    reason: str
    ready: str
    restarts: int
    node: str | None


class ClusterHealthOut(BaseModel):
    ready_nodes: int
    not_ready_nodes: int
    pods_total: int
    pods_running: int
    pods_pending: int
    pods_failed: int
    pods_succeeded: int
    pods_symptomatic: int
    pod_restarts_total: int
    containers_total: int
    deployments_total: int
    deployments_unavailable: int
    statefulsets_total: int
    daemonsets_total: int
    daemonsets_unavailable: int
    replicasets_total: int
    services_total: int
    pvc_total: int
    pvc_unbound: int
    symptomatic_pods: list[SymptomaticPodOut]
    partial: bool


class ClusterOverviewOut(BaseModel):
    reachable: bool
    server_version: str | None
    node_count: int
    namespace_count: int
    nodes: list[NodeSummaryOut]
    health: ClusterHealthOut | None = None
    error: str | None


class CommandSpecOut(BaseModel):
    id: str
    label: str
    kubectl: str
    group: str
    namespaced: bool
    supports_selector: bool


class KubectlRunIn(BaseModel):
    command_ids: list[str] = Field(..., min_length=1, max_length=25)
    namespace: str | None = Field(default=None, max_length=63)
    label_selector: str | None = Field(default=None, max_length=256)
    output: Literal["json", "yaml", "cli"] = "cli"


class KubectlResultOut(BaseModel):
    id: str
    command: str
    ok: bool
    output: str
    error: str | None


class KubectlRunOut(BaseModel):
    results: list[KubectlResultOut]


async def _load_cluster(
    slug: str, db: AsyncSession, tenant_id: uuid.UUID
) -> ClusterTunnel:
    row = (
        await db.execute(
            select(ClusterTunnel).where(
                ClusterTunnel.tenant_id == tenant_id,
                ClusterTunnel.slug == slug,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "cluster not found")
    return row


@router.get("/kubectl/catalog", response_model=list[CommandSpecOut])
async def kubectl_catalog(
    _user: User = Depends(current_user),
):
    """The fixed allowlist of read-only commands the console can run."""
    return [
        CommandSpecOut(
            id=c.id,
            label=c.label,
            kubectl=c.kubectl,
            group=c.group,
            namespaced=c.namespaced,
            supports_selector=c.supports_selector,
        )
        for c in kube_console.catalog()
    ]


@router.get("/{slug}/overview", response_model=ClusterOverviewOut)
async def cluster_overview(
    slug: str,
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    """Server version, node inventory, and namespace count for the cluster."""
    cluster = await _load_cluster(slug, db, tenant_id)
    ov = await kube_console.cluster_overview(db, cluster)
    health = None
    if ov.health is not None:
        health = ClusterHealthOut(
            **{k: v for k, v in vars(ov.health).items() if k != "symptomatic_pods"},
            symptomatic_pods=[
                SymptomaticPodOut(**vars(p)) for p in ov.health.symptomatic_pods
            ],
        )
    return ClusterOverviewOut(
        reachable=ov.reachable,
        server_version=ov.server_version,
        node_count=ov.node_count,
        namespace_count=ov.namespace_count,
        nodes=[NodeSummaryOut(**vars(n)) for n in ov.nodes],
        health=health,
        error=ov.error,
    )


class ClusterStoreOut(BaseModel):
    provider: str
    name: str
    status: str


# Stores the cluster page can open a console or dashboards for.
_OBS_PROVIDERS = ("prometheus", "thanos", "loki", "opensearch", "grafana")


@router.get("/{slug}/observability", response_model=list[ClusterStoreOut])
async def cluster_observability(
    slug: str,
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    """The metrics/logs/dashboard stores that belong to this cluster.

    The cluster page links its Dashboards tab only to these, so one
    cluster's page never opens another cluster's Prometheus or Loki.
    """
    from daalu_automation.models import Integration

    cluster = await _load_cluster(slug, db, tenant_id)
    rows = (
        await db.execute(
            select(Integration)
            .where(
                Integration.tenant_id == tenant_id,
                Integration.cluster_tunnel_id == cluster.id,
                Integration.provider.in_(_OBS_PROVIDERS),
            )
            .order_by(Integration.created_at)
        )
    ).scalars().all()
    return [
        ClusterStoreOut(
            provider=r.provider,
            name=r.name,
            status=getattr(r.status, "value", str(r.status)),
        )
        for r in rows
    ]


@router.post("/{slug}/kubectl", response_model=KubectlRunOut)
async def run_kubectl(
    slug: str,
    payload: KubectlRunIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    """Run one or more allowlisted read-only commands against the cluster."""
    cluster = await _load_cluster(slug, db, user.tenant_id)
    try:
        results = await kube_console.run_commands(
            db,
            cluster,
            command_ids=payload.command_ids,
            namespace=payload.namespace,
            selector=payload.label_selector,
            output=payload.output,
            actor_id=user.id,
        )
    except KubeConsoleError as e:
        raise HTTPException(400, str(e)) from e
    return KubectlRunOut(
        results=[
            KubectlResultOut(
                id=r.id, command=r.command, ok=r.ok, output=r.output,
                error=r.error,
            )
            for r in results
        ]
    )


@router.post("", response_model=ClusterCreateOut, status_code=201)
async def onboard_cluster(
    payload: ClusterCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_admin),
):
    """Create a new managed cluster row + return the one-shot install snippet."""
    if not _SLUG_RE.match(payload.slug):
        raise HTTPException(
            422,
            "slug must be lowercase letters/digits/hyphens, no leading/trailing dash",
        )

    settings = get_settings()
    endpoint = _require_endpoint(settings)
    api_public_url = _require_api_public_url(settings)

    kp = generate_keypair()
    pubkey, privkey_ct = encrypt_keypair(kp)
    invite_plain, invite_hash = generate_invite_token()
    ip = await allocate_tunnel_ip(db)

    row = ClusterTunnel(
        tenant_id=user.tenant_id,
        slug=payload.slug,
        name=payload.name,
        status=ClusterTunnelStatus.pending,
        operator_pubkey=pubkey,
        operator_privkey_encrypted=privkey_ct,
        tunnel_ip=ip,
        invite_token_hash=invite_hash,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        if "uq_cluster_tunnels_tenant_slug" in str(e.orig):
            raise HTTPException(409, f"cluster slug '{payload.slug}' already exists") from e
        if "uq_cluster_tunnels_tunnel_ip" in str(e.orig):
            # Race against another concurrent onboard — caller can retry.
            raise HTTPException(503, "tunnel IP allocation raced; retry") from e
        raise
    await db.commit()
    await db.refresh(row)

    hub_pubkey = await _load_hub_pubkey() or "<hub-pubkey-unavailable>"
    snippet = render_edge_install_snippet(
        operator_endpoint=endpoint,
        operator_api_url=api_public_url,
        hub_pubkey=hub_pubkey,
        cluster_slug=row.slug,
        invite_token_plain=invite_plain,
        tunnel_ip=str(row.tunnel_ip),
        listen_port=settings.wireguard_listen_port,
    )

    out = _to_out(row).model_dump()
    return ClusterCreateOut(
        **out,
        invite_token=invite_plain,
        install_snippet=snippet,
    )


async def _register_cluster_kubeconfig(
    db: AsyncSession, cluster: ClusterTunnel, kube_token: str
) -> None:
    """Store a kubeconfig for ``cluster`` built from a self-reported SA token.

    Replaces the manual "mint a token, hand-assemble a kubeconfig, paste it
    into Integrations → Kubernetes" step. The hub already knows the only two
    things that vary — the tunnel IP and the tenant — so the edge just has to
    supply the credential.

    ``insecure-skip-tls-verify`` is required, not laziness: the customer's
    apiserver certificate has no SAN for the tunnel address we dial it on
    (``https://<tunnel_ip>:6443``, the edge's apiserver-proxy). The hop is
    already inside the encrypted WireGuard tunnel and terminates at the
    in-cluster apiserver Service, so this adds no exposure.

    Upserts on (tenant, provider, cluster) so a cluster that is torn down and
    rebuilt overwrites its own dead credential rather than accumulating rows.
    """
    import yaml as _yaml

    from daalu_automation.models import Integration, IntegrationStatus

    name = f"{cluster.slug} (self-registered)"
    kubeconfig = _yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": cluster.slug,
                    "cluster": {
                        "server": f"https://{cluster.tunnel_ip}:6443",
                        "insecure-skip-tls-verify": True,
                    },
                }
            ],
            "users": [{"name": "daalu-edge", "user": {"token": kube_token}}],
            "contexts": [
                {
                    "name": cluster.slug,
                    "context": {"cluster": cluster.slug, "user": "daalu-edge"},
                }
            ],
            "current-context": cluster.slug,
        },
        sort_keys=False,
    )

    existing = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == cluster.tenant_id,
                Integration.provider == "kubernetes",
                Integration.cluster_tunnel_id == cluster.id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        cfg = dict(existing.config or {})
        cfg["kubeconfig"] = kubeconfig
        existing.config = cfg
        existing.name = name
        existing.status = IntegrationStatus.connected
        existing.last_error = None
    else:
        db.add(
            Integration(
                tenant_id=cluster.tenant_id,
                provider="kubernetes",
                module="infra",
                name=name,
                status=IntegrationStatus.connected,
                config={"kubeconfig": kubeconfig},
                cluster_tunnel_id=cluster.id,
            )
        )
    await db.commit()
    logger.info(
        "cluster.callback.kubeconfig_registered",
        slug=cluster.slug,
        tenant_id=str(cluster.tenant_id),
    )


@router.post("/{slug}/callback", response_model=ClusterOut)
async def cluster_callback(
    slug: str,
    payload: ClusterCallbackIn,
    db: AsyncSession = Depends(get_db),
):
    """Customer-side edge reports its pubkey + endpoint.

    Auth: the one-shot invite token, hashed and compared to the row's
    ``invite_token_hash``. No login dependency: this is called by the
    edge container before any user session exists, *but* the slug must
    match a pending row whose token hashes to the presented value, so
    arbitrary unauthenticated callers can't manufacture entries.
    """
    row = (
        await db.execute(
            select(ClusterTunnel).where(ClusterTunnel.slug == slug)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "cluster not found")
    if row.invite_token_hash is None:
        raise HTTPException(
            409,
            "invite token already redeemed — issue a new cluster row if you "
            "need to re-bootstrap this edge",
        )
    presented = hash_invite_token(payload.invite_token)
    # Constant-time compare so a timing oracle can't enumerate hashes.
    import hmac

    if not hmac.compare_digest(presented, row.invite_token_hash):
        raise HTTPException(401, "invalid invite token")

    row.customer_pubkey = payload.customer_pubkey
    row.customer_endpoint = payload.customer_endpoint
    row.invite_token_hash = None  # one-shot
    row.status = ClusterTunnelStatus.awaiting_handshake
    row.last_error = None
    await db.commit()
    await db.refresh(row)

    # Self-registered cluster credential. Best-effort: a failure here must not
    # fail the callback, because the tunnel (this endpoint's actual job) is
    # already recorded and the invite token is spent — raising would strand
    # the edge with no way to retry.
    if payload.kube_token:
        try:
            await _register_cluster_kubeconfig(db, row, payload.kube_token)
        except Exception as e:  # noqa: BLE001 — never break bootstrap
            logger.warning(
                "cluster.callback.kubeconfig_register_failed",
                slug=slug,
                error=str(e),
            )

    # Push the updated peer set to the hub Secret. A failure here is
    # logged but we still return success — the callback has done its
    # job (token consumed, pubkey recorded) and the next beat sync will
    # converge the Secret from the DB state.
    try:
        settings = get_settings()
        await sync_hub_secret(
            db,
            listen_port=settings.wireguard_listen_port,
            hub_address=settings.wireguard_hub_address,
        )
    except HubSecretUnavailable as e:
        logger.warning("cluster.callback.sync_failed", slug=slug, error=str(e))

    return _to_out(row)


@router.delete("/{slug}", status_code=204)
async def delete_cluster(
    slug: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_admin),
):
    """Tear down a managed cluster: remove the row + drop its Peer block."""
    row = (
        await db.execute(
            select(ClusterTunnel).where(
                ClusterTunnel.tenant_id == user.tenant_id,
                ClusterTunnel.slug == slug,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "cluster not found")
    # The kubeconfig the edge self-registered dies with the tunnel (it dials
    # the tunnel IP with that cluster's token). Left behind, the FK's SET NULL
    # turns it into a "connected" kubernetes integration with no cluster —
    # and NULL cluster_tunnel_id means "the hub itself" elsewhere.
    from daalu_automation.models import Integration

    await db.execute(
        sa_delete(Integration).where(
            Integration.tenant_id == row.tenant_id,
            Integration.provider == "kubernetes",
            Integration.cluster_tunnel_id == row.id,
            Integration.name == f"{row.slug} (self-registered)",
        )
    )
    await db.delete(row)
    await db.commit()
    try:
        settings = get_settings()
        await sync_hub_secret(
            db,
            listen_port=settings.wireguard_listen_port,
            hub_address=settings.wireguard_hub_address,
        )
    except HubSecretUnavailable as e:
        logger.warning("cluster.delete.sync_failed", slug=slug, error=str(e))
    return None
