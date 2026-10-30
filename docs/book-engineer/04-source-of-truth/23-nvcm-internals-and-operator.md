# 23. NV-CM internals & the controller (operator) integration

Chapters 20–21 cover *why* we wrap NVIDIA Config Manager and how the
network-plane executor drives it. This chapter is the deep dive the other two
defer: **what NV-CM actually is** (its services and how they fit together) and
**how daalu-automation installs and operates a per-tenant NV-CM stack** using
the Kubernetes operator pattern. Read 61 first for the responsibility split.

> **Terminology.** "NV-CM" = the upstream NVIDIA product (a Helm chart + a set
> of container images + a Python installer). "The controller" =
> daalu-automation's `config-manager-controller` service, which provisions and
> reconciles one NV-CM stack per tenant. We do **not** fork NV-CM; we vendor
> its chart, its installer (the "Deployer"), and its Nautobot bootstrap jobs,
> and we drive its REST API. See `src/nv_config_manager_installer/VENDORED.md`
> and `components/VENDORED.md`.

---

## 23.1 What NV-CM is

NV-CM is a microservice application for **intent-based network configuration**:
you store an intended switch/fabric config, it renders vendor-specific config,
diffs it against the live device, and pushes it through an approval workflow
with automatic rollback. It is **not** a single binary — it is ~12 services
plus their datastores, all installed by one Helm chart
(`deploy/charts/nv-config-manager-1.3.0/`).

### The services (chart components)

| Service | Role | Toggle (`row.components`) |
|---|---|---|
| **Render Service** | Renders intended config → vendor syntax; runs render *consumers* (per device-type / per Nautobot-change) | `render` |
| **Config Store** | Stores intended config files + serves the config API; has an in-memory **cache** sidecar deployment | `configStore` |
| **Temporal** | Workflow engine that orchestrates a deploy (stage → diff → approve → push → verify). Decomposes into `frontend`, `history` (×N), `matching`, `worker`, `scheduler`, plus an admin job and the `temporal-web` UI | `temporal` |
| **Nautobot** | The **source of truth** — inventory, IPAM, intent (Config Contexts). Bundled here so a tenant gets SoT + config management as one unit. Includes `nautobot`, `nautobot-celery`, `nautobot-celery-beat` | `nautobot` |
| **NATS** | JetStream message bus — workflow results + Nautobot change events. `nats` + `nats-box` | (always, with the above) |
| **Redis** | Cache / Celery broker for Nautobot | (always) |
| **UI** | NV-CM's own web console (optional; Daalu is the primary surface) | `ui` |
| **Gateway** | Envoy Gateway `Gateway` + `HTTPRoute`s exposing each service at `svc-*.<host>` and the human UI at `<host>` | (always) |
| **ZTP** | Zero-touch provisioning for *new* switches | `ztp` |
| **DHCP (Kea)** | Bundled DHCP for ZTP | `dhcp` |
| **CNPG Postgres** | Up to **4** CloudNativePG `Cluster`s: `cluster-config-store`, `cluster-nautobot`, `cluster-temporal`, `cluster-temporal-visibility` | (per the above) |

The **"Lean" profile** Daalu installs by default
(`values.DEFAULT_COMPONENTS`) is `render + configStore + temporal + nautobot`
(ztp / dhcp / ui off) — ~30 pods + 4 Postgres clusters. `size_profile`
(small/medium/large) maps to the CNPG instance count
(`values._SIZE_TO_PG_INSTANCES`: 1/2/3).

### How a deploy flows through the services

1. The hub stages intended config into **Config Store** (`POST /v1/config/…`).
2. The hub starts a **Temporal** `DeployWorkflow` (`POST /v1/workflow/ngc/deploy`).
3. Temporal calls **Render Service** to produce vendor config, computes a diff,
   and parks at a `perform_configuration_diff` **approval** stage.
