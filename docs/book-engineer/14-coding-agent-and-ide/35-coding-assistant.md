# 35. Coding Assistant architecture

Customer-facing chapter: user-guide/26.

This chapter is the engineer's tour: how the browser, daalu-api,
workspace-controller, code-server pod, and inference path fit
together. If you're about to change any of these, read this first.

> **Note.** Two things below differ from the default wiring; the prose is
> kept for the design rationale but read these first:
> - The **inference-gateway is optional**. Without it, workspace pods
>   reach the coder vLLM **directly over the WireGuard tunnel**
>   (e.g. `DAALU_GATEWAY_URL=http://10.200.0.2:8000` → edge socat →
>   `llm-coder`). The diagrams/tables that show inference-gateway as a
>   running component describe the *intended* end state. See
>   [ch. 74 "How it reaches the GPU"](74-coding-agent-extension.md).
> - The in-IDE assistant is the **Daalu Coding Agent** — now an *agent*
>   that reads your workspace files on its own (ch. 74), not a
>   plain chat. The upstream Copilot Chat is removed from the image.

---

## The four moving parts

1. **Browser** — opens `/workspace` to manage the IDE session, then
   navigates to `/ide/{workspace_id}/...` which is proxied to the
   workspace pod.
2. **daalu-api** — exposes `/api/v1/ide/sessions` (CRUD) and the
   reverse proxy at `/ide/{id}/...`. Authenticates with the user
   cookie; talks to the workspace-controller using a short-lived
   service-token JWT.
3. **workspace-controller** — its own Deployment, running in the hub
   namespace `daalu-automation` (so it inherits `DATABASE_URL` +
   `SERVICE_TOKEN_SECRET_KEY` from `daalu-automation-secrets` via
   `envFrom`, exactly like the gpu/nautobot/config-manager controllers).
   It holds a **cross-namespace** Role in `daalu-workspaces` to create
   Deployments + PVCs there. Owns the lifecycle of workspace pods.
   (Historical note: it used to run *in* `daalu-workspaces`, where that
   Secret doesn't exist — so it had no DB URL, fell back to a
   `localhost:5432` default, and every create 502'd. See chapter 36 for
   the incident + fix.)
4. **workspace pod** — `code-server` container + `token-minter`
   sidecar + tunnel sidecar. The AI side-panel inside code-server
   calls the inference-gateway on each prompt.

```
┌───────────────────────────────────────────────────────────────────┐
│                                                                   │
│     Browser                                                       │
│      │                                                            │
│      │ /api/v1/ide/sessions (cookie auth)                         │
│      ▼                                                            │
│  ┌────────────────┐         ┌────────────────────────────────┐    │
│  │   daalu-api    │ JWT ▶▶▶ │     workspace-controller       │    │
│  │  (user cookie) │         │  (reconciles workspaces table) │    │
│  └────────┬───────┘         └────────────┬───────────────────┘    │
│           │ /ide/<id>/...                │ kubectl apply          │
│           │ (auth proxy)                 ▼                        │
│           │              ┌─────────────────────────┐               │
│           │              │   workspace pod         │               │
│           ▼              │  ┌─────────┐  ┌────────┐│               │
│       (proxy to ─────────►│  │code-srv │  │tok-mint││               │
│        :8080)            │  │ (8080)  │  │        ││               │
│                          │  └────┬────┘  └────────┘│               │
│                          │       │ ext call        │               │
│                          └───────┼─────────────────┘               │
│                                  │                                 │
│                          ┌───────▼──────────┐                       │
│                          │ inference-gateway │ (Chapter 34)         │
│                          └──────────────────┘                       │
└───────────────────────────────────────────────────────────────────┘
```

---

## The session-creation flow

1. Browser: `POST /api/v1/ide/sessions  {profile, model, git_repo_url, git_branch, git_token?}`
2. daalu-api:
   * Verifies the user cookie (existing AuthGate middleware).
   * Resolves `model` (or `coding_model_default`) and **validates** it
     against `coding_models_enabled` — a non-servable pick is a 422
     (distinct from the 403 the eligibility gate raises).
   * Forwards the optional **`git_token`** (PAT for a private repo)
     verbatim; it is write-only and never returned. The controller
     encrypts it at rest. See
     [76. Private-repo authentication](76-private-repo-authentication.md).
   * Mints a 60 s service-token JWT (`core/service_tokens.py`) with
     `tenant_id`, `user_id`, `purpose='coding'`.
   * Calls `workspace-controller`'s
     `POST /workspaces/{user_id}` with the JWT (model in the body).
3. workspace-controller:
   * Verifies the JWT, extracts tenant + user.
   * Inserts or updates the `workspaces` row (state =
     `provisioning`).
   * Returns the row.
