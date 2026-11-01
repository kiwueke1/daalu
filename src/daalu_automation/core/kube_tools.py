"""Allowlisted kubectl-style tools the alert chat exposes to Claude.

Two tiers:

* **Read tools** (``get_pod_logs``, ``describe_pod``, ``get_pod_events``,
  ``list_pods``, ``get_deployment``, ``rollout_history``,
  ``recent_changes``) — auto-execute
  whenever the LLM calls them. They cannot mutate cluster state.
* **Write tools** (``rollout_undo``, ``scale_deployment``,
  ``restart_deployment``, ``delete_pod``, ``patch_resource``) — the call
  is *recorded* and surfaced to the operator in the chat panel; nothing
  runs until the operator clicks Approve.

The tool layer talks to the API server via the in-cluster
``ServiceAccount`` token mounted on the daalu-api pod (see
``deploy/k8s/api/deployment.yaml`` and the matching RBAC in
``deploy/k8s/rbac/remediator.yaml``).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx
import structlog
from sqlalchemy import select

from daalu_automation.core.tenant_settings import get_tenant_config
from daalu_automation.database import AsyncSessionLocal
from daalu_automation.models import Integration

logger = structlog.get_logger(__name__)


class KubeUnavailable(RuntimeError):
    """No kube credentials / not running in-cluster."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Coroutine[Any, Any, str]]
    # Write tools are gated by user approval before execution.
    requires_approval: bool = False


# ── kubernetes client lazy loader ────────────────────────────────────────


_default_kube_client = None
# Per-tenant clients keyed by (tenant_id, kubeconfig_hash) so a config
# rotation invalidates only the affected entry. Distinct clusters have
# distinct kubeconfigs → distinct cache entries, so multi-cluster is safe.
_tenant_kube_clients: dict[tuple[str, str], Any] = {}

# Which named Kubernetes cluster the current tool call targets. A tenant can
# register several `kubernetes` integration rows (one per cluster, keyed by
# name); ``execute_tool`` sets this from the alert's cluster tag so every
# handler reaches the right cluster without threading the name through ~12
# signatures. ``None`` (the default, and the single-cluster case) means "the
# tenant's sole/first cluster" — so single-cluster triage is unchanged.
_target_cluster: ContextVar[str | None] = ContextVar("daalu_target_cluster", default=None)

# Which onboarded cluster (ClusterTunnel slug) the call targets — set when a
# user or the copilot names one. Unlike ``_target_cluster`` (an integration
# name with a silent first-row fallback), this resolves the same way the
# cluster page does: the cluster's own kubeconfig, else the tenant default;
# and metrics/logs come only from the store linked to that cluster.
_target_tunnel: ContextVar[str | None] = ContextVar("daalu_target_tunnel", default=None)


async def _tunnel_id_for(db: Any, tenant_id: uuid.UUID, slug: str) -> uuid.UUID | None:
    from daalu_automation.models import ClusterTunnel

    return (
        await db.execute(
            select(ClusterTunnel.id).where(
                ClusterTunnel.tenant_id == tenant_id, ClusterTunnel.slug == slug
            )
        )
    ).scalar_one_or_none()


async def cluster_kubeconfig(db: Any, cluster: Any) -> dict[str, Any] | None:
    """The kubeconfig for one onboarded cluster, or ``None`` if it has none.

    1. a ``kubernetes`` integration linked to the cluster's tunnel;
    2. else an unlinked one whose API server is the cluster's own tunnel
       address (e.g. a hand-made ``https://<tunnel_ip>:6443`` kubeconfig
       that predates linking).

    Never another cluster's credentials: before this, a cluster with no
    kubeconfig of its own borrowed the tenant's first one, so its page and
    the copilot showed a different cluster's pods under its name.
    """
    from urllib.parse import urlparse

    import yaml

    rows = (
        await db.execute(
            select(Integration)
            .where(
                Integration.tenant_id == cluster.tenant_id,
                Integration.provider == "kubernetes",
            )
            .order_by(Integration.created_at)
        )
    ).scalars().all()

    def _blob(row: Any) -> dict[str, Any] | None:
        b = (row.config or {}).get("kubeconfig")
        if isinstance(b, str):
            try:
                b = yaml.safe_load(b)
            except Exception as e:  # noqa: BLE001
                raise KubeUnavailable(f"kubeconfig {row.name!r} isn't valid YAML — {e}") from e
        return b or None

    for row in rows:
        if row.cluster_tunnel_id == cluster.id and _blob(row):
            return _blob(row)
    tunnel_ip = str(cluster.tunnel_ip)
    for row in rows:
        b = _blob(row) if row.cluster_tunnel_id is None else None
        servers = [
            (c.get("cluster") or {}).get("server") or "" for c in (b or {}).get("clusters", [])
        ]
        if any(urlparse(sv).hostname == tunnel_ip for sv in servers):
            return b
    return None

# Kube API methods make a blocking HTTP call to the API server — often over a
# tenant tunnel that can be slow or down. Without a request timeout the call
# hangs the calling turn *forever* and the alert chat sits at "Triaging…". The
# generated client accepts ``_request_timeout`` on every method, so we default
# it here; an individual caller may still override it.
_KUBE_REQUEST_TIMEOUT_S = 20
# Whole-handler ceiling enforced in ``execute_tool``. Generous enough for the
# multi-hop handlers (e.g. rollout_history makes several kube calls) while still
# guaranteeing a turn can't hang indefinitely on one tool.
_TOOL_TIMEOUT_S = 90


