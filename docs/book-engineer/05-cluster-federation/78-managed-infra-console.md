# 78. The managed-infra console pattern

Customer-facing chapter: book-customer §17 ("Managed Infrastructure" →
*The cluster detail page* / *Running kubectl commands on a cluster*).

This chapter documents the per-cluster console — the page at
`/clusters/{slug}` that shows a managed cluster's overview and runs a
curated set of read-only `kubectl` commands against it over the tunnel.
It is the **first instance of a pattern** we intend to reuse for other
managed infra (Prometheus, logs, cloud), so the last section spells out
how to generalize it.

The property to preserve: **this surface is read-only and allowlisted.**
Nothing the user can do from the console can mutate a customer cluster,
and the only API paths it can reach are the ones baked into the catalog.

---

## The thirty-second tour

Three pieces:

1. **`core/kube_console.py`** — a fixed catalog of read-only commands, a
   generic `GET` executor that renders output as JSON / YAML / CLI table,
   per-cluster kubeconfig resolution, and a cluster overview builder.
2. **Three endpoints** on the existing clusters router
   (`api/routers/clusters.py`): `GET /clusters/{slug}/overview`,
   `GET /clusters/kubectl/catalog`, `POST /clusters/{slug}/kubectl`.
3. **The frontend** — `frontend/app/clusters/[slug]/page.tsx` (overview
   cards + nodes table + command runner), plus the rows on the Managed
   Infra clusters table made clickable
   (`frontend/app/clusters/page.tsx`).

Connectivity is **not new**: the console reuses the same kubeconfig the
alert-chat read tools use. The kubeconfig stored in the
`Integration(provider="kubernetes")` row points its API server at the
cluster's tunnel IP, and the daalu-api pod dials it over the WireGuard
mesh — exactly what `integration_health` already proves works with its
`list_node()` probe.

---

## Why a generic GET executor, not `kubectl exec`

We deliberately do **not** shell out to a `kubectl` binary. Instead each
catalog entry resolves to a single HTTP `GET` issued through the
kubernetes Python client's configured `ApiClient`
(`kube_console._raw_get`, `kube_console.py`). Three reasons:

- **No shell, no binary, no argument injection.** The only inputs the
  caller controls are a command *id* (validated against the catalog), an
  optional namespace, and an optional label selector — both regex-checked
  (`_NS_RE`, `_SELECTOR_RE`).
- **One code path, three faithful formats.** The API server renders all
  three output shapes via content negotiation:
  - `Accept: application/json` → dump the object (JSON) or
    `yaml.safe_dump` it (YAML);
  - `Accept: application/json;as=Table;v=v1;g=meta.k8s.io` → the server
    returns the same server-side printer columns `kubectl get` shows,
    which we align into text in `_render_table`.
- **Read-only by construction.** The executor only ever issues `GET`
  against list/get collection paths from the catalog. There is no verb,
  body, or path the user can supply that mutates state.

`_raw_get` calls `api_client.call_api(...)` with `auth_settings=["BearerToken"]`
the same way the generated client methods do, so bearer-token *and*
client-cert kubeconfigs both authenticate correctly.

---

## The command catalog

`CATALOG` is a list of `CommandSpec` (frozen dataclass) grouped by UI
section — Cluster / Workloads / Networking / Storage / Config. Each spec
carries:

- `id` / `label` / `kubectl` (the display string) / `group`;
- `path` (cluster-scoped collection) and, for namespaced resources,
  `namespaced_path` (a `{ns}` template) — `_resolve_path` picks the
  per-namespace path when a namespace is supplied, otherwise the
  all-namespaces collection;
- `supports_table` (false for `/version`) and `supports_selector`.

To **add a command**: append a `CommandSpec` with the right API path. No
other code changes — the executor, the catalog endpoint, and the
frontend grouping all read from `CATALOG`.

**Secrets are deliberately excluded** from the catalog. Even a Table
listing of secret *names* leaks more than an operator console should; the
rare "I need a value" case is covered by the Assistant's approval-gated
tools, not here.

---

## Per-cluster kubeconfig resolution

`_kube_for_cluster(db, cluster)` prefers a `kubernetes` Integration whose
`cluster_tunnel_id` matches this cluster; if none is linked it falls back
to `kube_tools._tenant_kube_or_default(tenant_id)` (today, the single
per-tenant kubeconfig). This makes the console correct now and ready for
the multi-cluster-per-tenant case later without touching the executor.

`_assert_reachable` short-circuits when the tunnel status isn't
`connected`/`degraded`, so we fail fast with a clear message instead of
hanging on a dead tunnel.

---

## Endpoints

All three live in `api/routers/clusters.py`:

- `GET /clusters/kubectl/catalog` (`current_user`) — the static
  allowlist, for rendering the checkboxes. Registered with a two-segment
  path so it never collides with `GET /clusters/{slug}`.
