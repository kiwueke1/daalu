"""Dry-run / blast-radius previews for proposed writes (design §6).

``preview_write`` reads the target object and says what approving the write
would do, so the approve card can show it:

* ``scale_deployment``   — current → target replicas;
* ``restart_deployment`` — the pods that will be recycled;
* ``rollout_undo``       — current vs target revision (images, settings);
* ``delete_pod``         — the pod, its owner, and whether it comes back;
* ``patch_resource``     — a server-side dry run (``dry_run="All"``) and the
  keys it changes.

Returns ``{summary, blast_radius: {pods, deployments}, details}``, or ``{}``
when there is nothing to preview. Best effort: never raises.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_TIMEOUT_S = 25
_REV_ANNOTATION = "deployment.kubernetes.io/revision"
# Keys that change on every write or carry no intent.
_NOISE = (
    "metadata.resourceVersion", "metadata.generation", "metadata.managedFields",
    "metadata.creationTimestamp", "metadata.uid", "status",
)
_RECREATING_OWNERS = {"ReplicaSet", "StatefulSet", "DaemonSet", "Job", "ReplicationController"}


async def _run(fn: Any, *args: Any, **kwargs: Any) -> Any:
    from daalu_automation.core import kube_tools

    return await kube_tools._run(fn, *args, **kwargs)


def _out(summary: str, *, pods: int = 0, deployments: int = 0, **details: Any) -> dict[str, Any]:
    return {
        "summary": summary,
        "blast_radius": {"pods": int(pods), "deployments": int(deployments)},
        "details": details,
    }


def _selector(d: Any) -> str:
    return ",".join(f"{k}={v}" for k, v in (d.spec.selector.match_labels or {}).items())


def _images(template: Any) -> list[str]:
    return [c.image for c in (template.spec.containers or [])]


async def _scale(core: Any, apps: Any, client_mod: Any, inp: dict[str, Any]) -> dict[str, Any]:
    ns, name, target = inp["namespace"], inp["name"], int(inp["replicas"])
    d = await _run(apps.read_namespaced_deployment, name=name, namespace=ns)
    current = int(d.spec.replicas or 0)
    delta = target - current
    if delta == 0:
        what = "no change"
    elif delta > 0:
        what = f"{delta} pod(s) added"
    else:
        what = f"{-delta} pod(s) removed"
    summary = f"Scale deployment {ns}/{name} from {current} to {target} replicas ({what})."
    if target == 0:
        summary += " The workload goes fully offline."
    return _out(
        summary, pods=abs(delta), deployments=1 if delta else 0,
        current_replicas=current, target_replicas=target,
        ready_replicas=int(d.status.ready_replicas or 0) if d.status else 0,
    )


async def _restart(core: Any, apps: Any, client_mod: Any, inp: dict[str, Any]) -> dict[str, Any]:
    ns, name = inp["namespace"], inp["name"]
    d = await _run(apps.read_namespaced_deployment, name=name, namespace=ns)
    pods = await _run(core.list_namespaced_pod, namespace=ns, label_selector=_selector(d))
    names = [p.metadata.name for p in pods.items]
    strategy = (d.spec.strategy.type if d.spec.strategy else None) or "RollingUpdate"
    summary = f"Rolling restart of deployment {ns}/{name}: {len(names)} pod(s) recycled"
    summary += (
        " all at once (strategy Recreate: brief downtime)." if strategy == "Recreate"
        else " one batch at a time (RollingUpdate)."
    )
    return _out(summary, pods=len(names), deployments=1, pods_recycled=names[:50], strategy=strategy)


async def _undo(core: Any, apps: Any, client_mod: Any, inp: dict[str, Any]) -> dict[str, Any]:
    from daalu_automation.core import kube_tools

    ns, name = inp["namespace"], inp["name"]
    d = await _run(apps.read_namespaced_deployment, name=name, namespace=ns)
    rs_list = await _run(apps.list_namespaced_replica_set, namespace=ns, label_selector=_selector(d))
    revs = []
    for rs in rs_list.items:
        rev = str((rs.metadata.annotations or {}).get(_REV_ANNOTATION, ""))
        if rev.isdigit():
            revs.append((int(rev), rs))
    revs.sort(key=lambda r: r[0], reverse=True)
    if len(revs) < 2:
        return _out(
            f"Rollback of {ns}/{name} will be refused: only {len(revs)} revision(s) on record.",
            revisions=[r for r, _ in revs],
        )
    current_rev, current = revs[0]
    to = inp.get("to_revision")
    if to is not None:
        target = next(((r, rs) for r, rs in revs if r == int(to)), None)
        if target is None:
            return _out(
                f"Rollback of {ns}/{name} will fail: revision {to} not found "
                f"(have {[r for r, _ in revs]}).",
            )
    else:
        target = revs[1]
    target_rev, target_rs = target
    cur_set = kube_tools._template_settings(current.spec.template)
    tgt_set = kube_tools._template_settings(target_rs.spec.template)
    changes = {
        k: {"from": cur_set.get(k), "to": tgt_set.get(k)}
        for k in sorted(set(cur_set) | set(tgt_set)) if cur_set.get(k) != tgt_set.get(k)
    }
    cur_img, tgt_img = _images(current.spec.template), _images(target_rs.spec.template)
    summary = f"Roll back deployment {ns}/{name} from revision {current_rev} to {target_rev}"
    if cur_img != tgt_img:
        summary += f": image {', '.join(cur_img)} → {', '.join(tgt_img)}"
    elif changes:
        summary += ": " + "; ".join(f"{k} {v['from']} → {v['to']}" for k, v in list(changes.items())[:3])
    replicas = int(d.spec.replicas or 0)
    summary += f". All {replicas} pod(s) are replaced."
    return _out(
        summary, pods=replicas, deployments=1,
        current_revision=current_rev, target_revision=target_rev,
        current_images=cur_img, target_images=tgt_img, setting_changes=changes,
    )


async def _delete_pod(core: Any, apps: Any, client_mod: Any, inp: dict[str, Any]) -> dict[str, Any]:
    ns, name = inp["namespace"], inp["name"]
    p = await _run(core.read_namespaced_pod, name=name, namespace=ns)
    owner = next((o for o in (p.metadata.owner_references or []) if o.controller), None) or next(
        iter(p.metadata.owner_references or []), None
    )
    owner_desc = f"{owner.kind}/{owner.name}" if owner else None
    deployment = None
    if owner is not None and owner.kind == "ReplicaSet":
        try:
            rs = await _run(apps.read_namespaced_replica_set, name=owner.name, namespace=ns)
            dep = next((o for o in (rs.metadata.owner_references or []) if o.kind == "Deployment"), None)
            deployment = dep.name if dep else None
        except Exception:  # noqa: BLE001
            deployment = None
    recreated = owner is not None and owner.kind in _RECREATING_OWNERS
    if recreated:
        via = f"deployment {deployment}" if deployment else owner_desc
        summary = f"Delete pod {ns}/{name}; {via} recreates it."
    else:
        summary = f"Delete pod {ns}/{name}; it has no controller, so it is NOT recreated."
    return _out(
        summary, pods=1, deployments=1 if deployment else 0,
        owner=owner_desc, deployment=deployment, recreated=recreated,
        phase=p.status.phase if p.status else None, node=p.spec.node_name if p.spec else None,
    )


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def _changed_paths(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    a, b = _flatten(before), _flatten(after)
    keys = sorted(
        k for k in set(a) | set(b)
        if a.get(k) != b.get(k) and not any(k == n or k.startswith(n + ".") or k.startswith(n + "[") for n in _NOISE)
    )
    return keys


def _to_dict(api_client: Any, obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if api_client is not None and hasattr(api_client, "sanitize_for_serialization"):
        return api_client.sanitize_for_serialization(obj)
    return obj.to_dict() if hasattr(obj, "to_dict") else {}


async def _patch(core: Any, apps: Any, client_mod: Any, inp: dict[str, Any]) -> dict[str, Any]:
    ns, kind, name, patch = inp["namespace"], str(inp["kind"]), inp["name"], inp.get("patch")
    if not isinstance(patch, dict) or not patch:
        return {}
    k = kind.strip().lower()
    table = {
        "deployment": (apps, "deployment"),
        "statefulset": (apps, "stateful_set"),
        "daemonset": (apps, "daemon_set"),
        "replicaset": (apps, "replica_set"),
        "pod": (core, "pod"),
        "configmap": (core, "config_map"),
        "service": (core, "service"),
        "secret": (core, "secret"),
    }
    if k not in table:
        return {}
    api, noun = table[k]
    read_fn = getattr(api, f"read_namespaced_{noun}")
    patch_fn = getattr(api, f"patch_namespaced_{noun}")
    api_client = getattr(api, "api_client", None)
    before = await _run(read_fn, name=name, namespace=ns)
    try:
        after = await _run(patch_fn, name=name, namespace=ns, body=patch, dry_run="All")
    except Exception as e:  # noqa: BLE001
        reason = getattr(e, "reason", None) or type(e).__name__
        body = str(getattr(e, "body", "") or e)[:500]
        return _out(
            f"Server-side dry run REJECTED the patch to {kind} {ns}/{name}: {reason}.",
            dry_run="rejected", error=body,
        )
    b, a = _to_dict(api_client, before), _to_dict(api_client, after)
    changed = _changed_paths(b, a)
    if k == "secret":
        changed = [p if not p.startswith(("data.", "stringData.")) else p.split(".")[0] + ".<redacted>"
                   for p in changed]
        changed = sorted(set(changed))
    template_changed = any(p.startswith("spec.template.") for p in changed)
    spec = (b.get("spec") or {}) if isinstance(b, dict) else {}
    status = (b.get("status") or {}) if isinstance(b, dict) else {}
    pods = deployments = 0
    if k in ("deployment", "statefulset") and template_changed:
        pods = int(spec.get("replicas") or 0)
    elif k == "daemonset" and template_changed:
        pods = int(status.get("desiredNumberScheduled") or status.get("desired_number_scheduled") or 0)
    elif k == "pod" and changed:
        pods = 1
    if k == "deployment" and changed:
        deployments = 1
    if not changed:
        summary = f"Server-side dry run of the patch to {kind} {ns}/{name} succeeded but changes nothing."
    else:
        shown = ", ".join(changed[:5]) + (f" (+{len(changed) - 5} more)" if len(changed) > 5 else "")
        summary = f"Server-side dry run OK: patch to {kind} {ns}/{name} changes {shown}."
        if template_changed and pods:
            summary += f" The pod template changes, so {pods} pod(s) are replaced."
        elif k in ("configmap", "secret"):
            summary += " Running pods do not pick this up until they restart."
    return _out(summary, pods=pods, deployments=deployments, dry_run="ok", changed_keys=changed[:50])


_PREVIEWERS = {
    "scale_deployment": _scale,
    "restart_deployment": _restart,
    "rollout_undo": _undo,
    "delete_pod": _delete_pod,
    "patch_resource": _patch,
}


async def preview_write(
    tool: str, tool_input: dict[str, Any], *, tenant_id: Any, cluster: str | None,
) -> dict[str, Any]:
    fn = _PREVIEWERS.get(tool)
    if fn is None:
        return {}
    try:
        from daalu_automation.core import kube_tools

        core, apps, client_mod = await kube_tools._tenant_kube_or_default(tenant_id, cluster)
        return await asyncio.wait_for(fn(core, apps, client_mod, dict(tool_input or {})), timeout=_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 — a preview must never block a proposal
        logger.info("aiops.preview.unavailable", tool=tool, error=f"{type(e).__name__}: {e}")
        return {}
