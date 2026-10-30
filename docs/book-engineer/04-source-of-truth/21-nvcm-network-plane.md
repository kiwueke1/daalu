# 21. The NV-CM network plane

How Daalu provisions and drives a per-tenant **NVIDIA Config Manager** stack
for switch/fabric config. Read chapter 20 first for the big picture.

> **Read chapter 23 for the install internals.** This chapter focuses on the
> network *executor* (§21.3) and multi-tenancy. The provisioning mechanics
> summarized in §21.1 are covered in depth — including the **Deployer** that
> replaced the bare-Helm path — in **chapter 23 (NV-CM internals & the
> controller integration)**.

## 21.1 What gets deployed, and how

NV-CM ships as one Helm chart. We vendor it **pinned** at
`deploy/charts/nv-config-manager-<version>/` (currently `1.3.0`) — the
chart only, never the Python source (the *installer* and the Nautobot
*bootstrap jobs* are vendored separately; see chapter 23). Images are mirrored
to Harbor.

> **Correction (the chart has no `global.imageRegistry`).** Earlier drafts said
> the upstream `registry.example.com/nvidia/...` placeholders are overridden
> via `global.imageRegistry`. That knob does **not** exist in this chart.
> Instead each `global.images.<key>.repository` is repointed individually at
> the Harbor mirror (`values.render_values` / `deployer_config`, keys in
> `values.NV_CONFIG_MANAGER_IMAGE_KEYS`). See `deploy/charts/README.md` for the
> mirror + version-bump procedure.

### The controller

`config_manager_controller/` is a hub-side reconciler that mirrors
`nautobot_controller/` (chapter 19) but provisions a full Helm release per
tenant. As of the Deployer adoption it drives NVIDIA's vendored **Deployer**
(not a bare `helm upgrade --install`, which deadlocks on the chart's
`secret-assembler` pre-install hook — see chapter 23 §23.3), gated by
`CONFIG_MANAGER_USE_DEPLOYER=true` (**on in prod**):

- `app.py` — FastAPI: `POST/GET/DELETE /tenants/{tenant_id}` (the *singular*
  routes, which resolve the tenant's **primary environment**) plus the
  environment-scoped family
  `GET/POST /tenants/{id}/environments[/{env_id}[/progress|/upgrade]]`
  (`app.py:389-561`; chapter 23 §23.9 covers named environments),
  `GET /tenants/{id}/progress` (live pods/events for the install panel) and
  `POST /tenants/{id}/upgrade` (chart bumps from the UI). All authenticated
  with a service-token JWT of purpose `config-manager-provision`
  (`core/service_tokens.py`). A 30 s reconcile loop converges each
  `config_manager_tenants` row.
- **Target-tunnel auto-select** (PR #112, `app.py:160-181`
  `_resolve_target_tunnel`): when the caller names no tunnel, the controller
  resolves the tenant's own **connected** cluster tunnel instead of silently
  defaulting to NULL. NULL is a *documented mode* meaning "install into the
  hub cluster" — but the hub carries none of NV-CM's Tier-A singletons (CNPG,
  the shared `envoy-gateway` GatewayClass), so the accidental default failed
  the precheck with an error that read like a false alarm (a live tenant sat
  in `error` for weeks over exactly this). An explicit id is validated
  (404/ownership); `None` is returned only when the tenant genuinely has no
  tunnel, preserving hub-local mode. The runtime half matters equally: the
  provision route now binds `Integration.cluster_tunnel_id` to whichever
  tunnel the controller actually installed through — the svc-* client
  resolves its edge proxy from that column.
- `values.py` — `render_values(row)` (legacy HelmRunner path) and
  `deployer_config.build_install_config(row)` (Deployer path) produce the
  multi-tenant-safe config: `createGatewayClass=false` + shared
  `className=envoy-gateway` (the one cluster-scoped collision — §21.4), a
  short `fullnameOverride` (63-char limit, chapter 23), unique
  `base_hostname`, namespace `cm-<slug>` (a tenant's *later* environments use
  `cm-<tenant>-<env>` / `<env>-<tenant>.<domain>` — chapter 23 §23.9),
  component toggles
  from `row.components`, CNPG instance count from `row.size_profile`, Harbor
  image overrides, and bearer-only OIDC pointed at daalu's Keycloak.
  `compute_urls()` derives the per-component URLs.
- `deployer_runner.py` / `helm_runner.py` — the Deployer driver (current) and
  the legacy bare-helm runner (flag-off). Both write values + (for remote
  clusters) the tunnel kubeconfig to short-lived temp files. See chapter 23.

### State machine

