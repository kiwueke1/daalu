# 75. The workspace platform internals

This chapter walks the *code* behind a per-user coding workspace: how the
**workspace-controller** turns a database row into a running browser IDE and
reaps it when idle, how the **`/ide` reverse proxy** stitches that pod into
`daalu-api`, and how the **token-minter** keeps the in-pod agent
authenticated. Chapter 35 is the architecture sketch; this is the
walkthrough you read with the files open.

> **Why this is the moat.** Aider, Cline, and OpenHands are all *agent
> loops*. None of them provision an isolated per-tenant IDE, manage its
> PVC/lifecycle, proxy it behind your auth, mint rotating service tokens, or
> network-lock it. That layer — everything in this chapter — is what makes a
> coding *agent* into a multi-tenant, sovereign coding *product*.

## The pieces and where they live

| Piece | Path |
|---|---|
| Controller app + reconcile loop | `src/daalu_automation/workspace_controller/app.py` |
| K8s resource builders + ops | `src/daalu_automation/workspace_controller/k8s.py` |
| `/ide` reverse proxy + WS bridge | `src/daalu_automation/api/ide_proxy.py` |
| Public session API | `src/daalu_automation/api/routers/workspace.py` |
| code-server image | `deploy/docker/code-server-with-daalu/Dockerfile` |
| Controller deploy + RBAC + netpol | `deploy/k8s/workspace-controller/` |

## The session API (what the browser calls)

The user-facing surface is `api/routers/workspace.py`. It never touches
Kubernetes directly — it gates on tenant eligibility and delegates to the
controller over a service-token call.

* `GET /eligibility` (`workspace.py:157`) and `GET /models`
  (`workspace.py:176`) drive the create-workspace UI. Eligibility is
  computed by `_eligible_tenant` (`workspace.py:71`); the model list comes
  from the catalog filtered by `settings.coding_models_enabled` (see
  [73](73-coding-model-catalog-and-serving.md)).
* `POST /sessions` (`create_session`, `workspace.py:200`) is get-or-create:
  it re-checks eligibility, then `_controller_call` (`workspace.py:114`)
  POSTs to the controller with a `purpose`-scoped service-token JWT and
  returns the `/ide/<id>/` URL.
* `GET /sessions/me` and `DELETE /sessions/me` (`workspace.py:240`,
  `:250`) read and tear down the caller's workspace.

## The controller's REST surface

`workspace_controller/app.py` is a small FastAPI app the only caller of
which is `daalu-api` (authenticated by the shared service-token secret):

* `POST /workspaces/{user_id}` (`app.py:106`) upserts the `workspaces` row
  to desired-state `provisioning` (or flips a paused one back to
  provisioning, and re-provisions on a model change — `app.py:247`).
* `GET /workspaces/{user_id}` (`app.py:132`) returns the `WorkspaceView`
  (`app.py:49`) the API relays to the browser.
* `DELETE /workspaces/{user_id}` (`app.py:146`) sets state `destroyed`.
* `POST /workspaces/{user_id}/heartbeat` (`app.py:165`) stamps
  `last_heartbeat_at` and un-pauses an idle workspace. The proxy also
  heartbeats on traffic (see below), so an actively-used IDE never pauses.

The REST handlers only mutate the **DB row**. Kubernetes is reconciled
asynchronously — the API call returns immediately and the pod appears a few
seconds later.

## The reconcile loop and the lifecycle state machine

`_reconcile_loop` (`app.py:257`) wakes every 15 s, selects every workspace
row that is not `destroyed` (`app.py:272`), and calls `_reconcile_one`
(`app.py:287`) on each. That function is the state machine (`app.py:287`
docstring):

```
none/provisioning → create Deployment + PVC + Service; when ≥1 replica
                    is Ready, transition to active (stamp heartbeat)
active            → noop, unless heartbeat is stale (>24h) → paused
paused            → scaled to 0; reaped to destroyed after 14d idle
destroyed         → delete manifests; leave the row for audit
```

Readiness is polled by `k8s.deployment_ready` (`k8s.py:427`); destruction by
`k8s.destroy_workspace` (`k8s.py:536`, which optionally releases the PVC).
The state lives in the DB, so the loop is **level-triggered and
crash-safe**: if the controller restarts, it re-derives all desired state
from `workspaces` rows on the next tick — there is no in-memory queue to
lose.

> **Phase-2 seam.** The 15 s poll is fine at current scale; the planned
> upgrade is Postgres `LISTEN/NOTIFY` so transitions are event-driven. The
> state machine itself doesn't change.

## The K8s resources a workspace is made of

`k8s.build_deployment_spec` (`k8s.py:161`) builds a **two-container pod**
plus an init container:

* **`code-server`** — the browser IDE (our custom image), bound to `:8080`,
  running as the unprivileged `coder` user.
* **`token-minter`** sidecar (`k8s.py:270`) — runs the backend image and
  mints a fresh `purpose=coding` service-token JWT every 30 s into a shared
  `emptyDir` at `/var/run/daalu/tokens/service.jwt`. The extension reads
  that file (chapter 75).
