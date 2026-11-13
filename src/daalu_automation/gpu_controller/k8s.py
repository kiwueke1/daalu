"""K8s helpers for the gpu-controller.

Reuses the generic create-or-patch machinery from
``nautobot_controller.k8s`` (``apply_one`` / ``delete_one`` /
``load_local_config`` / ``load_kubeconfig_dict``) — see the note in
``docs/plans/2026-06-02-gpu-onboarding.md`` §5 about extracting these to
a shared ``core/remote_k8s`` in P4. Adds the vLLM-specific readiness
probe used by the provisioning → active transition.
"""

from __future__ import annotations

from kubernetes_asyncio import client as k8s_client

from daalu_automation.gpu_controller.manifests import DEPLOYMENT_NAME

# Reuse the battle-tested generic helpers rather than copy them a third
# time. The PodDisruptionBudget (policy/v1) dispatch entry the GPU stack
# needs was added to that shared table.
from daalu_automation.nautobot_controller.k8s import (  # noqa: F401
    apply_one,
    delete_one,
    load_kubeconfig_dict,
    load_local_config,
)

GPU_CLASS_LABEL = "gpu-class"

# VRAM (MiB, as GPU Feature Discovery reports it in ``nvidia.com/gpu.memory``)
# → the ``gpu-class`` the manifests select on. Ordered ascending; a card is
# assigned the first class whose ceiling it fits under, so unlisted future
# cards fall through to the largest class rather than going unclassified.
_GPU_CLASS_BY_VRAM_MIB: tuple[tuple[int, str], ...] = (
    (20_000, "ada-16"),  # RTX 2000 Ada (16 GB) reports 16376
    (99_000, "ada-48"),  # RTX 6000 Ada (48 GB) reports 49140
)


def derive_gpu_class(labels: dict) -> str | None:
    """Infer the ``gpu-class`` for a node from its GPU-operator labels.

    ``gpu-class`` is a Daalu-specific label that the vLLM Deployment's
    nodeSelector matches on, but nothing in a stock GPU-operator install
    creates it — it was stamped by hand (or by scripts/deploy-gpu.sh), so a
    freshly onboarded cluster had an invisible prerequisite: until someone
    labelled the node, the pod sat ``Pending`` until the provision poll gave
    up, with nothing in the UI explaining why.

    GPU Feature Discovery already publishes what we need, so derive it rather
    than asking the operator to duplicate it. Returns ``None`` for a node with
    no usable VRAM label, in which case the caller keeps whatever class was
    explicitly requested.
    """
    raw = labels.get("nvidia.com/gpu.memory")
    if not raw:
        return None
    try:
        vram = int(str(raw))
    except (TypeError, ValueError):
        return None
    for ceiling, cls in _GPU_CLASS_BY_VRAM_MIB:
        if vram < ceiling:
            return cls
    return _GPU_CLASS_BY_VRAM_MIB[-1][1]


async def ensure_gpu_class_label(
    api: k8s_client.ApiClient, node_name: str, gpu_class: str
) -> None:
    """Stamp ``gpu-class=<gpu_class>`` on ``node_name`` if not already set.

    Called just before applying the vLLM stack so the Deployment's
    nodeSelector always has something to match. Idempotent, and a no-op when
    the label already holds the right value.
    """
    core = k8s_client.CoreV1Api(api)
    node = await core.read_node(name=node_name)
    current = ((node.metadata.labels or {}) if node.metadata else {}).get(
        GPU_CLASS_LABEL
    )
    if current == gpu_class:
        return
    await core.patch_node(
        name=node_name,
        body={"metadata": {"labels": {GPU_CLASS_LABEL: gpu_class}}},
    )


async def list_gpu_nodes(api: k8s_client.ApiClient) -> list[dict]:
    """List schedulable GPU nodes on the target cluster.

    Returns one dict per node that advertises ``nvidia.com/gpu`` in its
    allocatable resources: ``{name, gpu_class, gpu_product, allocatable_gpus,
    ready}``. ``gpu_class`` prefers an explicit ``gpu-class`` label and
    otherwise falls back to :func:`derive_gpu_class`, so the onboarding UI can
    pre-fill node + class on a cluster nobody has hand-labelled.
    """
    core = k8s_client.CoreV1Api(api)
    nodes = await core.list_node()
    out: list[dict] = []
    for n in nodes.items:
        alloc = (getattr(n.status, "allocatable", None) or {}) if n.status else {}
        gpus = alloc.get("nvidia.com/gpu")
        if not gpus or str(gpus) in ("0", ""):
            continue
        labels = (n.metadata.labels or {}) if n.metadata else {}
        ready = False
        for c in (getattr(n.status, "conditions", None) or []):
            if c.type == "Ready":
                ready = c.status == "True"
        out.append(
            {
                "name": n.metadata.name,
                "gpu_class": labels.get(GPU_CLASS_LABEL) or derive_gpu_class(labels),
                # Human-facing hint for the picker ("NVIDIA RTX 6000 Ada
                # Generation") so the operator recognises the card without
                # decoding a class string.
                "gpu_product": (labels.get("nvidia.com/gpu.product") or "").replace(
                    "-", " "
                )
                or None,
                "allocatable_gpus": str(gpus),
                "ready": ready,
            }
        )
    return out


