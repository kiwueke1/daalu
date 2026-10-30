"""K8s helpers for materializing a workspace pod.

We use the official ``kubernetes_asyncio`` client in-cluster — the
serviceAccountName on the workspace-controller pod carries the RBAC
that lets it create Deployments + PVCs in the ``daalu-workspaces``
namespace. Outside the cluster (tests, dev) we fall back to the
``KUBECONFIG`` env path.

Resource naming is deterministic: ``ws-<workspace_id_short>`` for
both the Deployment and the PVC. That makes the
workspace ↔ K8s relationship trivially observable
("kubectl get all -l daalu.io/workspace-id=…").
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlsplit

import structlog
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config

from daalu_automation.config import get_settings
from daalu_automation.core import model_catalog

logger = structlog.get_logger(__name__)

WORKSPACE_NAMESPACE = "daalu-workspaces"


_PROFILES: dict[str, dict[str, Any]] = {
    # CPU/memory: (request, limit). PVC: storage size.
    "small": {
        "cpu_req": "500m", "cpu_lim": "2",
        "mem_req": "1Gi", "mem_lim": "4Gi",
        "storage": "5Gi",
    },
    "medium": {
        "cpu_req": "1", "cpu_lim": "4",
        "mem_req": "4Gi", "mem_lim": "8Gi",
        "storage": "20Gi",
    },
    "large": {
        "cpu_req": "2", "cpu_lim": "8",
        "mem_req": "8Gi", "mem_lim": "16Gi",
        "storage": "50Gi",
    },
}


async def load_kube_config() -> None:
    """Choose in-cluster vs kubeconfig once at startup.

    ``load_incluster_config()`` is *synchronous* in kubernetes_asyncio
    (it just reads the mounted SA token/CA) — it returns None, so it must
    NOT be awaited. Only ``load_kube_config()`` is a coroutine. Awaiting
    the in-cluster one raises ``TypeError: object NoneType can't be used
    in 'await'`` the moment the controller runs in-cluster, which breaks
    the startup secret-mirror and every apply_workspace call.
    """
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        await k8s_config.load_kube_config()


def resource_names(workspace_id: str) -> tuple[str, str]:
    """Return (pod_name_prefix, pvc_name) for a workspace.

    The Deployment name is also the pod-name prefix; the actual pod
    name has a hash suffix appended by the ReplicaSet.
    """
    short = workspace_id[:8]
    return f"ws-{short}", f"ws-{short}-pvc"


def git_secret_name(workspace_id: str) -> str:
    """Per-workspace Secret holding the git credential-helper file.

    Separate from the mirrored ``daalu-automation-secrets`` so the PAT's
    blast radius is exactly one workspace: deleting the workspace (or
    clearing its token) deletes this Secret and nothing else.
    """
    short = workspace_id[:8]
    return f"ws-{short}-git"


# Where the git credential-helper file is mounted in the workspace + init
# containers. Outside /workspace so it never lands in the user's repo tree,
# and outside the token-minter's /var/run/daalu/tokens emptyDir.
_GIT_CRED_DIR = "/var/run/daalu/git"
_GIT_CRED_FILE = f"{_GIT_CRED_DIR}/credentials"


def _git_credentials_line(git_repo_url: str, git_token: str) -> str:
    """Build one ``store``-helper line: ``https://user:token@host[:port]``.

    The username git presents alongside the PAT is provider-specific:

    * GitHub (github.com / GHE)  → ``x-access-token`` (PAT-as-password; any
      username actually works, but this is the documented one).
    * GitLab (gitlab.* hosts)    → ``oauth2`` (PAT-as-password).
    * anything else (self-hosted Gitea/Bitbucket/…) → ``git``, which every
      server we've met accepts with a token-as-password.

    Match is by protocol+host (we leave ``credential.useHttpPath`` off), so a
    single host line covers every repo on that host. The remote URL stays
    clean (no embedded token), so ``git remote -v`` in the user's terminal
    never leaks the PAT and the token lives only in this in-memory file.
    """
    parts = urlsplit(git_repo_url.strip())
    host = parts.hostname or ""
    scheme = parts.scheme or "https"
    hostport = host if parts.port is None else f"{host}:{parts.port}"
    hl = host.lower()
    if hl == "github.com" or hl.endswith(".github.com") or "github" in hl:
        user = "x-access-token"
    elif "gitlab" in hl:
        user = "oauth2"
    else:
        user = "git"
    return f"{scheme}://{user}:{git_token}@{hostport}"


def _git_cred_env() -> list[dict[str, str]]:
    """Env that points every ``git`` invocation at the mounted helper file.

    Uses git's ``GIT_CONFIG_*`` env-config mechanism instead of writing a
    ``.gitconfig`` — so the same vars work in the throwaway ``alpine/git``
    init container *and* the long-lived code-server container without either
    one sharing a HOME. The token itself is never in env (only the file
    path), so ``printenv`` in the user's terminal can't reveal it.
    """
    return [
        {"name": "GIT_CONFIG_COUNT", "value": "1"},
        {"name": "GIT_CONFIG_KEY_0", "value": "credential.helper"},
        {"name": "GIT_CONFIG_VALUE_0", "value": f"store --file={_GIT_CRED_FILE}"},
    ]


def _git_cred_volume_mount() -> dict[str, str]:
    return {"name": "git-creds", "mountPath": _GIT_CRED_DIR, "readOnly": True}


def _fqdn_gateway_url() -> str:
    """Cross-namespace URL of the inference-gateway.

    Workspace pods live in ``daalu-workspaces`` but the gateway runs
    in the hub release namespace (typically ``daalu-automation``).
    The short DNS name only resolves within a namespace, so we
    promote the configured URL to fully-qualified.
    """
    settings = get_settings()
    raw = settings.daalu_hosted_gateway_url or settings.llm_local_base_url or ""
    # If the URL already contains a dot (fully-qualified or external),
    # trust it. Otherwise assume it's a short Service name in the hub
    # release namespace and qualify it.
    hub_ns = os.environ.get("DAALU_HUB_NAMESPACE", "daalu-automation")
    if not raw:
        return ""
    # Parse "http://name[:port]" — split off scheme and port if present.
    m = re.match(r"^(https?://)([^/:?]+)(:\d+)?(.*)$", raw)
    if not m:
        return raw
    scheme, host, port, rest = m.groups()
    if "." in host:
        return raw
    qualified = f"{scheme}{host}.{hub_ns}.svc.cluster.local{port or ''}{rest or ''}"
    return qualified


def _container_env(
    *,
    tenant_id: str,
    user_id: str,
    workspace_id: str,
    gateway_url: str,
    model: str | None,
    service_token_secret_key: str,
) -> list[dict[str, str]]:
    """Env vars baked into the code-server container.

    The Daalu Assistant extension and the ``daalu chat`` CLI read these to
    know where the inference-gateway is, which model to request, and how
    to mint short-lived tokens. Service-token signing happens inside a
    sidecar (not the code-server container) so the signing key stays out
    of the user-shell environment.

    ``DAALU_MODEL`` is the vLLM served-model-name resolved from the
    workspace's catalog id; the gateway routes on it. We resolve through
    the catalog (not the raw id) so a stale/null id still hands the pod a
    valid served name (the catalog default).
    """
    return [
        {"name": "DAALU_TENANT_ID", "value": tenant_id},
        {"name": "DAALU_USER_ID", "value": user_id},
        {"name": "DAALU_WORKSPACE_ID", "value": workspace_id},
        {"name": "DAALU_GATEWAY_URL", "value": gateway_url},
        {"name": "DAALU_MODEL", "value": model_catalog.served_name(model or "")},
        # Service-token signing key is *not* exposed to the user
        # container. The token-minter sidecar reads it from the same
        # K8s Secret daalu-api uses; the user container just consumes
        # the minted token from a shared emptyDir volume.
    ]


def build_pvc_spec(workspace_id: str, profile: str) -> dict[str, Any]:
    p = _PROFILES.get(profile, _PROFILES["small"])
    _, pvc_name = resource_names(workspace_id)
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": pvc_name,
            "namespace": WORKSPACE_NAMESPACE,
            "labels": {
                "daalu.io/workspace-id": workspace_id,
            },
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": p["storage"]}},
        },
    }


def build_git_secret_spec(
    workspace_id: str, git_repo_url: str, git_token: str
) -> dict[str, Any]:
    """Secret whose single ``credentials`` key is the git-helper file.

    ``stringData`` so we hand K8s the plaintext line and it does the
    base64; the value never appears in our logs or the Deployment manifest
    (only the init/code-server containers mount it). Labeled with the
    workspace id so it's swept on destroy.
    """
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {
            "name": git_secret_name(workspace_id),
            "namespace": WORKSPACE_NAMESPACE,
            "labels": {
                "daalu.io/workspace-id": workspace_id,
                "app": "daalu-workspace",
            },
        },
        "stringData": {
            "credentials": _git_credentials_line(git_repo_url, git_token) + "\n",
        },
    }


def build_deployment_spec(
    *,
    workspace_id: str,
    tenant_id: str,
    user_id: str,
    profile: str,
    model: str | None,
    git_repo_url: str | None,
    git_branch: str,
    git_authenticated: bool = False,
) -> dict[str, Any]:
    """Build the workspace Deployment manifest.

    Two containers: ``code-server`` (the IDE) and ``token-minter``
    (mints service-token JWTs on a 30-second loop into a shared
    emptyDir for the assistant extension to consume).
    """
    settings = get_settings()
    p = _PROFILES.get(profile, _PROFILES["small"])
    dep_name, pvc_name = resource_names(workspace_id)
    spec: dict[str, Any] = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": dep_name,
            "namespace": WORKSPACE_NAMESPACE,
            "labels": {
                "daalu.io/workspace-id": workspace_id,
                "daalu.io/workspace-tenant": tenant_id,
                "app": "daalu-workspace",
            },
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"daalu.io/workspace-id": workspace_id}},
            "template": {
                "metadata": {
                    "labels": {
                        "daalu.io/workspace-id": workspace_id,
                        "daalu.io/workspace-tenant": tenant_id,
                        "app": "daalu-workspace",
                    },
                },
                "spec": {
                    "automountServiceAccountToken": False,
                    # Image is on GHCR (private registry). The
                    # workspaces namespace inherits the same `ghcr`
                    # pull secret that the hub uses — the controller
                    # mirrors it at startup (TODO: implement the
                    # mirror in workspace_controller.app::_startup;
                    # for MVP the operator runs the kubectl copy
                    # step manually).
                    "imagePullSecrets": [{"name": "ghcr"}],
                    "volumes": [
                        {"name": "workspace", "persistentVolumeClaim": {"claimName": pvc_name}},
                        {"name": "tokens", "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"}},
                    ],
                    "initContainers": [
                        {
                            "name": "bootstrap",
                            "image": "alpine/git:latest",
                            "command": ["/bin/sh", "-c"],
                            "args": [_bootstrap_script(git_repo_url, git_branch)],
                            "volumeMounts": [
                                {"name": "workspace", "mountPath": "/workspace"},
                            ],
                        },
                    ],
                    "containers": [
                        {
                            "name": "code-server",
                            # Custom image with the Daalu Assistant
                            # VS Code extension pre-installed. Built
                            # by .github/workflows/build-code-server-image.yml
                            # from extensions/daalu-assistant + the
                            # Dockerfile under deploy/docker/.
                            "image": "ghcr.io/kiwueke1/code-server-with-daalu:latest",
                            "args": [
                                "--bind-addr", "0.0.0.0:8080",
                                "--auth", "none",
                                "--disable-telemetry",
                                "/workspace",
                            ],
                            "ports": [{"containerPort": 8080, "name": "http"}],
                            "env": _container_env(
                                tenant_id=tenant_id,
                                user_id=user_id,
                                workspace_id=workspace_id,
                                # Workspace pods sit in daalu-workspaces;
                                # daalu-api's hub-namespace URL doesn't
                                # resolve there. Promote to FQDN.
                                gateway_url=_fqdn_gateway_url(),
                                model=model,
                                service_token_secret_key="",
                            ),
                            "volumeMounts": [
                                {"name": "workspace", "mountPath": "/workspace"},
                                {"name": "tokens", "mountPath": "/var/run/daalu/tokens"},
                            ],
                            "resources": {
                                "requests": {"cpu": p["cpu_req"], "memory": p["mem_req"]},
                                "limits":   {"cpu": p["cpu_lim"], "memory": p["mem_lim"]},
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/healthz", "port": 8080},
                                "initialDelaySeconds": 5,
                                "periodSeconds": 5,
                            },
                        },
                        {
                            "name": "token-minter",
                            # Re-uses the backend image — the ``daalu
                            # workspace-mint-token`` subcommand prints
                            # a short-lived (purpose='coding') service
                            # token to stdout, which the shell loop
                            # writes atomically into the shared
                            # emptyDir for the assistant extension to
                            # consume.
                            "image": "ghcr.io/kiwueke1/daalu-automation:latest",
                            "command": ["/bin/sh", "-c"],
                            "args": [
                                # Atomic-rename so a torn read can never
                                # see a half-written JWT. `mv` is atomic
                                # within the same filesystem.
                                "while true; do "
                                "  daalu workspace-mint-token "
                                "    --ttl-seconds 120 "
                                "    > /var/run/daalu/tokens/service.jwt.new "
                                "  && mv /var/run/daalu/tokens/service.jwt.new "
                                "        /var/run/daalu/tokens/service.jwt; "
                                "  sleep 30; "
                                "done"
                            ],
                            "env": [
                                {"name": "DAALU_TENANT_ID", "value": tenant_id},
                                {"name": "DAALU_USER_ID", "value": user_id},
                            ],
                            "envFrom": [
                                # SERVICE_TOKEN_SECRET_KEY lives in the
                                # mirrored Secret. mint fails fast if
                                # it's missing — by design, since silent
                                # no-tokens would CrashLoop without a
                                # clear signal.
                                {"secretRef": {"name": "daalu-automation-secrets"}},
                            ],
                            "volumeMounts": [
                                {"name": "tokens", "mountPath": "/var/run/daalu/tokens"},
                            ],
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "32Mi"},
                                "limits":   {"cpu": "100m", "memory": "128Mi"},
                            },
                        },
                    ],
                },
            },
        },
    }

    # Private-repo auth: when the workspace carries a PAT, project the
    # per-workspace git Secret as an in-memory file and point git's
    # credential helper at it (via GIT_CONFIG_* env) in *both* the init
    # container (so the first-boot clone authenticates) and the code-server
    # container (so the user's terminal git pull/push keep working). The
    # token never enters the Deployment manifest — only a secret *reference*
    # does — so ``kubectl get deploy -o yaml`` can't reveal it.
    if git_authenticated:
        pod_spec = spec["spec"]["template"]["spec"]
        pod_spec["volumes"].append(
            {
                "name": "git-creds",
                "secret": {
                    "secretName": git_secret_name(workspace_id),
                    "defaultMode": 0o400,
                },
            }
        )
        init_c = pod_spec["initContainers"][0]
        init_c["volumeMounts"].append(_git_cred_volume_mount())
        init_c["env"] = _git_cred_env()
        code_c = pod_spec["containers"][0]
        code_c["volumeMounts"].append(_git_cred_volume_mount())
        code_c["env"] = list(code_c["env"]) + _git_cred_env()

    return spec


def build_service_spec(workspace_id: str) -> dict[str, Any]:
    """Build the per-workspace Service.

    The IDE reverse proxy in daalu-api resolves the pod by Service DNS
    (``ws-<short>.daalu-workspaces.svc.cluster.local:8080`` — see
    ``api.ide_proxy._pod_url``), so each workspace needs a Service whose
    name matches the Deployment name and that selects its pod. Without
    it the proxy gets NXDOMAIN and returns 502.
    """
    dep_name, _ = resource_names(workspace_id)
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": dep_name,
            "namespace": WORKSPACE_NAMESPACE,
            "labels": {
                "daalu.io/workspace-id": workspace_id,
                "app": "daalu-workspace",
            },
        },
        "spec": {
            "selector": {"daalu.io/workspace-id": workspace_id},
            "ports": [
                {"name": "http", "port": 8080, "targetPort": 8080},
            ],
        },
    }


def _bootstrap_script(git_repo_url: str | None, git_branch: str) -> str:
    """Init-container shell that seeds /workspace on first boot.

    Idempotent — if a clone already exists the script no-ops. Without
    this the user sees an empty editor on every workspace recreation
    even though their PVC retained the previous contents.
    """
    if not git_repo_url:
        return "echo 'no git_repo_url configured; starting with empty workspace'"
    # The clone URL stays credential-free: when the repo is private, auth is
    # supplied by git's credential helper (configured via GIT_CONFIG_* env +
    # the mounted Secret — see build_deployment_spec), so the PAT never lands
    # in /workspace/.git/config. On failure we drop a sentinel file the user
    # sees in the editor instead of a silently-empty workspace (private repo
    # with no/expired token is the usual cause). We don't `exit 1` — that
    # would CrashLoop the pod and wedge the workspace in `provisioning`.
    return (
        "set -eu; "
        "rm -f /workspace/.daalu-clone-failed; "
        "if [ -d /workspace/.git ]; then "
        "  echo 'workspace already initialised'; "
        "else "
        f"  echo 'cloning {git_repo_url} ({git_branch}) into /workspace'; "
        f"  if git clone --branch {git_branch} {git_repo_url} /workspace; then "
        "    echo 'clone ok'; "
        "  else "
        "    echo 'CLONE FAILED — if this is a private repo, check the access "
        "token (it may be missing, wrong, or expired) and recreate the "
        "workspace' | tee /workspace/.daalu-clone-failed; "
        "  fi; "
        "fi"
    )


async def _apply_git_secret(
    core: Any,
    workspace_id: str,
    git_repo_url: str | None,
    git_token: str | None,
) -> None:
    """Create/patch the per-workspace git Secret, or delete it when no token.

    Passing ``git_token=None`` is the "no auth" path — it removes any prior
    Secret so a user who clears their token (or switches to a public repo)
    stops carrying stale credentials.
    """
    name = git_secret_name(workspace_id)
    if not (git_token and git_repo_url):
        try:
            await core.delete_namespaced_secret(name=name, namespace=WORKSPACE_NAMESPACE)
        except k8s_client.exceptions.ApiException as e:
            if e.status != 404:
                raise
        return
    body = build_git_secret_spec(workspace_id, git_repo_url, git_token)
    try:
        await core.create_namespaced_secret(namespace=WORKSPACE_NAMESPACE, body=body)
    except k8s_client.exceptions.ApiException as e:
        if e.status == 409:
            await core.patch_namespaced_secret(
                name=name, namespace=WORKSPACE_NAMESPACE, body=body,
            )
        else:
            raise


async def apply_workspace(workspace_id: str, tenant_id: str, user_id: str,
                          profile: str, model: str | None,
                          git_repo_url: str | None,
                          git_branch: str,
                          git_token: str | None = None) -> None:
    """Create-or-update the K8s Deployment + PVC for one workspace.

    Idempotent. Uses server-side apply via the dynamic client so a
    second call with the same desired spec is a no-op (rather than
    bumping the resourceVersion and triggering a rollout).

    ``git_token`` is the *decrypted* PAT (the reconciler decrypts before
    calling). When present (and a repo URL is set) it's reconciled into a
    per-workspace Secret backing the git credential helper; when absent the
    Secret is removed so a cleared token stops authenticating.
    """
    await load_kube_config()
    git_authenticated = bool(git_token and git_repo_url)
    async with k8s_client.ApiClient() as api:
        core = k8s_client.CoreV1Api(api)
        apps = k8s_client.AppsV1Api(api)
        # Reconcile the git credential Secret *before* the Deployment so the
        # secret volume the pod mounts already exists on first schedule.
        await _apply_git_secret(
            core, workspace_id, git_repo_url, git_token if git_authenticated else None
        )
        pvc = build_pvc_spec(workspace_id, profile)
        dep = build_deployment_spec(
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            user_id=user_id,
            profile=profile,
            model=model,
            git_repo_url=git_repo_url,
            git_branch=git_branch,
            git_authenticated=git_authenticated,
        )
        # Create PVC (ignore "already exists").
        try:
            await core.create_namespaced_persistent_volume_claim(
                namespace=WORKSPACE_NAMESPACE, body=pvc,
            )
        except k8s_client.exceptions.ApiException as e:
            if e.status != 409:
                raise
        # Create-or-replace Deployment.
        try:
            await apps.create_namespaced_deployment(
                namespace=WORKSPACE_NAMESPACE, body=dep,
            )
        except k8s_client.exceptions.ApiException as e:
            if e.status == 409:
                dep_name, _ = resource_names(workspace_id)
                await apps.patch_namespaced_deployment(
                    name=dep_name, namespace=WORKSPACE_NAMESPACE, body=dep,
                )
            else:
                raise
        # Create Service (ignore "already exists"). The proxy resolves the
        # pod by this Service's DNS name; without it "Open IDE" 502s.
        svc = build_service_spec(workspace_id)
        try:
            await core.create_namespaced_service(
                namespace=WORKSPACE_NAMESPACE, body=svc,
            )
        except k8s_client.exceptions.ApiException as e:
            if e.status != 409:
                raise


async def deployment_ready(workspace_id: str) -> bool:
    """Return True once the workspace Deployment reports >=1 ready replica.

    Used by the reconcile loop to gate the `provisioning → active`
    transition. Previously the loop flipped to active optimistically;
    that left the proxy returning 502s until the pod was actually up.
    """
    await load_kube_config()
    dep_name, _ = resource_names(workspace_id)
    async with k8s_client.ApiClient() as api:
        apps = k8s_client.AppsV1Api(api)
        try:
            dep = await apps.read_namespaced_deployment(
                name=dep_name, namespace=WORKSPACE_NAMESPACE,
            )
        except k8s_client.exceptions.ApiException as e:
            if e.status == 404:
                return False
            raise
    status = getattr(dep, "status", None)
    if status is None:
        return False
    ready = getattr(status, "ready_replicas", None) or 0
    return int(ready) >= 1


async def mirror_secret_into_workspaces(
    *,
    source_namespace: str,
    source_name: str,
    target_name: str = "daalu-automation-secrets",
) -> None:
    """Copy a Secret from the hub namespace into ``daalu-workspaces``.

    Why: the workspace pod's token-minter sidecar needs the same
    ``SERVICE_TOKEN_SECRET_KEY`` that daalu-api uses to sign service
    tokens. Cross-namespace Secret reference is not a K8s primitive, so
    we mirror once at controller startup. Idempotent — patches an
    existing mirror in place rather than 409-ing.

    Only the keys workspace pods need are copied (token signing key
    plus the GHCR credentials for pulling the custom image). Everything
    else stays in the hub namespace.
    """
    await load_kube_config()
    keep_keys = {"SERVICE_TOKEN_SECRET_KEY"}
    async with k8s_client.ApiClient() as api:
        core = k8s_client.CoreV1Api(api)
        try:
            src = await core.read_namespaced_secret(
                name=source_name, namespace=source_namespace,
            )
        except k8s_client.exceptions.ApiException as e:
            logger.warning(
                "workspace_controller.secret_mirror.source_missing",
                source_namespace=source_namespace,
                source_name=source_name,
                status=e.status,
            )
            return
        if not src.data:
            return
        filtered = {k: v for k, v in src.data.items() if k in keep_keys}
        if not filtered:
            logger.warning(
                "workspace_controller.secret_mirror.no_matching_keys",
                wanted=list(keep_keys),
                found=list(src.data.keys()),
            )
            return
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {
                "name": target_name,
                "namespace": WORKSPACE_NAMESPACE,
                "labels": {"daalu.io/mirrored-from": source_namespace},
            },
            "data": filtered,
        }
        try:
            await core.create_namespaced_secret(
                namespace=WORKSPACE_NAMESPACE, body=body,
            )
            logger.info("workspace_controller.secret_mirror.created", name=target_name)
        except k8s_client.exceptions.ApiException as e:
            if e.status == 409:
                await core.patch_namespaced_secret(
                    name=target_name, namespace=WORKSPACE_NAMESPACE, body=body,
                )
                logger.info("workspace_controller.secret_mirror.patched", name=target_name)
            else:
                raise


async def scale_workspace(workspace_id: str, replicas: int) -> None:
    """Scale a workspace Deployment to N. Used for pause (0) / resume (1)."""
    await load_kube_config()
    async with k8s_client.ApiClient() as api:
        apps = k8s_client.AppsV1Api(api)
        dep_name, _ = resource_names(workspace_id)
        await apps.patch_namespaced_deployment_scale(
            name=dep_name,
            namespace=WORKSPACE_NAMESPACE,
            body={"spec": {"replicas": replicas}},
        )


async def destroy_workspace(workspace_id: str, *, release_pvc: bool = True) -> None:
    """Tear down the Deployment and (optionally) the PVC.

    ``release_pvc=False`` keeps the user's files around for a future
    resume — used by the pause path. Full destroy from the reaper sets
    ``release_pvc=True``.
    """
    await load_kube_config()
    dep_name, pvc_name = resource_names(workspace_id)
    async with k8s_client.ApiClient() as api:
        apps = k8s_client.AppsV1Api(api)
        core = k8s_client.CoreV1Api(api)
        try:
            await apps.delete_namespaced_deployment(name=dep_name, namespace=WORKSPACE_NAMESPACE)
        except k8s_client.exceptions.ApiException as e:
            if e.status != 404:
                raise
        # The Service shares the Deployment name; tear it down too.
        try:
            await core.delete_namespaced_service(name=dep_name, namespace=WORKSPACE_NAMESPACE)
        except k8s_client.exceptions.ApiException as e:
            if e.status != 404:
                raise
        # Drop the per-workspace git credential Secret on teardown. (Pause
        # scales to 0 rather than calling this, so the Secret survives a pause
        # and a resume re-applies it from the still-encrypted DB row.)
        try:
            await core.delete_namespaced_secret(
                name=git_secret_name(workspace_id), namespace=WORKSPACE_NAMESPACE,
            )
        except k8s_client.exceptions.ApiException as e:
            if e.status != 404:
                raise
        if release_pvc:
            try:
                await core.delete_namespaced_persistent_volume_claim(
                    name=pvc_name, namespace=WORKSPACE_NAMESPACE,
                )
            except k8s_client.exceptions.ApiException as e:
                if e.status != 404:
                    raise