def _load_kube(*, tenant_kubeconfig: dict[str, Any] | None = None) -> Any:
    """Return a tuple of (core_v1, apps_v1, client_mod) or raise.

    Resolution order:
      1. ``tenant_kubeconfig`` (dict in kubeconfig YAML shape) — used
         when the caller has a per-tenant integration row pointing at
         the customer's cluster.
      2. In-cluster SA token (the API pod's mounted ServiceAccount).
      3. ``~/.kube/config`` — local-dev fallback only.

    Each variant is cached so the chain only runs once per worker.
    """
    try:
        from kubernetes import client, config  # type: ignore
    except ImportError as e:
        raise KubeUnavailable(
            "kubernetes python client is not installed in this image"
        ) from e

    if tenant_kubeconfig is not None:
        import hashlib

        key = (
            "tenant",
            hashlib.sha256(
                json.dumps(tenant_kubeconfig, sort_keys=True).encode()
            ).hexdigest(),
        )
        cached = _tenant_kube_clients.get(key)
        if cached is not None:
            return cached
        cfg = client.Configuration()
        try:
            config.load_kube_config_from_dict(
                config_dict=tenant_kubeconfig,
                client_configuration=cfg,
            )
        except Exception as e:
            raise KubeUnavailable(
                f"per-tenant kubeconfig is malformed — {e}"
            ) from e
        api_client = client.ApiClient(configuration=cfg)
        triple = (
            client.CoreV1Api(api_client=api_client),
            client.AppsV1Api(api_client=api_client),
            client,
        )
        _tenant_kube_clients[key] = triple
        return triple

    global _default_kube_client
    if _default_kube_client is not None:
        return _default_kube_client
    try:
        config.load_incluster_config()
    except Exception:
        try:
            config.load_kube_config()
        except Exception as e:
            raise KubeUnavailable(
                "no in-cluster SA token and no ~/.kube/config available"
            ) from e
    _default_kube_client = (client.CoreV1Api(), client.AppsV1Api(), client)
    return _default_kube_client


async def _tenant_kube_or_default(
    tenant_id: uuid.UUID | None, cluster_name: str | None = None
) -> Any:
    """Return ``_load_kube`` output, preferring the tenant's stored
    kubeconfig when one is registered as an Integration row.

    A tenant may register multiple ``kubernetes`` rows (one per cluster,
    distinguished by ``name``). ``cluster_name`` — falling back to the
    ``_target_cluster`` context var set by ``execute_tool`` — picks which
    one. When it's unset or doesn't match a registered cluster we use the
    first row (ordered by creation), so the single-cluster path is unchanged.
    """
    if tenant_id is None:
        return _load_kube()
    if cluster_name is None:
        cluster_name = _target_cluster.get()
    slug = _target_tunnel.get()
    async with AsyncSessionLocal() as db:
        if slug:
            from daalu_automation.models import ClusterTunnel

            cluster = (
                await db.execute(
                    select(ClusterTunnel).where(
                        ClusterTunnel.tenant_id == tenant_id, ClusterTunnel.slug == slug
                    )
                )
            ).scalar_one_or_none()
            if cluster is None:
                raise KubeUnavailable(f"no cluster named {slug!r} is onboarded")
            blob = await cluster_kubeconfig(db, cluster)
            if blob is None:
                raise KubeUnavailable(
                    f"cluster {slug!r} has no Kubernetes credentials registered"
                )
            return _load_kube(tenant_kubeconfig=blob)
        rows = (
            await db.execute(
                Integration.__table__.select()
                .where(
                    Integration.tenant_id == tenant_id,
                    Integration.provider == "kubernetes",
                )
                .order_by(Integration.__table__.c.created_at)
            )
        ).fetchall()
    if not rows:
        return _load_kube()
    row = None
    if cluster_name:
        row = next(
            (r for r in rows if r._mapping["name"] == cluster_name), None
        )
    row = row or rows[0]
    config_blob = (row._mapping["config"] or {}).get("kubeconfig")
    if not config_blob:
        return _load_kube()
    if isinstance(config_blob, str):
        import yaml  # PyYAML — already pulled in by kubernetes client

        try:
            config_blob = yaml.safe_load(config_blob)
        except Exception as e:
            raise KubeUnavailable(
                f"per-tenant kubeconfig isn't valid YAML — {e}"
            ) from e
    return _load_kube(tenant_kubeconfig=config_blob)