async def vllm_progress(
    api: k8s_client.ApiClient,
    namespace: str,
    *,
    deployment_name: str = DEPLOYMENT_NAME,
    tail_lines: int = 200,
) -> dict:
    """Everything the UI needs to show *why* a deploy is still running.

    A first vLLM boot is three sequential multi-minute waits — pull the ~10 GB
    image, pull ~5 GB of weights, compile CUDA graphs — and from the outside
    all three look identical: "Deploying…". This surfaces which one you're in.

    Returns ``{pods: [...], events: [...], phase}``. Events come from the
    kubelet/scheduler and are the *only* signal available before a container
    starts (image pull, scheduling failures, volume problems); logs only exist
    once vLLM is actually running, so both are returned and the caller renders
    whichever is populated.

    Never raises for a missing pod or unreadable log — a half-created stack is
    the normal case here, and a 500 would blank the panel exactly when it is
    most useful.
    """
    core = k8s_client.CoreV1Api(api)
    out: dict = {"pods": [], "events": [], "phase": None}

    try:
        pods = await core.list_namespaced_pod(
            namespace=namespace, label_selector=f"app={deployment_name}"
        )
    except k8s_client.exceptions.ApiException:
        return out

    for p in pods.items:
        name = p.metadata.name
        status = getattr(p, "status", None)
        cstatuses = (getattr(status, "container_statuses", None) or []) if status else []
        containers = []
        for cs in cstatuses:
            st = getattr(cs, "state", None)
            # Surface the *reason* a container isn't up (ImagePullBackOff,
            # CrashLoopBackOff, …) rather than just "not ready".
            reason, message = None, None
            for kind in ("waiting", "terminated"):
                sub = getattr(st, kind, None) if st else None
                if sub is not None:
                    reason = getattr(sub, "reason", None)
                    message = getattr(sub, "message", None)
                    break
            containers.append(
                {
                    "name": cs.name,
                    "ready": bool(cs.ready),
                    "restarts": int(cs.restart_count or 0),
                    "reason": reason,
                    "message": (message or "")[:500] or None,
                }
            )

        logs = None
        # Only worth asking once a container has started; before that the API
        # returns a ContainerCreating error that reads like a real failure.
        if any(c["ready"] or c["reason"] is None for c in containers):
            try:
                logs = await core.read_namespaced_pod_log(
                    name=name,
                    namespace=namespace,
                    container="vllm",
                    tail_lines=tail_lines,
                )
            except k8s_client.exceptions.ApiException:
                logs = None

        out["pods"].append(
            {
                "name": name,
                "phase": getattr(status, "phase", None) if status else None,
                "node": getattr(p.spec, "node_name", None) if p.spec else None,
                "containers": containers,
                "logs": logs,
            }
        )

    if out["pods"]:
        out["phase"] = out["pods"][0]["phase"]

    # Events are namespace-wide rather than per-pod: a stack that failed before
    # the pod existed (quota, nodeSelector unsatisfiable) has events on the
    # ReplicaSet, and those are the ones worth reading.
    try:
        evs = await core.list_namespaced_event(namespace=namespace, limit=50)
        rows = []
        for e in evs.items:
            io = getattr(e, "involved_object", None)
            io_name = getattr(io, "name", "") or ""
            if deployment_name not in io_name:
                continue
            ts = getattr(e, "last_timestamp", None) or getattr(
                e, "event_time", None
            )
            rows.append(
                {
                    "type": e.type,
                    "reason": e.reason,
                    "object": f"{getattr(io, 'kind', '?')}/{io_name}",
                    "count": int(e.count or 1),
                    "message": (e.message or "")[:500],
                    "at": ts.isoformat() if ts else None,
                }
            )
        rows.sort(key=lambda r: r["at"] or "")
        out["events"] = rows[-30:]
    except k8s_client.exceptions.ApiException:
        pass

    return out


async def vllm_deployment_ready(
    api: k8s_client.ApiClient, namespace: str
) -> bool:
    """Has the vLLM Deployment reported at least one ready replica?

    Gates the reconcile loop's provisioning → active transition. First
    boot pulls ~5 GB of weights + compiles CUDA graphs, so this can take
    several minutes to flip true; the startupProbe (10-min budget) keeps
    the pod from being killed in the meantime.
    """
    apps = k8s_client.AppsV1Api(api)
    try:
        dep = await apps.read_namespaced_deployment(
            name=DEPLOYMENT_NAME, namespace=namespace
        )
    except k8s_client.exceptions.ApiException as e:
        if e.status == 404:
            return False
        raise
    status = getattr(dep, "status", None)
    if status is None:
        return False
    return int(getattr(status, "ready_replicas", 0) or 0) >= 1


# ── Live pod view (Add GPU: "kubectl get pods" + logs) ─────────────────

