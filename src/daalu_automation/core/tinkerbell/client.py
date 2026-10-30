"""Async Tinkerbell CRD client (apply/watch over the tunnel).

Wraps ``kubernetes_asyncio.CustomObjectsApi`` for the Tinkerbell + Rufio
custom resources. The client targets the mgmt cluster the servers live in:
in-cluster when the controller runs there, or via a kubeconfig dict loaded
from the tenant's ``ClusterTunnel`` → ``Integration(provider="kubernetes")``
row (the same remote-apply path the nautobot-controller uses).

Reuses ``nautobot_controller.k8s.load_kubeconfig_dict`` / ``load_local_config``
so there is one place that decides in-cluster vs kubeconfig.
"""

from __future__ import annotations

from typing import Any

import structlog
from kubernetes_asyncio import client as k8s_client

from daalu_automation.nautobot_controller import k8s as k8s_helpers

logger = structlog.get_logger(__name__)

TINK_GROUP = "tinkerbell.org"
BMC_GROUP = "bmc.tinkerbell.org"
TINK_VERSION = "v1alpha1"
BMC_VERSION = "v1alpha1"

# CRD kind → (group, version, plural)
_GVR: dict[str, tuple[str, str, str]] = {
    "Hardware": (TINK_GROUP, TINK_VERSION, "hardware"),
    "Template": (TINK_GROUP, TINK_VERSION, "templates"),
    "Workflow": (TINK_GROUP, TINK_VERSION, "workflows"),
    "Machine": (BMC_GROUP, BMC_VERSION, "machines"),
    "Job": (BMC_GROUP, BMC_VERSION, "jobs"),
    "Task": (BMC_GROUP, BMC_VERSION, "tasks"),
}


class TinkerbellError(RuntimeError):
    """Raised on a CRD apply/read failure."""