async def _run(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    # Kube client API methods accept _request_timeout; default it so a slow or
    # unreachable API server / tunnel can't block the calling turn forever.
    kwargs.setdefault("_request_timeout", _KUBE_REQUEST_TIMEOUT_S)
    return await asyncio.to_thread(fn, *args, **kwargs)


# ── Read tools ───────────────────────────────────────────────────────────


async def _get_pod_logs(
    *,
    namespace: str,
    pod: str,
    container: str | None = None,
    tail_lines: int = 200,
    previous: bool = False,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    core, _apps, _client = await _tenant_kube_or_default(_tenant_id)
    text = await _run(
        core.read_namespaced_pod_log,
        name=pod,
        namespace=namespace,
        container=container,
        tail_lines=tail_lines,
        previous=previous,
    )
    return text or "(empty)"


async def _describe_pod(
    *, namespace: str, pod: str, _tenant_id: uuid.UUID | None = None
) -> str:
    core, _apps, _client = await _tenant_kube_or_default(_tenant_id)
    p = await _run(core.read_namespaced_pod, name=pod, namespace=namespace)
    status = p.status
    spec = p.spec
    lines = [
        f"Pod: {p.metadata.name}",
        f"Namespace: {p.metadata.namespace}",
        f"Phase: {status.phase}",
        f"Node: {spec.node_name}",
        f"Start: {status.start_time}",
        "",
        "Containers:",
    ]
    for c in status.container_statuses or []:
        state = c.state
        if state.running:
            s = f"running since {state.running.started_at}"
        elif state.waiting:
            s = f"waiting: {state.waiting.reason} — {state.waiting.message or ''}"
        elif state.terminated:
            t = state.terminated
            s = f"terminated: {t.reason} (exit {t.exit_code}) — {t.message or ''}"
        else:
            s = "unknown"
        line = f"  - {c.name}: restarts={c.restart_count} ready={c.ready} {s}"
        # A crash-looping container's CURRENT state is usually
        # "waiting: CrashLoopBackOff" — the reason it keeps dying (OOMKilled,
        # Error, exit code) lives in last_state. Surface it so the agent can
        # see WHY it's restarting, not just that it is.
        ls = c.last_state
        if ls and ls.terminated:
            lt = ls.terminated
            line += (
                f"; last termination: {lt.reason} (exit {lt.exit_code})"
                f"{' — ' + lt.message if lt.message else ''}"
            )
        lines.append(line)
    if status.conditions:
        lines.append("")
        lines.append("Conditions:")
        for cond in status.conditions:
            lines.append(f"  - {cond.type}={cond.status}: {cond.message or ''}")
    return "\n".join(lines)


async def _get_pod_events(
    *,
    namespace: str,
    pod: str | None = None,
    limit: int = 30,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    core, _apps, _client = await _tenant_kube_or_default(_tenant_id)
    field_selector = f"involvedObject.name={pod}" if pod else None
    events = await _run(
        core.list_namespaced_event,
        namespace=namespace,
        field_selector=field_selector,
        limit=limit,
    )
    lines = []
    for e in events.items:
        lines.append(
            f"[{e.type}] {e.reason} ({e.involved_object.kind}/{e.involved_object.name}) "
            f"x{e.count or 1} last={e.last_timestamp}: {e.message}"
        )
    return "\n".join(lines) or "(no events)"


async def _list_pods(
    *,
    namespace: str,
    label_selector: str | None = None,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    core, _apps, _client = await _tenant_kube_or_default(_tenant_id)
    pods = await _run(
        core.list_namespaced_pod,
        namespace=namespace,
        label_selector=label_selector,
    )
    lines = ["NAME  READY  STATUS  RESTARTS  NODE"]
    for p in pods.items:
        ready = sum(1 for c in (p.status.container_statuses or []) if c.ready)
        total = len(p.spec.containers)
        restarts = sum(c.restart_count for c in (p.status.container_statuses or []))
        lines.append(
            f"{p.metadata.name}  {ready}/{total}  {_pod_status(p)}  "
            f"{restarts}  {p.spec.node_name}"
        )
    return "\n".join(lines)


def _pod_status(p: Any) -> str:
    """kubectl-style STATUS: prefer a container's notable reason (e.g.
    CrashLoopBackOff, OOMKilled, ImagePullBackOff) over the bare pod phase, so
    the list shows *why* a pod is unhealthy instead of just "Running"/"Pending".
    """
    for c in p.status.container_statuses or []:
        st = c.state
        if st.waiting and st.waiting.reason and st.waiting.reason != "ContainerCreating":
            return st.waiting.reason
        if st.terminated and st.terminated.reason and st.terminated.reason != "Completed":
            return st.terminated.reason
        ls = c.last_state
        if not c.ready and ls and ls.terminated and ls.terminated.reason:
            return ls.terminated.reason
    return p.status.phase


async def _get_deployment(
    *, namespace: str, name: str, _tenant_id: uuid.UUID | None = None
) -> str:
    _core, apps, _client = await _tenant_kube_or_default(_tenant_id)
    d = await _run(apps.read_namespaced_deployment, name=name, namespace=namespace)
    status = d.status
    spec = d.spec
    images = ", ".join(c.image for c in spec.template.spec.containers)
    lines = [
        f"Deployment: {d.metadata.name}",
        f"Namespace: {d.metadata.namespace}",
        f"Replicas: desired={spec.replicas} ready={status.ready_replicas or 0} "
        f"available={status.available_replicas or 0} updated={status.updated_replicas or 0}",
        f"Images: {images}",
        f"Generation: spec={d.metadata.generation} observed={status.observed_generation}",
    ]
    # Container resource requests/limits — needed to diagnose OOMKills and to
    # size a memory/CPU bump correctly.
    res_lines = []
    for c in spec.template.spec.containers:
        r = c.resources
        req = dict(r.requests) if r and r.requests else {}
        lim = dict(r.limits) if r and r.limits else {}
        res_lines.append(f"  - {c.name}: requests={req or '{}'} limits={lim or '{}'}")
    if res_lines:
        lines.append("Resources:")
        lines.extend(res_lines)
    if status.conditions:
        lines.append("Conditions:")
        for c in status.conditions:
            lines.append(f"  - {c.type}={c.status}: {c.message or ''}")
    return "\n".join(lines)


async def _rollout_history(
    *, namespace: str, name: str, _tenant_id: uuid.UUID | None = None
) -> str:
    _core, apps, client_mod = await _tenant_kube_or_default(_tenant_id)
    # ReplicaSets owned by the deployment carry revision annotations.
    d = await _run(apps.read_namespaced_deployment, name=name, namespace=namespace)
    rs_list = await _run(
        apps.list_namespaced_replica_set,
        namespace=namespace,
        label_selector=",".join(
            f"{k}={v}" for k, v in (d.spec.selector.match_labels or {}).items()
        ),
    )
    rows = []
    for rs in rs_list.items:
        rev = (rs.metadata.annotations or {}).get(
            "deployment.kubernetes.io/revision", "?"
        )
        rows.append((int(rev) if rev.isdigit() else 0, rs))
    rows.sort(key=lambda r: r[0], reverse=True)
    rows = rows[:10]
    # Revisions that share an image usually differ in env or args (a tuning
    # flag, a memory setting). Show exactly those differences per revision,
    # or every revision looks identical and the agent can't see that an
    # earlier one was healthy (it patched instead of rolling back).
    settings = [_template_settings(rs.spec.template) for _, rs in rows]
    changed = sorted(
        {k for cfg in settings for k in cfg}
        - {k for k in settings[0] if all(cfg.get(k) == settings[0][k] for cfg in settings)}
    ) if settings else []
    current = (d.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "?")
    lines = [
        f"Current revision: {current}",
        "REVISION  POD-TEMPLATE-HASH  IMAGE  CREATED  SETTINGS-THAT-DIFFER",
    ]
    for (rev, rs), cfg in zip(rows, settings, strict=True):
        image = ", ".join(c.image for c in rs.spec.template.spec.containers)
        diff = ", ".join(f"{k}={cfg.get(k, '(unset)')}" for k in changed)
        lines.append(
            f"{rev}  {rs.metadata.labels.get('pod-template-hash', '')}  "
            f"{image}  {rs.metadata.creation_timestamp}  {diff or '-'}"
        )
    return "\n".join(lines)


def _template_settings(template: Any) -> dict[str, str]:
    """Flatten a pod template's per-container env and args to ``{key: value}``.

    Keys look like ``vllm env GPU_MEM_UTIL`` / ``vllm args``. Env sourced from a
    Secret or ConfigMap is shown by reference only, never by value.
    """
    out: dict[str, str] = {}
    for c in template.spec.containers or []:
        for e in c.env or []:
            out[f"{c.name} env {e.name}"] = (
                e.value if e.value_from is None else "(from reference)"
            )
        if c.args:
            out[f"{c.name} args"] = " ".join(c.args)
        if c.command:
            out[f"{c.name} command"] = " ".join(c.command)
    return out


async def _recent_changes(
    *,
    namespace: str | None = None,
    hours: int = 24,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """Deployment rollouts, Deployment events and Argo CD syncs in the last
    ``hours`` (see ``core/aiops/change_context.py``)."""
    from daalu_automation.core.aiops import change_context

    return await change_context.recent_changes(
        tenant_id=_tenant_id,
        cluster=_target_cluster.get(),
        namespace=namespace,
        hours=hours,
    )


# ── Write tools (require approval) ───────────────────────────────────────


async def _rollout_undo(
    *,
    namespace: str,
    name: str,
    to_revision: int | None = None,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """Roll a Deployment back to the previous (or specified) revision.

    Implementation: find the matching ReplicaSet and patch the
    Deployment's pod template to match. Mirrors what ``kubectl rollout
    undo`` does under the hood.
    """
    _core, apps, _client = await _tenant_kube_or_default(_tenant_id)
    d = await _run(apps.read_namespaced_deployment, name=name, namespace=namespace)
    rs_list = await _run(
        apps.list_namespaced_replica_set,
        namespace=namespace,
        label_selector=",".join(
            f"{k}={v}" for k, v in (d.spec.selector.match_labels or {}).items()
        ),
    )
    revisions = []
    for rs in rs_list.items:
        rev = (rs.metadata.annotations or {}).get(
            "deployment.kubernetes.io/revision", ""
        )
        if rev.isdigit():
            revisions.append((int(rev), rs))
    revisions.sort(key=lambda r: r[0], reverse=True)
    if len(revisions) < 2:
        return f"refusing to rollback {namespace}/{name}: only {len(revisions)} revision(s) on record"

    if to_revision is not None:
        target = next((rs for rev, rs in revisions if rev == to_revision), None)
        if target is None:
            return f"revision {to_revision} not found for {namespace}/{name}"
    else:
        # Skip the current revision (highest), pick the next one down.
        target = revisions[1][1]

    # Build the patch with the client's own serializer, NOT .to_dict():
    # .to_dict() emits snake_case keys (container_port), but the API server's
    # strategic-merge-patch needs camelCase or it 500s with "does not contain
    # declared merge key: containerPort". sanitize_for_serialization walks each
    # model's attribute_map → the camelCase the server expects.
    api_client = _client.ApiClient()
    body = {
        "spec": {
            "template": api_client.sanitize_for_serialization(
                target.spec.template
            )
        }
    }
    await _run(
        apps.patch_namespaced_deployment,
        name=name,
        namespace=namespace,
        body=body,
    )
    image = ", ".join(c.image for c in target.spec.template.spec.containers)
    return f"rolled back deployment/{namespace}/{name} to image {image}"


async def _scale_deployment(
    *,
    namespace: str,
    name: str,
    replicas: int,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    _core, apps, _client = await _tenant_kube_or_default(_tenant_id)
    body = {"spec": {"replicas": replicas}}
    await _run(
        apps.patch_namespaced_deployment_scale,
        name=name,
        namespace=namespace,
        body=body,
    )
    return f"scaled deployment/{namespace}/{name} to {replicas} replicas"


async def _restart_deployment(
    *, namespace: str, name: str, _tenant_id: uuid.UUID | None = None
) -> str:
    from datetime import datetime, timezone

    _core, apps, _client = await _tenant_kube_or_default(_tenant_id)
    stamp = datetime.now(tz=timezone.utc).isoformat()
    body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": stamp,
                    }
                }
            }
        }
    }
    await _run(
        apps.patch_namespaced_deployment,
        name=name,
        namespace=namespace,
        body=body,
    )
    return f"triggered rolling restart of deployment/{namespace}/{name}"


async def _delete_pod(
    *, namespace: str, name: str, _tenant_id: uuid.UUID | None = None
) -> str:
    core, _apps, _client = await _tenant_kube_or_default(_tenant_id)
    await _run(core.delete_namespaced_pod, name=name, namespace=namespace)
    return f"deleted pod {namespace}/{name} — replacement will be scheduled"


async def _patch_resource(
    *,
    namespace: str,
    kind: str,
    name: str,
    patch: dict[str, Any],
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """Apply a strategic-merge patch to a namespaced workload / config object.

    Equivalent to ``kubectl patch <kind> <name> -n <namespace>
    --type=strategic -p '<patch json>'``. The Python client defaults
    its Content-Type to ``application/strategic-merge-patch+json`` for
    these calls, which is what kubectl uses unless ``--type`` says
    otherwise — so list-merge keys like containers-by-name behave the
    way an operator expects.
    """
    core, apps, _client = await _tenant_kube_or_default(_tenant_id)
    kind_lc = kind.strip().lower()
    dispatch: dict[str, tuple[Callable[..., Any], str]] = {
        "deployment": (apps.patch_namespaced_deployment, "deployment"),
        "statefulset": (apps.patch_namespaced_stateful_set, "statefulset"),
        "daemonset": (apps.patch_namespaced_daemon_set, "daemonset"),
        "replicaset": (apps.patch_namespaced_replica_set, "replicaset"),
        "pod": (core.patch_namespaced_pod, "pod"),
        "configmap": (core.patch_namespaced_config_map, "configmap"),
        "service": (core.patch_namespaced_service, "service"),
        "secret": (core.patch_namespaced_secret, "secret"),
    }
    chosen = dispatch.get(kind_lc)
    if chosen is None:
        return (
            f"error: patch_resource does not support kind {kind!r} — "
            "supported kinds are Deployment, StatefulSet, DaemonSet, "
            "ReplicaSet, Pod, ConfigMap, Service, Secret."
        )
    patch_fn, label = chosen
    await _run(patch_fn, name=name, namespace=namespace, body=patch)
    return f"patched {label}/{namespace}/{name} via strategic merge"


# ── Metrics / logs (Prometheus + Loki) ───────────────────────────────────


async def _query_prometheus(
    *,
    query: str,
    time_range: str | None = None,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """Run a PromQL query against the tenant-configured Prometheus.

    ``time_range`` selects the API:
      - omitted → /api/v1/query (instant)
      - "Nm" / "Nh" → /api/v1/query_range over the last N minutes/hours
        with a sensible step.

    Returns the raw JSON ``result`` array as pretty-printed text. The
    LLM is more than happy to summarise it.
    """
    if _tenant_id is None:
        return "error: tenant context missing for query_prometheus"
    from daalu_automation.core.cluster_proxy import get_proxy_url

    async with AsyncSessionLocal() as db:
        # Look the row up directly (instead of going through
        # get_tenant_config) so we can also pick up its cluster_tunnel_id
        # and route the query through the edge proxy when set.
        # Prefer Thanos Query for PromQL. It speaks the same
        # /api/v1/query{,_range} surface as Prometheus but aggregates
        # long-history blocks, and — importantly — the `prometheus`
        # integration url is frequently an *Alertmanager* endpoint (it backs
        # the alerts surface + PrometheusAdapter.health, which probe
        # /api/v2/alerts) that 404s on /api/v1/query. So try `thanos` first,
        # fall back to `prometheus`, then the env default.
        slug = _target_tunnel.get()
        scope = []
        if slug:
            tunnel_id = await _tunnel_id_for(db, _tenant_id, slug)
            if tunnel_id is None:
                return f"error: no cluster named {slug!r} is onboarded"
            scope = [Integration.cluster_tunnel_id == tunnel_id]
        thanos_row = (
            await db.execute(
                select(Integration).where(
                    Integration.tenant_id == _tenant_id,
                    Integration.provider == "thanos",
                    *scope,
                ).order_by(Integration.created_at).limit(1)
            )
        ).scalar_one_or_none()
        prom_row = (
            await db.execute(
                select(Integration).where(
                    Integration.tenant_id == _tenant_id,
                    Integration.provider == "prometheus",
                    *scope,
                ).order_by(Integration.created_at).limit(1)
            )
        ).scalar_one_or_none()
        if slug and thanos_row is None and prom_row is None:
            return (
                f"error: cluster {slug!r} has no metrics store (Prometheus/"
                "Thanos) connected, so there are no metrics to query for it."
            )
        # A thanos row stuck in `error` must not shadow a healthy prometheus
        # row — observed live: a stale hub-local thanos URL DNS-failed every
        # agent PromQL for the tenant while the tunnel-routed prometheus
        # integration sat right there working.
        if thanos_row is not None and (
            getattr(thanos_row.status, "value", thanos_row.status) == "error"
        ):
            thanos_row = None
        chosen_row: Integration | None = thanos_row or prom_row
        prom_url = (chosen_row.config or {}).get("url") if chosen_row else ""
        if not prom_url:
            # Env-default fallback for legacy deploys with no integration row.
            cfg = await get_tenant_config(db, _tenant_id)
            prom_url = cfg.prometheus_url
        proxy = await get_proxy_url(
            db, chosen_row.cluster_tunnel_id if chosen_row else None
        )
    if not prom_url:
        return (
            "error: no Prometheus URL configured for this tenant — add a "
            "`prometheus` or `thanos` integration with `url` set."
        )
    base = prom_url.rstrip("/")
    params: dict[str, Any] = {"query": query}
    if time_range:
        seconds = _duration_to_seconds(time_range)
        if seconds is None:
            return f"error: bad time_range {time_range!r} (use e.g. '15m', '6h')"
        from datetime import datetime, timezone

        end = datetime.now(tz=timezone.utc).timestamp()
        params.update(
            {
                "start": end - seconds,
                "end": end,
                "step": max(15, seconds // 60),
            }
        )
        url = f"{base}/api/v1/query_range"
    else:
        url = f"{base}/api/v1/query"
    async with httpx.AsyncClient(timeout=15, proxy=proxy) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    if data.get("status") != "success":
        return f"error: prometheus replied {data.get('status')}: {data}"
    result = data.get("data", {}).get("result", [])
    return json.dumps(result, indent=2, default=str)


async def _query_loki(
    *,
    query: str,
    limit: int = 200,
    since: str = "15m",
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """Run a LogQL query against the tenant-configured Loki."""
    if _tenant_id is None:
        return "error: tenant context missing for query_loki"
    from daalu_automation.core.cluster_proxy import get_proxy_url

    async with AsyncSessionLocal() as db:
        slug = _target_tunnel.get()
        scope = []
        if slug:
            tunnel_id = await _tunnel_id_for(db, _tenant_id, slug)
            if tunnel_id is None:
                return f"error: no cluster named {slug!r} is onboarded"
            scope = [Integration.cluster_tunnel_id == tunnel_id]
        row = (
            await db.execute(
                select(Integration).where(
                    Integration.tenant_id == _tenant_id,
                    Integration.provider == "loki",
                    *scope,
                ).order_by(Integration.created_at).limit(1)
            )
        ).scalar_one_or_none()
        if not row and slug:
            return (
                f"error: cluster {slug!r} has no Loki log store connected, "
                "so there are no logs to query for it."
            )
        if not row:
            return (
                "error: no Loki integration configured for this tenant — add "
                "a `loki` integration with `url` set."
            )
        config = row.config or {}
        proxy = await get_proxy_url(db, row.cluster_tunnel_id)
    base = (config.get("url") or "").rstrip("/")
    if not base:
        return "error: loki integration has no `url`"
    auth = _auth_header_from_integration(config)
    seconds = _duration_to_seconds(since)
    if seconds is None:
        return f"error: bad since {since!r} (use e.g. '15m', '6h')"
    from datetime import datetime, timezone

    end_ns = int(datetime.now(tz=timezone.utc).timestamp() * 1e9)
    start_ns = end_ns - int(seconds * 1e9)
    params = {
        "query": query,
        "start": start_ns,
        "end": end_ns,
        "limit": limit,
        "direction": "backward",
    }
    headers = {"Authorization": auth} if auth else {}
    async with httpx.AsyncClient(timeout=15, headers=headers, proxy=proxy) as client:
        r = await client.get(f"{base}/loki/api/v1/query_range", params=params)
        r.raise_for_status()
        data = r.json()
    streams = data.get("data", {}).get("result", [])
    if not streams:
        return "(no log lines matched)"
    lines: list[str] = []
    for stream in streams:
        label = ",".join(
            f"{k}={v}" for k, v in (stream.get("stream") or {}).items() if k in ("app", "pod", "namespace", "container")
        )
        for _ts, msg in stream.get("values", []):
            lines.append(f"[{label}] {msg}")
    return "\n".join(lines[-limit:])


# ── Generic external HTTP (network gear, custom servers, …) ──────────────


async def _call_external_api(
    *,
    provider: str,
    method: str = "GET",
    path: str = "/",
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """Call any registered external integration's HTTP API.

    The tenant admin registers the device/server as an Integration row
    with ``config = {"base_url": "https://…", "auth_header": "Bearer …",
    "verify_tls": true}``. The LLM then references it by ``provider``;
    the runner attaches the auth header and forwards the request.

    Method is restricted to safe verbs (GET/POST/PUT/PATCH/DELETE) and
    the response body is truncated to ~4KB so a chatty endpoint doesn't
    blow up the context window.
    """
    method = method.upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        return f"error: method {method!r} not allowed"
    if _tenant_id is None:
        return "error: tenant context missing for call_external_api"
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                Integration.__table__.select().where(
                    Integration.tenant_id == _tenant_id,
                    Integration.provider == provider,
                )
            )
        ).first()
    if not row:
        return f"error: no integration registered for provider={provider!r}"
    config = row._mapping["config"] or {}
    base_url = (config.get("base_url") or "").rstrip("/")
    if not base_url:
        return f"error: integration {provider!r} has no `base_url`"
    verify = bool(config.get("verify_tls", True))
    headers: dict[str, str] = {}
    auth = _auth_header_from_integration(config)
    if auth:
        headers["Authorization"] = auth
    if config.get("extra_headers"):
        headers.update({str(k): str(v) for k, v in config["extra_headers"].items()})

    url = base_url + (path if path.startswith("/") else "/" + path)
    async with httpx.AsyncClient(timeout=20, headers=headers, verify=verify) as client:
        try:
            r = await client.request(method, url, params=query, json=body)
        except httpx.RequestError as e:
            return f"error: request failed — {e}"
    snippet = r.text or ""
    if len(snippet) > 4096:
        snippet = snippet[:4096] + f"… (truncated, {len(r.text)} bytes total)"
    return f"HTTP {r.status_code} {url}\n\n{snippet}"


# ── Helpers ──────────────────────────────────────────────────────────────


def _duration_to_seconds(s: str) -> int | None:
    s = s.strip()
    if not s:
        return None
    unit = s[-1]
    num = s[:-1]
    if not num.isdigit():
        return None
    n = int(num)
    if unit == "s":
        return n
    if unit == "m":
        return n * 60
    if unit == "h":
        return n * 3600
    if unit == "d":
        return n * 86400
    return None


def _auth_header_from_integration(config: dict[str, Any]) -> str | None:
    """Build an ``Authorization`` header value from a few common shapes."""
    if config.get("auth_header"):
        return str(config["auth_header"])
    token = config.get("bearer_token") or config.get("api_token") or config.get("password")
    if token:
        return f"Bearer {token}"
    user = config.get("username")
    pwd = config.get("password")
    if user and pwd:
        import base64

        return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return None


# ── Registry ─────────────────────────────────────────────────────────────


TOOLS: dict[str, ToolSpec] = {
    "get_pod_logs": ToolSpec(
        name="get_pod_logs",
        description=(
            "Fetch recent stdout/stderr from a pod. Use this first whenever an alert "
            "mentions CrashLoopBackOff or a service error."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "pod": {"type": "string"},
                "container": {
                    "type": "string",
                    "description": "Optional container name; omit for single-container pods.",
                },
                "tail_lines": {"type": "integer", "default": 200},
                "previous": {
                    "type": "boolean",
                    "default": False,
                    "description": "Set true to fetch logs from the *previous* crashed instance.",
                },
            },
            "required": ["namespace", "pod"],
        },
        handler=_get_pod_logs,
    ),
    "describe_pod": ToolSpec(
        name="describe_pod",
        description="Summarise a pod's phase, container statuses, restart counts, and conditions.",
        input_schema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "pod": {"type": "string"},
            },
            "required": ["namespace", "pod"],
        },
        handler=_describe_pod,
    ),
    "get_pod_events": ToolSpec(
        name="get_pod_events",
        description=(
            "List recent Kubernetes events for a namespace, optionally filtered to "
            "a single pod. Surfaces OOMKill, ImagePullBackOff, FailedScheduling, "
            "Liveness probe failures, etc."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "pod": {"type": "string"},
                "limit": {"type": "integer", "default": 30},
            },
            "required": ["namespace"],
        },
        handler=_get_pod_events,
    ),
    "list_pods": ToolSpec(
        name="list_pods",
        description="List pods in a namespace with status + restart counts.",
        input_schema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "label_selector": {"type": "string"},
            },
            "required": ["namespace"],
        },
        handler=_list_pods,
    ),
    "get_deployment": ToolSpec(
        name="get_deployment",
        description="Show desired/ready replica counts, images, and conditions for a Deployment.",
        input_schema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["namespace", "name"],
        },
        handler=_get_deployment,
    ),
    "rollout_history": ToolSpec(
        name="rollout_history",
        description="List the last ~10 revisions of a Deployment with images and timestamps.",
        input_schema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["namespace", "name"],
        },
        handler=_rollout_history,
    ),
    "recent_changes": ToolSpec(
        name="recent_changes",
        description=(
            "What changed recently: Deployment rollouts (each new revision with its "
            "image/env/args change), Deployment scaling events, and Argo CD syncs, "
            "newest first. Call this early — most incidents follow a change."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": "Namespace to inspect; omit for the whole cluster.",
                },
                "hours": {
                    "type": "integer",
                    "default": 24,
                    "description": "How far back to look (1-336).",
                },
            },
            "required": [],
        },
        handler=_recent_changes,
    ),
    "rollout_undo": ToolSpec(
        name="rollout_undo",
        description=(
            "Roll a Deployment back to its previous revision (or a specific revision). "
            "Requires operator approval before executing."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "name": {"type": "string"},
                "to_revision": {
                    "type": "integer",
                    "description": "Specific revision to roll back to; omit for previous.",
                },
            },
            "required": ["namespace", "name"],
        },
        handler=_rollout_undo,
        requires_approval=True,
    ),
    "scale_deployment": ToolSpec(
        name="scale_deployment",
        description="Set a Deployment's replica count. Requires operator approval.",
        input_schema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "name": {"type": "string"},
                "replicas": {"type": "integer", "minimum": 0},
            },
            "required": ["namespace", "name", "replicas"],
        },
        handler=_scale_deployment,
        requires_approval=True,
    ),
    "restart_deployment": ToolSpec(
        name="restart_deployment",
        description=(
            "Trigger a rolling restart of a Deployment by stamping the pod template. "
            "Equivalent to `kubectl rollout restart`. Requires operator approval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["namespace", "name"],
        },
        handler=_restart_deployment,
        requires_approval=True,
    ),
    "delete_pod": ToolSpec(
        name="delete_pod",
        description=(
            "Delete a single pod so its controller reschedules a fresh replacement. "
            "Use to clear a stuck instance. Requires operator approval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["namespace", "name"],
        },
        handler=_delete_pod,
        requires_approval=True,
    ),
    "patch_resource": ToolSpec(
        name="patch_resource",
        description=(
            "Apply a strategic-merge patch to a namespaced workload or config "
            "object (Deployment, StatefulSet, DaemonSet, ReplicaSet, Pod, "
            "ConfigMap, Service, Secret). Use for spec edits not covered by the "
            "targeted write tools — adding container capabilities (NET_ADMIN, "
            "NET_RAW), tweaking resource limits, flipping a config-map key, or "
            "adjusting a service selector. The patch body is the minimal JSON "
            "path to the field(s) you want to set; strategic merge handles "
            "list-merge keys like containers-by-name automatically. Requires "
            "operator approval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": [
                        "Deployment",
                        "StatefulSet",
                        "DaemonSet",
                        "ReplicaSet",
                        "Pod",
                        "ConfigMap",
                        "Service",
                        "Secret",
                    ],
                },
                "name": {"type": "string"},
                "patch": {
                    "type": "object",
                    "description": (
                        "Strategic-merge patch body. Example to add NET_ADMIN "
                        "+ NET_RAW to a DaemonSet container called "
                        "'cilium-agent': "
                        "{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"cilium-agent\",\"securityContext\":{\"capabilities\":{\"add\":[\"NET_ADMIN\",\"NET_RAW\"]}}}]}}}}"
                    ),
                },
            },
            "required": ["namespace", "kind", "name", "patch"],
        },
        handler=_patch_resource,
        requires_approval=True,
    ),
    "query_prometheus": ToolSpec(
        name="query_prometheus",
        description=(
            "Run a PromQL query against this tenant's Prometheus. Use for "
            "latency, error rate, saturation, and any metric the alert payload "
            "references. Omit `time_range` for an instant query, or pass e.g. "
            "'15m' / '6h' for a range query (returns the time series)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "PromQL expression."},
                "time_range": {
                    "type": "string",
                    "description": "Range like '15m', '1h', '6h'. Omit for instant query.",
                },
            },
            "required": ["query"],
        },
        handler=_query_prometheus,
    ),
    "query_loki": ToolSpec(
        name="query_loki",
        description=(
            "Run a LogQL query against this tenant's Loki to fetch matching log "
            "lines. Use when you need server / application logs that aren't "
            "scoped to a single Kubernetes pod (e.g. ingress logs, edge logs)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "LogQL expression, e.g. '{app=\"checkout\"} |= \"ERROR\"'.",
                },
                "limit": {"type": "integer", "default": 200, "maximum": 1000},
                "since": {
                    "type": "string",
                    "description": "Window like '5m', '1h'. Defaults to 15m.",
                },
            },
            "required": ["query"],
        },
        handler=_query_loki,
    ),
    "call_external_api": ToolSpec(
        name="call_external_api",
        description=(
            "Make an HTTP call to any external system this tenant has "
            "registered as an Integration. Use for switches, firewalls, "
            "managed servers, custom internal APIs, etc. The integration "
            "row supplies the base URL and auth — you only pass the path, "
            "method, query and body. Method is restricted to GET/POST/PUT/"
            "PATCH/DELETE. Response body is truncated at 4KB."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "Integration provider slug (e.g. 'cisco-meraki', 'datadog', 'custom-cmdb').",
                },
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                    "default": "GET",
                },
                "path": {"type": "string", "description": "Path on the base URL, starting with /."},
                "query": {"type": "object", "description": "Optional query string parameters."},
                "body": {"type": "object", "description": "Optional JSON request body."},
            },
            "required": ["provider", "path"],
        },
        handler=_call_external_api,
        # Default to safe (GET-equivalent) — the LLM has to explicitly call
        # with a write method, at which point the per-method gating below
        # kicks in.
    ),
}


