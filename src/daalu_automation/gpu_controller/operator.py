"""Install the NVIDIA GPU Operator on a connected cluster.

AI Factory → Add GPU lists the GPU nodes a cluster advertises, and a cluster
without the GPU Operator advertises none — so the form used to stop at "No
GPU node found" and send you to a page of helm commands. This module does
those commands for you, over the tunnel, with the cluster-admin credential
the edge registered:

* works out the settings the cluster needs (on k3s the operator's container
  toolkit must not edit containerd's config, or the node goes NotReady),
* stamps the DCGM metrics with the signed-in tenant's id so AI Factory shows
  them, and only asks for a ServiceMonitor when the Prometheus Operator is
  there to read it,
* reports progress, and recognises a node that still has no NVIDIA runtime
  so the form can show the exact host commands instead of a stuck pod.

The NVIDIA driver on the node stays a prerequisite: the operator's prebuilt
driver images don't cover every distro/kernel, and platform teams expect to
own it.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client.exceptions import ApiException

from daalu_automation.config_manager_controller.helm_runner import HelmRunner

CHART_REPO = "https://helm.ngc.nvidia.com/nvidia"
CHART = "gpu-operator"
# v25.3.0+ is needed on containerd 2.x (config version 3); this is the
# version tested end to end on k3s v1.35 / containerd 2.2.
CHART_VERSION = "v25.10.1"
NAMESPACE = "gpu-operator"
RELEASE = "gpu-operator"
SERVICEMONITOR_CRD = "servicemonitors.monitoring.coreos.com"

# k3s: where the operator's toolkit may write its containerd snippet. k3s
# generates its own containerd config and never reads this file; pointing the
# toolkit here keeps it off k3s's config (writing that with stock CNI paths is
# what takes a k3s node NotReady).
K3S_TOOLKIT_SCRATCH_CONFIG = "/etc/daalu/nvidia-toolkit-unused/config.toml"
K3S_CONTAINERD_SOCKET = "/run/k3s/containerd/containerd.sock"

# Until the toolkit has signalled containerd (and, on k3s, k3s has come back),
# "no runtime for nvidia" is the expected state, not a problem. Only call it
# blocked once the operator has been there this long.
BLOCKED_AFTER_SECONDS = 180

# Pod-sandbox errors containerd returns when a pod asks for the ``nvidia``
# runtime class and the node has no NVIDIA runtime configured.
_RUNTIME_MISSING_MARKERS = (
    'no runtime for "nvidia"',
    "unknown runtime handler",
    'runtime handler "nvidia"',
)

# What to run on a k3s GPU node that has the driver but not the toolkit.
K3S_HOST_TOOLKIT_COMMANDS = """\
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \\
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \\
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \\
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo systemctl restart k3s"""


# ── Pure helpers (unit-tested) ─────────────────────────────────────────


def is_k3s(node_infos: list[dict[str, str]]) -> bool:
    """True when any node reports a k3s kubelet or k3s's bundled containerd."""
    for info in node_infos:
        kubelet = info.get("kubelet_version") or ""
        runtime = info.get("container_runtime_version") or ""
        if "+k3s" in kubelet or "-k3s" in runtime:
            return True
    return False


def build_values(*, tenant_id: str, k3s: bool, servicemonitor: bool) -> dict[str, Any]:
    """Helm values for the operator on this cluster.

    * ``driver.enabled=false`` — the driver is the node's prerequisite.
    * ``cdi.enabled=true`` — GPUs reach pods through the CDI spec, so Daalu's
      vLLM pod needs only ``nvidia.com/gpu: 1``, no runtime class.
    * k3s: the toolkit installs its binaries under /usr/local/nvidia/toolkit
      but writes its containerd snippet to a file k3s never reads. Left to
      its defaults it writes containerd's config with stock CNI paths, which
      k3s doesn't use, and the node goes NotReady. It then signals
      containerd; k3s's embedded containerd exits on that, systemd restarts
      k3s once (about a minute without the API; running pods keep running),
      and k3s finds the toolkit's runtime and registers ``nvidia`` itself.
      Verified on k3s v1.35 / containerd 2.2 with no toolkit on the host.
    * DCGM's ServiceMonitor ships off; AI Factory needs it on, with every
      series labelled ``tenant=<id>`` (its queries are tenant-scoped).
    """
    values: dict[str, Any] = {
        "driver": {"enabled": False},
        "cdi": {"enabled": True},
    }
    if k3s:
        values["toolkit"] = {
            "enabled": True,
            "env": [
                {"name": "CONTAINERD_CONFIG", "value": K3S_TOOLKIT_SCRATCH_CONFIG},
                {"name": "CONTAINERD_SOCKET", "value": K3S_CONTAINERD_SOCKET},
                {"name": "RUNTIME_CONFIG_SOURCE", "value": "file"},
            ],
        }
    if servicemonitor:
        values["dcgmExporter"] = {
            "serviceMonitor": {
                "enabled": True,
                "interval": "15s",
                "relabelings": [
                    {
                        "action": "replace",
                        "targetLabel": "tenant",
                        "replacement": str(tenant_id),
                    }
                ],
            }
        }
    return values


def runtime_missing(messages: list[str]) -> bool:
    """Do these pod/event messages say the node has no NVIDIA runtime?"""
    return any(m in (msg or "") for msg in messages for m in _RUNTIME_MISSING_MARKERS)