class TinkerbellClient:
    """Apply and watch Tinkerbell/Rufio CRs on a target mgmt cluster.

    Use as an async context manager so the kubeconfig is loaded (and the
    underlying aiohttp session closed) deterministically::

        async with TinkerbellClient(kubeconfig=cfg, namespace="tink-system") as tk:
            await tk.apply("Hardware", hardware_cr)
            await tk.create_rufio_power_job(...)
    """

    def __init__(
        self,
        *,
        kubeconfig: dict[str, Any] | None = None,
        namespace: str = "tink-system",
    ) -> None:
        self._kubeconfig = kubeconfig
        self.namespace = namespace
        self._api: k8s_client.ApiClient | None = None

    async def __aenter__(self) -> TinkerbellClient:
        if self._kubeconfig is None:
            await k8s_helpers.load_local_config()
        else:
            await k8s_helpers.load_kubeconfig_dict(self._kubeconfig)
        self._api = k8s_client.ApiClient()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._api is not None:
            await self._api.close()
            self._api = None

    def _co(self) -> k8s_client.CustomObjectsApi:
        if self._api is None:
            raise TinkerbellError("TinkerbellClient used outside its context manager")
        return k8s_client.CustomObjectsApi(self._api)

    # ── generic CR apply / read ──────────────────────────────────────────

    async def apply(self, kind: str, body: dict[str, Any]) -> None:
        """Create-or-patch one CR (idempotent). Namespaced."""
        group, version, plural = _GVR[kind]
        co = self._co()
        name = body["metadata"]["name"]
        ns = body["metadata"].get("namespace", self.namespace)
        try:
            await co.create_namespaced_custom_object(
                group=group, version=version, namespace=ns, plural=plural, body=body
            )
            return
        except k8s_client.exceptions.ApiException as e:
            if e.status != 409:
                raise TinkerbellError(
                    f"create {kind}/{name} failed: {e.status} {e.reason}"
                ) from e
        try:
            await co.patch_namespaced_custom_object(
                group=group,
                version=version,
                namespace=ns,
                plural=plural,
                name=name,
                body=body,
            )
        except k8s_client.exceptions.ApiException as e:
            raise TinkerbellError(
                f"patch {kind}/{name} failed: {e.status} {e.reason}"
            ) from e

    async def get(self, kind: str, name: str) -> dict[str, Any] | None:
        """Read one CR, or ``None`` if absent."""
        group, version, plural = _GVR[kind]
        co = self._co()
        try:
            return await co.get_namespaced_custom_object(
                group=group,
                version=version,
                namespace=self.namespace,
                plural=plural,
                name=name,
            )
        except k8s_client.exceptions.ApiException as e:
            if e.status == 404:
                return None
            raise TinkerbellError(
                f"get {kind}/{name} failed: {e.status} {e.reason}"
            ) from e

    async def delete(self, kind: str, name: str) -> None:
        """Delete one CR. 404 is fine."""
        group, version, plural = _GVR[kind]
        co = self._co()
        try:
            await co.delete_namespaced_custom_object(
                group=group,
                version=version,
                namespace=self.namespace,
                plural=plural,
                name=name,
            )
        except k8s_client.exceptions.ApiException as e:
            if e.status != 404:
                raise TinkerbellError(
                    f"delete {kind}/{name} failed: {e.status} {e.reason}"
                ) from e

    # ── reachability ──────────────────────────────────────────────────────

    async def probe(self) -> None:
        """Lightweight reachability check for the mgmt cluster.

        Lists Workflows (``limit=1``) in the target namespace. Raises
        :class:`TinkerbellError` if the API server is unreachable over the
        tunnel or the Tinkerbell CRDs aren't installed — exactly the
        failures we want to surface at onboarding rather than at execute
        time. Cheap: the list is bounded to one item and discarded.
        """
        group, version, plural = _GVR["Workflow"]
        co = self._co()
        try:
            await co.list_namespaced_custom_object(
                group=group,
                version=version,
                namespace=self.namespace,
                plural=plural,
                limit=1,
            )
        except k8s_client.exceptions.ApiException as e:
            raise TinkerbellError(
                f"tinkerbell unreachable in ns/{self.namespace}: "
                f"{e.status} {e.reason}"
            ) from e

    # ── status helpers ───────────────────────────────────────────────────

    async def workflow_state(self, name: str) -> str | None:
        """Return a Tinkerbell Workflow's ``status.state`` (e.g.
        ``STATE_PENDING`` / ``STATE_RUNNING`` / ``STATE_SUCCESS`` /
        ``STATE_FAILED``), or ``None`` if the Workflow isn't present yet."""
        wf = await self.get("Workflow", name)
        if wf is None:
            return None
        return (wf.get("status") or {}).get("state")

    async def rufio_job_state(self, name: str) -> str | None:
        """Return a Rufio Job's condition summary.

        Rufio reports per-Job conditions; we surface a simple
        ``"Completed"`` / ``"Failed"`` / ``"Running"`` derived from the
        ``status.conditions`` list, or ``None`` if absent.
        """
        job = await self.get("Job", name)
        if job is None:
            return None
        conditions = (job.get("status") or {}).get("conditions") or []
        states = {c.get("type"): c.get("status") for c in conditions}
        if states.get("Failed") == "True":
            return "Failed"
        if states.get("Completed") == "True":
            return "Completed"
        return "Running"

    # ── Rufio power/boot convenience ──────────────────────────────────────

    def build_power_job(
        self,
        *,
        name: str,
        machine_ref: str,
        tasks: list[dict[str, Any]],
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Build a Rufio ``Job`` CR body (power/boot tasks).

        ``tasks`` is the ordered task list, e.g.::

            [{"powerAction": "off"},
             {"oneTimeBootDeviceAction": {"device": ["pxe"], "efiBoot": True}},
             {"powerAction": "on"}]

        mirrors what the ``daalu`` project's tinkerbell_installer creates.
        """
        return {
            "apiVersion": f"{BMC_GROUP}/{BMC_VERSION}",
            "kind": "Job",
            "metadata": {"name": name, "namespace": namespace or self.namespace},
            "spec": {
                "machineRef": {"name": machine_ref, "namespace": namespace or self.namespace},
                "tasks": tasks,
            },
        }