4. The controller's **reconcile loop** (every 15 s) sees the row,
   calls `apply_workspace` which `kubectl create`s the Deployment +
   PVC in `daalu-workspaces`. Once the Deployment reports a ready
   replica, the row transitions to `state='active'`.
5. daalu-api returns to the browser: `{id, state, ide_url}`. The
   browser opens `ide_url` in a new tab.

---

## The /ide reverse proxy

`/ide/{workspace_id}/{path:path}` is *not* a FastAPI route — it's
a Starlette `Route` + `WebSocketRoute` pair registered directly
on `app.router.routes`. Why:

* FastAPI's response model serialisation gets in the way of raw
  passthroughs.
* WebSocket upgrades need the bare ASGI surface — code-server uses
  WS for the integrated terminal and LSP messages.

The handler (``api/ide_proxy.py``) does:

1. Pull the `daalu_session` cookie from the request.
2. Decode the JWT (does *not* re-validate against the DB — that's
   a deliberate optimisation; the proxy is on the hot path).
3. Look up the `Workspace` row by `id`; refuse if the user_id in
   the JWT doesn't match the row's user_id.
4. Resolve the workspace pod's in-cluster URL via
   `k8s.resource_names(workspace_id)` →
   `ws-<short>.daalu-workspaces.svc.cluster.local:8080`.
5. Forward the request with `httpx`, stripping cookies and the
   `host` header.
6. Update `Workspace.last_heartbeat_at` (best-effort).

Cross-tenant access returns 404 (not 403) — we deliberately don't
leak the existence of other tenants' workspaces.

**WebSocket bridge**: both directions are implemented in
``ide_proxy_ws``. The handler:

* Validates ownership before accepting the upgrade (so a failed
  upgrade doesn't even reach an open WS).
* Opens an upstream connection via ``websockets.asyncio.client.connect``
  to the pod's :8080, propagating any subprotocol the browser
  negotiated (code-server picks one during the terminal handshake).
* Pumps frames with two ``asyncio`` tasks fed into ``asyncio.wait(
  FIRST_COMPLETED)`` — when one side closes the other is cancelled
  and both connections are torn down.

Long-polling fallback is no longer in the picture — code-server uses
the WS path for the terminal, file watcher, and LSP traffic.

---

## The token-minter sidecar

The Daalu Assistant extension inside code-server needs a valid
service-token JWT to call the inference-gateway. We don't want to
mint the JWT inside the code-server container itself, because that
container runs user shells — leaking the signing key into the
user's terminal would be very bad.

Solution: a tiny **sidecar** in the same pod that:

* Reads the signing key from a K8s Secret mounted into *only* the
  sidecar (not code-server).
* Mints a fresh JWT every 30 s.
* Writes it to a shared `emptyDir` volume (mounted at
  `/var/run/daalu/tokens/service.jwt` in both containers).

The extension reads the file on each prompt. The token's TTL (60 s)
is longer than the mint interval (30 s) so there's always a valid
token in the file.

This pattern is similar to the kube-apiserver bootstrap-token
sidecar. It's a well-known idiom; see also the
``daalu-edge`` deployment's similar split.

---

## Model selection (the catalog)

Users pick a coding model when they create a workspace. The mechanism is
a small **catalog** that is the single source of truth — see chapter 73
for the full nuts-and-bolts. In one paragraph:

`core/model_catalog.py` lists each model's `served_model_name`,
`gpu_class`, HF repo and quantization. The Create-Workspace form calls
`GET /ide/sessions`'s sibling `GET /ide/models` (returns the catalog,
each entry flagged `available` from `coding_models_enabled`). The chosen
catalog id is persisted on `workspaces.model`, and the controller injects
`DAALU_MODEL=<served_model_name>` into the code-server container. The
Daalu Assistant extension and `daalu chat` read `DAALU_MODEL` and put it
in the `model` field of their `/v1/chat/completions` call; the
inference-gateway's `resolve_upstream(model)` routes it to the vLLM that
advertises that exact name. Five places, one string — derive them all
from the catalog or routing silently 404s.

---

## NetworkPolicy posture

`daalu-workspaces` namespace has a default-deny applied at install
time (`templates/namespace.yaml`). The chart layers three explicit
allows:

* Ingress: daalu-api → workspace pods :8080 (the proxy)
* Egress: workspace pods → kube-dns
* Egress: workspace pods → inference-gateway and daalu-api in the
  release namespace