# Container states that mean "this won't come up by itself".
_FAILING_REASONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "InvalidImageName",
    "CreateContainerConfigError",
    "CreateContainerError",
    "RunContainerError",
    "Error",
    "OOMKilled",
}


def _container_reason(cs) -> tuple[str | None, str | None]:
    st = getattr(cs, "state", None)
    for kind in ("waiting", "terminated"):
        sub = getattr(st, kind, None) if st else None
        if sub is not None:
            return getattr(sub, "reason", None), getattr(sub, "message", None)
    return None, None


def pod_row(p, *, now=None) -> dict:
    """One ``kubectl get pods`` row, plus a health word the UI colours by.

    STATUS follows kubectl: ``Init:1/4`` while init containers run,
    ``Init:CrashLoopBackOff`` when one is stuck, a container's waiting or
    terminated reason (``ContainerCreating``, ``CrashLoopBackOff``,
    ``Completed``) when it has one, else the pod phase. ``health`` is
    ``ready`` (all containers ready), ``done`` (Succeeded), ``failing``
    (a reason that won't fix itself) or ``starting``.
    """
    from datetime import datetime, timezone

    status = getattr(p, "status", None)
    spec = getattr(p, "spec", None)
    phase = getattr(status, "phase", None) if status else None
    inits = (getattr(status, "init_container_statuses", None) or []) if status else []
    mains = (getattr(status, "container_statuses", None) or []) if status else []
    n_main = len(getattr(spec, "containers", None) or []) or len(mains)
    ready = sum(1 for c in mains if c.ready)
    restarts = sum(int(c.restart_count or 0) for c in [*inits, *mains])

    shown = phase or "Pending"
    failing = False
    if getattr(p.metadata, "deletion_timestamp", None):
        shown = "Terminating"
    else:
        done_inits = 0
        init_note = None
        for c in inits:
            reason, _ = _container_reason(c)
            st = getattr(c, "state", None)
            term = getattr(st, "terminated", None) if st else None
            if term is not None and (term.exit_code or 0) == 0:
                done_inits += 1
                continue
            if reason and reason not in ("PodInitializing",):
                init_note = f"Init:{reason}"
                failing = failing or reason in _FAILING_REASONS
            break
        if inits and done_inits < len(inits):
            shown = init_note or f"Init:{done_inits}/{len(inits)}"
        else:
            for c in mains:
                reason, _ = _container_reason(c)
                if reason and not c.ready:
                    shown = reason
                    failing = failing or reason in _FAILING_REASONS
                    break
            else:
                if phase == "Succeeded":
                    shown = "Completed"

    if phase == "Succeeded":
        health = "done"
    elif failing or phase == "Failed":
        health = "failing"
    elif phase == "Running" and mains and ready == len(mains):
        health = "ready"
    else:
        health = "starting"

    created = getattr(p.metadata, "creation_timestamp", None)
    now = now or datetime.now(tz=timezone.utc)
    containers = [c.name for c in (getattr(spec, "init_containers", None) or [])] + [
        c.name for c in (getattr(spec, "containers", None) or [])
    ]
    return {
        "name": p.metadata.name,
        "ready": f"{ready}/{n_main}",
        "status": shown,
        "restarts": restarts,
        "age_seconds": int((now - created).total_seconds()) if created else None,
        "node": getattr(spec, "node_name", None) if spec else None,
        "health": health,
        "containers": containers,
        # The container whose logs say the most right now: a running main
        # container, else the init container currently in progress.
        "log_container": _log_container(inits, mains, spec),
    }


def _log_container(inits, mains, spec) -> str | None:
    for c in mains:
        st = getattr(c, "state", None)
        if st and (getattr(st, "running", None) or getattr(st, "terminated", None)):
            return c.name
    for c in inits:
        st = getattr(c, "state", None)
        if st and getattr(st, "running", None):
            return c.name
        term = getattr(st, "terminated", None) if st else None
        if term is not None and (term.exit_code or 0) != 0:
            return c.name
    names = [c.name for c in (getattr(spec, "containers", None) or [])]
    return names[0] if names else None


async def list_pod_rows(api: k8s_client.ApiClient, namespace: str) -> list[dict]:
    """``kubectl get pods -n <namespace>``, as rows (sorted by name)."""
    core = k8s_client.CoreV1Api(api)
    pods = await core.list_namespaced_pod(namespace=namespace)
    return sorted((pod_row(p) for p in pods.items), key=lambda r: r["name"])


async def read_pod_logs(
    api: k8s_client.ApiClient,
    namespace: str,
    pod: str,
    container: str | None,
    *,
    tail_lines: int = 200,
) -> str:
    """Last lines of one container's log; a not-started container says so."""
    core = k8s_client.CoreV1Api(api)
    try:
        return await core.read_namespaced_pod_log(
            name=pod, namespace=namespace, container=container, tail_lines=tail_lines
        )
    except k8s_client.exceptions.ApiException as e:
        if e.status in (400, 404):
            # ContainerCreating / PodInitializing / gone: not an error worth a 500.
            return ""
        raise
