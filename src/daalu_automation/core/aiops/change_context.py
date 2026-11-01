"""What changed recently? (design §6) — backs the ``recent_changes`` read tool.

Most incidents follow a change. This collects, for a namespace (or the whole
cluster) over the last N hours, newest first:

* Deployment rollouts — each new ReplicaSet revision, with what differs from
  the revision before it (image, env, args, command), so "the image changed
  ten minutes before the alert" is one line, not three tool calls;
* Deployment events — ``ScalingReplicaSet`` and anything else the controller
  reports against a Deployment;
* Argo CD ``Application`` syncs (``status.history`` and the current
  ``status.operationState``) targeting the namespace, if Argo CD is installed.

Uses ``kube_tools``' client resolution, so it reaches tenant clusters through
the WireGuard federation exactly like the other kube tools.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_MAX_LINES = 40
_REV_ANNOTATION = "deployment.kubernetes.io/revision"


def _ts(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        s = str(v).replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fmt(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _run(fn: Any, *args: Any, **kwargs: Any) -> Any:
    from daalu_automation.core import kube_tools

    return await kube_tools._run(fn, *args, **kwargs)


def _template_view(template: Any) -> tuple[dict[str, str], dict[str, str]]:
    """``({container: image}, {setting: value})`` for a pod template."""
    from daalu_automation.core import kube_tools

    images = {c.name: c.image for c in (template.spec.containers or [])}
    return images, kube_tools._template_settings(template)


def _diff(prev: Any, cur: Any) -> str:
    p_img, p_set = _template_view(prev.spec.template)
    c_img, c_set = _template_view(cur.spec.template)
    parts = []
    for name in sorted(set(p_img) | set(c_img)):
        a, b = p_img.get(name), c_img.get(name)
        if a != b:
            parts.append(f"image {name}: {a or '(none)'} → {b or '(none)'}")
    for key in sorted(set(p_set) | set(c_set)):
        a, b = p_set.get(key), c_set.get(key)
        if a != b:
            parts.append(f"{key}: {a if a is not None else '(unset)'} → {b if b is not None else '(unset)'}")
    if not parts:
        return "pod template changed (no image/env/args/command difference — e.g. a restart annotation or resources)"
    return "; ".join(parts[:6]) + (f"; +{len(parts) - 6} more" if len(parts) > 6 else "")


def _rollout_lines(rs_items: list[Any], *, since: datetime, cluster_wide: bool) -> list[tuple[datetime, str]]:
    by_deploy: dict[tuple[str, str], list[tuple[int, Any]]] = {}
    for rs in rs_items:
        owner = next(
            (o for o in (rs.metadata.owner_references or []) if o.kind == "Deployment"), None
        )
        if owner is None:
            continue
        rev = str((rs.metadata.annotations or {}).get(_REV_ANNOTATION, ""))
        if not rev.isdigit():
            continue
        by_deploy.setdefault((rs.metadata.namespace, owner.name), []).append((int(rev), rs))
    out: list[tuple[datetime, str]] = []
    for (ns, name), revs in by_deploy.items():
        revs.sort(key=lambda r: r[0])
        label = f"deployment/{ns}/{name}" if cluster_wide else f"deployment/{name}"
        for i, (rev, rs) in enumerate(revs):
            created = _ts(rs.metadata.creation_timestamp)
            if created is None or created < since:
                continue
            ready = f"{rs.status.ready_replicas or 0}/{rs.spec.replicas or 0} ready" if rs.status else ""
            if i == 0:
                images = ", ".join(c.image for c in rs.spec.template.spec.containers or [])
                what = f"revision {rev} created (image {images})"
            else:
                what = f"revision {revs[i - 1][0]} → {rev}: {_diff(revs[i - 1][1], rs)}"
            current = " [current]" if i == len(revs) - 1 else ""
            out.append((created, f"{label} rollout {what}{current}{', ' + ready if ready else ''}"))
    return out


def _event_lines(events: list[Any], *, since: datetime, cluster_wide: bool) -> list[tuple[datetime, str]]:
    out: list[tuple[datetime, str]] = []
    for e in events:
        obj = e.involved_object
        if obj is None or obj.kind != "Deployment":
            continue
        when = _ts(e.last_timestamp) or _ts(getattr(e, "event_time", None)) or _ts(
            e.metadata.creation_timestamp if e.metadata else None
        )
        if when is None or when < since:
            continue
        label = f"deployment/{obj.namespace}/{obj.name}" if cluster_wide else f"deployment/{obj.name}"
        count = f" x{e.count}" if (e.count or 1) > 1 else ""
        out.append((when, f"{label} event {e.type or 'Normal'} {e.reason}{count}: {e.message or ''}".rstrip()))
    return out


def _argo_lines(apps: list[dict[str, Any]], *, namespace: str | None, since: datetime) -> list[tuple[datetime, str]]:
    out: list[tuple[datetime, str]] = []
    for app in apps:
        meta = app.get("metadata") or {}
        spec = app.get("spec") or {}
        status = app.get("status") or {}
        dest_ns = (spec.get("destination") or {}).get("namespace")
        if namespace and dest_ns != namespace:
            continue
        label = f"argocd app/{meta.get('name', '?')}"
        for h in status.get("history") or []:
            when = _ts(h.get("deployedAt"))
            if when is None or when < since:
                continue
            src = h.get("source") or {}
            rev = str(h.get("revision") or "")[:12]
            where = src.get("repoURL") or src.get("chart") or ""
            target = src.get("targetRevision")
            out.append((when, f"{label} synced revision {rev}"
                              f"{' from ' + where if where else ''}"
                              f"{' @' + str(target) if target else ''} (history id {h.get('id')})"))
        op = status.get("operationState") or {}
        when = _ts(op.get("finishedAt")) or _ts(op.get("startedAt"))
        if op and when is not None and when >= since:
            rev = str((op.get("syncResult") or {}).get("revision") or "")[:12]
            msg = (op.get("message") or "").strip()
            out.append((when, f"{label} last operation {op.get('phase', '?')}"
                              f"{' revision ' + rev if rev else ''}{': ' + msg[:200] if msg else ''}"))
    return out


async def _argo_apps(core: Any, client_mod: Any) -> tuple[list[dict[str, Any]], str | None]:
    """Argo CD Applications cluster-wide. ``([], None)`` when Argo CD isn't
    installed; ``([], note)`` when it can't be read."""
    try:
        custom = client_mod.CustomObjectsApi(api_client=getattr(core, "api_client", None))
        res = await _run(
            custom.list_cluster_custom_object,
            group="argoproj.io", version="v1alpha1", plural="applications",
        )
    except Exception as e:  # noqa: BLE001
        status = getattr(e, "status", None)
        if status == 404:
            return [], None  # CRD absent — Argo CD isn't installed
        if status == 403:
            return [], "Argo CD Applications not readable (RBAC forbids applications.argoproj.io)"
        return [], f"Argo CD Applications not readable ({type(e).__name__})"
    items = (res or {}).get("items") if isinstance(res, dict) else None
    return list(items or []), None