# Tool names whose effect is potentially state-changing. Anything in this
# set lands as a pending AlertAction and waits for operator approval.
_WRITE_TOOLS = {
    "rollout_undo",
    "scale_deployment",
    "restart_deployment",
    "delete_pod",
    "patch_resource",
}


# Merge in the cloud read tools. Each cloud module exposes a `tool_specs()`
# function returning plain dicts; we wrap them in ToolSpec here so the
# rest of the file (anthropic_tool_definitions, execute_tool,
# tool_requires_approval) doesn't have to special-case cloud vs kube.
# Cloud tools are read-only in v1 — requires_approval stays False.
def _register_cloud_tools() -> None:
    from daalu_automation.core import cloud_aws, cloud_azure, cloud_gcp

    for mod in (cloud_aws, cloud_gcp, cloud_azure):
        for name, raw in mod.tool_specs().items():
            TOOLS[name] = ToolSpec(
                name=name,
                description=raw["description"],
                input_schema=raw["input_schema"],
                handler=raw["handler"],
                requires_approval=raw.get("requires_approval", False),
            )


_register_cloud_tools()


# SoT tools (propose_change, …). Same merge pattern as cloud tools so
# the rest of the registry doesn't have to special-case them. The
# propose_change tool sets requires_approval=False — the ChangeProposal
# row it creates IS the approval surface; gating it at the chat-action
# level would split authority across two distinct UIs.
def _register_sot_tools() -> None:
    from daalu_automation.core import sot_tools

    for name, raw in sot_tools.tool_specs().items():
        TOOLS[name] = ToolSpec(
            name=name,
            description=raw["description"],
            input_schema=raw["input_schema"],
            handler=raw["handler"],
            requires_approval=raw.get("requires_approval", False),
        )