- `GET /clusters/{slug}/overview` (`current_tenant_id`) — server version,
  node inventory (`NodeSummary`: status, roles, kubelet version, internal
  IP, CPU/mem capacity, age), and namespace count. Degrades to
  `reachable: false` + an `error` string rather than 500-ing when the
  cluster can't be read.
- `POST /clusters/{slug}/kubectl` (`current_user`) — body is
  `{command_ids[], namespace?, label_selector?, output}`; returns one
  result per command (`ok`, `output`, `error`). `KubeConsoleError` maps to
  HTTP 400. The cluster is always loaded tenant-scoped via
  `_load_cluster`, so cross-tenant access is impossible.

Every run is logged (`logger.info("kube_console.run", ...)`) with the
tenant, actor, commands, namespace, and format — the audit trail.

---

## Frontend

- `frontend/app/clusters/[slug]/page.tsx` — the detail page. Header
  (name, status badge, tunnel IP, last handshake), overview `StatCard`s,
  `NodesTable`, and `KubectlConsole`. The console groups the catalog by
  `group`, holds a `Set` of selected ids, a namespace, a selector, and an
  output toggle, and renders each result in its own copy-able `<pre>`.
- `frontend/app/clusters/page.tsx` — the cluster rows are now clickable:
  the name links to `/clusters/{slug}` and there's an explicit **Open ›**
  affordance in the actions cell.
- `frontend/lib/api.ts` — `api.clusters.overview` / `.kubectlCatalog` /
  `.runKubectl`, plus the `ClusterOverview`, `ClusterNode`,
  `KubectlCommandSpec`, and `KubectlResult` types.

Tests: `tests/test_kube_console.py` covers catalog integrity, the
secrets-excluded invariant, path resolution, the display string, table
rendering, and the input-validation rejections — all without a live
cluster.

---

## Generalizing to other managed infra

The console is really two reusable ideas:

1. **An overview** keyed by a target identifier (here, a cluster slug).
2. **A command catalog + a format-selectable runner**: a fixed allowlist
   of read-only operations, an optional set of validated scope inputs, an
   output-format choice, and a generic executor that routes to the target
   over the right transport and renders the result.

**Status: shipped.** The metrics/logs sibling landed a week after the kube
console (commit 6f89bfe), exactly per the sequencing below:
`core/observability_console.py` implements the catalogs + executors — a
metrics catalog (targets up/down, firing alerts, build version, per-job
health, plus **one** free-form PromQL box) and a logs catalog (Loki label /
namespace inventory + LogQL recipes), family-dispatched by provider
(`family_for`, line 73) — and every connected `prometheus` / `thanos` /
`loki` integration row gets an **Open** link into
`frontend/app/observability/[provider]`. All reads go through the store's
HTTP query API; nothing mutates state.

Two transport lessons from making it work in prod (commit 985f5e7), both
easy to regress:

- **Register the Prometheus *server*, not Alertmanager.** The prometheus
  integration had been registered with the Alertmanager URL (`:9093`), which
  has no `/api/v1/query` — every PromQL panel 404'd. The right URL is the
  Prometheus server (`prometheus-operated:9090`), which backs both the
  metric console *and* alert ingestion (Alertmanager `/api/v2/alerts` when
  present, else Prometheus' own `/api/v1/alerts`).
- **`Accept-Encoding: identity` through the edge proxy.** A gzip'd store
  response reached through the WireGuard edge HTTP proxy returns a wrong
  `Content-Length`, and httpx truncates the body (`JSONDecodeError`) or
  stalls. `_headers()` (`core/observability_console.py:310-315`) forces
  identity encoding so the store returns uncompressed JSON the proxy can't
  mangle — covering the prometheus query, prometheus health, and loki paths.
  Any *new* code that dials a store through the edge proxy must do the same.

The kube console itself also gained a hard-won portability fix (PR #98):
`_raw_get` uses `_preload_content=False` and parses the raw JSON body itself,
because kubernetes-client 36 renamed `ApiClient.call_api`'s `response_type`
kwarg and broke the runner ("unexpected keyword argument") when the unpinned
image rebuild floated the client — see chapter 43 on the lockfile. And the
cluster detail page grew a **native k8s health overview** (PR #99):
`kube_console.cluster_overview` collects a cluster-wide rollup — pod-phase
breakdown + container restarts, ready/unready nodes, unbound PVCs,
deployments/daemonsets with unavailable replicas, workload counts, and a
capped symptomatic-pods list — with each collector guarded so a
forbidden/timed-out list degrades that one metric (`partial=true`) instead of
blanking the overview.

One navigation note: the standalone **Integrations** page left the sidebar
(PR #121) and is now a tab inside `/managed-infra` (`?tab=`-synced;
`/integrations` redirects) — the consoles all hang off Managed infra now.

The invariants to carry into every infra type: **read-only by
construction, allowlisted commands, validated scope inputs, secrets/values
excluded by default, and an audit log entry per run.**
