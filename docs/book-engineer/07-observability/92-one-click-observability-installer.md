# 92. The one-click observability installer (OpenSearch, Fluent Bit, Grafana)

Chapter 41 covers *connecting* an observability stack the tenant already runs:
Daalu stores a URL on an `Integration` row and dials it, through the cluster's
edge proxy when the store lives in a workload cluster. This chapter covers the
other button on the same screen — **Install into cluster** — which applies a
small, vendored stack into the tenant's cluster and then registers it, so the
Daalu-native consoles light up without the tenant writing any YAML.

Only two stores are installable. Prometheus, Thanos and Loki are
**connect-only**.

| Piece | Path |
|---|---|
| Installer | `src/daalu_automation/core/observability_install.py` (283 lines) |
| Route | `POST /observability/{provider}/install`, `api/routers/observability.py:208-236` |
| Vendored manifests | `src/daalu_automation/deploy_assets/observability/{opensearch,fluent-bit,grafana}.yaml` |
| UI | `frontend/components/managed-infra/network-view.tsx:1690` (`OBS_INSTALLABLE`), `EnvObservability` at 1692 |
| API client | `frontend/lib/api.ts:739` (`api.observability.install`) |
| Consumers | `core/observability_console.py` (OpenSearch console), `core/grafana.py` (`/dashboards`) |
| Tests | `tests/test_observability_install.py` |

Introduced in PR #139 (`64002abf`, "one-click install-into-cluster for Grafana /
OpenSearch").

---

## 92.1 What "installable" means

The whole catalog is one dict (`observability_install.py:42-53`):

| Provider | Manifests applied | URL written to the Integration row | Display name |
|---|---|---|---|
| `grafana` | `grafana.yaml` | `http://grafana.monitoring.svc.cluster.local` | "Grafana (installed by Daalu)" |
| `opensearch` | `opensearch.yaml` **+** `fluent-bit.yaml` | `http://opensearch.opensearch.svc.cluster.local:9200` | "OpenSearch (installed by Daalu)" |

The URL is **deterministic** — it is the Service the manifests create, as seen
from inside the cluster. That is what lets the installer write the integration
row in the same request, before any pod is running: there is nothing to
discover.

OpenSearch is bundled with its shipper. A search store with nothing writing to
it would show an empty console, so installing "Search & analytics" always
brings Fluent Bit along (`test_opensearch_bundle_includes_shipper` pins this).

Anything else is rejected with `ObsInstallError` → **400**: `load_manifests`
raises for an unknown provider (`observability_install.py:67-79`), and
`test_unknown_provider_rejected` checks that `prometheus` — connectable, but not
installable — is refused.

### Why the manifests live under `src/`

They are in `src/daalu_automation/deploy_assets/`, not `deploy/` or `docs/`,
because they have to be **inside the backend image** at runtime: `_ASSETS`
resolves relative to the module file (`observability_install.py:39`). The PR
description calls this out ("baked into the image, unlike docs/").

---

## 92.2 The request flow

`POST /observability/{provider}/install` requires `current_admin`
(`observability.py:213-218`). `install()` (`observability_install.py:141-211`)
does, in order:

1. **Validate and load** the provider's manifests (`load_manifests`): every
   YAML document with a `kind` becomes one dict.
2. **Find the cluster.** `_kubernetes_row` (`observability_install.py:126-138`)
   takes the tenant's **first** `kubernetes` Integration by `created_at`. No row
   → 400 "onboard the cluster first (Managed infra → Onboarding)".
3. **Get a client.** `_tenant_kube_or_default(tenant_id)`
   (`core/kube_tools.py:220`) loads that tenant's registered kubeconfig — the
   same per-tenant credential the kubectl tools and the gpu-controller use,
   pointing at the cluster's API server over the WireGuard tunnel (chapter 27).
   With no cluster selected it also picks the first row by creation order, so
   the client and the tunnel binding in step 5 refer to the same cluster.
   `KubeUnavailable` → 400.