4. The hub (pre-approved — the human already approved in Daalu's UI) approves;
   Temporal pushes to the device and verifies, with commit-confirm rollback.
5. Results land on **NATS**; `workers/nats_bridge.py` republishes them onto the
   hub Redis bus (§21.5).

**Nautobot** underpins all of it: devices, intended Config Contexts
(`daalu_intent`), and observed snapshots (`daalu_observed`) all live there, and
the SoT reconciler (chapter 14) reads/writes it via `core/sot/nautobot.py`.

---

## 23.2 The integration shape: a Kubernetes operator

Each tenant's NV-CM stack is a **managed resource**. Daalu manages it with the
classic operator pattern, hand-rolled (no CRD/kubebuilder — the desired state
lives in a Postgres row, not an etcd CRD):

```
  desired state         controller (reconcile loop)        actual state
  ─────────────         ──────────────────────────         ────────────
  config_manager_   ─►  config-manager-controller     ─►   Helm release
  tenants row            every 30s: row → cluster            "cm-<id>" in
  (state, components,                                        namespace
   size, hostname, …)   ◄─ writes back state/urls ◄──        cm-<slug>
```

- **Desired state** = a `ConfigManagerTenant` row
  (`models/config_manager_tenant.py`, table `config_manager_tenants`). It is
  the operator's "spec + status" in one row: `components`, `size_profile`,
  `base_hostname`, `target_cluster_tunnel_id` on the spec side; `state`,
  `urls`, `last_error`, `last_ready_at` on the status side.
- **The controller** = `src/daalu_automation/config_manager_controller/`, its
  own ArgoCD-managed Deployment in `daalu-automation`, isolated from the hub
  API so a controller bug can't wedge the SaaS (`deploy/argocd/applications/
  config-manager-controller.yaml`). It exposes
  `POST/GET/DELETE /tenants/{tenant_id}` (service-token-JWT authed, purpose
  `config-manager-provision`) and runs a **30 s reconcile loop**
  (`app.py::_reconcile_loop` → `_reconcile_one`).
- **Actual state** = a Helm release `cm-<tenant_id.hex[:12]>` in namespace
  `cm-<slug>` on the target cluster (local hub cluster, or a customer cluster
  reached over the WireGuard tunnel when `target_cluster_tunnel_id` is set).

### The state machine (`_reconcile_one`)

```
        pending ──┐
        error  ──┼─► provisioning ──► active        (provision succeeded)
                 │        │
   (every tick)  │        └─────────► error          (precheck / install failed)
                 │
        active ──┘   ← re-driven every tick (idempotent re-converge)

        deleting ─────────► destroyed                (teardown)
```

`pending`/`error`/`provisioning`/`active` all run the provision path each tick;
`deleting` runs teardown. The loop reads every row where `state != destroyed`.
On success it writes `urls` (`values.compute_urls(base_hostname)`) and flips to
`active`; the onboarding route copies the machine URLs onto
`Integration(provider="config_manager")`. **Provisioning is idempotent** — a
re-tick re-converges (this is what makes it self-healing, and also what made
the loader-PVC contention a recurring failure rather than a one-off).

### The provision request flow (UI → controller → cluster) — async since PR #114

1. Operator/user hits `POST /onboarding/config-manager/provision`
   (`api/routers/onboarding.py:819`) with `{components, size_profile,
   base_hostname?, target_cluster_tunnel_id?, admin_username?,
   admin_password?}`. The route returns **202 as soon as the controller
   accepts the spec** (`start_provision_via_config_manager_controller`,
   `core/configmgr/provision.py:70`). It used to hold the request open for up
   to 600 s while the edge proxy in front of the API cuts idle connections at
   ~100 s — so **every** install, including successful ones, surfaced in the
   browser as `Failed to fetch` before a single pod appeared. (The GPU flow
   hit the identical trap first — PR #107.)
2. The controller resolves the target tunnel (auto-select — chapter 21 §21.1),
   validates `chart_version` against the on-disk vendored charts
   (`app.py:215` `_validate_chart_version` — it was previously string-joined
   straight into a filesystem path), upserts the row, and the 30 s reconcile
   loop provisions it → `active`.
3. The UI polls `GET /onboarding/config-manager/progress`
   (`onboarding.py:990` → controller `GET /tenants/{id}/progress`,
   `app.py:622` `_progress_payload`): row state + pods with ready counts and
   blocking reasons + notable namespace events + `last_error`, read through
   whichever cluster the tenant targets. Pods in `Init:*` are styled as
   progress, not failure — they sit `Pending` for minutes on a first install.
4. The `Integration(provider="config_manager")` row is written by **the first
   progress poll that observes `active`**
   (`onboarding.py:876` `_upsert_config_manager_integration`) — not by the
   request that started the install, which no longer waits. That also makes
   the integration converge after an API restart mid-install, which a
   background task would not. It carries the resolved `config_store_url` /
   `render_url` / `workflow_url` / `nautobot_url` / `ui_url`, the
   `gateway_internal_url`, the **bundled-Nautobot token** (below), and binds
   `cluster_tunnel_id` to the tunnel actually installed through. From here
   the SoT adapter (§20.6) and the network executor (§21.3) use that
   integration.

### The bundled-Nautobot token is mirrored automatically (PR #121)

The chart generates a `nautobot-token` Secret at install. The controller
**mirrors it onto the row** — encrypted into the environment's secrets bag
(`app.py:691` `_stamp_nautobot_token`) — and reports the ciphertext in the
progress payload; the hub's integration upsert persists it as
`nautobot_token_ciphertext`. This closed the "populated in a follow-up step"
TODO that had left the SoT credential loader with no token to use (chapter 15
has the full story, including why pasting a `*.cmtools.example.com` URL into the
BYO wizard can never work). `_stamp_gateway_url` (`app.py:669`) does the same
for the Envoy proxy Service address.

### In-app chart upgrades (PR #112)

`chart_version` was persisted but exposed nowhere. The controller view now
carries it alongside the set of on-disk vendored charts,
`latest_chart_version` and an `upgrade_available` flag (`app.py:122-126`) —
ordering is **semver-aware**, not lexicographic, because the live pins span a
prerelease (`1.2.2-rc.23`) and a release (`1.3.0`) and string compare gets
pairs like `1.9.0` vs `1.10.0` backwards. `POST /tenants/{id}/upgrade` (and
the hub's `POST /onboarding/config-manager/upgrade`, `onboarding.py:1111`)
schedule the bump. Two deliberate details:

- the upgrade **resets the row to `pending`**, because the reconciler
  short-circuits an `active` tenant whose release is already `deployed` —
  bumping the version column alone would never roll anything out;
- the hub route returns **202** rather than blocking — a full NV-CM rollout
  outlives the edge proxy's ~100 s ceiling (same trap as provisioning).

---

## 23.3 How the stack is installed: the vendored Deployer

> This is the part that changed most since chapters 20–21 were written. The
> controller no longer `helm upgrade --install`s the chart directly
> (`HelmRunner`, legacy). It drives NVIDIA's **Deployer** (the engine behind
> their installer CLI/TUI), gated by `CONFIG_MANAGER_USE_DEPLOYER=true`
> (**on in prod**). `app.py::_reconcile_one` branches on the flag:
> `DeployerRunner` when on, `HelmRunner` when off.

### Why the Deployer (the secret-assembler deadlock)

A bare `helm upgrade --install` of the chart **deadlocks**: the chart's
`secret-assembler` *pre-install hook* blocks waiting for secrets
(`redis-password`, `nautobot-token`, `device-creds`, `cluster-*-app`,
`nats-*`) that are only minted *later in the same release*. The hook never
completes → helm never proceeds → the secrets are never minted. The upstream
**Deployer pre-creates those secrets (and the namespace) before helm runs**,
breaking the cycle. Adopting it is the entire reason `nv_config_manager_installer`
is vendored.

### The three vendored pieces

| Vendored | Path | Purpose |
|---|---|---|
| **Installer ("Deployer")** | `src/nv_config_manager_installer/` | The engine: pre-creates secrets, renders values, uploads PVC content, runs helm. Apache-2.0, minus the Textual `tui/`. |
| **Helm chart** | `deploy/charts/nv-config-manager-1.3.0/` | The actual k8s manifests. |
| **Bootstrap jobs** | `components/nautobot/nv_config_manager_jobs/` | The Nautobot migration runner + seed data the Deployer uploads into each tenant's `nautobot-custom-jobs` PVC. |

All three are pinned to the **same upstream tag** (chart `1.3.0`,
upstream `@ 7455da6`). Re-vendor them together — the migration runner must
match the chart schema.

### Driving the Deployer from the async controller

`config_manager_controller/deployer_runner.py`:

- `DeployerRunner.upgrade_install(row, kubeconfig)` builds the upstream config
  (below), then runs the **synchronous, blocking** Deployer in a worker thread
  (`asyncio.to_thread`) under a module-level `asyncio.Lock` — the Deployer
  mutates `os.environ` and the process CWD, so two reconciles must never run it
  concurrently.
- `_DaaluDeployer(Deployer)` is a thin subclass that injects the handful of
  Daalu-specific behaviours the upstream config can't express. **This is where
  Daalu customization lives — we never hand-edit the vendored Deployer.** It
  overrides:
  - `_generate_values()` — layers a values overlay onto what the upstream
    wrote: the shared `envoy-gateway` GatewayClass
    (`createGatewayClass=false`, see §21.4) and a **`fullnameOverride`** =
    release name (so object names fit the 63-char k8s limit — e.g. the
    temporal-headless-Service overflow).
  - `_patch_gateway()` — skipped; the shared Envoy is traefik-fronted, not a
    local-dev NodePort.
  - `_setup_jobs_pvc()` / `_setup_templates_pvc()` — pin the upload "loader"
    pod to the node already holding the RWO PVC, avoiding a
    cross-node multi-attach error on re-runs.
- `_KubeconfigEnv` — writes the tunnel kubeconfig to **both** `$KUBECONFIG`
  (for helm/kubectl subprocesses) **and** `~/.kube/config` (for the Deployer's
  embedded Python `kubernetes` client, which ignores `$KUBECONFIG`).
- `uninstall()` — teardown is helm-native (`helm uninstall`) + a namespace
  sweep, since the Deployer pre-creates secrets/CNPG objects helm doesn't own.

### Row → upstream config (`deployer_config.py`)

`build_install_config(row)` is the single, pure translation point from a
`ConfigManagerTenant` row to the upstream `NVConfigManagerInstallConfig`
(the same Pydantic model the upstream CLI/TUI produce):

- **cluster**: `hostname`, `namespace`, `release_name = cm-<hex[:12]>`,
  `size`, `environment`, **`airgapped=True`** (subcharts are vendored, so skip
  `helm dependency update`, which would hit public repos).
- **secrets**: always `SecretsMethod.KUBERNETES` — the whole point (no
  ESO/Vault); the Deployer pre-creates them in-cluster.
- **services**: toggled from `row.components` via `_COMPONENT_TO_SERVICE`
  (Daalu's `ui` toggle has no upstream equivalent and is dropped — the chart UI
  rides config-store).
- **images**: the chart has **no `global.imageRegistry`** knob (a common
  misconception — see the correction in chapter 21). Instead each
  `global.images.<key>.repository` is repointed at the Harbor mirror
  (`<harbor>/nv-config-manager/<basename>`) for exactly the keys in
  `values.NV_CONFIG_MANAGER_IMAGE_KEYS`.
- **sso**: bearer-only OIDC — issuer + audiences from the Keycloak settings;
  no interactive login wired (the hub presents its own service JWTs, §21.2).
- **infrastructure**: `tls=True` (cert-manager issues per-tenant gateway
  certs); LoadBalancer stays `NONE` (traefik fronts the shared gateway).

### Tier-A precheck

Before installing, `prechecks.host_cluster_ready(kubeconfig)` enforces that the
host cluster has the cluster-scoped singletons NV-CM needs — Envoy Gateway
(GatewayClass CRDs), cert-manager (+ ClusterIssuer support), CloudNativePG —
and flips the row to `error` with a precise "missing …" message instead of
attempting a doomed install. It **detects** but does not **install** them
(one-time operator setup). Bypass with
`CONFIG_MANAGER_SKIP_HOST_PRECHECK=true` (dev only).

---

## 23.4 Multi-tenancy & the one shared cluster-scoped object

The chart is namespace-safe **except** the cluster-scoped `GatewayClass`.
Resolution (mirrored in both `values.render_values` and the Deployer overlay):
install **one** shared `envoy-gateway` GatewayClass in Tier-A and set
`createGatewayClass=false` + `className=envoy-gateway` on every tenant release,
each with a unique `base_hostname`. CNPG `Cluster`s, Issuers, HTTPRoutes,
SecurityPolicies, Secrets, and PVCs are all namespaced — no cross-tenant
collision. Per-tenant hostnames: `<slug>.cm.example.com` (UI) and
`svc-*.<slug>.cm.example.com` (machine APIs).

---

## 23.5 Settings reference

Controller/hub settings (`config.py`, all `config_manager_*` / `keycloak_*`):

| Setting | Default | Purpose |
|---|---|---|
| `config_manager_controller_url` | "" | In-cluster URL of the controller (hub side) |
| `config_manager_use_deployer` | `false` (prod: **true**) | Deployer vs legacy HelmRunner |
| `config_manager_chart_dir` | `deploy/charts` | Vendored chart dir (resolved against `/app`) |
| `config_manager_default_chart_version` | `1.3.0` | Pinned chart version |
| `config_manager_harbor_registry` | "" | Harbor mirror for image overrides |
| `config_manager_image_pull_secret` | "" | Optional imagePullSecret name |
| `config_manager_skip_host_precheck` | `false` | Skip Tier-A precheck (dev) |
| `keycloak_issuer_url` | "" | OIDC issuer the hub trusts for `svc-*` |
| `keycloak_token_url` | "" | Explicit token endpoint (derived if empty) |
| `keycloak_token_audience` | `nv-config-manager` | Audience in machine JWTs (== NV-CM `oidc.audiences`) |

---

## 23.6 Tests

`tests/config_manager_controller/`:

- `test_nvcm_vendor_import.py` — the vendored Deployer import chain loads, the
  install-config schema defaults to `SecretsMethod.KUBERNETES`, the TUI is
  *not* vendored. (Skipped < py3.11 — the vendored package uses `StrEnum`; CI
  on 3.11 is the real gate.)
- `test_config_manager_deployer_config.py` — the row → `NVConfigManagerInstallConfig`
  mapper (stable release name, secrets always kubernetes, component→service
  toggles, Harbor image overrides, OIDC).
- `test_config_manager_deployer_runner.py` — the gateway/fullname overlays,
  `_deep_merge`, and the uninstall helm/kubectl argv (via an injected
  `CommandRunner`, no real cluster).

---

## 23.7 Relationship to the legacy `nautobot-controller`

There are two tenant controllers in the tree. **`config-manager-controller`**
(this chapter) provisions the full NV-CM stack, which **includes its own
Nautobot** — so for a `config_manager_enabled` tenant the bundled Nautobot is
the SoT and **no separate `nautobot-controller` provision is needed**. The
older **`nautobot-controller`** (chapter 19, per-tenant standalone Nautobot)
remains only as a fallback for tenants that want Nautobot without the rest of
NV-CM; `core/sot/nautobot.py::_load_credentials` prefers the NV-CM-bundled
Nautobot and falls back to the standalone one. New installs should use NV-CM.

---

## 23.8 Where the tenant gateway gets its cmtools shape

The Deployer overlay this chapter describes (`_daalu_values_overlay`,
`_generate_values`) is also where each tenant Envoy gateway is rendered for
**hub-proxied tool SSO**: flat per-component hostnames, an HTTP-only listener,
the in-cluster `oidc.jwksUri`, and `oidc.interactiveRedirect=false`. The auth
plane that consumes that gateway — the `*.cmtools.example.com` tool proxy, the
per-user Keycloak token-exchange, and the `SecurityPolicy` itself — is
[Part XIII — Auth integrations](../13-auth-integrations/README.md), chapters
[68](../13-auth-integrations/68-nvcm-tool-sso-through-the-hub.md) (§68.6–68.7
for the gateway values) and [70](../13-auth-integrations/70-keycloak-realm-clients-and-provisioning.md)
(the realm clients). This chapter owns the operator/Deployer mechanics; Part
XIII owns the auth semantics.

---

## 23.9 Named, multi-environment management stacks (PR #117)

A customer can now run **several independently-named management environments**
("Production", "Staging", …), each its own Helm release / namespace / set of
hostnames. `config_manager_tenants` went from one-row-per-customer
(`uq_config_manager_tenants_tenant_id`) to one-row-per-**(customer,
environment)** with `name`, `slug`, and `is_primary`
(`models/config_manager_tenant.py:59-93`; migration
`migrations/versions/0030_config_manager_environments.py`).

### What keeps every pre-environment caller working: `is_primary`

The four consumers that resolve "the tenant's config manager" — the tool
proxy, the SoT client, the NATS bridge, and the onboarding writer — all used
`.scalar_one_or_none()`, which **raises** on more than one row. They now
resolve the **primary** instead (`_find_row`, `app.py:563`), and a partial
unique index (`uq_config_manager_tenants_primary`, `WHERE is_primary`)
enforces at most one primary per customer. The first environment a tenant
creates is marked primary. Existing rows kept their namespace and hostname,
backfilled with a slug derived from the hostname they already serve — live
URLs did not move.

### The naming rules are load-bearing

The slug (≤24 chars, immutable — `config_manager_controller/env_slug.py`)
lands in a **namespace and a public DNS label**, so everything stays inside a
*single* label: new environments get namespace `cm-<tenant>-<env>` and host
`<env>-<tenant>.<domain>`. A dotted scheme would break the
`*.cmtools.example.com` wildcard cert, the wildcard DNS record *and* the ingress
host rule at once — a wildcard matches exactly one label. And a slug may not
start with a **tool prefix** (`nautobot-`, `render-`, …): the tool proxy
strips those and treats the remainder as the environment, so a `render-prod`
environment's own browser host would resolve as "the render tool of
environment `prod`" and silently hit the wrong service. Immutability makes
create-time the only enforcement point; the server's 400/409 messages are
written to be shown verbatim, and the browser deliberately does **not**
duplicate the reserved-prefix rule (two copies would drift).

### Host routing

The tool proxy resolves a whole label **as a tenant slug first** — keeping
legacy hosts stable and preventing a later environment from hijacking an
existing tenant's host — otherwise it splits `<env>-<tenant>` preferring the
**longest tenant match**, since a tenant slug is globally unique while an
environment slug is only unique within its tenant. The ownership check stays
immediately after resolution, before anything is read on the tenant's behalf.

### `gateway_internal_url` moved onto the row (migration 0031)

It previously lived only in `Integration.config`, and there is exactly **one**
Integration per tenant (tracking the primary) — a second environment had
nowhere to publish its gateway. Migration
`0031_config_manager_gateway_url.py` adds it to the row (additive, nullable,
seeded from the existing Integration for primaries). A non-primary
environment deliberately does **not** fall back to the Integration's URL:
that would forward a secondary environment's traffic — writes included — into
the primary's stack. It fails closed instead.

> CI runs no Postgres and never invokes alembic; 0030's backfill and all four
> constraint invariants (second environment allowed, duplicate slug rejected,
> second primary rejected, same slug under another customer allowed) were
> verified by hand against a scratch database. Keep doing that for future
> constraint migrations.

### The UI surface (PRs #115/#116/#117)

The Network & servers page is a list of **environment tiles** you click into
(selection is local state, not a route push — the page mounts inside the
Managed-infra tab shell). Each detail view shows live install progress
(pods, ready counts, blocking reasons, events) and, once up, a completion
state with link cards to the provisioned consoles plus Daalu's Topology and
Operations. The create form takes a **Name** with a live identifier preview
(it becomes the namespace and public hostnames, so the form says plainly it
cannot be changed); members don't see a create form that would only 403.
`latest_chart_version` + `components` pass through the environment view so a
tile can offer a concrete upgrade target and per-component counts for
non-primary environments.

All user-visible copy is **capability-based** (PR #115): "Network management
stack", "Workflow engine", "Source of truth", "DHCP" — no upstream project is
named anywhere a customer can see, including the `Integration.name` written at
provision time. Component *keys* are unchanged (they are the chart's
contract, not display text); code comments still name the upstream and are
stripped from the production bundle. One resolver gotcha fixed by PR #135:
the env card resolves console URLs from the environment row / progress
payload first, where `compute_urls()` stores the config-browser UI under the
bare `ui` key — the tile resolver must accept that spelling, not only the
integration-config `ui_human`/`ui_url` ones.