* **init container** — clones the optional starter git repo on first boot
  (`_bootstrap_script`). The repo can be **private**: see
  [76. Private-repo authentication](76-private-repo-authentication.md) for
  how a PAT is stored, mounted, and consumed without ever landing in the
  Deployment manifest or the cloned repo's `.git/config`.

Sizing comes from `_PROFILES` (`k8s.py:33`) — `small`/`medium`/`large` map
to CPU/memory and a PVC size via `build_pvc_spec` (`k8s.py:141`). The PVC is
what makes a workspace survive pod restarts.

`build_service_spec` (`k8s.py:320`) creates a `ClusterIP` Service named
`ws-<short>` selecting `daalu.io/workspace-id=<id>`, so the proxy can reach
the pod by a stable DNS name rather than a pod IP.

### Secret mirroring

The token-minter needs `SERVICE_TOKEN_SECRET_KEY`, which lives in
`daalu-automation-secrets` in the controller's namespace — but the pod runs
in `daalu-workspaces`. On startup and on reconcile the controller mirrors
that Secret across namespaces (`k8s.py:461`). If V2 of the operator runbook
([36](36-coding-assistant-operator.md)) shows an
empty mirrored secret, the controller's secret-reader Role can't see the
source — that's the first thing to check.

## The `/ide` reverse proxy

A workspace pod is never exposed directly; `daalu-api` proxies it so the
browser only ever talks to the authenticated front door.
`api/ide_proxy.py`:

* `_check_authorisation` (`ide_proxy.py:37`) confirms the caller owns the
  workspace before any bytes flow.
* `_pod_url` (`ide_proxy.py:70`) maps `/ide/<id>/<path>` to the
  `ws-<short>` Service.
* `ide_proxy_http` (`ide_proxy.py:81`) proxies HTTP and, on success, bumps
  `last_heartbeat_at` so active use keeps the workspace alive
  (`ide_proxy.py:127`). It **forces `Accept-Encoding: identity`**
  (`ide_proxy.py:109`): code-server will otherwise reply `br`/`zstd` and the
  proxy would forward compressed bytes the browser renders as garbage — a
  real incident we fixed.
* `ide_proxy_ws` (`ide_proxy.py:143`) is a bidirectional WebSocket bridge
  for the terminal and the language server. It propagates the negotiated
  subprotocol (`ide_proxy.py:155`); getting that wrong is what surfaced to
  the browser as a `1006` abnormal close (the workbench-1006 fix).

## The custom code-server image

`deploy/docker/code-server-with-daalu/Dockerfile` is a two-stage build: an
`extbuilder` stage (`node:20-alpine`) compiles and packages the
`daalu-assistant` extension to a `.vsix` (`Dockerfile:17–25`), then the
runtime stage installs it (`Dockerfile:83–84`) on top of
`codercom/code-server`. The image also:

* removes the built-in Copilot Chat extension and sets
  `chat.disableAIFeatures: true` so users aren't offered a second,
  non-sovereign assistant;
* drops a `settings.json` pointing the extension at the injected gateway URL
  and the token file;
* bundles scoped tooling (kubectl, helm, git, python3) for the workspace
  user.

CI rebuilds this image when the extension changes
(`.github/workflows/build-code-server-image.yml`); the `tsc` build there is
what type-checks the extension.

## Network posture

Workspace pods are default-deny. The NetworkPolicies
(`deploy/k8s/workspace-controller/namespace.yaml` and the policies
described in [36](36-coding-assistant-operator.md))
allow: ingress from `daalu-api` (the proxy); egress to the inference
gateway/tunnel and DNS; and **outbound TCP/443 only** for the bootstrap
`git clone` (`namespace.yaml`, the `allow-workspace-egress-to-gateway-and-dns`
policy). That 443 rule explicitly *excludes* the RFC-1918 ranges
(`10/8`, `172.16/12`, `192.168/16`) and link-local, so a workspace can
reach `github.com`/`gitlab.com` to clone but **cannot** reach arbitrary
internal HTTP services. The assistant's chat traffic still never touches
the public internet — it rides the gateway/tunnel egress, not the 443 rule.

The practical consequences for repo support:

* **Public + private GitHub/GitLab.com over HTTPS** → works (443 is open).
  Private repos authenticate with a PAT — see
  [76](76-private-repo-authentication.md).
* **SSH remotes** (`git@github.com:…`) → *not* reachable: port 22 is not in
  the egress allowlist. This is deliberate — PAT-over-HTTPS is the
  supported path. (An operator could add a `:22` egress rule if a tenant
  insists on deploy-key SSH.)
* **Self-hosted / "local" git on an internal IP** → blocked by the
  RFC-1918 exclusion. An operator must add a targeted `ipBlock` egress rule
  for that host (mirroring the `10.200.0.2/32` gateway rule already in
  `namespace.yaml`) before such a repo will clone.

This is also why an off-the-shelf agent that assumes broad
browser/internet/MCP-over-the-web access cannot be dropped in unmodified.

## See also

* [76. Private-repo authentication](76-private-repo-authentication.md)
* [74. The Daalu Coding Agent extension — internals](74-coding-agent-extension.md)
* [35. Coding-assistant architecture](35-coding-assistant.md)
* [36. Coding-assistant — operator runbook](36-coding-assistant-operator.md)
