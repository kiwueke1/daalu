# 36. Coding Assistant — operator's setup runbook

Chapter 35 is the architecture tour. This chapter is the **runbook**:
exactly what the operator does to make the Coding Assistant work for
users, in the order it needs to happen. If you're standing up the
feature for the first time, or onboarding a new tenant to use it, this
is your checklist.

Customer-facing walkthrough: user-guide/26.

> **Note.** The "wire the gateway" step here assumes the
> in-hub inference-gateway, which is optional. Without it, wire
> the IDE **directly to the coder vLLM over the WireGuard
> tunnel** (e.g. `DAALU_HOSTED_GATEWAY_URL=http://10.200.0.2:8000` on the
> workspace-controller + a `10.200.0.2/32:8000` egress allow + the
> `daalu-edge` `svcproxy-vllm` socat targeting `llm-coder`). The
> end-to-end runbook for that path lives in
> [ch. 74](74-coding-agent-extension.md#how-it-reaches-the-gpu).

---

## What "working end-to-end" means

A user with an eligible tenant should be able to:

1. Click **Workspace** in the left sidebar.
2. See the create-workspace form (no 403, no broken UI).
3. Provision a workspace pod that boots within ~60 s.
4. Click **Open IDE** and land in browser VS Code with their code
   loaded, a working terminal, and the Daalu Assistant in the
   activity bar.
5. Either chat with the agent in the side-panel **or** run
   `daalu chat` in the terminal for the Claude-Code-style REPL —
   both surfaces hit the same inference gateway under
   `purpose='coding'`.

If any step fails, this chapter tells you where to look.

---

## The 502 incident — controller namespace + DB wiring (read this first)

For the feature's whole early life, **Create workspace returned 500/502**
and the workspace-controller never worked. Root cause, for the record:

* The controller ran in the `daalu-workspaces` namespace, with
  `envFrom: secretRef{name: daalu-automation-secrets, optional: true}`.
* That Secret only exists in `daalu-automation`. `envFrom` cannot cross
  namespaces, so **no env was injected** — no `DATABASE_URL`, no
  `SERVICE_TOKEN_SECRET_KEY`.
* With `optional: true` swallowing the miss, the controller fell back to
  the `localhost:5432` config default → `ConnectionRefusedError` on every
  DB call, and the DB-touching path starved the event loop enough to flap
  the `/healthz` readiness probe. The Service had no ready endpoints, so
  daalu-api's call surfaced as a 502.
* The startup secret-mirror could never fix this: it only copies
  `SERVICE_TOKEN_SECRET_KEY`, and a pod's env is fixed at container start
  anyway.

**The fix: relocate the controller into `daalu-automation`** — the same
namespace as its sibling controllers (gpu/nautobot/config-manager), where
`daalu-automation-secrets` resolves natively via `envFrom` (now
*non-optional*). It manages workspace pods in `daalu-workspaces` through a
**cross-namespace Role**, and the pod carries the
`app.kubernetes.io/part-of: daalu-automation` label so
`daalu-postgres-ingress`/`daalu-redis-ingress` don't deny its DB
connection. The controller's REST ingress (daalu-api → :8081) is governed
by `workspace-controller-ingress` in `daalu-automation`. If you ever see
`ConnectionRefused … localhost:5432` in the controller logs, the
`envFrom` Secret isn't resolving — check the controller is in
`daalu-automation` and the Secret is non-optional.

---

## One-time prereqs (operator-cluster install)

Do these once per Daalu deploy. None are per-tenant.

### 1. Build + publish the workspace image

The workspace pod runs ``ghcr.io/kiwueke1/code-server-with-daalu:latest``.
The image bakes in:

* code-server (versioned via ``CODE_SERVER_VERSION`` build arg).
* The Daalu Assistant VS Code extension
  (``extensions/daalu-assistant/``, compiled + packaged in the
  builder stage).
* OS tooling the assistant + user need: ``git``, ``curl``,
  ``jq``, ``python3``, ``kubectl``, ``helm``.

Build is automated:
``.github/workflows/build-code-server-image.yml`` watches for changes
under ``extensions/daalu-assistant/**`` or
``deploy/docker/code-server-with-daalu/**`` and pushes to GHCR. Trigger
a manual build:

```bash
gh workflow run build-code-server-image.yml
```

To build + push locally (faster iteration, requires GHCR write
credentials):

```bash
docker login ghcr.io -u <github-user>      # write:packages scope
docker build -f deploy/docker/code-server-with-daalu/Dockerfile \
  --build-arg CODE_SERVER_VERSION=4.121.0 \
  -t ghcr.io/kiwueke1/code-server-with-daalu:latest .
docker push ghcr.io/kiwueke1/code-server-with-daalu:latest
```

The image is ~1.2 GiB — first pull on a workspace pod takes 30–60 s
depending on the node's network. That's why the reconciler waits on
``readyReplicas`` before flipping the row to ``active``; otherwise the
UI would announce "active" while the proxy was still 502-ing.

### 2. Ship the workspace-controller to the cluster

The controller's manifests live under
``deploy/k8s/workspace-controller/`` and are deployed by an Argo
Application at ``deploy/argocd/applications/workspace-controller.yaml``.

To install on a fresh cluster:

```bash
kubectl apply -f deploy/argocd/applications/workspace-controller.yaml
```

Argo will:

1. Create the ``daalu-workspaces`` namespace (for the workspace *pods*)
   with its default-deny + workspace-pod NetworkPolicies.
2. Create the controller ServiceAccount **in ``daalu-automation``**, a
   cross-namespace Role + RoleBinding in ``daalu-workspaces`` (manage
   workspace pods), and a single-resource Role in ``daalu-automation``
   to read ``daalu-automation-secrets`` for the mirror.
3. Apply ``workspace-controller-ingress`` (daalu-api → :8081) in
   ``daalu-automation``.
4. Deploy the controller **in ``daalu-automation``** — one replica,
   50m/200m CPU, 256/512 MiB, ``envFrom daalu-automation-secrets``.

Confirm:

```bash
kubectl get pods -n daalu-automation -l app=workspace-controller
# Expect: workspace-controller-* in 1/1 Running.

kubectl logs deploy/workspace-controller -n daalu-automation | head
# Expect:
#   workspace_controller.reconcile_loop_started
#   workspace_controller.secret_mirror.{created|patched}  name=daalu-automation-secrets
# Must NOT contain: ConnectionRefusedError ... localhost:5432
```

The "secret_mirror" line is critical — that's the controller copying
``SERVICE_TOKEN_SECRET_KEY`` from ``daalu-automation`` into
``daalu-workspaces`` so per-workspace token-minter sidecars can
mint JWTs. If that log entry isn't present, jump to **Troubleshooting**
below.

### 3. Wire daalu-api's controller URL

The IDE routes on daalu-api are no-ops unless ``WORKSPACE_CONTROLLER_URL``
is set. The Argo manifest at
``deploy/k8s/api/deployment.yaml`` injects it as a literal env var:

```yaml
env:
  - name: WORKSPACE_CONTROLLER_URL
    value: http://workspace-controller.daalu-automation.svc.cluster.local
```

That's the in-cluster Service URL (same namespace now, so the short name
`http://workspace-controller` also works via the Helm path). If you're
running daalu-api outside the cluster (dev), set the env var to your
tunnelled controller URL.

Verify after a daalu-api roll:

```bash
kubectl exec -n daalu-automation deploy/daalu-api -- sh -c \
  'echo "controller=$WORKSPACE_CONTROLLER_URL"'
# Expect: http://workspace-controller.daalu-automation.svc.cluster.local
```

### 4. Confirm GHCR pull-secret in `daalu-workspaces`

Workspace pods reference ``imagePullSecrets: [{name: ghcr}]`` so they
can pull the private code-server image. The Secret must exist in the
``daalu-workspaces`` namespace (it's referenced by name, not synced
from anywhere).

```bash
kubectl get secret ghcr -n daalu-workspaces
```

If missing, copy from ``daalu-automation``:

```bash
kubectl get secret ghcr -n daalu-automation -o yaml \
  | sed 's/namespace: daalu-automation/namespace: daalu-workspaces/' \
  | kubectl apply -f -
```

(Cross-namespace pull-secret references aren't a K8s primitive, so the
copy is necessary. The token needs ``read:packages``; a
``repo``-only token will silently 401 on pull.)

### 5. Serve a coding model + enable it in the catalog

The assistant needs an LLM endpoint. Models live in
``core/model_catalog.py`` (the single source of truth — see chapter 73);
the manifest is generated from a catalog entry:

```bash
# Render + apply the coder vLLM on the GPU/workload cluster.
export KUBECONFIG=<gpu-cluster-kubeconfig>
daalu render-coding-model qwen2.5-coder-14b --gpu-class ada-16 \
  > deploy/k8s/gpu/10-vllm-qwen-coder-14b.yaml

# The GPU is allocated EXCLUSIVELY to one pod — a 16 GB card
# can't hold the 8B classifier AND a 14B coder. Free the card first
# (if your classifier runs on an external provider, it's idle):
kubectl -n daalu scale deploy/llama-classifier --replicas=0
kubectl -n daalu create secret generic hf-token \
  --from-literal=token="$HF_TOKEN" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f deploy/k8s/gpu/10-vllm-qwen-coder-14b.yaml

# Verify the served name == the catalog served_model_name == DAALU_MODEL:
kubectl -n daalu port-forward svc/llm-coder 8000:80 &
curl -s localhost:8000/v1/models | jq '.data[].id'
#   "qwen/qwen2.5-coder-14b-instruct"
```

Then wire the hub (`daalu-automation-secrets` / Helm values):

| Setting | Value |
|---|---|
| `DAALU_HOSTED_GATEWAY_URL` | `http://inference-gateway` (deploy the `inference-gateway` chart if absent) |
| `DAALU_HOSTED_UPSTREAM_URL` | the coder Service over the WG tunnel, e.g. `http://10.200.0.x:8000` |
| `CODING_MODELS_ENABLED` | `["qwen2.5-coder-14b"]` — the catalog ids users can pick |

With **no `gpu_pools` rows** the gateway routes everything to
`DAALU_HOSTED_UPSTREAM_URL` (single-model bootstrap). The 48 GB `ada-48`
models (`qwen2.5-coder-32b`, `qwen3-coder-30b-a3b`) render the same way
once the RTX 6000 Ada is labeled `gpu-class=ada-48`; add their ids to
`CODING_MODELS_ENABLED` and they become selectable in the UI.

---

## Per-tenant enablement

Workspace eligibility is gated by *any* of three conditions on the
Tenant row:

| Condition | Means |
|---|---|
| ``feature_flags.workspaces_enabled = true`` | Operator-set override. |
| ``sovereign_inference_url`` is non-empty | Tenant brought their own GPU via the federation tunnel; coding prompts route there. |
| ``feature_flags.daalu_hosted_enabled = true`` AND the deploy has ``daalu_hosted_gateway_url`` set | Tenant opted into the operator's GPU pool. |

The check is centralised in ``_eligible_tenant`` in
``api/routers/workspace.py``; it is consulted on both
``GET /ide/eligibility`` (cheap UI probe) and ``POST /ide/sessions``
(authoritative gate). Any code path that creates a workspace must go
through ``_eligible_tenant`` — never copy the predicate.

### Turning the feature on for a tenant

For an internal tenant or a customer beta, the simplest path is the
operator override. Connect to the DB and:

```sql
UPDATE tenants
SET feature_flags = jsonb_set(
        coalesce(feature_flags, '{}'::jsonb),
        '{workspaces_enabled}',
        'true'::jsonb)
WHERE slug = 'acme';
```

There is no admin UI for this flag yet — it's a deliberate friction
point so the operator stays in the loop while we're in v1. The next
session of work on this feature should add it to
``/admin/tenants/{slug}`` (see chapter 19 for the admin-page pattern).

### Confirming the tenant is wired

Have a user in the tenant hit ``GET /api/v1/ide/eligibility``. Expect:

```json
{"eligible": true, "reason": ""}
```

The ``Workspace`` sidebar entry appears unconditionally — the page
itself surfaces the "not eligible" CTA so users discover the feature
even when they need to upgrade their tier to use it.

---

## The first session, step-by-step

Once the prereqs are in place and the tenant is enabled:

1. User logs in, clicks **Workspace** in the sidebar.
2. The page calls ``GET /ide/eligibility`` → renders the create form.
3. User picks a **coding model** (the picker lists the catalog; the
   ada-48 models are disabled until that card is online) and a profile
   (default ``small``), optionally enters a git repo URL + branch.
4. User clicks **Create workspace**. Browser POSTs to
   ``/api/v1/ide/sessions``.
5. daalu-api: mints a 60 s service-token JWT with
   ``purpose='coding'``, calls workspace-controller's
   ``POST /workspaces/{user_id}``.
6. Controller: inserts a ``workspaces`` row with ``state='provisioning'``.
7. Reconcile loop (15 s tick) sees the row, calls
   ``apply_workspace`` → creates PVC + Deployment in
   ``daalu-workspaces``.
8. Workspace pod starts. Init container clones the git repo if set
   (idempotent on subsequent boots — won't re-clone if ``.git``
   exists).
9. code-server + token-minter sidecar come up. Token-minter writes
   ``/var/run/daalu/tokens/service.jwt`` (atomic rename to avoid torn
   reads).
10. Reconciler observes ``readyReplicas >= 1``, flips the row to
    ``state='active'`` and stamps ``last_heartbeat_at``.
11. Browser polls ``GET /ide/sessions/me`` every 5 s; once state is
    ``active`` the "Open IDE" button becomes clickable.
12. User clicks **Open IDE**. New tab opens ``/ide/{workspace_id}/``.
13. daalu-api's reverse proxy (``api/ide_proxy``) authenticates the
    cookie, looks up the workspace row, forwards the HTTP request
    to ``ws-<short>.daalu-workspaces.svc.cluster.local:8080``.
14. Browser establishes WebSocket → daalu-api accepts the upgrade,
    opens upstream ``websockets`` connection to the pod, pumps frames
    in both directions. Terminal + LSP now work.
15. Inside the IDE: user opens the Daalu Assistant activity-bar
    panel, types a question. Extension reads
    ``/var/run/daalu/tokens/service.jwt``, POSTs to the inference
    gateway with ``Authorization: Bearer <jwt>``.
16. Inference gateway verifies the JWT (same key as daalu-api signed
    with), notes ``purpose='coding'``, routes to LOCAL or SOVEREIGN
    only — external tiers are unreachable for this purpose
    (``core/llm.py:317–387``).

---

## Verification checklist

Run these in order. If any fails, fix that one before continuing.

### V1. Controller pod is healthy

```bash
kubectl get deploy -n daalu-automation workspace-controller
# READY 1/1, AVAILABLE 1
```

### V2. Secret mirror succeeded

```bash
kubectl get secret -n daalu-workspaces daalu-automation-secrets \
  -o jsonpath='{.data.SERVICE_TOKEN_SECRET_KEY}' | head -c 30
# Expect: a non-empty base64 string. Empty = the controller's
# secret-reader Role can't see the source Secret.
```

### V3. NetworkPolicies present

```bash
kubectl get networkpolicy -n daalu-workspaces
# Workspace-pod policies: default-deny, allow-api-to-workspaces,
# allow-workspace-egress-to-gateway-and-dns.
kubectl get networkpolicy -n daalu-automation workspace-controller-ingress
# The controller's own ingress now lives in daalu-automation.
```

### V4. daalu-api can see the controller

```bash
kubectl exec -n daalu-automation deploy/daalu-api -- \
  curl -sf -o /dev/null -w "%{http_code}\n" \
  http://workspace-controller.daalu-automation.svc.cluster.local/healthz
# Expect: 200
```

### V5. Eligibility endpoint works for an enabled tenant

```bash
curl -sf -b cookie.jar https://ops.example.com/api/v1/ide/eligibility | jq
# Expect: {"eligible": true, "reason": ""}
```

### V6. A workspace actually boots end-to-end

After clicking "Create workspace" in the UI:

```bash
kubectl get pods -n daalu-workspaces -l app=daalu-workspace -w
# Expect a pod ws-<short>-... to go 0/2 → 2/2 within ~60s.

kubectl logs -n daalu-workspaces deploy/ws-<short> -c token-minter
# Expect: no output (the loop is quiet). No CrashLoopBackOff.

kubectl exec -n daalu-workspaces deploy/ws-<short> -c code-server -- \
  ls -la /var/run/daalu/tokens/
# Expect: service.jwt with a recent mtime.
```

### V7. Open IDE actually renders code-server

In the browser, click **Open IDE**. Expected:

- code-server loads.
- The activity bar (left edge) shows the Daalu Assistant icon. Click
  it; the chat panel renders with the system-prompt banner.
- Open a terminal (``Terminal → New Terminal``). ``kubectl``,
  ``helm``, ``git`` are on PATH.
- Run ``daalu chat``. Expect the banner:

  ```
  daalu chat — coding assistant (terminal mode)
  workspace:  /workspace
  gateway:    http://inference-gateway.daalu-automation.svc.cluster.local
  model:      qwen/qwen2.5-coder-14b-instruct
  ```

  The ``model`` line is the served name resolved from the model the
  user picked at create time (injected as ``DAALU_MODEL``).

---

## Troubleshooting

### "Workspace not enabled for your tenant"

The user's tenant fails ``_eligible_tenant``. Check, in order:

1. ``feature_flags`` JSON on the Tenant row (SQL).
2. ``sovereign_inference_url`` populated?
3. ``settings.daalu_hosted_gateway_url`` set on daalu-api?

The error response now includes a precise ``reason`` field — the UI
surfaces it directly.

### Workspace pod CrashLoopBackOff in token-minter

The sidecar runs ``daalu workspace-mint-token`` on a 30 s loop. Two
common causes:

- ``SERVICE_TOKEN_SECRET_KEY`` not in the mirrored Secret
  (``kubectl get secret daalu-automation-secrets -n daalu-workspaces``).
  Restart the controller to retry the mirror.
- ``daalu`` CLI image doesn't have the ``workspace-mint-token``
  subcommand (pre-2026-05 image). Push a new daalu-automation image
  built from master.

### Workspace pod CrashLoopBackOff in code-server

Usually the bootstrap init container failing to clone:

```bash
kubectl logs -n daalu-workspaces deploy/ws-<short> -c bootstrap
```

Public HTTPS egress is allowed by the NetworkPolicy with a 0.0.0.0/0
+ except-RFC1918 rule; private repos need a credentialled URL — the
init container does an anonymous clone today.

### "Open IDE" → 502 from the proxy

The pod isn't ready yet. The reconciler should have prevented this
by holding ``provisioning`` until ``readyReplicas ≥ 1``; if it
didn't:

```bash
kubectl get deploy -n daalu-workspaces -l daalu.io/workspace-id=<id>
# Look at READY column — if it's 0/1, code-server isn't up.

kubectl describe pod -n daalu-workspaces -l daalu.io/workspace-id=<id>
```

Common cause: ``ImagePullBackOff`` on
``code-server-with-daalu:latest`` — pull secret missing in
``daalu-workspaces`` (V4 step above).

### "Open IDE" works but terminal is blank

WebSocket upgrade is failing. Check daalu-api logs:

```bash
kubectl logs -n daalu-automation deploy/daalu-api | grep ide_proxy.ws
```

A ``ide_proxy.ws.upstream_connect_failed`` line means the proxy
couldn't reach the pod's :8080 over the WS path. Usually
NetworkPolicy: confirm ``allow-api-to-workspaces`` permits port 8080
from ``daalu-automation/daalu-api`` to ``daalu-workspaces``'s
workspace pods.

### Daalu Assistant panel says "cannot read service token"

The extension reads ``/var/run/daalu/tokens/service.jwt``. If the
sidecar isn't writing it:

```bash
kubectl exec -n daalu-workspaces deploy/ws-<short> -c token-minter -- \
  daalu workspace-mint-token
```

If this prints a JWT, the loop is misconfigured. If it errors, the
``SERVICE_TOKEN_SECRET_KEY`` is missing from the sidecar's env
(re-check V2 + the ``envFrom`` block in ``k8s.build_deployment_spec``).

### Private-repo clone failed → empty workspace

The user pasted a private repo URL but the editor is empty. The init
container leaves a breadcrumb instead of failing the pod — look for it:

```bash
kubectl exec -n daalu-workspaces deploy/ws-<short> -c code-server -- \
  cat /workspace/.daalu-clone-failed 2>/dev/null
kubectl logs -n daalu-workspaces deploy/ws-<short> -c bootstrap
```

Checklist:

* **Token present?** ``kubectl get secret -n daalu-workspaces ws-<short>-git``
  should exist. If it's missing, the workspace was created without a PAT —
  the user re-creates it with a token (see
  [76](76-private-repo-authentication.md)).
* **Token valid/scoped?** A 403/authentication error in the bootstrap log
  means the PAT is wrong, expired, or under-scoped (`repo` for GitHub /
  `read_repository` for GitLab).
* **Self-hosted/internal host?** A *timeout* (not a 403) usually means the
  egress policy blocked it: the 443 rule excludes RFC-1918, so an internal
  git host needs a targeted ``ipBlock`` egress rule added to
  ``namespace.yaml`` (model it on the ``10.200.0.2/32`` gateway rule).
* **SSH URL?** ``git@host:…`` can't work — port 22 isn't allowed. Use the
  HTTPS URL + PAT.

If the controller logs ``workspace.git_token_decrypt_failed``, the stored
ciphertext no longer decrypts — almost always ``secret_key`` was rotated
without re-encrypting. The user re-enters the token to re-store it.

---

## Phase 2 work (planned, not shipped)

The current shape works inside the **operator's** cluster — every
workspace pod lives in the hub's ``daalu-workspaces`` namespace. Two
extensions are in the design pile but not built:

### Phase 2a — VS Code extension polish

The activity-bar chat panel is functional but minimal:

- Streaming responses (today the gateway returns a complete payload;
  streaming requires gateway-side SSE which is gated on the streaming-
  quota work).
- Apply-block rendering: when the model proposes a multi-file change,
  the panel today shows each block separately. Folding multiple writes
  into a single "Apply all" gesture would be nicer.
- Tool calls beyond ``write`` / ``run`` — eventually the panel should
  be able to ask the daalu API for drift, propose change-proposals,
  query Prometheus. Same shape as ``daalu chat`` in the terminal.

### Phase 2b — Customer-cluster workspace pods

The product story is "your code never leaves your perimeter." Today
the workspace pod is in the operator's cluster — code is at rest on
an operator-managed PVC. For customers who need stricter sovereignty,
the workspace pod should run inside the **customer's** federated
cluster, accessed via the existing WireGuard tunnel.

Two viable architectures:

**A) Remote-create via the federation tunnel.** The central
``workspace-controller`` keeps the DB row, but for tenants flagged
``workspace_target=customer_cluster``, talks to the customer cluster's
K8s API through the WireGuard tunnel using the kubeconfig stored on
the ``ClusterTunnel`` row. Creates the Deployment / PVC / Service in
a ``daalu-workspaces`` namespace there. daalu-api's IDE proxy routes
the HTTP/WS traffic through the tunnel to reach the pod.

**B) Per-cluster controller agent.** A small controller pod ships
into the customer's cluster as part of federation onboarding, watches
a subset of the central DB (via tunnel), and creates workspace pods
locally.

Pros / cons:

| | A: remote-create | B: per-cluster agent |
|---|---|---|
| Control-plane complexity | low — central controller does it all | high — two controllers to deploy/monitor |
| Blast radius if central controller is compromised | wide — admin creds to every cluster | narrow — central only talks to per-cluster agents |
| Customer-cluster prereqs | inbound tunnel for daalu-api → pod | none beyond what federation already requires |
| Latency (keystrokes through tunnel) | high — every keystroke round-trips | high — same |

Recommendation when this lands: **start with A**, behind a tenant flag,
and revisit B only once we have a customer who explicitly needs
"daalu has zero kube credentials to my cluster." Both architectures
keep the central daalu-api IDE proxy in the picture — the only
question is where the pod runs.

### Phase 2c — Long-tail polish

Tracked in chapter 35's "Phase 2" list — tunnel sidecar for
in-workspace cluster access, K8s watch instead of polling, PVC
backup, etc.

---

## See also

* Chapter 35 — architecture deep-dive.
* Chapter 34 — inference-gateway (the LLM backend for both the
  activity-bar panel and ``daalu chat``).
* Chapter 57 — tenant-isolation patterns.
* Customer-facing walkthrough: user-guide chapter 56.
