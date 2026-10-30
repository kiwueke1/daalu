# Google Cloud Platform integration

This guide walks through connecting a Google Cloud project to Daalu
so the alert-chat agent can pull Compute Engine state, Cloud
Logging entries, Cloud Monitoring metrics, Cloud SQL state, and
Cloud Functions config when an incident touches GCP-hosted
infrastructure. The integration is **read-only in v1** — no
resources are modified, no agents are installed on the tenant's
side, and all calls go outbound from the operator's `daalu-api`
pod over HTTPS to `*.googleapis.com`.

Contents:

1. [What gets collected](#1-what-gets-collected)
2. [Auth model](#2-auth-model)
3. [Operator prerequisites](#3-operator-prerequisites)
4. [Tenant prerequisites](#4-tenant-prerequisites)
5. [Setting it up in the UI](#5-setting-it-up-in-the-ui)
6. [Setting it up via the API](#6-setting-it-up-via-the-api)
7. [Field reference](#7-field-reference)
8. [How signals are collected](#8-how-signals-are-collected)
9. [How tool calls flow through the system](#9-how-tool-calls-flow-through-the-system)
10. [Distributed traces (Cloud Trace) — roadmap](#10-distributed-traces-cloud-trace--roadmap)
11. [Verifying the integration](#11-verifying-the-integration)
12. [Troubleshooting](#12-troubleshooting)
13. [Security considerations](#13-security-considerations)
14. [Roadmap — write tools](#14-roadmap--write-tools)

---

## 1. What gets collected

After onboarding, the alert-chat agent gains access to five GCP
read tools. Each maps to one signal type and surfaces in the
matching evidence tile in the per-alert UI.

| Tool | Signal | Underlying GCP API | Evidence tile |
|---|---|---|---|
| `gcp_list_instances` | Compute Engine VM state | `compute.instances.list` (aggregated across zones, or scoped to one zone) | Cloud instances |
| `gcp_describe_sql_instance` | Cloud SQL instance state | Cloud SQL Admin v1beta4 `instances.get` (via the `google-api-python-client` discovery client) | Cloud databases |
| `gcp_describe_function` | Cloud Functions (gen2) state | `cloudfunctions.functions.get` | Serverless functions |
| `gcp_query_logging` | Application / service logs | Cloud Logging `entries.list` with advanced-filter syntax | Logs |
| `gcp_query_monitoring` | Metrics (latency / error rate / saturation / anything in Cloud Monitoring) | Cloud Monitoring `timeSeries.list` | Metrics |

Distributed traces (Cloud Trace) are on the roadmap — see §10.

The agent picks tools based on the alert payload. For an alert
that mentions a VM name it pulls `gcp_list_instances` with a
filter. For a Cloud Functions alert it pulls `gcp_describe_function`
plus a `gcp_query_monitoring` call on
`cloudfunctions.googleapis.com/function/execution_count` or
`/error_count`. For a Cloud SQL alert it combines
`gcp_describe_sql_instance` with the matching
`cloudsql.googleapis.com/database/cpu/utilization` metric.

## 2. Auth model

Daalu authenticates to GCP using a **service-account JSON key**.
The tenant creates a service account in their GCP project, binds
the read-only roles in §4.2, generates a JSON key, and pastes the
full JSON blob into the wizard. The operator's API pod builds a
`google.oauth2.service_account.Credentials` object from the JSON,
caches it per tenant, and passes it to every typed-SDK client.

Workload Identity Federation (no static keys) is on the roadmap;
v1 ships with the static-key path because it's simpler and matches
the AWS/Azure flows. Either way, the key never leaves the operator
pod's process memory in cleartext, and the `Credentials` object
auto-refreshes the access token when it's about to expire.

## 3. Operator prerequisites

These are things the team running Daalu does once, not per tenant.

### 3.1 Ship the GCP SDKs in the API image

These deps are declared in `pyproject.toml`:

```
google-cloud-compute>=1.19.0
google-cloud-logging>=3.10.0
google-cloud-monitoring>=2.22.0
google-cloud-functions>=1.16.0
google-api-python-client>=2.140.0
```

Verify they shipped:

```bash
kubectl -n daalu-automation exec deploy/daalu-api -- python -c \
  "import google.cloud.compute_v1, google.cloud.logging, google.cloud.monitoring_v3, google.cloud.functions_v2; \
   from googleapiclient.discovery import build; print('ok')"
```

If you see `ImportError`, the image hasn't rolled — re-run
`build-images` and `install.sh`.

### 3.2 Allow egress to GCP endpoints

Cloud read tools dial out from the `daalu-api` pod to
`*.googleapis.com`. If your cluster applies a NetworkPolicy that
restricts egress, allowlist `*.googleapis.com` on TCP/443 for the
`daalu-api` Deployment.

Verify connectivity:

```bash
kubectl -n daalu-automation exec deploy/daalu-api -- python -c \
  "import urllib.request; \
   urllib.request.urlopen('https://compute.googleapis.com/', timeout=5)" \
  && echo "egress OK"
```

A connect timeout means egress is blocked. The HTTP response itself
will be 401 — that's expected (no auth header), the point is that
the TCP+TLS handshake completes.

### 3.3 (Optional) Service-account key lifecycle

GCP service-account keys don't expire by default, but tenants with
strict rotation policies often disable / re-create keys on a 90-day
cadence. Tell the tenant they can re-run the wizard step (or hit
`PUT /api/v1/integrations/config/gcp`) with the new key JSON and
the client cache picks up the change on the next tool call.

## 4. Tenant prerequisites

The tenant — the team whose GCP project you're connecting — needs
to do this in the GCP console (or via `gcloud`).

### 4.1 Create the service account

```bash
PROJECT=acme-prod
SA_NAME=daalu-remediation

gcloud iam service-accounts create "$SA_NAME" \
  --project="$PROJECT" \
  --display-name="Daalu Automation read-only"

SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
echo "Service account: $SA"
```

### 4.2 Grant the read-only roles

```bash
PROJECT=acme-prod
SA="daalu-remediation@${PROJECT}.iam.gserviceaccount.com"

for ROLE in \
  roles/compute.viewer \
  roles/logging.viewer \
  roles/monitoring.viewer \
  roles/cloudsql.viewer \
  roles/cloudfunctions.viewer
do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${SA}" \
    --role="$ROLE"
done
```

Role rationale:

| Role | Used by |
|---|---|
| `roles/compute.viewer` | `gcp_list_instances`, the wizard test probe (which calls `compute.projects.get`) |
| `roles/logging.viewer` | `gcp_query_logging` |
| `roles/monitoring.viewer` | `gcp_query_monitoring` |
| `roles/cloudsql.viewer` | `gcp_describe_sql_instance` |
| `roles/cloudfunctions.viewer` | `gcp_describe_function` |

If your security team prefers role consolidation, `roles/viewer`
at project scope covers everything above plus access to every
other GCP API in the project — broader than necessary but easier
to audit.

### 4.3 Generate a JSON key

```bash
PROJECT=acme-prod
SA="daalu-remediation@${PROJECT}.iam.gserviceaccount.com"

gcloud iam service-accounts keys create /tmp/daalu-remediation.json \
  --iam-account="$SA"

cat /tmp/daalu-remediation.json
# Paste the whole JSON into the wizard's "Service-account key JSON" field.
# Delete the local file after copying — the key is sensitive.
```

The JSON has this shape:

```json
{
  "type": "service_account",
  "project_id": "acme-prod",
  "private_key_id": "...",
  "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
  "client_email": "daalu-remediation@acme-prod.iam.gserviceaccount.com",
  "client_id": "...",
  "auth_uri": "https://accounts.google.com/o/oauth2/auth",
  "token_uri": "https://oauth2.googleapis.com/token",
  "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
  "client_x509_cert_url": "...",
  "universe_domain": "googleapis.com"
}
```

The wizard accepts either the full JSON or a JSON-encoded string;
internally the backend parses it with `json.loads` before handing
it to `google.oauth2.service_account.Credentials.from_service_account_info`.

### 4.4 Note the project ID

The wizard requires a project ID separately from the JSON key.
Even though the JSON contains a `project_id` field, the typed SDK
clients don't reliably infer it across versions — we make it
explicit to avoid surprises.

## 5. Setting it up in the UI

1. Open `https://<your-daalu>/onboarding` and sign in with a
   tenant-admin account.
2. The wizard's Welcome screen lists every integration. Click
   **Begin** to walk through them in order, or **Manage
   individually** in the top right and pick **Google Cloud
   project** from the catalog.
3. On the **Google Cloud project** step:

   - Toggle the step from **Skipped** to **Enabled**.
   - **Project ID** — e.g. `acme-prod`. This becomes the implicit
     project scope for every tool call.
   - **Service-account key JSON** — paste the full JSON downloaded
     in §4.3. The textarea handles multi-line input; trailing
     whitespace is trimmed. The key remains in the textarea after
     save (other secret fields are masked because they're scalar
     strings; the JSON one needs to round-trip the whole blob so
     edits work).

4. Click **Test connection**. The wizard fires
   `POST /api/v1/onboarding/test/gcp` against the entered values;
   the backend parses the JSON, builds Credentials, and calls
   `compute.projects().get(project=…)`. Outcomes:

   - **Green badge** — `GCP accepted; project metadata fetched`.
     The project ID resolved, and the SA can read project
     metadata (so `roles/compute.viewer` is bound).
   - **Red badge** — the exact error from the API. Common
     failures:
     - `service_account_json and project_id are required` — one of
       the fields is empty.
     - `Expecting value: line 1 column 1 (char 0)` — the JSON blob
       didn't parse. Re-paste; make sure you copied the whole
       file, not just one field.
     - `403 The caller does not have permission` — the SA doesn't
       have `roles/compute.viewer` on the project. Run §4.2.

5. Click **Save & continue**. The wizard fires
   `PUT /api/v1/integrations/config/gcp`; the row is upserted into
   `integrations` keyed by `(tenant_id, provider='gcp')`.

6. Re-running the wizard later loads the saved row back into the
   form (the JSON key is shown in clear in the textarea — anyone
   with tenant-admin rights can read it back out; treat the
   integrations page as sensitive accordingly).

## 6. Setting it up via the API

Skip the UI entirely:

```bash
# Paste the SA key into a heredoc to preserve newlines:
SA_KEY=$(cat /tmp/daalu-remediation.json)

curl -fsS -X PUT https://<your-daalu>/api/v1/integrations/config/gcp \
  -H "Authorization: Bearer $TENANT_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(jq -nc --arg p acme-prod --arg k "$SA_KEY" \
    '{config: {project_id: $p, service_account_json: $k}}')"
```

Pre-flight without saving:

```bash
curl -fsS -X POST https://<your-daalu>/api/v1/onboarding/test/gcp \
  -H "Authorization: Bearer $TENANT_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(jq -nc --arg p acme-prod --arg k "$SA_KEY" \
    '{config: {project_id: $p, service_account_json: $k}}')"
```

## 7. Field reference

| Field | Key in `config` | Type | Required | Notes |
|---|---|---|---|---|
| Project ID | `project_id` | string | yes | The numeric project number works too, but the alphanumeric project ID is what most operators recognise. |
| Service-account key JSON | `service_account_json` | string | yes | The full JSON blob from §4.3, stored as a string in JSONB. The backend re-parses on every credential build. |

The `integrations` row stored for a populated config looks like:

```json
{
  "id": "...",
  "tenant_id": "...",
  "provider": "gcp",
  "module": "infra",
  "name": "Google Cloud project",
  "status": "connected",
  "config": {
    "project_id": "acme-prod",
    "service_account_json": "{\"type\":\"service_account\",\"project_id\":\"acme-prod\",...}"
  }
}
```

## 8. How signals are collected

### 8.1 Compute Engine VMs

`gcp_list_instances` calls the Compute Engine v1
`InstancesClient`. Inputs:

```python
{
  "zone": "us-central1-a",       # optional, scopes to one zone
  "filter_expr": "labels.env=prod"  # optional, GCP filter syntax
}
```

When `zone` is omitted, the handler uses `aggregated_list` to scan
every zone in the project in one call — useful when an alert
references a VM by name but you don't know which zone it's in.
When `zone` is set, it scopes to one zone, which is cheaper if you
already know.

Returned JSON per instance includes `name`, `status` (RUNNING /
TERMINATED / STOPPING), `zone`, `machineType` (last segment of the
URL — e.g. `n2-standard-4`), `networkIP`, `externalIP`,
`creationTimestamp`, and `labels`.

### 8.2 Cloud SQL state

`gcp_describe_sql_instance` calls the Cloud SQL Admin v1beta4 API
via the discovery client (typed SDK split across packages):

```python
{
  "instance": "prod-db"  # the Cloud SQL instance ID
}
```

Output is the full instance resource — see
[`sqladmin.instances.get`](https://cloud.google.com/sql/docs/postgres/admin-api/rest/v1beta4/instances/get)
for the schema. The agent typically reads `settings.tier`,
`state` (RUNNABLE / SUSPENDED / FAILED), and
`databaseInstalledVersion` for triage.

### 8.3 Cloud Functions (gen2)

`gcp_describe_function` calls
`cloudfunctions_v2.FunctionServiceClient.get_function`:

```python
{
  "function_name": "checkout-on-order",   # required
  "region": "us-central1"                  # required
}
```

Output is slimmed to `name`, `state` (ACTIVE / FAILED / DEPLOYING),
`buildConfig.runtime`, `buildConfig.entryPoint`,
`serviceConfig.uri`, `serviceConfig.availableMemory`,
`serviceConfig.timeoutSeconds`,
`serviceConfig.minInstanceCount`,
`serviceConfig.maxInstanceCount`, and `updateTime`. Pair with
`gcp_query_monitoring` on `cloudfunctions.googleapis.com/function/error_count`
for "is this function failing right now?".

### 8.4 Cloud Logging

`gcp_query_logging` wraps
`google.cloud.logging.Client.list_entries`. Inputs:

```python
{
  "filter_expr": "resource.type=\"gce_instance\" severity>=ERROR AND \"connection refused\"",
  "since_minutes": 60,    # optional, default 60
  "limit": 100            # optional, max 1000
}
```

The filter uses the **advanced filter language** documented at
[Logging advanced
queries](https://cloud.google.com/logging/docs/view/advanced-queries) —
the same syntax the Cloud Console's Logs Explorer accepts. The
handler appends `timestamp>="…"` / `timestamp<="…"` automatically
based on `since_minutes`, so don't include timestamp filters in
your input.

Output is one line per log entry, `<ISO timestamp>  [<severity>]
<payload>`, sorted newest-first.

### 8.5 Cloud Monitoring

`gcp_query_monitoring` calls
`monitoring_v3.MetricServiceClient.list_time_series`. Inputs:

```python
{
  "metric_type": "compute.googleapis.com/instance/cpu/utilization",
  "filter_expr": "resource.label.instance_name=\"api-prod-1\"",  # optional, extra filter clauses
  "since_minutes": 60
}
```

The `metric_type` is the fully-qualified Cloud Monitoring metric
descriptor — common ones:

| Service | Metric |
|---|---|
| Compute Engine | `compute.googleapis.com/instance/cpu/utilization` |
| Compute Engine | `compute.googleapis.com/instance/network/received_bytes_count` |
| Cloud SQL | `cloudsql.googleapis.com/database/cpu/utilization` |
| Cloud Functions | `cloudfunctions.googleapis.com/function/execution_count` |
| Cloud Functions | `cloudfunctions.googleapis.com/function/error_count` |
| Cloud Run | `run.googleapis.com/request_count` |
| GKE | `kubernetes.io/container/cpu/core_usage_time` |

Browse the full catalogue at [Metrics list](https://cloud.google.com/monitoring/api/metrics_gcp).

Output is one entry per time series, each with `metric.labels`,
`resource.labels`, and a `points` array of `{time, value}` pairs.

## 9. How tool calls flow through the system

When the alert-chat agent decides to call e.g. `gcp_list_instances`:

1. Claude's response includes a `tool_use` block with `name`,
   `input`.
2. The chat router persists it as a pending `AlertAction`.
3. Because GCP read tools aren't in `_WRITE_TOOLS`, the router
   skips approval and calls
   `kube_tools.execute_tool(name, input, tenant_id=tenant_id)`.
4. The unified `TOOLS` registry dispatches to
   `cloud_gcp._gcp_list_instances`. The handler:
   - Loads the tenant's GCP Integration row.
   - Parses `service_account_json` and builds
     `google.oauth2.service_account.Credentials` (cached per
     tenant).
   - Builds a typed Compute Engine client with those credentials.
   - Calls `instances.aggregated_list` (or `.list` with a zone) via
     `asyncio.to_thread` so the event loop isn't blocked.
   - Slims the response and returns it as a JSON string.
5. `AlertAction` flips to `status='executed'`, the tool result is
   appended to the transcript, the agent sees it.
6. The frontend routes the action into the **Cloud instances**
   evidence tile via `KIND_OF` in `investigation-tiles.tsx`.

If anything fails — missing Integration row, malformed JSON key,
GCP API error — the handler returns `error: <ExceptionType>:
<message>` and the action row flips to `failed`. The agent treats
those failures as a signal that the provider isn't connected and
moves on.

## 10. Distributed traces (Cloud Trace) — roadmap

Cloud Trace isn't wired into a read tool in v1. To prepare for it
without re-onboarding, bind one extra role now:

```bash
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${SA}" \
  --role="roles/cloudtrace.user"
```

The planned tool will be `gcp_query_traces` with inputs along the
lines of:

```python
{
  "filter_expr": "service:\"checkout\" AND latency:>1s",
  "since_minutes": 60,
  "limit": 50
}
```

Output will be one row per trace summary with root span name,
latency, span count, and the trace ID. The role grant above is
forward-compatible.

## 11. Verifying the integration

After saving, the simplest end-to-end verification is to fire a
synthetic alert that mentions a GCP resource and watch the agent's
tool calls land in the cloud evidence tiles.

To verify without a real alert:

```bash
# 1. Confirm the integration row landed
curl -fsS -H "Authorization: Bearer $TENANT_TOKEN" \
  https://<your-daalu>/api/v1/integrations/config | jq '.[]|select(.provider=="gcp") | {provider, status}'

# 2. Re-run the identity probe
curl -fsS -X POST https://<your-daalu>/api/v1/onboarding/test/gcp \
  -H "Authorization: Bearer $TENANT_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(jq -nc --arg p acme-prod --arg k "$SA_KEY" \
    '{config: {project_id: $p, service_account_json: $k}}')"

# 3. See recent agent tool calls
kubectl -n daalu-automation exec daalu-postgres-0 -- \
  psql -U daalu -d daalu_automation -c "
    SELECT tool_name, status, executed_at, LEFT(result_output, 80)
    FROM alert_actions
    WHERE tool_name LIKE 'gcp_%'
    ORDER BY executed_at DESC LIMIT 10;
  "
```

## 12. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Wizard's **Test connection** returns `service_account_json and project_id are required` | One of the two wizard fields is empty | Refill both. The JSON field needs the whole blob; the project ID is separate. |
| `Expecting value: line 1 column 1 (char 0)` | The JSON blob didn't parse — usually a copy-paste truncation, smart quotes from a chat app, or whitespace mangling | Re-paste from a plain-text source. `cat /tmp/daalu-remediation.json` is the canonical form. |
| `403 The caller does not have permission` on test | `roles/compute.viewer` isn't bound to the SA on this project | Run the `gcloud projects add-iam-policy-binding` from §4.2. |
| Test passes but a tool call returns `error: PermissionDenied: ...` | A more specific role is missing — e.g. `gcp_query_logging` fails because `roles/logging.viewer` was skipped | Bind the missing role from §4.2. The error name (`PermissionDenied`) tells you which API path was hit. |
| `error: GCPUnavailable: no GCP integration registered for this tenant` | The Integration row for `provider='gcp'` doesn't exist | The wizard step wasn't saved, or was saved against a different tenant. Re-open `/onboarding`, toggle the GCP step on, and save. |
| `Could not deserialize key data` | The `service_account_json` value is the public-cert JSON, not the private-key JSON. Console download offers both. | Re-create a key via §4.3 and download the **JSON key** option (which includes `private_key`). |
| Tool calls succeed but return empty arrays | The filter is too restrictive, or the resources are in a different project. Verify with `gcloud compute instances list --project=$PROJECT` from your laptop to confirm there are resources to see. | Loosen the filter; if you onboarded the wrong project, re-save with the correct `project_id`. |
| `quota exceeded` on long sessions | Cloud Monitoring's read quotas are generous but not unlimited. Multiple agents looping on the same metric can hit them. | Reduce `since_minutes` on metric calls; the agent rarely needs more than 60 minutes of history per triage. |

## 13. Security considerations

- **Credentials at rest**. The full SA key JSON is stored in the
  `integrations.config` JSONB column. If your Postgres is on
  encrypted storage, the key is encrypted at rest. If not, add
  storage-level encryption — Daalu does not currently
  envelope-encrypt this column.
- **JSON visible in the UI**. Unlike the AWS secret key field, the
  GCP JSON field is a textarea and the saved value is shown in
  clear when the tenant-admin re-opens the wizard. This is
  intentional so edits work; treat the integrations page as
  sensitive accordingly.
- **Network exposure**. All calls go from the operator pod to
  `*.googleapis.com` over HTTPS. No tenant-side agent, no inbound
  surface area on the tenant's project.
- **Least privilege**. The five viewer roles in §4.2 are minimal.
  `roles/viewer` is broader and easier to audit but grants access
  to every API in the project. `roles/owner` should never be used.
- **Audit trail**. Every GCP tool call is recorded as an
  `AlertAction` row in Daalu, and as an Admin Activity / Data
  Access log entry on the GCP side under the SA's identity. The
  two are correlatable by timestamp.
- **Key rotation**. Generate a fresh key via §4.3 and re-save the
  wizard step. The credential cache picks up the new key on the
  next tool call. Disable the old key in IAM → Service Accounts →
  Keys once the rollover is confirmed.

## 14. Roadmap — write tools

V1 is intentionally read-only. The path to add a write tool (e.g.
restart a VM, redeploy a Cloud Function, reboot Cloud SQL) is:

1. Implement the handler in `src/daalu_automation/core/cloud_gcp.py`
   mirroring the existing read tools.
2. Set `requires_approval=True` in the tool spec.
3. Add the tool name to `_WRITE_TOOLS` in
   `src/daalu_automation/core/kube_tools.py`.
4. Bind the matching writer role to the SA — e.g.
   `roles/compute.instanceAdmin.v1` for instance restart,
   `roles/cloudfunctions.developer` for function redeploy.

The Approve UI in the Remediation tile picks up new tools
automatically. Same flow as `patch_resource` / `delete_pod`.

Likely first writes when added:

- `gcp_reset_instance` — `compute.instances.reset`
- `gcp_restart_cloud_sql` — `sqladmin.instances.restart`
- `gcp_redeploy_function` — `cloudfunctions.functions.update` (no-op
  source bump to force a cold start)

When that work lands, it moves into §1 and the role list in §4.2
picks up the matching bindings.