Notably absent: any egress to the public internet. `git push` to
GitHub doesn't work from inside the workspace; the `daalu propose`
CLI is the supported path (the executor does the push from outside
the workspace's network namespace).

The tunnel sidecar (planned — TODO) gets its own network
namespace, so its egress *to the tenant's cluster* doesn't go
through these rules.

---

## Lifecycle state machine

```
   none ─create──▶ provisioning ──ready──▶ active ──idle 24h──▶ paused
                       │                       │                  │
                       │ failed                │                  │
                       ▼                       │                  │ idle 14d
                     error                     │                  ▼
                                               │              destroyed
                                               │
                                               └─ destroy request ──▶ destroyed
```

Implementation lives in `workspace_controller.app::_reconcile_one`.
The reconciler is **idempotent**: applying the same desired state
to K8s twice is a no-op. This is important because the loop runs
on a 15 s tick *and* on user-driven events (POST /heartbeat); both
can race.

---

## What runs where (deployment-target summary)

| Component | Namespace | Image | Replicas | RBAC |
|-----------|-----------|-------|----------|------|
| daalu-api | daalu-automation | backend | 3 | none new |
| workspace-controller | daalu-automation | backend (entrypoint differs) | 1 | SA in `daalu-automation`, cross-ns Role in `daalu-workspaces` |
| inference-gateway | daalu-automation | backend | 1 *(not yet deployed — pods use the direct WG route today; see ch. 74)* | none |
| workspace pod (per user) | daalu-workspaces | code-server + backend (sidecar) | 1 | none |
| coder vLLM (`qwen-coder`/`llm-coder`) | `daalu` (GPU cluster) | vllm-openai | 1 | none |

All four use the same backend image — different entrypoints (`daalu
server`, `daalu workspace-controller`, `daalu inference-gateway`,
`daalu workspace-mint-token` for the sidecar).

---

## Wiring it up

Three things daalu-api needs to know about:

| Env var | Purpose |
|---|---|
| `workspace_controller_url` | URL of the controller's Service (`http://workspace-controller` — same namespace now). |
| `daalu_hosted_gateway_url` | URL of the inference-gateway (also handed to workspace pods as `DAALU_GATEWAY_URL`). |
| `daalu_hosted_upstream_url` | Where the gateway proxies to — the coder vLLM over the WG tunnel. |
| `coding_models_enabled` / `coding_model_default` | Which catalog ids are selectable + the default. |
| `service_token_secret_key` | Shared signing key. |

Three things the *workspace pod's sidecar* needs:

| Env var | Purpose |
|---|---|
| `service_token_secret_key` | Same key, mounted from the same Secret. |
| `DAALU_TENANT_ID`, `DAALU_USER_ID` | Identity to bake into the JWT. |

Set them via the existing `daalu-automation-secrets` mechanism —
same Secret the rest of the stack uses, so rotation is one operation.

---

## Known gaps for production-grade

What's shipped (Phase 1):
* WebSocket proxy bidirectional bridge — implemented in ``ide_proxy_ws``.
* Daalu Assistant VS Code extension pre-installed in the custom
  ``code-server-with-daalu`` image (built by
  ``.github/workflows/build-code-server-image.yml``).
* Token-minter sidecar — ``daalu workspace-mint-token`` CLI mints a
  ``purpose='coding'`` JWT every 30 s.
* ``daalu chat`` terminal REPL — Claude-Code-style agent for the
  terminal side of the workspace.
* 24 h pause + 14 d destroy lifecycle, with readiness gated on
  ``readyReplicas ≥ 1`` before flipping to ``active``.
* Real eligibility gate on ``/ide/sessions`` and a separate
  ``/ide/eligibility`` probe for the UI.

Still on the runway (Phase 2):
* [ ] Tunnel sidecar (`wg-edge`) to give the workspace access to a
  customer's federated cluster. The pre-installed `kubectl` is
  wired but won't reach anywhere without the tunnel.
* [ ] Reconciler scaling: the 15 s polling loop is fine for ~hundreds
  of workspaces; beyond that, switch to a K8s watch + Postgres
  LISTEN/NOTIFY shape.
* [ ] Workspace pod backup. PVC contents are best-effort — node
  failure loses unpushed work. The customer-facing chapter is
  honest about this; longer term a CronJob that ``tar``s the PVC
  into S3 closes the gap.
* [ ] Pen-test the cross-tenant proxy boundary explicitly.
* [ ] Per-tenant profile cap enforcement (today the controller
  accepts whatever profile the API forwards; the API forwards
  whatever the user picked).
* [ ] 12-day idle-warning email before the 14 d destroy.
* [ ] Run workspace pods inside the customer's federated cluster
  rather than the operator's — chapter 36 §"Phase 3" sketches the
  design.

---

## See also

* Chapter 34 — inference-gateway (the LLM backend)
* Chapter 18 (user guide) — change proposals (the path edits take)
* Chapter 57 — Tenant isolation (the NetworkPolicy + RBAC pattern this chapter uses)