async def recent_changes(
    *, tenant_id: Any, cluster: str | None, namespace: str | None = None, hours: int = 24,
) -> str:
    """Plain-text timeline of recent changes, newest first."""
    from daalu_automation.core import kube_tools

    try:
        hours = max(1, min(int(hours or 24), 24 * 14))
    except (TypeError, ValueError):
        hours = 24
    namespace = (namespace or "").strip() or None
    since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    scope = f"namespace {namespace}" if namespace else "all namespaces"
    try:
        core, apps, client_mod = await kube_tools._tenant_kube_or_default(tenant_id, cluster)
    except Exception as e:  # noqa: BLE001
        return f"error: kube client unavailable — {e}"

    cluster_wide = namespace is None
    if namespace:
        rs_call = _run(apps.list_namespaced_replica_set, namespace=namespace)
        ev_call = _run(
            core.list_namespaced_event, namespace=namespace,
            field_selector="involvedObject.kind=Deployment",
        )
    else:
        rs_call = _run(apps.list_replica_set_for_all_namespaces)
        ev_call = _run(
            core.list_event_for_all_namespaces, field_selector="involvedObject.kind=Deployment",
        )
    rs_res, ev_res, argo = await asyncio.gather(
        rs_call, ev_call, _argo_apps(core, client_mod), return_exceptions=True,
    )

    lines: list[tuple[datetime, str]] = []
    notes: list[str] = []
    if isinstance(rs_res, BaseException):
        notes.append(f"ReplicaSets not readable: {type(rs_res).__name__}: {rs_res}")
    else:
        lines += _rollout_lines(rs_res.items or [], since=since, cluster_wide=cluster_wide)
    if isinstance(ev_res, BaseException):
        notes.append(f"events not readable: {type(ev_res).__name__}: {ev_res}")
    else:
        lines += _event_lines(ev_res.items or [], since=since, cluster_wide=cluster_wide)
    if isinstance(argo, BaseException):
        notes.append(f"Argo CD not readable: {argo}")
    else:
        argo_items, argo_note = argo
        if argo_note:
            notes.append(argo_note)
        lines += _argo_lines(argo_items, namespace=namespace, since=since)

    lines.sort(key=lambda x: x[0], reverse=True)
    header = f"Recent changes in {scope}, last {hours}h, newest first:"
    if not lines:
        body = [f"No Deployment rollouts, Deployment events or Argo CD syncs in {scope} in the last {hours}h."]
    else:
        body = [f"{_fmt(t)}  {text}" for t, text in lines[:_MAX_LINES]]
        if len(lines) > _MAX_LINES:
            body.append(f"… {len(lines) - _MAX_LINES} older entries omitted")
    return "\n".join([header, *body, *(f"note: {n}" for n in notes)])
