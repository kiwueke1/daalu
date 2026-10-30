"""Drive the vendored NV-CM ``Deployer`` for a tenant's NV-CM release.

The Deployer-era replacement for :class:`HelmRunner`. A bare ``helm upgrade
--install`` deadlocks on the chart's ``secret-assembler`` pre-install hook
(it waits for secrets only minted later in the same release); the upstream
``Deployer`` pre-creates those secrets and the namespace *before* helm runs.
This module owns invoking it from the async reconcile loop:

* builds the upstream config from the row (:mod:`.deployer_config`),
* applies the two Daalu-specific Helm knobs the upstream config can't express
  (shared GatewayClass, no NodePort gateway patch) via a thin ``Deployer``
  subclass,
* runs the (synchronous, blocking) Deployer in a worker thread under a lock —
  it mutates ``os.environ['KUBECONFIG']`` and the process CWD, so concurrent
  runs would race,
* targets the workload cluster by pointing ``KUBECONFIG`` at the tunnel
  kubeconfig (whose ``current-context`` the Deployer pins to).

Teardown stays helm-native (``helm uninstall``) plus a namespace sweep, since
the Deployer pre-creates secrets/CNPG resources helm doesn't own.

See docs/design/nv-config-manager-integration.md and engineer chapter 64.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

from daalu_automation.config import Settings, get_settings
from daalu_automation.config_manager_controller.deployer_config import (
    GATEWAY_CLASS,
    build_install_config,
    release_name,
)
from daalu_automation.config_manager_controller.values import render_values

logger = structlog.get_logger(__name__)

# (argv, env) -> (returncode, stdout, stderr). Injectable so tests can assert
# on the helm/kubectl argv without a real binary or cluster.
CommandRunner = Callable[
    [list[str], dict[str, str]], Awaitable[tuple[int, str, str]]
]

# The Deployer mutates os.environ['KUBECONFIG'] and the process CWD (it chdir's
# to the chart's project root). Serialise every run so two reconciles can never
# clobber each other's global state. The reconcile loop is sequential today;
# this is belt-and-braces.
_DEPLOYER_LOCK = asyncio.Lock()

# How many trailing Deployer log lines to keep for surfacing on failure.
_LOG_TAIL = 60

@dataclass
class DeployResult:
    """Outcome of a Deployer run (parity with HelmResult's ok/stderr shape)."""

    ok: bool
    summary: str
    error: str = ""
    log_tail: list[str] = field(default_factory=list)


async def _default_runner(
    argv: list[str], env: dict[str, str]
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    out, err = await proc.communicate()
    return (
        proc.returncode or 0,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )


def _daalu_values_overlay(row: Any) -> dict[str, Any]:
    """Daalu multi-tenant, hub-terminated gateway values the upstream Deployer
    can't express — derived from the single source of truth ``render_values``.

    The Deployer's generated values model a self-contained tenant: nested
    ``<tool>.<baseHostname>`` hostnames served by the tenant's own HTTPS gateway
    (its local-dev default forces a self-signed cert). Daalu instead serves every
    tenant tool *through the hub*: the hub terminates TLS at its
    ``*.cmtools.daalu.io`` wildcard and reverse-proxies over the WireGuard tunnel
    (see api/tool_proxy.py). So the tenant gateway is **HTTP-only** (no cert, no
    public DNS to satisfy ACME) and host-routes the **flat** ``<tool>-<slug>.<zone>``
    hosts a single wildcard cert + DNS record cover.

    This overlay forces, on top of the Deployer's values:
    - the flat per-component human hostnames (machine ``svc-*`` hosts stay at the
      chart default ``svc-<tool>.<base>`` — the hub dials those over the tunnel),
    - the HTTP-only listener + ``certificates.enabled=false`` (render_values'
      gateway block), and
    - the shared-GatewayClass / no-NodePort knobs the upstream local-dev path
      needs disabled (it must reference the one cluster-scoped ``envoy-gateway``
      GatewayClass read-only, never create its own or NodePort-patch the shared
      Envoy).
    """
    rv = render_values(row)
    gw = dict(rv.get("gateway", {}))
    gw["createGatewayClass"] = False
    gw["gatewayClassName"] = GATEWAY_CLASS
    gw["className"] = GATEWAY_CLASS
    gw.setdefault("nodePort", {})["enabled"] = False
    overlay: dict[str, Any] = {"gateway": gw}
    # Turn OFF the gateway's interactive OIDC redirect (keep JWT validation +
    # claim-to-header). The Deployer's generated values enable it; on flat
    # sibling tool hosts the oauth2 session cookie (scoped to baseHostname) is
    # never sent back, so the redirect loops ("buffers forever"). The hub
    # authenticates the user and injects a service Bearer the JWT block trusts —
    # see render_values' oidc block + the chart security-policy.yaml gate.
    overlay["oidc"] = {"interactiveRedirect": False}
    # Force the Nautobot auth plugin's JWKS to whichever source this
    # environment's TARGET CLUSTER can actually reach — the in-cluster keycloak
    # Service for operator-cluster installs, the public issuer for customer
    # clusters over WireGuard (which run no Keycloak). render_values owns that
    # choice; see its target_cluster_tunnel_id gate. The Deployer picks its own
    # value, so both keys are overridden here rather than left to it.
    _oidc_rv = rv.get("oidc", {})
    if _oidc_rv.get("internalIssuerUrl"):
        overlay["oidc"]["internalIssuerUrl"] = _oidc_rv["internalIssuerUrl"]
    # jwksUri wins over internalIssuerUrl in the chart helpers.
    if _oidc_rv.get("jwksUri"):
        overlay["oidc"]["jwksUri"] = _oidc_rv["jwksUri"]
    # Flat human hostname per enabled component (render_values only emits a
    # ``gateway`` block for the components that are turned on).
    for comp in (
        "nautobot",
        "renderService",
        "configStore",
        "temporal",
        "networkZtp",
        "networkDhcp",
    ):
        c = rv.get(comp)
        if isinstance(c, dict) and isinstance(c.get("gateway"), dict):
            # Carry the flat gateway hostname, any rbac block (nautobot's
            # superuserGroups — maps the JWT roles claim to Nautobot superuser)
            # and any admin block (the operator-chosen break-glass username;
            # the Deployer hardcodes "admin", so this must override it).
            overlay[comp] = {
                k: c[k] for k in ("gateway", "rbac", "admin") if k in c
            }
    # Top-level Temporal workflow RBAC. The Deployer computes this via the
    # vendored installer's get_known_workflows(), whose values-rbac-open.yaml
    # lookup misses in the controller image and silently yields
    # ``workflows: []`` — no execute_roles anywhere, every workflow greyed
    # out in the NV-CM UI. render_values reads the file from the vendored
    # chart itself; carry its answer over the Deployer's empty one.
    if isinstance(rv.get("rbac"), dict):
        overlay["rbac"] = rv["rbac"]
    return overlay


def _fullname_overlay(config: Any) -> dict[str, Any]:
    """Force the chart's ``fullname`` to the (short) release name.

    The chart derives most object names from ``fullname`` =
    ``<release>-nv-config-manager`` (33 chars for our ``cm-<hex[:12]>`` release
    names). A few component templates then append long suffixes WITHOUT
    re-truncating to 63, e.g. the temporal headless Service
    ``<fullname>-temporal-frontend-service-headless`` → 68 chars, which the API
    server rejects (``metadata.name: must be no more than 63 characters``) and
    fails the whole ``helm install`` — so no tenant can reach ``active``.

    Setting ``fullnameOverride`` to just the release name drops the
    ``-nv-config-manager`` segment (18 chars), bringing the longest generated
    name to ~50 chars. ``fullnameOverride`` also feeds component pod-selector
    labels (which are immutable), so this must be set from the FIRST install —
    flipping it on an existing release fails the immutable-selector check.
    """
    return {"fullnameOverride": config.cluster.release_name}


def _strip_empty_image_pull_secrets(values: dict[str, Any]) -> None:
    """Drop empty ``global.imagePullSecrets`` entries (in place).

    When no pull secret is configured, helm_values emits
    ``global.imagePullSecrets: [""]`` (the empty pull-secret name). The chart
    renders that as ``imagePullSecrets: [{}]`` on every pod. helm's first
    *install* applies it fine, but every later *upgrade* — and the controller
    re-drives each tenant (incl. ``active`` rows) every 30 s — computes a
    strategic-merge patch over that list, whose merge key is ``name``; the
    empty ``{}`` entry has no ``name`` →
    ``failed to create patch: map[] does not contain declared merge key: name``.
    The upgrade fails, the row flips ``active→error→provisioning``, and helm
    accumulates failed revisions + ``pending-upgrade`` locks. Filtering the
    empty entries to ``[]`` makes the chart emit no ``imagePullSecrets`` block,
    so upgrades merge cleanly. A real configured secret is preserved.
    """
    g = values.get("global")
    if isinstance(g, dict) and isinstance(g.get("imagePullSecrets"), list):
        g["imagePullSecrets"] = [s for s in g["imagePullSecrets"] if s]


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *overlay* onto *base* (Helm-like override semantics)."""
    result = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _build_daalu_deployer(
    config: Any, options: Any, callback: Any, values_overlay: dict[str, Any]
) -> Any:
    """Construct a Deployer subclass with Daalu's gateway behaviour.

    ``values_overlay`` is the flat-hostname / hub-terminated gateway overlay
    (:func:`_daalu_values_overlay`) merged onto the Deployer's generated values.

    Built lazily (import inside) because the vendored package is StrEnum-based
    (Python 3.11+); importing at module top would break 3.10 dev tooling that
    only touches the still-default HelmRunner path.
    """
    from nv_config_manager_installer.deployer import Deployer

    class _DaaluDeployer(Deployer):
        # ── RWO loader co-location ─────────────────────────────────────────
        # The Deployer uploads PVC content (jobs, templates) via a short-lived
        # "loader" pod that mounts the target PVC read-write. Those PVCs are
        # ReadWriteOnce on Ceph RBD. On a FRESH install this is fine — the
        # loader runs before helm, so nothing else holds the PVC. But the
        # controller re-drives a tenant on every 30 s reconcile (incl. `active`
        # rows), and the upstream content-hash skip is defeated by a
        # non-deterministic gzip mtime, so `setup-jobs-pvc` re-runs every time.
        # By then Nautobot/render already hold the RWO PVC on one node, and if
        # the loader is scheduled to the *other* node it wedges with a
        # cross-node "Multi-Attach error" → the step times out → the provision
        # fails → the row flips to `error` and oscillates. Pin the loader to the
        # node that already holds the PVC (RWO ⇒ all consumers are co-located,
        # so any consumer's node is correct). Fresh install: no consumer yet →
        # leave the selector empty so the loader schedules freely.
        def _pvc_consumer_node(self, pvc_name: str) -> str | None:
            ns = self.config.cluster.namespace
            try:
                pods = self._k8s.v1.list_namespaced_pod(ns).items  # type: ignore[union-attr]
            except Exception:  # pragma: no cover - best-effort, never fatal
                return None
            for pod in pods:
                # Ignore the loader pods themselves — we want the workload node.
                name = getattr(pod.metadata, "name", "") or ""
                if name.endswith("-loader") or "jobs-loader" in name or "tpl-loader" in name:
                    continue
                for vol in (pod.spec.volumes or []):
                    pvc = getattr(vol, "persistent_volume_claim", None)
                    if pvc and pvc.claim_name == pvc_name and pod.spec.node_name:
                        return pod.spec.node_name
            return None

        def _pin_loader(self, pvc_name: str, cfg: Any) -> None:
            node = self._pvc_consumer_node(pvc_name)
            if node:
                cfg.node_selector = {"kubernetes.io/hostname": node}
                self.callback.on_log(
                    f"Daalu: pinning {pvc_name} loader to node {node} "
                    "(RWO PVC already attached there)"
                )

        # Pre-create the gateway OIDC redirect's client secret alongside the
        # Deployer's other pre-created secrets. The chart's gateway
        # SecurityPolicy OIDC block references a `oidc-client-secret` Secret
        # (key `client-secret`) for Envoy's confidential authorization-code
        # flow; if it's absent the human `<host>` URLs 500. We create it from
        # the configured UI client secret so interactive browser login works
        # and survives a teardown + reinstall. No-op when unset (machine svc-*
        # JWT path is unaffected).
        def _create_secrets(self) -> None:  # type: ignore[override]
            super()._create_secrets()
            secret = getattr(self.config.sso, "client_secret", "") or ""
            if secret and self._k8s is not None:
                self._k8s.apply_secret(
                    "oidc-client-secret",
                    self.config.cluster.namespace,
                    {"client-secret": secret},
                )
                self.callback.on_log(
                    "Daalu: pre-created oidc-client-secret (gateway OIDC redirect)"
                )

        def _setup_jobs_pvc(self) -> None:  # type: ignore[override]
            self._pin_loader(
                "nautobot-custom-jobs", self.config.content.jobs_config
            )
            super()._setup_jobs_pvc()

        def _setup_templates_pvc(self) -> None:  # type: ignore[override]
            self._pin_loader(
                "render-service-template-plugins",
                self.config.content.template_plugins_config,
            )
            super()._setup_templates_pvc()

        # Layer the shared-GatewayClass / no-NodePort overlay + a short
        # fullnameOverride onto the values the upstream step already wrote, so
        # helm install picks them up.
        def _generate_values(self) -> None:  # type: ignore[override]
            super()._generate_values()
            values_file = getattr(self, "_values_file", None)
            if not values_file:
                return
            path = Path(values_file)
            current = yaml.safe_load(path.read_text()) or {}
            overlay = _deep_merge(values_overlay, _fullname_overlay(self.config))
            merged = _deep_merge(current, overlay)
            _strip_empty_image_pull_secrets(merged)
            path.write_text(yaml.safe_dump(merged, default_flow_style=False))
            gw = merged.get("gateway", {})
            self.callback.on_log(
                "Daalu overlay: createGatewayClass=false, nodePort disabled, "
                f"className={GATEWAY_CLASS}, HTTP-only gateway "
                f"(listeners={[ll.get('protocol') for ll in gw.get('listeners', [])]}, "
                f"cert.enabled={gw.get('certificates', {}).get('enabled')}), "
                f"flat hostnames, fullnameOverride={merged.get('fullnameOverride')}"
            )

        # The shared Envoy Gateway is fronted by traefik — never patch it with
        # local-dev host ports (the upstream step would otherwise run because
        # we use LoadBalancer provider NONE).
        def _patch_gateway(self) -> None:  # type: ignore[override]
            self._skip_step(
                "patch-gateway",
                "Daalu: shared envoy-gateway fronted by traefik, no NodePort patch",
            )

    return _DaaluDeployer(config, options, callback)


class _CaptureCallback:
    """DeployCallback that forwards to structlog and keeps a log tail."""

    def __init__(self, *, tenant_id: str, env: str = "") -> None:
        self._tenant_id = tenant_id
        # A tenant can run several environments, each with its own Deployer
        # pass. Without the environment on the line, a failure log says only
        # which *customer* is broken — not which of their stacks.
        self._env = env
        self.log_tail: list[str] = []
        self.failed_step: str = ""
        self.failed_error: str = ""

    def on_step_update(self, step: Any) -> None:
        status = getattr(step, "status", "")
        # StepStatus is a StrEnum; compare by value to avoid importing it here.
        if str(status) == "failed":
            self.failed_step = getattr(step, "label", getattr(step, "id", ""))
            self.failed_error = getattr(step, "error", "")
            logger.warning(
                "config_manager_controller.deployer_step_failed",
                tenant_id=self._tenant_id,
                env=self._env,
                step=self.failed_step,
                error=self.failed_error[:500],
            )

    def on_log(self, message: str) -> None:
        self.log_tail.append(message)
        if len(self.log_tail) > _LOG_TAIL:
            del self.log_tail[: -_LOG_TAIL]

    def on_complete(self, success: bool, endpoints: list[str]) -> None:
        logger.info(
            "config_manager_controller.deployer_complete",
            tenant_id=self._tenant_id,
            env=self._env,
            success=success,
        )


class DeployerRunner:
    """Invoke the vendored NV-CM Deployer; ``helm uninstall`` for teardown."""

    def __init__(
        self,
        *,
        chart_dir: str,
        settings: Settings | None = None,
        runner: CommandRunner | None = None,
        helm_bin: str = "helm",
        kubectl_bin: str = "kubectl",
        helm_timeout: str = "15m",
    ) -> None:
        self._chart_dir = chart_dir
        self._settings = settings or get_settings()
        self._runner = runner or _default_runner
        self._helm = helm_bin
        self._kubectl = kubectl_bin
        self._helm_timeout = helm_timeout

    async def upgrade_install(
        self,
        *,
        row: Any,
        kubeconfig: dict[str, Any] | None = None,
    ) -> DeployResult:
        """Provision/upgrade the tenant's NV-CM release via the Deployer."""
        config = build_install_config(row, settings=self._settings)
        tenant_id = str(getattr(row, "tenant_id", ""))
        env_label = str(getattr(row, "slug", "") or getattr(row, "namespace", ""))
        values_overlay = _daalu_values_overlay(row)

        def _run() -> DeployResult:
            from nv_config_manager_installer.deployer import DeployOptions

            options = DeployOptions(
                chart_dir=self._chart_dir,
                build_images=False,
                load_kind=False,
                install_envoy_gateway=False,
                install_cert_manager=False,
                install_cnpg_operator=False,
                helm_timeout=self._helm_timeout,
                recreate_secrets=False,
                run_tests=False,
                dry_run=False,
            )
            callback = _CaptureCallback(tenant_id=tenant_id, env=env_label)
            with _KubeconfigEnv(kubeconfig):
                deployer = _build_daalu_deployer(
                    config, options, callback, values_overlay
                )
                ok = deployer.run()
            if ok:
                return DeployResult(
                    ok=True, summary="deployer succeeded", log_tail=callback.log_tail
                )
            err = callback.failed_error or "deployer failed (see log_tail)"
            if callback.failed_step:
                err = f"[{callback.failed_step}] {err}"
            return DeployResult(
                ok=False,
                summary="deployer failed",
                error=err,
                log_tail=callback.log_tail,
            )

        async with _DEPLOYER_LOCK:
            return await asyncio.to_thread(_run)

    async def release_status(
        self,
        *,
        row: Any,
        kubeconfig: dict[str, Any] | None = None,
    ) -> str:
        """Return the helm release status (e.g. ``deployed``), or ``""``.

        Used to short-circuit the reconcile of an already-``active`` tenant: a
        deployed release needs no re-`helm upgrade` (which would otherwise mint
        a new revision every tick — churn + `pending-upgrade` lock risk). Any
        uncertainty (release absent, helm error, unparseable) returns ``""`` so
        the caller falls back to a full provision (safe default).
        """
        import json as _json

        release = release_name(row)
        with _TempKubeconfig(kubeconfig) as kubeconfig_path:
            env = dict(os.environ)
            if kubeconfig_path:
                env["KUBECONFIG"] = kubeconfig_path
            rc, out, err = await self._runner(
                [self._helm, "status", release, "-n", row.namespace, "-o", "json"],
                env,
            )
        if rc != 0:
            return ""
        try:
            return str((_json.loads(out).get("info") or {}).get("status") or "")
        except (ValueError, AttributeError):
            return ""

    async def clear_pending_release(
        self,
        *,
        row: Any,
        kubeconfig: dict[str, Any] | None = None,
    ) -> str | None:
        """Unwedge a release stuck mid-operation; returns what was cleared.

        Helm marks a release ``pending-install``/``pending-upgrade`` for the
        duration of an operation and only writes a terminal status when it
        finishes. If the controller dies in between — an image roll, an ArgoCD
        sync, an OOM — nothing ever clears it, and every later
        ``helm upgrade --install`` fails in under a second with "another
        operation is in progress". That is a permanent wedge: the reconciler
        retries forever and can never win. Observed in prod when a CD rollout
        restarted the controller mid-install.

        Recovery depends on whether there is a good revision to fall back to:
        ``pending-upgrade`` rolls back to the last deployed one, while a
        ``pending-install`` has none — its release record is deleted so the
        next install starts clean. That is safe because the objects helm
        created carry ``meta.helm.sh/release-name``/``-namespace``, so the
        reinstall *adopts* the existing resources instead of duplicating them.
        """
        status = await self.release_status(row=row, kubeconfig=kubeconfig)
        if not status.startswith("pending-"):
            return None

        release = release_name(row)
        logger.warning(
            "config_manager_controller.release_wedged",
            tenant_id=str(getattr(row, "tenant_id", "")),
            release=release,
            status=status,
        )
        with _TempKubeconfig(kubeconfig) as kubeconfig_path:
            env = dict(os.environ)
            if kubeconfig_path:
                env["KUBECONFIG"] = kubeconfig_path

            if status == "pending-install":
                # No prior revision to roll back to — drop the stuck record.
                rc, _out, err = await self._runner(
                    [
                        self._kubectl, "delete", "secret",
                        "-n", row.namespace,
                        "-l", f"owner=helm,name={release},status=pending-install",
                        "--ignore-not-found",
                    ],
                    env,
                )
            else:
                rc, _out, err = await self._runner(
                    [self._helm, "rollback", release, "-n", row.namespace, "--wait",
                     "--timeout", self._helm_timeout],
                    env,
                )
        if rc != 0:
            logger.warning(
                "config_manager_controller.release_unwedge_failed",
                release=release, status=status, stderr=err[:400],
            )
            return None
        logger.info(
            "config_manager_controller.release_unwedged",
            release=release, cleared=status,
        )
        return status

    async def uninstall(
        self,
        *,
        row: Any,
        kubeconfig: dict[str, Any] | None = None,
    ) -> DeployResult:
        """Tear down the release: ``helm uninstall`` + namespace sweep.

        The Deployer pre-creates secrets and CNPG resources helm doesn't own,
        so after the graceful helm uninstall (which honours pre-delete hooks /
        finalizers) we delete the per-tenant namespace to sweep the leftovers.
        """
        release = release_name(row)
        namespace = row.namespace
        with _TempKubeconfig(kubeconfig) as kubeconfig_path:
            env = dict(os.environ)
            if kubeconfig_path:
                env["KUBECONFIG"] = kubeconfig_path

            helm_argv = [
                self._helm, "uninstall", release, "-n", namespace,
                "--ignore-not-found", "--wait", "--timeout", self._helm_timeout,
            ]
            rc, out, err = await self._runner(helm_argv, env)
            if rc != 0:
                logger.warning(
                    "config_manager_controller.deployer_uninstall_failed",
                    tenant_id=str(getattr(row, "tenant_id", "")),
                    stderr=err[:500],
                )
                return DeployResult(
                    ok=False, summary="helm uninstall failed", error=err
                )

            ns_argv = [
                self._kubectl, "delete", "namespace", namespace,
                "--ignore-not-found", "--wait=false",
            ]
            ns_rc, ns_out, ns_err = await self._runner(ns_argv, env)
            if ns_rc != 0:
                # Non-fatal: the release is gone; a lingering namespace is a
                # sweep concern, not a teardown failure.
                logger.warning(
                    "config_manager_controller.deployer_namespace_sweep_failed",
                    tenant_id=str(getattr(row, "tenant_id", "")),
                    stderr=ns_err[:500],
                )
        return DeployResult(ok=True, summary="uninstalled")


class _TempKubeconfig:
    """Write a kubeconfig dict to a temp file, deleted on exit."""

    def __init__(self, kubeconfig: dict[str, Any] | None) -> None:
        self._kubeconfig = kubeconfig
        self._path: str | None = None

    def __enter__(self) -> str | None:
        if self._kubeconfig is None:
            return None
        kf = tempfile.NamedTemporaryFile(
            mode="w", suffix=".kubeconfig", delete=False
        )
        yaml.safe_dump(self._kubeconfig, kf)
        kf.flush()
        kf.close()
        self._path = kf.name
        return self._path

    def __exit__(self, *exc: object) -> None:
        if self._path:
            try:
                os.unlink(self._path)
            except OSError:
                pass


class _KubeconfigEnv:
    """Sync context manager: make a kubeconfig the active target for the run.

    The Deployer reaches the cluster two ways, and they resolve the kubeconfig
    *differently*:

    * ``kubectl``/``helm`` subprocesses honour the ``KUBECONFIG`` env var.
    * the embedded Python ``kubernetes`` client's ``load_kube_config()`` does
      **not** honour ``KUBECONFIG`` — it only reads its default location,
      ``~/.kube/config``. In the controller pod there is no ``~/.kube/config``,
      so setting the env var alone makes ``K8sClient()`` fail at the prereqs
      step with "Invalid kube-config file. No configuration found."

    So we satisfy both: set ``KUBECONFIG`` (for the subprocesses) *and* write the
    kubeconfig to ``~/.kube/config`` (for the Python client), restoring/removing
    both on exit. For the local/in-cluster case (kubeconfig is None) we leave
    the environment alone — the Deployer falls back to in-cluster config.
    """

    def __init__(self, kubeconfig: dict[str, Any] | None) -> None:
        self._kubeconfig = kubeconfig
        self._tmp = _TempKubeconfig(kubeconfig)
        self._prev: str | None = None
        self._had_prev = False
        # ~/.kube/config backup/restore state.
        self._default_path = Path.home() / ".kube" / "config"
        self._wrote_default = False
        self._default_backup: bytes | None = None
        self._default_existed = False

    def __enter__(self) -> None:
        path = self._tmp.__enter__()
        if path is None:
            return
        self._had_prev = "KUBECONFIG" in os.environ
        self._prev = os.environ.get("KUBECONFIG")
        os.environ["KUBECONFIG"] = path

        # Also write to the Python kubernetes client's default location, which
        # ignores KUBECONFIG. Back up any pre-existing file so we can restore it.
        self._default_path.parent.mkdir(parents=True, exist_ok=True)
        if self._default_path.exists():
            self._default_existed = True
            self._default_backup = self._default_path.read_bytes()
        with open(self._default_path, "w") as f:
            yaml.safe_dump(self._kubeconfig, f)
        self._wrote_default = True

    def __exit__(self, *exc: object) -> None:
        if self._wrote_default:
            try:
                if self._default_existed and self._default_backup is not None:
                    self._default_path.write_bytes(self._default_backup)
                else:
                    self._default_path.unlink(missing_ok=True)
            except OSError:
                pass
        if self._tmp._path is not None:
            if self._had_prev and self._prev is not None:
                os.environ["KUBECONFIG"] = self._prev
            else:
                os.environ.pop("KUBECONFIG", None)
        self._tmp.__exit__(*exc)