`config_manager_tenants.state`: `pending → provisioning → active`, plus
`deleting → destroyed` for teardown and `error` on failure (retried next tick;
provisioning is idempotent). `active` rows are **re-driven every tick** too
(self-healing re-converge). On `active` the reconciler writes `urls` (the
svc-* + human URLs) back to the row; since the provision route went async
(PR #114, chapter 23 §23.2) the `Integration(provider="config_manager")` row
is written by the **first progress poll that observes `active`**, not by the
request that started the install.

Three reconciler hardenings worth knowing (all observed in prod):

- **Unwedge `pending-*` releases** (PR #116,
  `deployer_runner.py:491-547`). A controller restart mid-install (e.g. a CD
  rollout) leaves the Helm release `pending-install`/`pending-upgrade`, and
  every later install fails "another operation is in progress" — a permanent
  wedge the retry loop can never clear. The reconciler now unwedges *before*
  installing: `pending-install` deletes the stuck release record (nothing to
  roll back to; the objects carry `meta.helm.sh/release-*` so the reinstall
  adopts them rather than duplicating), `pending-upgrade` rolls back to the
  last deployed revision. Best-effort — a failed unwedge logs
  `release_unwedge_failed` and lets the provision proceed.
- **Delete-vs-provision race** (PR #112). A provision holds its ORM row across
  a rollout of up to 15 minutes, then commits its in-memory copy — which
  silently overwrote a `deleting` state scheduled meanwhile. Fixed by
  re-reading before commit.
- **Raising runners wedged the state machine** (PR #112). A runner that
  *raised* (instead of returning `(ok, err)`) escaped to the loop-level
  handler, which stamps `last_error` but leaves `state=provisioning` — a
  combination the state machine cannot clear, so the row retried a failing
  install every 30 s forever.

### Target cluster

`target_cluster_tunnel_id IS NULL` → daalu's local/in-cluster Helm; non-null
→ the customer cluster, reached by loading the tunnel kubeconfig
(`nautobot_controller.app._load_customer_kubeconfig`, reused).

## 21.2 Authentication (hub → NV-CM)

NV-CM's `svc-*` hostnames are **JWT-only** (no OIDC redirect — for machine
clients). `core/keycloak.py:get_machine_token()` mints a client-credentials
JWT per `(issuer, client_id, audience)` and caches it until just before
expiry. The JWT's `roles` claim maps to NV-CM RBAC; the hub client is granted
an **execute** role for `DeployWorkflow`. Self-approval is intentional: the
human already approved the `ChangeProposal` in daalu's UI, so the hub is the
**pre-approved executor**.

Connection params live on the `Integration(provider="config_manager")` row;
`core/configmgr/client.py:conn_from_integration_config()` builds the
`NvcmConn` (service URLs + Keycloak client + decrypted secret).

This is the **machine** face of NV-CM. The **human** face — a browser reaching
a tenant's private tool *UI* through the hub at `*.cmtools.example.com`, logged in
as the real operator via a per-user Keycloak token-exchange — is the cmtools
tool proxy in [Part XIII — Auth integrations](../13-auth-integrations/README.md)
(chapters [68](../13-auth-integrations/68-nvcm-tool-sso-through-the-hub.md)–[69](../13-auth-integrations/69-per-user-token-exchange.md)).
That path reuses *this* `daalu-hub-nvcm` client-credentials token as its
service-identity fallback when a session carries no user refresh token.

## 21.3 The executor

`core/device/config_manager.py:ConfigManagerExecutor` is a real
`DeviceAdapter` (`transport="config_manager"`), so it reuses the gate
verbatim:

- `render(NetworkFacts)` → deterministic canonical text (sorted interfaces /
  vlans / routes). This is the snapshot the gate stores and re-checks for
  drift — determinism is load-bearing.
- `execute(creds, rendered)` → the NV-CM sequence (verified against the
  vendored OpenAPI specs):
  1. `POST /v1/config/{uuid}/{file}` — stage intended config (Config Store).
  2. `POST /v1/workflow/ngc/deploy` — start `DeployWorkflow`.
  3. Poll `GET /v1/workflow/{id}` to the `perform_configuration_diff`
     `PENDING_APPROVAL` stage, then `POST …/approve/perform_configuration_diff`.
  4. Poll to `COMPLETED`/`FAILED` → `ExecutionResult`.

The interface mismatch (the ABC's `execute(creds, rendered)` has no device
UUID or service URLs) is solved by smuggling them through `Credentials.extra`
(`nvcm_conn`, `device_uuid`, `filename`), populated by
`change_proposals.resolve_credentials` for `transport == "config_manager"`.

## 21.4 Multi-tenant coexistence

Many tenants share one host cluster. The chart is namespaced-safe **except**
the cluster-scoped `GatewayClass` (`templates/gatewayclass.yaml` names it
`{{ .Values.gateway.className }}`, not release-scoped). Resolution: install
**one shared Envoy Gateway in Tier-A** and set `createGatewayClass=false` on
every tenant release with a unique `baseHostname`. CNPG `Cluster`s, Issuers,
HTTPRoutes, SecurityPolicies are all namespaced — no collision.

Tier-A (install once per host cluster, before any tenant): Envoy Gateway,
cert-manager, CloudNativePG operator, prometheus-operator-crds, plus mirrored
images in Harbor. The reconciler **enforces** Tier-A before the first tenant
release — `prechecks.host_cluster_ready()` flips the row to `error` with a
precise "missing …" message rather than attempting a doomed install (chapter
23 §23.3; bypass with `CONFIG_MANAGER_SKIP_HOST_PRECHECK=true` in dev).

## 21.5 Event bridge

`workers/nats_bridge.py` subscribes to the tenant's NV-CM NATS JetStream
(`nv-config-manager.workflow.result`, `…nautobotchange`) over the tunnel and
republishes onto the hub Redis bus as `EventEnvelope`s
(`config_manager.workflow.result` / `config_manager.device_change`). Failed
workflows surface at `warning` severity so the infra agent picks them up and
can open drift proposals — closing the loop. `nats-py` is a runtime dep
(lazily imported).

## 21.6 Footprint & sizing

A full stack is ~15–20 pods + up to four CNPG Postgres clusters per tenant.
`size_profile` (small/medium/large) maps to CNPG instance count
(`values._SIZE_TO_PG_INSTANCES`); small tenants run single-instance Postgres.
This is why the stack lives **outside** the hub (in daalu's local or the
customer's cluster) and component selection matters — the "Lean" profile is
Nautobot + Config Store + Render + Temporal.