_register_sot_tools()


# Every approval-gated action carries the model's one-line "why + expected
# outcome" so the approve card can tell the operator what a patch is *for*,
# not just which object it touches. It's metadata for the human: stripped in
# ``execute_tool`` before the handler runs.
REASON_ARG = "reason"
_REASON_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        "One short sentence for the operator approving this action: why it is "
        "needed (cite the evidence) and what will happen once it runs. E.g. "
        "'Roll back to revision 3, which set GPU_MEM_UTIL=0.10, so vLLM fits "
        "beside the other model on the GPU and the pod becomes Ready.'"
    ),
}


def _gated(spec: ToolSpec) -> bool:
    """Can this tool ever need approval? (call_external_api does for non-GET.)"""
    return spec.requires_approval or spec.name == "call_external_api"


def _model_schema(spec: ToolSpec) -> dict[str, Any]:
    """The input schema shown to the model: approval-gated tools gain ``reason``."""
    if not _gated(spec):
        return spec.input_schema
    schema = dict(spec.input_schema)
    schema["properties"] = {**schema.get("properties", {}), REASON_ARG: _REASON_SCHEMA}
    if spec.requires_approval:
        schema["required"] = [*schema.get("required", []), REASON_ARG]
    return schema


def anthropic_tool_definitions() -> list[dict[str, Any]]:
    """Tool specs in the shape ``anthropic.messages.create`` expects."""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": _model_schema(spec),
        }
        for spec in TOOLS.values()
    ]


