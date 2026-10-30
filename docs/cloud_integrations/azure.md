# Microsoft Azure integration

This guide walks through connecting an Azure subscription to Daalu
so the alert-chat agent can pull Virtual Machine state, Log
Analytics / Application Insights data (including distributed
traces), Azure Monitor metrics, SQL database state, and App
Service / Function App configuration when an incident touches
Azure-hosted infrastructure. The integration is **read-only in
v1** — no resources are modified, no agents are installed on the
tenant's side, and all calls go outbound from the operator's
`daalu-api` pod over HTTPS to `management.azure.com` and the
matching data-plane endpoints (`api.applicationinsights.io`,
`api.loganalytics.io`).

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
10. [Distributed traces (App Insights) — already covered](#10-distributed-traces-app-insights--already-covered)
11. [Verifying the integration](#11-verifying-the-integration)
12. [Troubleshooting](#12-troubleshooting)
13. [Security considerations](#13-security-considerations)
14. [Roadmap — write tools](#14-roadmap--write-tools)

---

## 1. What gets collected

After onboarding, the alert-chat agent gains access to five Azure
read tools. Each maps to one signal type and surfaces in the
matching evidence tile in the per-alert UI.

| Tool | Signal | Underlying Azure API | Evidence tile |
|---|---|---|---|
| `azure_list_vms` | Virtual Machine state | `Microsoft.Compute/virtualMachines` (management plane) | Cloud instances |
| `azure_describe_sql_db` | Azure SQL database state | `Microsoft.Sql/servers/databases` | Cloud databases |
| `azure_describe_function` | Function App / Web App state | `Microsoft.Web/sites` + `web/config` | Serverless functions |
| `azure_query_log_analytics` | Logs **and traces** — anything in a Log Analytics workspace, including App Insights tables | Azure Monitor Logs (`api.loganalytics.io`) via Kusto | Logs |
| `azure_query_metrics` | Metrics (latency / error rate / saturation / anything in Azure Monitor) | Azure Monitor Metrics (`management.azure.com/{resource}/providers/Microsoft.Insights/metrics`) | Metrics |

Unlike AWS X-Ray and GCP Cloud Trace, Azure's distributed-trace
data lives in **Application Insights** tables (`requests`,
`dependencies`, `exceptions`) which are accessed through the same
Log Analytics endpoint — so `azure_query_log_analytics` covers
traces out of the box. See §10.

The agent picks tools based on the alert payload. For a VM alert
it pulls `azure_list_vms` plus the matching CPU/memory metrics.
For an App Service alert it pulls `azure_describe_function` plus
`azure_query_metrics` on `Http5xx` / `ResponseTime`. For SQL
issues it combines `azure_describe_sql_db` with
`azure_query_metrics` on `dtu_consumption_percent` or
`storage_percent`.

## 2. Auth model

Daalu authenticates to Azure using a **service principal with
client-credentials flow** — the same pattern as `az login
--service-principal`. The tenant:

1. Registers an Azure AD application (the SP).
2. Generates a client secret for it.
3. Assigns the right RBAC roles at subscription + workspace scope.
4. Pastes the tenant ID, client ID, client secret, and
   subscription ID into the wizard.

The operator's API pod builds an
`azure.identity.ClientSecretCredential` from those four values and
passes it to every typed SDK client (`ComputeManagementClient`,
`SqlManagementClient`, `WebSiteManagementClient`, etc.) as well as
to `LogsQueryClient` / `MetricsQueryClient` for data-plane calls.
The credential auto-refreshes its access token before expiry.

Managed-identity / Workload Identity Federation paths are on the
roadmap; v1 ships with client credentials because it works
uniformly from any pod, anywhere.

## 3. Operator prerequisites

These are things the team running Daalu does once, not per tenant.

### 3.1 Ship the Azure SDKs in the API image

These deps are declared in `pyproject.toml`:

```
azure-identity>=1.17.0
azure-mgmt-compute>=33.0.0
azure-mgmt-sql>=4.0.0b15
azure-mgmt-web>=7.3.0
azure-monitor-query>=1.4.0
```

Verify they shipped:

```bash
kubectl -n daalu-automation exec deploy/daalu-api -- python -c \
  "from azure.identity import ClientSecretCredential; \
   from azure.mgmt.compute import ComputeManagementClient; \
   from azure.mgmt.sql import SqlManagementClient; \
   from azure.mgmt.web import WebSiteManagementClient; \
   from azure.monitor.query import LogsQueryClient, MetricsQueryClient; \
   print('ok')"
```

If you see `ImportError`, re-run `build-images` and `install.sh`.

### 3.2 Allow egress to Azure endpoints

The cloud read tools dial out from the `daalu-api` pod to several
Azure endpoints:

| Endpoint | Used by |
|---|---|
| `management.azure.com` | All `azure-mgmt-*` SDK clients + the wizard test probe |
| `login.microsoftonline.com` | OAuth token endpoint (auth) |
| `api.loganalytics.io` | `azure_query_log_analytics` |
| `metrics.monitor.azure.com` (or `management.azure.com/.../metrics`) | `azure_query_metrics` |

Allowlist `*.microsoftonline.com`, `*.azure.com`, and
`*.loganalytics.io` on TCP/443 for the `daalu-api` Deployment.

Verify:

```bash
kubectl -n daalu-automation exec deploy/daalu-api -- python -c \
  "import urllib.request; \
   urllib.request.urlopen('https://management.azure.com/', timeout=5)" \
  && echo "egress OK"
```

A connect timeout means egress is blocked. A 401 response is
expected (no auth header); the point is that the TCP+TLS
handshake completes.

### 3.3 (Optional) Client secret rotation policy

Azure AD client secrets have a configurable lifetime (max 2 years).
The wizard surfaces the expiry indirectly — when a secret expires,
the next tool call returns `invalid_client` and the integration's
status flips to `error`. Plan a rotation cadence inside the
secret's validity window and re-save the wizard step (or hit
`PUT /api/v1/integrations/config/azure`) with the new value.

## 4. Tenant prerequisites

The tenant — the team whose Azure subscription you're connecting —
needs to do this in the Azure portal (or via `az`).

### 4.1 Register the application (create the SP)

```bash
TENANT_DISPLAY_NAME="daalu-remediation"

# Create the app registration (which implicitly creates the SP)
APP=$(az ad app create --display-name "$TENANT_DISPLAY_NAME" --query '{appId:appId,objectId:id}' -o json)
APP_ID=$(echo "$APP" | jq -r .appId)
APP_OBJ_ID=$(echo "$APP" | jq -r .objectId)

# Create the SP backing the app
az ad sp create --id "$APP_ID" --query '{objectId:id}' -o tsv
SP_OBJ_ID=$(az ad sp show --id "$APP_ID" --query id -o tsv)

# Generate a client secret valid for 2 years
SECRET_JSON=$(az ad app credential reset --id "$APP_ID" --years 2 -o json)
CLIENT_SECRET=$(echo "$SECRET_JSON" | jq -r .password)

TENANT_ID=$(echo "$SECRET_JSON" | jq -r .tenant)
echo "App (client) ID:    $APP_ID"
echo "Client secret:      $CLIENT_SECRET    # only shown once — save it now"
echo "Tenant ID:          $TENANT_ID"
```

Save the four values:

- `tenant_id` — the Azure AD tenant where the app lives
- `client_id` — the app registration's Application ID
- `client_secret` — the just-printed password
- `subscription_id` — set in §4.3

The secret is the only one Azure shows in clear; re-running
`az ad app credential reset` generates a new secret and **revokes
the old one**. Treat the value like any other long-lived secret.

### 4.2 Assign the read-only roles

The SP needs three role assignments to cover all the tools:

```bash
SUB_ID="<subscription-guid>"

# Required for: azure_list_vms, azure_describe_sql_db,
# azure_describe_function, and the wizard test (which hits
# management.azure.com via ClientSecretCredential.get_token).
az role assignment create \
  --assignee-object-id "$SP_OBJ_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Reader" \
  --scope "/subscriptions/$SUB_ID"

# Required for: azure_query_metrics
az role assignment create \
  --assignee-object-id "$SP_OBJ_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Monitoring Reader" \
  --scope "/subscriptions/$SUB_ID"
```

For Log Analytics + App Insights queries you bind the
`Log Analytics Reader` role **per workspace** the agent should
query:

```bash
WORKSPACE_RG="rg-observability"
WORKSPACE_NAME="ws-prod"
WORKSPACE_SCOPE="/subscriptions/$SUB_ID/resourceGroups/$WORKSPACE_RG/providers/Microsoft.OperationalInsights/workspaces/$WORKSPACE_NAME"

az role assignment create \
  --assignee-object-id "$SP_OBJ_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Log Analytics Reader" \
  --scope "$WORKSPACE_SCOPE"
```

If you have multiple workspaces (one per environment, one per app),
bind `Log Analytics Reader` to each. The `azure_query_log_analytics`
tool takes a workspace GUID at call time, so the agent can target
the right one.

Role rationale:

| Role | Used by |
|---|---|
| `Reader` (subscription) | Wizard test, `azure_list_vms`, `azure_describe_sql_db`, `azure_describe_function` |
| `Monitoring Reader` (subscription) | `azure_query_metrics` |
| `Log Analytics Reader` (per workspace) | `azure_query_log_analytics` |

### 4.3 Find the subscription ID

```bash
az account show --query id -o tsv
# or via the portal: Subscriptions → click your sub → Subscription ID
```

If the SP needs to span multiple subscriptions, assign `Reader` on
each. The wizard scopes to one subscription per Integration row;
register additional providers (e.g. `provider='azure-staging'`)
for additional subscriptions — note the agent only knows about
`provider='azure'` in v1, so secondary-subscription support is a
manual step.

## 5. Setting it up in the UI

1. Open `https://<your-daalu>/onboarding` and sign in with a
   tenant-admin account.
2. The wizard's Welcome screen lists every integration. Click
   **Begin** to walk through them in order, or **Manage
   individually** in the top right and pick **Azure subscription**
   from the catalog.
3. On the **Azure subscription** step:

   - Toggle the step from **Skipped** to **Enabled**.
   - **Azure AD tenant ID** — the GUID from §4.1
     (`echo $TENANT_ID`).
   - **Application (client) ID** — the GUID from §4.1
     (`echo $APP_ID`).
   - **Client secret** — the value from §4.1
     (`echo $CLIENT_SECRET`). The field is masked.
   - **Subscription ID** — the GUID from §4.3.

4. Click **Test connection**. The wizard fires
   `POST /api/v1/onboarding/test/azure` against the entered values;
   the backend builds a `ClientSecretCredential` and calls
   `.get_token("https://management.azure.com/.default")`. Outcomes:

   - **Green badge** — `Azure issued a management.azure.com token`.
     Auth works; role assignments may still be missing for
     specific data-plane endpoints but the SP itself is valid.
   - **Red badge** — the exact error from Azure AD. Common
     failures:
     - `AADSTS70011: invalid scope` — the SP doesn't have any
       roles assigned. Run §4.2.
     - `AADSTS7000215: Invalid client secret` — the secret is
       wrong or has expired. Re-run §4.1 and update.
     - `AADSTS700016: app … was not found in the tenant` — the
       client ID is wrong, or you used the *object ID* instead of
       the *application ID*.
     - `AADSTS90002: tenant … not found` — the tenant ID is
       wrong, or the SP lives in a different Azure AD tenant than
       you pasted.

5. Click **Save & continue**. The wizard fires
   `PUT /api/v1/integrations/config/azure`; the row is upserted
   into `integrations` keyed by `(tenant_id, provider='azure')`.

6. Re-running the wizard later loads the saved row back into the
   form (secrets remain masked).

## 6. Setting it up via the API

Skip the UI entirely:

```bash
curl -fsS -X PUT https://<your-daalu>/api/v1/integrations/config/azure \
  -H "Authorization: Bearer $TENANT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "config": {
      "tenant_id": "00000000-0000-0000-0000-000000000000",
      "client_id": "00000000-0000-0000-0000-000000000000",
      "client_secret": "...",
      "subscription_id": "00000000-0000-0000-0000-000000000000"
    }
  }'
```

Pre-flight without saving:

```bash
curl -fsS -X POST https://<your-daalu>/api/v1/onboarding/test/azure \
  -H "Authorization: Bearer $TENANT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "config": {
      "tenant_id": "...",
      "client_id": "...",
      "client_secret": "...",
      "subscription_id": "..."
    }
  }'
```

## 7. Field reference

| Field | Key in `config` | Type | Required | Notes |
|---|---|---|---|---|
| Azure AD tenant ID | `tenant_id` | string (GUID) | yes | The Azure Active Directory tenant the SP belongs to. Same value as `az account show --query tenantId`. |
| Application (client) ID | `client_id` | string (GUID) | yes | The App Registration's *Application (client) ID*, NOT the Object ID. |
| Client secret | `client_secret` | string | yes | The password from `az ad app credential reset`. Rotates whenever you regenerate. |
| Subscription ID | `subscription_id` | string (GUID) | yes | The Azure subscription this integration scopes to. |

The `integrations` row stored for a populated config looks like:

```json
{
  "id": "...",
  "tenant_id": "...",
  "provider": "azure",
  "module": "infra",
  "name": "Azure subscription",
  "status": "connected",
  "config": {
    "tenant_id": "00000000-0000-0000-0000-000000000000",
    "client_id": "00000000-0000-0000-0000-000000000000",
    "client_secret": "...",
    "subscription_id": "00000000-0000-0000-0000-000000000000"
  }
}
```

## 8. How signals are collected

### 8.1 Virtual Machines

`azure_list_vms` calls `ComputeManagementClient.virtual_machines.
list_all()` (or `.list(resource_group)` when scoped). Inputs:

```python
{
  "resource_group": "rg-prod-web"  # optional
}
```

Output is one entry per VM with `name`, `id` (full ARM ID),
`location`, `vmSize` (e.g. `Standard_D4s_v5`), `osType`
(Linux/Windows), `tags`, and `provisioningState`. The
`provisioningState` field is what most VM-health alerts hinge on
(Succeeded / Failed / Updating).

### 8.2 Azure SQL databases

`azure_describe_sql_db` calls
`SqlManagementClient.databases.get`. Inputs:

```python
{
  "resource_group": "rg-prod-data",
  "server_name": "sqlsrv-prod",
  "database_name": "appdb"
}
```

Output: `name`, `id`, `location`, `status`, `sku.{name,tier,capacity}`,
`maxSizeBytes`, `earliestRestoreDate`,
`currentServiceObjectiveName`. The `status` field flags whether
the DB is online or in a paused / restoring state.

### 8.3 Function Apps / Web Apps

`azure_describe_function` calls
`WebSiteManagementClient.web_apps.get` + `.get_configuration`:

```python
{
  "resource_group": "rg-prod-apps",
  "name": "func-checkout"
}
```

Azure Functions and App Service share the
`Microsoft.Web/sites` resource type — the tool works for both.
Output: `name`, `state` (Running / Stopped), `kind`,
`defaultHostName`, `enabled`, `httpsOnly`, plus a `siteConfig`
slim including `linuxFxVersion` (runtime), `appCommandLine`,
`alwaysOn`, `minTlsVersion`, and `lastModifiedTimeUtc`.

Pair with `azure_query_metrics` on `Http5xx` / `Http2xx` /
`AverageResponseTime` for live error-rate triage, and with
`azure_query_log_analytics` against the App Insights workspace
for stack traces.

### 8.4 Log Analytics + Application Insights

`azure_query_log_analytics` is the most powerful tool in the
Azure surface — it lets the agent run **Kusto** queries against
any Log Analytics workspace. Inputs:

```python
{
  "workspace_id": "abc12345-...",            # the workspace GUID, not the ARM ID
  "kusto": "AppRequests | where Success == false | take 50",
  "since_minutes": 60                        # optional, default 60
}
```

The `workspace_id` is the **Workspace ID** field shown on the
workspace's Overview page in the portal — NOT the resource ID and
NOT the customer ID prefix. Output is a list of row objects,
column names mapped to values.

Useful query patterns:

```kusto
# App Insights requests with errors in the last hour
AppRequests
| where TimeGenerated > ago(1h) and Success == false
| project TimeGenerated, Name, ResultCode, DurationMs, OperationId
| order by TimeGenerated desc
| take 50

# Failed dependencies (traces) — distributed-trace signal
AppDependencies
| where TimeGenerated > ago(1h) and Success == false
| project TimeGenerated, Target, Type, ResultCode, DurationMs, OperationId
| order by DurationMs desc
| take 50

# Container-app stdout/stderr filtered for ERROR
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(30m)
| where Log_s contains "ERROR"
| project TimeGenerated, ContainerAppName_s, Log_s
| take 100

# Azure activity log — who changed what in the subscription
AzureActivity
| where TimeGenerated > ago(6h)
| where ActivityStatusValue == "Failed"
| project TimeGenerated, Caller, ResourceGroup, OperationName, ActivityStatusValue
| order by TimeGenerated desc
```

The tool appends time-window scoping based on `since_minutes`, so
queries don't need a `| where TimeGenerated > ago(…)` clause —
but including one does no harm (the workspace's time-range filter
takes precedence).

### 8.5 Azure Monitor Metrics

`azure_query_metrics` calls
`MetricsQueryClient.query_resource`. Inputs:

```python
{
  "resource_id": "/subscriptions/.../resourceGroups/.../providers/Microsoft.Web/sites/func-checkout",
  "metric_names": ["Http5xx", "Http2xx"],
  "aggregation": "Total",            # Average/Total/Minimum/Maximum/Count
  "since_minutes": 60
}
```

`resource_id` is the **full ARM resource ID** — get it from the
portal's "Properties" pane or via `az resource show -n <name> -g
<rg> --resource-type <type> --query id -o tsv`. Output is one
entry per metric, each with the unit and one or more
`series` (one per dimension combination) of `{time, value}` pairs.

Common metric names by resource type:

| Resource type | Metrics |
|---|---|
| `Microsoft.Web/sites` (Func/App Service) | `Http5xx`, `Http4xx`, `Http2xx`, `AverageResponseTime`, `Requests` |
| `Microsoft.Compute/virtualMachines` | `Percentage CPU`, `Network In`, `Network Out`, `Disk Read Bytes` |
| `Microsoft.Sql/servers/databases` | `dtu_consumption_percent`, `storage_percent`, `connection_successful` |
| `Microsoft.ContainerService/managedClusters` (AKS) | `node_cpu_usage_percentage`, `kube_pod_status_phase` |

## 9. How tool calls flow through the system

When the alert-chat agent decides to call e.g.
`azure_query_log_analytics`:

1. Claude's response includes a `tool_use` block.
2. The chat router persists it as a pending `AlertAction`.
3. Because Azure read tools aren't in `_WRITE_TOOLS`, the router
   skips approval and calls
   `kube_tools.execute_tool(name, input, tenant_id=tenant_id)`.
4. The unified `TOOLS` registry dispatches to
   `cloud_azure._azure_query_log_analytics`. The handler:
   - Loads the tenant's Azure Integration row.
   - Builds a `ClientSecretCredential` from `tenant_id`,
     `client_id`, `client_secret` (cached per tenant).
   - Builds a `LogsQueryClient` with that credential.
   - Calls `query_workspace` with the Kusto string and timespan via
     `asyncio.to_thread`.
   - Flattens the response into a list of column-keyed dicts and
     returns it as JSON.
5. `AlertAction` flips to `status='executed'`, the result lands in
   the transcript.
6. The frontend routes the action into the **Logs** evidence tile
   via `KIND_OF` in `investigation-tiles.tsx`.

If anything fails — missing Integration row, expired client secret,
no role on the workspace — the handler returns `error:
<ExceptionType>: <message>` and the action row flips to `failed`.
The agent treats those failures as signal that the path isn't
available and adapts.

## 10. Distributed traces (App Insights) — already covered

Unlike AWS X-Ray and GCP Cloud Trace, **Azure ships trace data
into Log Analytics out of the box**. Application Insights stores
distributed traces in three tables in its backing workspace:

- `AppRequests` — server-side request spans
- `AppDependencies` — client-side spans (outbound calls)
- `AppExceptions` — captured exceptions tied to a trace
- `AppTraces` — manual log messages tied to a trace context

The trace correlation ID is `OperationId` — querying for one
request usually starts there:

```kusto
union AppRequests, AppDependencies, AppExceptions
| where TimeGenerated > ago(1h)
| where OperationId == "f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6"
| order by TimeGenerated asc
| project TimeGenerated, ItemType, Name, Target, DurationMs, ResultCode, Success
```

…gives the agent the complete fan-out of a single request. The
agent figures out which `OperationId` to chase from a recent
failure in `AppRequests`:

```kusto
AppRequests
| where TimeGenerated > ago(1h) and Success == false and DurationMs > 1000
| project TimeGenerated, Name, ResultCode, DurationMs, OperationId
| order by DurationMs desc
| take 20
```

No additional tool, no extra IAM. The `Log Analytics Reader` role
on the App Insights workspace from §4.2 covers it.

## 11. Verifying the integration

After saving, the simplest end-to-end verification is to fire a
synthetic alert that mentions an Azure resource and watch the
agent's tool calls land in the cloud evidence tiles.

To verify without a real alert:

```bash
# 1. Confirm the integration row landed
curl -fsS -H "Authorization: Bearer $TENANT_TOKEN" \
  https://<your-daalu>/api/v1/integrations/config | jq '.[]|select(.provider=="azure") | {provider, status}'

# 2. Re-run the identity probe
curl -fsS -X POST https://<your-daalu>/api/v1/onboarding/test/azure \
  -H "Authorization: Bearer $TENANT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"config":{"tenant_id":"…","client_id":"…","client_secret":"…","subscription_id":"…"}}'

# 3. See recent agent tool calls
kubectl -n daalu-automation exec daalu-postgres-0 -- \
  psql -U daalu -d daalu_automation -c "
    SELECT tool_name, status, executed_at, LEFT(result_output, 80)
    FROM alert_actions
    WHERE tool_name LIKE 'azure_%'
    ORDER BY executed_at DESC LIMIT 10;
  "
```

## 12. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Wizard's **Test connection** returns `AADSTS7000215: Invalid client secret` | Secret is wrong, or has expired since you saved it | Run `az ad app credential reset --id $APP_ID --years 2` and re-paste. Note the **old secret is revoked** by `reset` — anyone else using it stops working. |
| `AADSTS700016: Application … was not found in the tenant` | The Application (client) ID is wrong, or you pasted the Object ID. They look similar. | Run `az ad app show --id $APP_ID --query appId -o tsv` to confirm — that's the value the wizard wants. |
| `AADSTS90002: Tenant … not found` | The tenant ID is wrong, or the SP lives in a different Azure AD tenant than you pasted | Run `az account show --query tenantId -o tsv` from the same identity that created the SP. |
| `AuthorizationFailed: The client … does not have authorization to perform action 'Microsoft.Compute/virtualMachines/read'` | Test passed (auth works) but a tool call hit a resource the SP can't read | Bind the missing role from §4.2. The error names the action; `Reader` at subscription scope covers all four `read` actions used by v1 tools. |
| `azure_query_log_analytics` returns `error: Forbidden: The request was forbidden`, despite Test passing | `Log Analytics Reader` not bound on the target workspace | Run the workspace-scoped role assignment from §4.2 again with the correct `WORKSPACE_SCOPE`. The workspace's "Access control (IAM)" pane in the portal lists current role assignments. |
| `azure_query_log_analytics` returns empty rows | The Kusto query's table doesn't exist in that workspace, or the time window is too narrow. App Insights tables (e.g. `AppRequests`) only exist if the workspace is paired with an App Insights instance. | List available tables: `union withsource=TableName * \| summarize by TableName`. Confirm the workspace ID is right (Portal → Workspace → Overview → Workspace ID). |
| `error: AzureUnavailable: no Azure integration registered for this tenant` | Integration row missing | Re-open `/onboarding`, toggle the Azure step on, save. |
| `azure_query_metrics` returns `Could not get providers for the subscription` | The SP doesn't have `Monitoring Reader` (or `Reader`) at subscription scope | Re-run the role assignments from §4.2. |
| Test passes but every typed-SDK call hangs ~30s and times out | Egress to `management.azure.com` is restricted | Re-check §3.2. The OAuth token call (`login.microsoftonline.com`) is what succeeds for the wizard test; the data-plane endpoints might be separately blocked. |
| Wizard status shows `disconnected` after saving | The `connected` flip happens on the first successful real tool call, not on the wizard test | Cosmetic — trigger a real alert that touches Azure and the status flips on the first tool call. |

## 13. Security considerations

- **Client secret at rest**. Stored as a string in
  `integrations.config` (JSONB). If your Postgres is on encrypted
  storage, the secret is encrypted at rest. If not, add
  storage-level encryption — Daalu does not currently
  envelope-encrypt this column.
- **Secret lifetime**. Azure AD secrets expire (default 6 months
  or 12 months via the portal, up to 24 months via `az ad app
  credential reset --years 2`). The integration breaks silently
  when the secret expires — schedule rotation **inside** the
  validity window. Daalu does not currently surface a "secret
  expiring soon" warning in the UI; track it in the
  organisation's secret-rotation tooling.
- **Network exposure**. All calls go from the operator pod to
  Azure public endpoints over HTTPS. No tenant-side agent, no
  inbound surface area opened on the tenant's subscription.
- **Least privilege**. Three roles (Reader, Monitoring Reader,
  Log Analytics Reader per workspace) is the minimum. Avoid
  attaching `Contributor` or `Owner` — those are write roles and
  give the agent powers Daalu doesn't yet use.
- **Audit trail**. Every Azure tool call is recorded as an
  `AlertAction` row in Daalu, and as an entry in the Azure
  Activity Log (for management plane reads) / Diagnostic Settings
  audit log (for data plane reads) under the SP's display name.
  Correlate the two by timestamp.
- **Secret rotation**. `az ad app credential reset --id $APP_ID
  --years 2` generates a new secret and revokes the previous one.
  Update the wizard step / API config immediately to avoid
  downtime; the client-credential cache picks up the new value on
  the next tool call. There is no "rotate without revoke" path on
  Azure AD secrets — overlap requires creating a second credential
  with `--append`, switching Daalu over, then revoking the
  original explicitly.

## 14. Roadmap — write tools

V1 is intentionally read-only. The path to add a write tool (e.g.
restart a VM, redeploy a Function App, reboot SQL DB) is:

1. Implement the handler in `src/daalu_automation/core/cloud_azure.py`
   mirroring the existing read tools.
2. Set `requires_approval=True` in the tool spec.
3. Add the tool name to `_WRITE_TOOLS` in
   `src/daalu_automation/core/kube_tools.py`.
4. Bind a matching writer role to the SP — e.g.
   `Virtual Machine Contributor` for VM restart, `Website
   Contributor` for App Service / Functions, `SQL DB Contributor`
   for SQL operations.

The Approve UI in the Remediation tile picks up new tools
automatically. Same flow as `patch_resource` / `delete_pod`.

Likely first writes when added:

- `azure_restart_vm` — `Microsoft.Compute/virtualMachines/restart/action`
- `azure_restart_function` — `Microsoft.Web/sites/restart/action`
- `azure_failover_sql` — `Microsoft.Sql/servers/databases/failover/action`

When that work lands, it moves into §1 and the role list in §4.2
picks up the matching bindings.