def summarise(
    *,
    installed: bool,
    gpu_nodes: int,
    k3s: bool,
    pods_total: int,
    pods_ready: int,
    problem_messages: list[str],
    installed_for_seconds: float = BLOCKED_AFTER_SECONDS,
) -> dict[str, Any]:
    """Phase + the one hint the form should show."""
    hint: str | None = None
    commands: str | None = None
    if gpu_nodes > 0:
        phase = "ready"
    elif not installed:
        phase = "absent"
    elif runtime_missing(problem_messages) and installed_for_seconds >= BLOCKED_AFTER_SECONDS:
        phase = "blocked"
        if k3s:
            hint = (
                "The GPU node still has no NVIDIA runtime. k3s registers it "
                "when it restarts; if it wasn't restarted (k3s not run by "
                "systemd), install the NVIDIA Container Toolkit on the node "
                "and restart k3s."
            )
            commands = K3S_HOST_TOOLKIT_COMMANDS
        else:
            hint = (
                "The GPU node has no NVIDIA runtime configured in containerd. "
                "Install the NVIDIA Container Toolkit on the node and configure "
                "it for containerd (nvidia-ctk runtime configure)."
            )
    else:
        phase = "installing"
    return {
        "phase": phase,
        "installed": installed,
        "gpu_nodes": gpu_nodes,
        "distribution": "k3s" if k3s else "kubernetes",
        "pods_total": pods_total,
        "pods_ready": pods_ready,
        "hint": hint,
        "host_commands": commands,
    }


# ── Cluster reads ──────────────────────────────────────────────────────


async def cluster_facts(api: k8s_client.ApiClient) -> dict[str, Any]:
    """k3s or not, GPU nodes advertised, and whether ServiceMonitors exist."""
    core = k8s_client.CoreV1Api(api)
    nodes = (await core.list_node()).items
    infos = [
        {
            "kubelet_version": getattr(n.status.node_info, "kubelet_version", "") or "",
            "container_runtime_version": getattr(
                n.status.node_info, "container_runtime_version", ""
            )
            or "",
        }
        for n in nodes
        if n.status and n.status.node_info
    ]
    gpu_nodes = 0
    for n in nodes:
        alloc = (n.status.allocatable or {}) if n.status else {}
        if str(alloc.get("nvidia.com/gpu") or "0") not in ("0", ""):
            gpu_nodes += 1
    try:
        await k8s_client.ApiextensionsV1Api(api).read_custom_resource_definition(
            SERVICEMONITOR_CRD
        )
        servicemonitor = True
    except ApiException as e:
        if e.status != 404:
            raise
        servicemonitor = False
    return {"k3s": is_k3s(infos), "gpu_nodes": gpu_nodes, "servicemonitor": servicemonitor}


async def operator_status(api: k8s_client.ApiClient) -> dict[str, Any]:
    """Where the operator install is, read from the cluster itself."""
    facts = await cluster_facts(api)
    installed_for = 0.0
    try:
        dep = await k8s_client.AppsV1Api(api).read_namespaced_deployment(
            "gpu-operator", NAMESPACE
        )
        installed = True
        created = dep.metadata.creation_timestamp if dep.metadata else None
        if created is not None:
            installed_for = (datetime.now(tz=timezone.utc) - created).total_seconds()
    except ApiException as e:
        if e.status != 404:
            raise
        installed = False

    pods_total = pods_ready = 0
    messages: list[str] = []
    if installed:
        core = k8s_client.CoreV1Api(api)
        for p in (await core.list_namespaced_pod(NAMESPACE)).items:
            pods_total += 1
            phase = p.status.phase if p.status else None
            statuses = (p.status.container_statuses or []) if p.status else []
            if phase == "Succeeded" or (
                phase == "Running" and statuses and all(c.ready for c in statuses)
            ):
                pods_ready += 1
            for c in statuses + ((p.status.init_container_statuses or []) if p.status else []):
                waiting = c.state.waiting if c.state else None
                if waiting and waiting.message:
                    messages.append(waiting.message)
        events = await core.list_namespaced_event(NAMESPACE)
        messages.extend(e.message or "" for e in events.items if e.type == "Warning")

    return summarise(
        installed=installed,
        gpu_nodes=facts["gpu_nodes"],
        k3s=facts["k3s"],
        pods_total=pods_total,
        pods_ready=pods_ready,
        problem_messages=messages,
        installed_for_seconds=installed_for,
    )


# ── Install ────────────────────────────────────────────────────────────


def _helm_env_defaults() -> None:
    # helm caches the chart under $HOME; the app containers' HOME may not be
    # writable, so keep helm's state in /tmp.
    for var, path in (
        ("HELM_CACHE_HOME", "/tmp/helm/cache"),
        ("HELM_CONFIG_HOME", "/tmp/helm/config"),
        ("HELM_DATA_HOME", "/tmp/helm/data"),
    ):
        os.environ.setdefault(var, path)


async def install(
    *,
    kubeconfig: dict[str, Any],
    tenant_id: str,
    k3s: bool,
    servicemonitor: bool,
    runner: Any = None,
) -> tuple[bool, str]:
    """``helm upgrade --install`` the operator. Returns ``(ok, error)``.

    Doesn't ``--wait``: the operator's pods take minutes, and the form polls
    :func:`operator_status` for that instead.
    """
    _helm_env_defaults()
    helm = HelmRunner(
        chart_path=CHART,
        runner=runner,
        timeout="5m",
        extra_args=["--repo", CHART_REPO, "--version", CHART_VERSION],
    )
    res = await helm.upgrade_install(
        release=RELEASE,
        namespace=NAMESPACE,
        values=build_values(tenant_id=tenant_id, k3s=k3s, servicemonitor=servicemonitor),
        kubeconfig=kubeconfig,
        wait=False,
    )
    if res.ok:
        return True, ""
    return False, (res.stderr or res.stdout or "helm failed").strip()[-1000:]
