"""Read-only Kubernetes console for a managed cluster.

This backs the per-cluster page under ``/clusters/{slug}`` in the UI. It
gives an operator two things against a customer cluster reached over the
WireGuard mesh:

* **Overview** — server version, node inventory (status / roles / kubelet
  version / capacity), and namespace count.
* **A curated kubectl runner** — a fixed allowlist of read-only
  ``kubectl get``-style commands. The operator ticks one or more of them,
  picks an output format (``json`` / ``yaml`` / ``cli`` table), and we run
  each against the cluster and return the rendered output.

Why a generic GET executor instead of shelling out to ``kubectl``:

* No shell, no binary, no argument-injection surface. The only inputs the
  caller controls are a command *id* (validated against the catalog), an
  optional namespace, and an optional label selector (both regex-checked).
* Every command resolves to a single HTTP ``GET`` against a fixed API
  path. Nothing in this module can mutate cluster state.
* The Kubernetes API server itself renders all three output shapes:
  ``application/json`` for json/yaml, and the
  ``application/json;as=Table`` content negotiation for the ``cli`` view,
  which returns the exact server-side printer columns ``kubectl get``
  shows. One code path, three faithful formats.

Connectivity reuses the same kubeconfig resolution the alert-chat read
tools use (:mod:`daalu_automation.core.kube_tools`): the kubeconfig stored
in the ``Integration(provider="kubernetes")`` row points its API server at
the cluster's tunnel IP, and the daalu-api pod dials it over the mesh.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import structlog
import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.core.kube_tools import (
    KubeUnavailable,
    _load_kube,
    cluster_kubeconfig,
)
from daalu_automation.models import ClusterTunnel, ClusterTunnelStatus

logger = structlog.get_logger(__name__)


class KubeConsoleError(RuntimeError):
    """A console command could not be resolved or executed."""


OutputFormat = Literal["json", "yaml", "cli"]

# Content negotiation. The Table accept header asks the API server to do
# the same server-side printing ``kubectl get`` relies on.
_ACCEPT_JSON = "application/json"
_ACCEPT_TABLE = (
    "application/json;as=Table;v=v1;g=meta.k8s.io,"
    "application/json;as=Table;v=v1beta1;g=meta.k8s.io,"
    "application/json"
)

# Validation for the only free-form inputs we accept.
_NS_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_SELECTOR_RE = re.compile(r"^[A-Za-z0-9_.\-/=,!() ]{0,256}$")

_MAX_COMMANDS_PER_RUN = 25


# ── command catalog ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommandSpec:
    """One read-only command surfaced in the UI.

    ``path`` is the cluster-scoped API path. For namespaced resources,
    ``namespaced_path`` is the per-namespace template (``{ns}`` slot) used
    when the caller supplies a namespace; otherwise ``path`` (the
    all-namespaces collection) is used.
    """

    id: str
    label: str
    kubectl: str
    group: str
    path: str
    namespaced: bool = False
    namespaced_path: str | None = None
    # Some endpoints (``/version``) don't support Table printing.
    supports_table: bool = True
    # Whether a label selector is meaningful for this resource.
    supports_selector: bool = True


CATALOG: list[CommandSpec] = [
    # ── Cluster ──────────────────────────────────────────────────────────
    CommandSpec(
        id="version",
        label="Server version",
        kubectl="kubectl version",
        group="Cluster",
        path="/version",
        supports_table=False,
        supports_selector=False,
    ),
    CommandSpec(
        id="get-nodes",
        label="Nodes",
        kubectl="kubectl get nodes -o wide",
        group="Cluster",
        path="/api/v1/nodes",
    ),
    CommandSpec(
        id="get-namespaces",
        label="Namespaces",
        kubectl="kubectl get namespaces",
        group="Cluster",
        path="/api/v1/namespaces",
        supports_selector=False,
    ),
    CommandSpec(
        id="top-nodes",
        label="Node resource usage",
        kubectl="kubectl top nodes",
        group="Cluster",
        path="/apis/metrics.k8s.io/v1beta1/nodes",
    ),
    CommandSpec(
        id="get-events",
        label="Events",
        kubectl="kubectl get events",
        group="Cluster",
        path="/api/v1/events",
        namespaced=True,
        namespaced_path="/api/v1/namespaces/{ns}/events",
        supports_selector=False,
    ),
    # ── Workloads ────────────────────────────────────────────────────────
    CommandSpec(
        id="get-pods",
        label="Pods",
        kubectl="kubectl get pods -o wide",
        group="Workloads",
        path="/api/v1/pods",
        namespaced=True,
        namespaced_path="/api/v1/namespaces/{ns}/pods",
    ),
    CommandSpec(
        id="get-deployments",
        label="Deployments",
        kubectl="kubectl get deployments",
        group="Workloads",
        path="/apis/apps/v1/deployments",
        namespaced=True,
        namespaced_path="/apis/apps/v1/namespaces/{ns}/deployments",
    ),
    CommandSpec(
        id="get-statefulsets",
        label="StatefulSets",
        kubectl="kubectl get statefulsets",
        group="Workloads",
        path="/apis/apps/v1/statefulsets",
        namespaced=True,
        namespaced_path="/apis/apps/v1/namespaces/{ns}/statefulsets",
    ),
    CommandSpec(
        id="get-daemonsets",
        label="DaemonSets",
        kubectl="kubectl get daemonsets",
        group="Workloads",
        path="/apis/apps/v1/daemonsets",
        namespaced=True,
        namespaced_path="/apis/apps/v1/namespaces/{ns}/daemonsets",
    ),
    CommandSpec(
        id="get-replicasets",
        label="ReplicaSets",
        kubectl="kubectl get replicasets",
        group="Workloads",
        path="/apis/apps/v1/replicasets",
        namespaced=True,
        namespaced_path="/apis/apps/v1/namespaces/{ns}/replicasets",
    ),
    CommandSpec(
        id="get-jobs",
        label="Jobs",
        kubectl="kubectl get jobs",
        group="Workloads",
        path="/apis/batch/v1/jobs",
        namespaced=True,
        namespaced_path="/apis/batch/v1/namespaces/{ns}/jobs",
    ),
    CommandSpec(
        id="get-cronjobs",
        label="CronJobs",
        kubectl="kubectl get cronjobs",
        group="Workloads",
        path="/apis/batch/v1/cronjobs",
        namespaced=True,
        namespaced_path="/apis/batch/v1/namespaces/{ns}/cronjobs",
    ),
    # ── Networking ───────────────────────────────────────────────────────
    CommandSpec(
        id="get-services",
        label="Services",
        kubectl="kubectl get services -o wide",
        group="Networking",
        path="/api/v1/services",
        namespaced=True,
        namespaced_path="/api/v1/namespaces/{ns}/services",
    ),
    CommandSpec(
        id="get-endpoints",
        label="Endpoints",
        kubectl="kubectl get endpoints",
        group="Networking",
        path="/api/v1/endpoints",
        namespaced=True,
        namespaced_path="/api/v1/namespaces/{ns}/endpoints",
    ),
    CommandSpec(
        id="get-ingresses",
        label="Ingresses",
        kubectl="kubectl get ingresses",
        group="Networking",
        path="/apis/networking.k8s.io/v1/ingresses",
        namespaced=True,
        namespaced_path="/apis/networking.k8s.io/v1/namespaces/{ns}/ingresses",
    ),
    # ── Storage ──────────────────────────────────────────────────────────
    CommandSpec(
        id="get-pvcs",
        label="PersistentVolumeClaims",
        kubectl="kubectl get pvc",
        group="Storage",
        path="/api/v1/persistentvolumeclaims",
        namespaced=True,
        namespaced_path="/api/v1/namespaces/{ns}/persistentvolumeclaims",
    ),
    CommandSpec(
        id="get-pvs",
        label="PersistentVolumes",
        kubectl="kubectl get pv",
        group="Storage",
        path="/api/v1/persistentvolumes",
    ),
    CommandSpec(
        id="get-storageclasses",
        label="StorageClasses",
        kubectl="kubectl get storageclasses",
        group="Storage",
        path="/apis/storage.k8s.io/v1/storageclasses",
    ),
    # ── Config ───────────────────────────────────────────────────────────
    #
    # ConfigMaps are listed (names/keys only — Table printing never returns
    # data values). Secrets are deliberately excluded: even a name listing
    # leaks more than an operator console should, and the agent's
    # approval-gated tools cover the rare case where a value is needed.
    CommandSpec(
        id="get-configmaps",
        label="ConfigMaps",
        kubectl="kubectl get configmaps",
        group="Config",
        path="/api/v1/configmaps",
        namespaced=True,
        namespaced_path="/api/v1/namespaces/{ns}/configmaps",
    ),
]

_CATALOG_BY_ID: dict[str, CommandSpec] = {c.id: c for c in CATALOG}


def catalog() -> list[CommandSpec]:
    return list(CATALOG)


# ── kube client resolution (per cluster) ─────────────────────────────────


async def _kube_for_cluster(db: AsyncSession, cluster: ClusterTunnel) -> Any:
    """Return ``(core_v1, apps_v1, client_mod)`` for ``cluster``.

    Uses :func:`cluster_kubeconfig`: the kubeconfig linked to this cluster,
    else an unlinked one that targets its tunnel address. A cluster with
    neither raises :class:`KubeUnavailable` — it must never fall back to
    another cluster's kubeconfig and show that cluster's state as its own.
    """
    blob = await cluster_kubeconfig(db, cluster)
    if blob is None:
        raise KubeUnavailable(
            f"no Kubernetes credentials are registered for {cluster.slug!r}"
        )
    return _load_kube(tenant_kubeconfig=blob)


def _assert_reachable(cluster: ClusterTunnel) -> None:
    if cluster.status not in (
        ClusterTunnelStatus.connected,
        ClusterTunnelStatus.degraded,
    ):
        raise KubeConsoleError(
            f"cluster tunnel is {cluster.status.value} — commands can only run "
            "while the tunnel is connected"
        )


# ── overview ─────────────────────────────────────────────────────────────


@dataclass
class NodeSummary:
    name: str
    status: str
    roles: list[str]
    version: str
    internal_ip: str | None
    os_image: str | None
    cpu: str | None
    memory: str | None
    created_at: str | None


@dataclass
class SymptomaticPod:
    """A pod that is neither Running nor Succeeded — surfaced so an operator
    sees *which* workloads are unhealthy, not just a count. Mirrors the
    "Pods in symptomatic phases" panel Datadog shows."""

    namespace: str
    name: str
    phase: str
    reason: str  # CrashLoopBackOff / OOMKilled / Pending reason / phase
    ready: str  # "1/2"
    restarts: int
    node: str | None


@dataclass
class ClusterHealth:
    """Cluster-wide health rollup, mirroring the Datadog Kubernetes overview:
    pod phase breakdown, restarts, node readiness, unbound volumes, and
    workloads with unavailable replicas.

    ``partial`` is set when one or more collectors failed (e.g. the linked
    kubeconfig's RBAC can't list a resource) — the surfaced numbers are then
    a lower bound, and the UI flags them as incomplete rather than wrong.
    """

    # Nodes
    ready_nodes: int = 0
    not_ready_nodes: int = 0
    # Pods (phase breakdown + restart rollup)
    pods_total: int = 0
    pods_running: int = 0
    pods_pending: int = 0
    pods_failed: int = 0
    pods_succeeded: int = 0
    pods_symptomatic: int = 0  # not Running/Succeeded
    pod_restarts_total: int = 0
    containers_total: int = 0
    # Workloads
    deployments_total: int = 0
    deployments_unavailable: int = 0
    statefulsets_total: int = 0
    daemonsets_total: int = 0
    daemonsets_unavailable: int = 0
    replicasets_total: int = 0
    services_total: int = 0
    # Storage
    pvc_total: int = 0
    pvc_unbound: int = 0
    # Detail (capped)
    symptomatic_pods: list[SymptomaticPod] = field(default_factory=list)
    partial: bool = False


@dataclass
class ClusterOverview:
    reachable: bool
    server_version: str | None = None
    node_count: int = 0
    namespace_count: int = 0
    nodes: list[NodeSummary] = field(default_factory=list)
    health: ClusterHealth | None = None
    error: str | None = None


# Cap on the symptomatic-pod detail list so a cluster in a bad way (hundreds
# of Pending pods) can't blow up the response / context window.
_MAX_SYMPTOMATIC_PODS = 50


def _pod_reason(p: Any) -> str:
    """kubectl-style reason for a pod: prefer a container's notable waiting/
    terminated reason (CrashLoopBackOff, OOMKilled, ImagePullBackOff) over the
    bare phase, falling back to the pod-level reason then the phase."""
    for c in p.status.container_statuses or []:
        st = c.state
        if st and st.waiting and st.waiting.reason and st.waiting.reason != "ContainerCreating":
            return st.waiting.reason
        if st and st.terminated and st.terminated.reason and st.terminated.reason != "Completed":
            return st.terminated.reason
        ls = c.last_state
        if not c.ready and ls and ls.terminated and ls.terminated.reason:
            return ls.terminated.reason
    return p.status.reason or p.status.phase or "Unknown"


def _collect_health(core: Any, apps: Any) -> ClusterHealth:
    """Best-effort cluster-wide health rollup.

    Each resource family is listed independently and guarded so a single
    forbidden/timed-out call (e.g. the linked kubeconfig lacking list rights on
    one kind) degrades that metric to zero + ``partial=True`` instead of
    blanking the whole overview.
    """
    h = ClusterHealth()
    timeout = 25

    # Nodes — readiness split.
    try:
        for n in core.list_node(_request_timeout=timeout).items:
            conds = {c.type: c.status for c in (n.status.conditions or [])}
            if conds.get("Ready") == "True":
                h.ready_nodes += 1
            else:
                h.not_ready_nodes += 1
    except Exception:  # noqa: BLE001
        h.partial = True

    # Pods — phase breakdown, restarts, symptomatic detail.
    try:
        pods = core.list_pod_for_all_namespaces(_request_timeout=timeout).items
        h.pods_total = len(pods)
        for p in pods:
            phase = p.status.phase or "Unknown"
            cs = p.status.container_statuses or []
            h.containers_total += len(p.spec.containers or [])
            h.pod_restarts_total += sum(c.restart_count or 0 for c in cs)
            if phase == "Running":
                h.pods_running += 1
            elif phase == "Succeeded":
                h.pods_succeeded += 1
            elif phase == "Pending":
                h.pods_pending += 1
            elif phase == "Failed":
                h.pods_failed += 1
            # "symptomatic" = anything not steady-state Running/Succeeded.
            if phase not in ("Running", "Succeeded"):
                h.pods_symptomatic += 1
                if len(h.symptomatic_pods) < _MAX_SYMPTOMATIC_PODS:
                    ready = sum(1 for c in cs if c.ready)
                    total = len(p.spec.containers or [])
                    h.symptomatic_pods.append(
                        SymptomaticPod(
                            namespace=p.metadata.namespace,
                            name=p.metadata.name,
                            phase=phase,
                            reason=_pod_reason(p),
                            ready=f"{ready}/{total}",
                            restarts=sum(c.restart_count or 0 for c in cs),
                            node=p.spec.node_name,
                        )
                    )
    except Exception:  # noqa: BLE001
        h.partial = True

    # Deployments — desired vs available.
    try:
        deps = apps.list_deployment_for_all_namespaces(_request_timeout=timeout).items
        h.deployments_total = len(deps)
        for d in deps:
            desired = d.spec.replicas or 0
            available = d.status.available_replicas or 0
            unavailable = d.status.unavailable_replicas or 0
            if unavailable > 0 or available < desired:
                h.deployments_unavailable += 1
    except Exception:  # noqa: BLE001
        h.partial = True

    # StatefulSets — count only.
    try:
        h.statefulsets_total = len(
            apps.list_stateful_set_for_all_namespaces(_request_timeout=timeout).items
        )
    except Exception:  # noqa: BLE001
        h.partial = True

    # DaemonSets — count + unavailable.
    try:
        dss = apps.list_daemon_set_for_all_namespaces(_request_timeout=timeout).items
        h.daemonsets_total = len(dss)
        for ds in dss:
            if (ds.status.number_unavailable or 0) > 0:
                h.daemonsets_unavailable += 1
    except Exception:  # noqa: BLE001
        h.partial = True

    # ReplicaSets — count only.
    try:
        h.replicasets_total = len(
            apps.list_replica_set_for_all_namespaces(_request_timeout=timeout).items
        )
    except Exception:  # noqa: BLE001
        h.partial = True

    # Services — count only.
    try:
        h.services_total = len(
            core.list_service_for_all_namespaces(_request_timeout=timeout).items
        )
    except Exception:  # noqa: BLE001
        h.partial = True

    # PVCs — bound vs unbound.
    try:
        pvcs = core.list_persistent_volume_claim_for_all_namespaces(
            _request_timeout=timeout
        ).items
        h.pvc_total = len(pvcs)
        for pvc in pvcs:
            if (pvc.status.phase or "") != "Bound":
                h.pvc_unbound += 1
    except Exception:  # noqa: BLE001
        h.partial = True

    return h


async def cluster_overview(db: AsyncSession, cluster: ClusterTunnel) -> ClusterOverview:
    """Server version, node inventory, namespace count, and a cluster-wide
    health rollup for the cluster detail page."""
    try:
        _assert_reachable(cluster)
        core, apps, client_mod = await _kube_for_cluster(db, cluster)
    except (KubeConsoleError, KubeUnavailable) as e:
        return ClusterOverview(reachable=False, error=str(e))

    def _collect() -> ClusterOverview:
        version_api = client_mod.VersionApi(core.api_client)
        ver = version_api.get_code()
        nodes = core.list_node().items
        namespaces = core.list_namespace().items
        node_rows: list[NodeSummary] = []
        for n in nodes:
            conds = {c.type: c.status for c in (n.status.conditions or [])}
            ready = conds.get("Ready")
            status = "Ready" if ready == "True" else "NotReady"
            if (n.spec.taints or []) and any(
                t.effect == "NoSchedule"
                and t.key == "node.kubernetes.io/unschedulable"
                for t in n.spec.taints
            ):
                status += ",SchedulingDisabled"
            roles = sorted(
                k.split("/", 1)[1] or "<none>"
                for k in (n.metadata.labels or {})
                if k.startswith("node-role.kubernetes.io/")
            ) or ["<none>"]
            addrs = {a.type: a.address for a in (n.status.addresses or [])}
            cap = n.status.capacity or {}
            node_rows.append(
                NodeSummary(
                    name=n.metadata.name,
                    status=status,
                    roles=roles,
                    version=(n.status.node_info.kubelet_version
                             if n.status.node_info else ""),
                    internal_ip=addrs.get("InternalIP"),
                    os_image=(n.status.node_info.os_image
                              if n.status.node_info else None),
                    cpu=cap.get("cpu"),
                    memory=cap.get("memory"),
                    created_at=n.metadata.creation_timestamp.isoformat()
                    if n.metadata.creation_timestamp
                    else None,
                )
            )
        health = _collect_health(core, apps)
        return ClusterOverview(
            reachable=True,
            server_version=ver.git_version,
            node_count=len(nodes),
            namespace_count=len(namespaces),
            nodes=node_rows,
            health=health,
        )

    try:
        return await asyncio.to_thread(_collect)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "kube_console.overview_failed", cluster=cluster.slug, error=str(e)
        )
        return ClusterOverview(reachable=False, error=str(e)[:500])


# ── command execution ────────────────────────────────────────────────────


@dataclass
class CommandResult:
    id: str
    command: str
    ok: bool
    output: str
    error: str | None = None


def _resolve_path(spec: CommandSpec, namespace: str | None) -> str:
    if spec.namespaced and namespace and spec.namespaced_path:
        return spec.namespaced_path.format(ns=namespace)
    return spec.path


def _display_command(spec: CommandSpec, namespace: str | None,
                     selector: str | None, output: OutputFormat) -> str:
    parts = [spec.kubectl]
    if spec.namespaced:
        parts.append(f"-n {namespace}" if namespace else "-A")
    if selector and spec.supports_selector:
        parts.append(f"-l {selector}")
    if output in ("json", "yaml"):
        parts.append(f"-o {output}")
    return " ".join(parts)


def _raw_get(api_client: Any, path: str, *, accept: str,
             query: list[tuple[str, str]] | None = None) -> Any:
    """Issue a single authenticated GET via the configured ApiClient and
    return the parsed JSON body.

    Uses ``_preload_content=False`` to get the raw response and parse JSON
    ourselves, instead of relying on the client's response deserialization.
    The deserialization keyword on ``call_api`` changed across
    kubernetes-client majors (``response_type`` → ``response_types_map`` in
    v31+/v36), so passing it breaks on a client upgrade — parsing the raw
    bytes is version-stable. Auth (bearer token or client cert from the
    kubeconfig) and TLS are still applied by ``call_api``.
    """
    resp = api_client.call_api(
        path,
        "GET",
        query_params=query or [],
        header_params={"Accept": accept},
        auth_settings=["BearerToken"],
        _preload_content=False,
    )
    data = resp.data
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8")
    return json.loads(data)


def _render_table(obj: dict[str, Any]) -> str:
    """Render an API ``Table`` object as aligned columns, like kubectl."""
    cols = obj.get("columnDefinitions") or []
    rows = obj.get("rows") or []
    # priority 0 = always shown; >0 = wide-only. We surface everything the
    # server offers (the catalog already requests ``-o wide`` semantics).
    headers = [c.get("name", "") for c in cols]
    if not headers:
        return "(no columns returned)"
    body = [[("" if v is None else str(v)) for v in (r.get("cells") or [])]
            for r in rows]
    widths = [len(h) for h in headers]
    for line in body:
        for i, cell in enumerate(line):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))
    fmt = "   ".join(f"{{:<{w}}}" for w in widths)
    out = [fmt.format(*headers).rstrip()]
    for line in body:
        padded = line + [""] * (len(headers) - len(line))
        out.append(fmt.format(*padded[: len(headers)]).rstrip())
    if not body:
        out.append("(no resources found)")
    return "\n".join(out)


def _run_one(api_client: Any, spec: CommandSpec, namespace: str | None,
             selector: str | None, output: OutputFormat) -> str:
    path = _resolve_path(spec, namespace)
    query: list[tuple[str, str]] = []
    if selector and spec.supports_selector:
        query.append(("labelSelector", selector))

    if output == "cli" and spec.supports_table:
        obj = _raw_get(api_client, path, accept=_ACCEPT_TABLE, query=query)
        if isinstance(obj, dict) and obj.get("kind") == "Table":
            return _render_table(obj)
        # Server ignored the Table accept (older API) — fall through to JSON.
        return json.dumps(obj, indent=2, default=str)

    obj = _raw_get(api_client, path, accept=_ACCEPT_JSON, query=query)
    if output == "yaml":
        return yaml.safe_dump(obj, default_flow_style=False, sort_keys=False)
    return json.dumps(obj, indent=2, default=str)


async def run_commands(
    db: AsyncSession,
    cluster: ClusterTunnel,
    *,
    command_ids: list[str],
    namespace: str | None,
    selector: str | None,
    output: OutputFormat,
    actor_id: uuid.UUID | None = None,
) -> list[CommandResult]:
    """Run each selected command read-only and return rendered output."""
    if not command_ids:
        raise KubeConsoleError("select at least one command")
    if len(command_ids) > _MAX_COMMANDS_PER_RUN:
        raise KubeConsoleError(
            f"too many commands selected (max {_MAX_COMMANDS_PER_RUN})"
        )
    if output not in ("json", "yaml", "cli"):
        raise KubeConsoleError(f"unknown output format {output!r}")
    if namespace is not None:
        namespace = namespace.strip() or None
    if namespace is not None and not _NS_RE.match(namespace):
        raise KubeConsoleError(f"invalid namespace {namespace!r}")
    if selector is not None:
        selector = selector.strip() or None
    if selector is not None and not _SELECTOR_RE.match(selector):
        raise KubeConsoleError("invalid label selector")

    specs: list[CommandSpec] = []
    for cid in command_ids:
        spec = _CATALOG_BY_ID.get(cid)
        if spec is None:
            raise KubeConsoleError(f"unknown command {cid!r}")
        specs.append(spec)

    _assert_reachable(cluster)
    core, _apps, _client_mod = await _kube_for_cluster(db, cluster)
    api_client = core.api_client

    logger.info(
        "kube_console.run",
        cluster=cluster.slug,
        tenant_id=str(cluster.tenant_id),
        actor_id=str(actor_id) if actor_id else None,
        commands=command_ids,
        namespace=namespace,
        output=output,
    )

    def _execute() -> list[CommandResult]:
        results: list[CommandResult] = []
        for spec in specs:
            display = _display_command(spec, namespace, selector, output)
            try:
                text = _run_one(api_client, spec, namespace, selector, output)
                results.append(
                    CommandResult(id=spec.id, command=display, ok=True,
                                  output=text)
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "kube_console.command_failed",
                    cluster=cluster.slug, command=spec.id, error=str(e),
                )
                results.append(
                    CommandResult(
                        id=spec.id, command=display, ok=False, output="",
                        error=_humanise_error(e),
                    )
                )
        return results

    return await asyncio.to_thread(_execute)


def _humanise_error(e: Exception) -> str:
    """Best-effort short message from a kubernetes ApiException or other."""
    status = getattr(e, "status", None)
    reason = getattr(e, "reason", None)
    if status is not None:
        body = getattr(e, "body", "") or ""
        msg = ""
        try:
            msg = (json.loads(body) or {}).get("message", "")
        except Exception:  # noqa: BLE001
            msg = ""
        return f"HTTP {status} {reason or ''}: {msg or body[:200]}".strip()
    return f"{type(e).__name__}: {e}"


# Stamp used by the API layer for audit log timestamps.
def utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