4. **Apply** with `_apply_docs_sync` in a worker thread (§92.3).
5. **Upsert the Integration row** (`observability_install.py:171-200`):
   - new row: `provider`, `module="infra"`, the display name, status
     **`connected`**, `config={"url": …}`, and `cluster_tunnel_id` copied from
     the kubernetes row — so every later call is dialed through that cluster's
     edge proxy (chapter 41);
   - existing row: merge `{"url": …}` into the existing config (keeping any
     `api_token` a previous Grafana finalize stored), keep an existing tunnel
     binding, set status `connected`.
6. **Grafana only:** if the row has no `api_token` yet, start
   `_finalize_grafana` as a fire-and-forget task (§92.5) and return
   `finalizing: true`.

The response is `{"applied": ["Namespace/opensearch", "StatefulSet/opensearch", …],
"finalizing": bool}` (`ObsInstallOut`, `observability.py:208-210`).

**The route does not wait for pods.** It returns as soon as the API server has
accepted the objects. That matters for how the UI behaves (§92.6).

---

## 92.3 Apply semantics: create, then merge-patch

`_apply_docs_sync` (`observability_install.py:82-123`) is a deliberately small
apply loop over the dynamic client:

1. **Namespaces first.** Collect every namespace a namespaced document
   references, subtract the ones declared as `Namespace` documents, and create
   the rest, ignoring 409. This exists because `grafana.yaml` puts Grafana in
   `monitoring` without declaring that namespace — a cluster that never had
   kube-prometheus-stack wouldn't have it.
2. **Each document:** `create`; on **409 Conflict**, `patch` with
   `application/merge-patch+json`. Any other error propagates (and becomes a
   500 from the route — only `ObsInstallError` is mapped to 400).

So a re-install is idempotent: unchanged objects are merge-patched with the same
content. The module docstring names the alternative that was rejected:
server-side apply would need a minimum `kubernetes` client version; create +
merge-patch works with what the image already ships.

Two limits follow from merge-patch rather than a real apply:

- fields you *remove* from a vendored manifest are not removed from the live
  object on re-install;
- there is **no uninstall**. Nothing in the code deletes these objects. The S1-07
  episode's reset steps (`kubectl delete namespace opensearch`, the Fluent Bit
  ClusterRole/Binding, and the four Grafana objects in `monitoring`) are the
  manual teardown — note that deleting the `monitoring` namespace would take
  Prometheus and Loki with it.