def openai_tool_definitions() -> list[dict[str, Any]]:
    """The same registry in OpenAI / DeepSeek function-calling shape.

    Mirrors :func:`anthropic_tool_definitions` but wraps each spec in the
    ``{"type": "function", "function": {...}}`` envelope the OpenAI
    chat-completions API — and OpenAI-compatible servers like DeepSeek
    and vLLM — expect. ``input_schema`` is already a JSON Schema object,
    which is exactly what ``parameters`` wants. Used by the alert-chat
    agent so its investigation runs on whatever tier the LLM router
    selects instead of being hardcoded to Anthropic.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": _model_schema(spec),
            },
        }
        for spec in TOOLS.values()
    ]


async def execute_tool(
    name: str,
    tool_input: dict[str, Any],
    *,
    tenant_id: uuid.UUID | None = None,
    cluster_name: str | None = None,
    cluster_slug: str | None = None,
) -> str:
    """Run a tool by name.

    Some handlers (``query_prometheus``, ``query_loki``,
    ``call_external_api``) need the tenant_id to look up integration
    credentials. We pass it in as ``_tenant_id`` only when the handler
    accepts it, so kube-only tools stay strict-keyword-clean.

    ``cluster_name`` selects which registered Kubernetes cluster the kube
    handlers operate on (set from the alert's cluster tag). It's published
    on a context var rather than threaded through every handler signature.

    ``cluster_slug`` targets an onboarded cluster by its slug (the copilot
    passes the one the user named or is viewing): kube tools use that
    cluster's kubeconfig (else the tenant default, like the cluster page),
    and metrics/logs come only from the store linked to that cluster.
    """
    spec = TOOLS.get(name)
    if spec is None:
        return f"error: unknown tool {name!r}"
    kwargs = dict(tool_input)
    kwargs.pop(REASON_ARG, None)
    handler_params = inspect.signature(spec.handler).parameters
    if "_tenant_id" in handler_params:
        kwargs["_tenant_id"] = tenant_id
    token = _target_cluster.set(cluster_name)
    tunnel_token = _target_tunnel.set(cluster_slug)
    try:
        # Hard ceiling so a single hung tool can never wedge the whole triage
        # turn. Per-call kube/httpx timeouts bound each network hop; this is the
        # backstop for a handler that makes several hops or blocks elsewhere.
        return await asyncio.wait_for(spec.handler(**kwargs), timeout=_TOOL_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("kube_tool.timed_out", tool=name, input=tool_input)
        return f"error: tool {name!r} timed out after {_TOOL_TIMEOUT_S}s"
    except KubeUnavailable as e:
        return f"error: kube client unavailable — {e}"
    except Exception as e:
        # A tool failure must always come back as an "error: …" string the
        # model/UI can show — never an unhandled 500. Guard the log call too,
        # so a logging hiccup can't escalate a handled error into a crash.
        try:
            logger.exception("kube_tool.failed", tool=name, tool_input=tool_input)
        except Exception:
            pass
        return f"error: {type(e).__name__}: {e}"
    finally:
        _target_cluster.reset(token)
        _target_tunnel.reset(tunnel_token)


def tool_requires_approval(
    name: str, tool_input: dict[str, Any] | None = None
) -> bool:
    """Decide whether a given tool call needs operator approval.

    Static-write tools (rollout_undo, scale_deployment, …) always
    require approval. ``call_external_api`` requires approval whenever
    the method is anything other than GET — write HTTP verbs are
    operator-gated just like kubectl writes.
    """
    spec = TOOLS.get(name)
    if spec is None:
        return False
    if spec.requires_approval:
        return True
    if name == "call_external_api" and tool_input is not None:
        method = str(tool_input.get("method", "GET")).upper()
        return method != "GET"
    return False


_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "boolean": bool,
}


def validate_write_input(
    name: str, tool_input: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """Check an approval-gated call's arguments before it becomes an approve card.

    Returns ``(normalized_input, error)``. A malformed call must go back to the
    model as a tool error it can correct, never reach the operator: Qwen via
    vLLM's ``qwen3_coder`` parser has emitted ``patch_resource(patch="")``,
    which rendered as an approvable action that could only fail or no-op.

    Normalizes an object argument sent as a JSON string (a common tool-parser
    quirk) into the object. Checks required keys, top-level JSON types and
    enums, rejects empty required values, and requires a non-empty ``reason``.
    """
    spec = TOOLS.get(name)
    if spec is None:
        return tool_input, f"unknown tool {name!r}"
    schema = _model_schema(spec)
    props: dict[str, Any] = schema.get("properties", {})
    out = dict(tool_input)
    problems: list[str] = []

    for key, prop in props.items():
        if key not in out or out[key] is None:
            continue
        want = prop.get("type")
        val = out[key]
        if want == "object" and isinstance(val, str):
            try:
                parsed = json.loads(val) if val.strip() else None
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                out[key] = val = parsed
        expected = _JSON_TYPES.get(want) if isinstance(want, str) else None
        # bool is an int subclass; don't let True pass as an integer.
        if expected is not None and (
            not isinstance(val, expected)
            or (want == "integer" and isinstance(val, bool))
        ):
            problems.append(f"`{key}` must be a JSON {want}, got {val!r}")
        elif "enum" in prop and val not in prop["enum"]:
            problems.append(f"`{key}` must be one of {prop['enum']}, got {val!r}")

    # ``reason`` is required on every call that needs approval, including a
    # non-GET call_external_api whose schema can't mark it required.
    required = dict.fromkeys([*schema.get("required", []), REASON_ARG])
    for key in required:
        val = out.get(key)
        if val is None or (isinstance(val, (str, dict, list)) and not (
            val.strip() if isinstance(val, str) else val
        )):
            problems.append(f"`{key}` is required and must not be empty")

    if not problems:
        return out, None
    return out, (
        f"{name} was NOT queued for approval — invalid arguments: "
        + "; ".join(dict.fromkeys(problems))
        + ". Call it again with every required argument filled in, or pick a "
        "better-fitting tool (e.g. rollout_undo when the rollout history shows "
        "the change that broke the workload)."
    )


def render_tool_call(name: str, tool_input: dict[str, Any]) -> str:
    """Human-readable preview of a pending tool call (for the approval card)."""
    return f"{name}({json.dumps(tool_input, sort_keys=True)})"