The same function is reused by the config-manager controller to apply its
syslog collectors on every reconcile tick
(`config_manager_controller/collectors.py:77-100`, cached by content hash so the
30 s loop doesn't re-apply unchanged docs). Changing `_apply_docs_sync` changes
both callers.

---

## 92.4 What lands in the cluster

The manifests are **lab-grade**. Their own header comments say "for the gpu-node-1 lab"
and "lab-only". Know these before presenting the result as anything more.

### `opensearch.yaml` (namespace `opensearch`)

- `Namespace/opensearch`.
- `StatefulSet/opensearch`, 1 replica, `opensearchproject/opensearch:2.17.1`,
  `discovery.type=single-node`, **security plugin disabled**
  (`DISABLE_SECURITY_PLUGIN=true`), heap `-Xms1g -Xmx1g`, requests 200m /
  1536Mi, limit 3Gi. Readiness: `GET /_cluster/health`.
- A **privileged** `busybox` init container runs
  `sysctl -w vm.max_map_count=262144` — OpenSearch refuses to start below that.
- PVC template `data`: 20Gi, `ReadWriteOnce`,
  **`storageClassName: local-path`** (the only StorageClass on the lab cluster).
  On a cluster without `local-path` the PVC stays Pending and so does
  `opensearch-0`.
- `Deployment/opensearch-dashboards` (2.17.1, security dashboards plugin off) and
  Services `opensearch:9200`, `opensearch-dashboards:80`.

### `fluent-bit.yaml` (namespace `opensearch`)

- ServiceAccount, `ClusterRole/fluent-bit-read` (get/list/watch pods and
  namespaces) and its ClusterRoleBinding — cluster-scoped objects, so they
  survive deleting the namespace.
- `DaemonSet/fluent-bit` (`fluent/fluent-bit:3.1.9`), tolerating every taint
  (`operator: Exists`) so it runs on every node, mounting the host's `/var/log`
  read-only.
- Pipeline (ConfigMap `fluent-bit-config`): `tail` `/var/log/containers/*.log`
  with a CRI regex parser → `kubernetes` filter (pod/namespace metadata,
  `Merge_Log On`) → `es` output to the OpenSearch Service with
  `Logstash_Format On`, `Logstash_Prefix kubernetes`. The result is one index
  per day, **`kubernetes-YYYY.MM.DD`** — the pattern the console searches
  (`_OS_INDEX_PATTERN = "kubernetes-*"`, `observability_console.py:820`).

### `grafana.yaml` (namespace `monitoring`)

- `Secret/grafana-admin` with a **fixed admin credential** (`admin` /
  `daalu-grafana-lab`). The installer holds the same pair as `_GRAFANA_ADMIN`
  (`observability_install.py:57`) and uses it exactly once, to mint a token.
- `ConfigMap/grafana-datasources`, provisioning two datasources:
  Prometheus at `kube-prometheus-stack-prometheus.monitoring.svc…:9090`
  (default) and Loki at `loki-gateway.monitoring.svc…`. These are the lab's
  Service names; on a cluster where the kube-prometheus-stack release has a
  different name (chapter 80 §80.4 has the classic example) the datasources
  point at nothing. Daalu doesn't use these datasources to render dashboards
  (below), so this only affects Grafana's own UI.
- `Deployment/grafana` (`grafana/grafana:11.4.0`), readiness `GET /api/health`,
  storage on **`emptyDir`**, and `Service/grafana:80`.

Images are pulled from Docker Hub at the tags above; nothing is mirrored.

---

## 92.5 The Grafana finalizer: minting the token

`/dashboards` needs a Grafana API token on the integration row
(`core/grafana.py:142` reads `api_token`). The token can't be known at apply
time, so `_finalize_grafana` (`observability_install.py:214-283`) runs in the
background:

- Every **10 s** for up to **300 s** (`_FINALIZE_POLL_S`, `_FINALIZE_TIMEOUT_S`,
  lines 59-60) it reloads the row — returning early if the row is gone or
  already has an `api_token` (a concurrent install finished first) — resolves
  the tunnel proxy with `get_proxy_url`, and calls Grafana **through the edge
  proxy** with basic auth.
- When `GET /api/health` returns 200, it creates a service account
  `daalu-hub` with role **Admin** (`POST /api/serviceaccounts`). If Grafana
  answers 400 "already exists" (a re-install), it looks the account up by name
  instead.
- It mints a token named `daalu-hub-<6 hex>` and writes it into the row as
  `config.api_token`.
- Every exception is swallowed and retried until the deadline; on timeout it
  logs `obs_install.grafana_finalize_timeout` and stops.

Design notes and consequences:

- **Fire-and-forget on the API's event loop**
  (`asyncio.get_running_loop().create_task`, line 206). It is not a Celery task,
  so an API restart during those five minutes kills it. Re-clicking install
  restarts it, because a row without `api_token` always spawns a new finalizer.
- **The token survives re-installs** (the upsert merges config), and a re-install
  of a row that already has a token does **not** re-mint.
- **Grafana's state is on `emptyDir`.** If the Grafana pod is rescheduled, its
  database — including the `daalu-hub` service account and its token — is gone,
  while the row still holds the old token. Because the row still *has* an
  `api_token`, clicking install again won't fix it; the stale key has to be
  cleared from the integration first. The code has no detection for this.
- The service account is Grafana **Admin** because Daalu also *writes*
  dashboards back to Grafana (AI-created dashboards are POSTed to
  `/api/dashboards/db`, per the `core/grafana.py` docstring).

Grafana here is a **catalog**, not a renderer: Daalu reads dashboard definitions
from it and runs each panel's PromQL/LogQL itself against the tenant's
Prometheus/Loki integrations (`core/grafana.py:1-25`). Grafana never proxies a
data point.

---

## 92.6 The UI

The **Observability** section of an environment's detail view
(`EnvObservability`, `network-view.tsx:1692`, mounted at line 1255 inside the
Managed-infra page) shows four tiles: Metrics (`prometheus`), Logs (`loki`),
Search & analytics (`opensearch`), Dashboards (`grafana`). A tile is
"connected" when **any Integration row with that provider exists**
(`connected` is built from `/integrations/config`, line 1698). Tiles in
`OBS_INSTALLABLE` (`grafana`, `opensearch`) get an **Install into cluster**
button next to "connect an existing one"; on success the query is invalidated
and the result line reads "Installed: N objects applied", plus "finishing setup
in the background…" when `finalizing` is true.

Be precise about when the tile turns green: the row is written with status
`connected` **in the install request**, so the tile flips as soon as the API
returns — before OpenSearch or Grafana is Ready. The success message's wording
("the tile flips to connected once the store answers", line 1782) is not what
the code does. To know the store is actually up, watch the pods, open the
console (its overview calls the store and reports `reachable`), or wait for the
integration health sweep (`OpenSearchAdapter.health` probes `/_cluster/health`,
`modules/infra/integrations.py:631-656`).

---

## 92.7 How the installed stores are read

- **OpenSearch** → `/observability/opensearch`, served by
  `core/observability_console.py`. `opensearch` is a "logs"-family provider
  (`LOGS_PROVIDERS`, line 57). The overview lists non-system indices and
  aggregates `kubernetes.namespace_name.keyword` for the namespace picker
  (`opensearch_overview`, line 835); queries are fixed **panels** —
  Recent logs, Errors & exceptions, Warnings, OOM / killed, Crashes & restarts
  (`OPENSEARCH_PANELS`, line 245) — each a Lucene `query_string` on the `log`
  field, filtered by `@timestamp >= now-<since>` and optionally a namespace
  and a quoted search phrase, both regex-validated (`_run_opensearch_queries`,
  line 890). Hits render as `[namespace/pod] timestamp message`. Running
  queries requires admin, like the kubectl console.
- **Grafana** → `/dashboards`, via `core/grafana.py` and the minted token.

**What the triage agent reads.** The alert-investigation agent's log tool is
`query_loki` (`core/kube_tools.py:825`, spec at 1265); there is no OpenSearch
query tool. OpenSearch logs are for humans in the console. The copilot's
cluster summary does count OpenSearch as "logs: yes"
(`api/routers/copilot.py:221`), so don't infer agent access from that line.

---

## 92.8 Operational notes

- **Which cluster?** The tenant's *first-registered* `kubernetes` integration.
  The install route takes no cluster parameter, so a tenant with several
  clusters can't choose; make sure the oldest row is the one you mean.
- **Resource floor:** OpenSearch requests 1.5 GiB and needs a `local-path`
  StorageClass; `opensearch-0` Pending is almost always one of those two.
- **No indices yet** right after install just means Fluent Bit hasn't flushed.
- **Security posture:** OpenSearch runs with its security plugin off and Grafana
  with a known admin password. Both are reachable only inside the cluster (no
  Ingress, ClusterIP Services) and Daalu reaches them over the tunnel, but
  anything else in the cluster can reach them too. The manifests are the lab
  setup, not a hardened install.
- **Privileges:** the init container is privileged (sysctl) and Fluent Bit
  mounts host `/var/log` — clusters enforcing Pod Security "restricted" will
  reject them.

Related chapters: [41 · Onboarding observability stacks](41-onboarding-stacks-into-daalu.md),
[40 · Dashboards and alerts](40-dashboards-and-alerts.md),
[38 · Loki and Promtail](38-loki-promtail.md),
[27 · Cross-cluster API access and scraping](../05-cluster-federation/27-cross-cluster.md).
