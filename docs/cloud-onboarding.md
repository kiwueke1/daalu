# Cloud onboarding — connecting AWS / GCP / Azure

The alert chat agent investigates incidents by calling read-only tools
against whatever infrastructure the tenant has registered. For
on-prem Kubernetes the connection is a kubeconfig or a WireGuard
reverse tunnel (see [cluster-tunnel-feature.md](cluster-tunnel-feature.md)); for cloud-hosted
infra the connection is one row per provider in the `integrations`
table, set up either through the onboarding wizard in the UI or by
calling the Integrations API directly.

This doc covers:

1. [What it looks like to the user](#1-what-it-looks-like-to-the-user)
2. [What happens in the backend](#2-what-happens-in-the-backend)
3. [What the operator (you) needs to set up first](#3-operator-prerequisites)
4. [Per-provider credential checklists](#4-per-provider-checklists)
5. [Adding cloud write tools later](#5-adding-cloud-write-tools-later)

---

## 1. What it looks like to the user

### Wizard path (recommended)

Navigate to **`/onboarding`** in the dashboard. The wizard's Welcome
screen lists every integration the tenant can wire up. Three new
steps live under the **Cloud** category, after the
Kubernetes/cluster-tunnel steps:

- **AWS account** — IAM access key (or assume-role ARN) + default region
- **Google Cloud project** — service-account JSON key + project ID
- **Azure subscription** — service principal (tenant + client_id +
  client_secret) + subscription ID

For each step the user:

1. Toggles the step from **Skipped** to **Enabled**.
2. Fills in the fields. Each field has placeholder text and
   help-line context; secrets are masked.
3. Clicks **Test connection**. The wizard hits
   `POST /onboarding/test/{provider}` with the values the user just
   typed; the API performs a no-side-effect identity probe (see §2).
   The result lights up green (latency shown) or red (error message
   inline).
4. Clicks **Save & continue**. The wizard calls
   `PUT /integrations/config/{provider}` which upserts a row into
   the `integrations` table.

If a tenant has already configured a provider via the API, the
wizard pre-fills the form from `GET /integrations/config` and shows
a **Already configured. Edit any field to update.** badge — running
the wizard again is non-destructive.

The cluster-tunnel step is unchanged: it still launches
`<ClusterWorkflow />` for the WireGuard hub setup.

### Direct API path

Skip the UI entirely:

```bash
curl -fsS -X PUT https://$OPS/api/v1/integrations/config/aws \
  -H "Authorization: Bearer $TENANT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "config": {
      "access_key_id": "AKIA...",
      "secret_access_key": "...",
      "region": "us-east-1"
    }
  }'
```

### Per-alert behaviour after onboarding

Once a provider is connected, the alert chat agent can call cloud
read tools whenever an alert references that provider. Results land
in dedicated evidence tiles below the investigation row:

| Tile | Tools |
|---|---|
| **Cloud instances** | `aws_describe_instances`, `gcp_list_instances`, `azure_list_vms` |
| **Cloud databases** | `aws_describe_rds_instances`, `gcp_describe_sql_instance`, `azure_describe_sql_db` |
| **Serverless functions** | `aws_describe_lambda`, `gcp_describe_function`, `azure_describe_function` |
| **Logs** (existing) | `aws_get_cloudwatch_logs`, `gcp_query_logging`, `azure_query_log_analytics` (alongside k8s log tools) |
| **Metrics** (existing) | `aws_query_cloudwatch_metric`, `gcp_query_monitoring`, `azure_query_metrics` (alongside Prometheus) |

---

## 2. What happens in the backend

### Storage

Cloud credentials live in `integrations.config` (PostgreSQL `JSONB`)
keyed by `(tenant_id, provider)`. The shape:

```
provider = 'aws'   | config = { access_key_id, secret_access_key, region, [session_token, role_arn] }
provider = 'gcp'   | config = { project_id, service_account_json: "<JSON string>" }
provider = 'azure' | config = { tenant_id, client_id, client_secret, subscription_id }
```

No new tables — the row schema is identical to every existing
integration (Slack webhook, Prometheus URL, etc.).

### Onboarding test endpoint

`POST /onboarding/test/{provider}` runs a **no-side-effect identity
probe** per provider:

- **aws** — `boto3.client("sts").get_caller_identity()`. Returns the
  IAM caller ARN; no resource access required, no billable call.
- **gcp** — Build a Compute Engine v1 client with the
  service-account JSON, call `projects().get(project=…)`. Confirms
  both the key and the project ID resolve, with `compute.viewer` as
  the minimum role.
- **azure** — `ClientSecretCredential.get_token(
  "https://management.azure.com/.default")`. Pure auth call against
  Azure AD; doesn't touch any resource.

The probe's `(ok, message, latency_ms)` triple is what lights up the
green/red badge in the wizard.

### Tool execution path

When the agent calls one of the cloud read tools (e.g.
`aws_describe_instances`), the alert-chat router dispatches via
`kube_tools.execute_tool(name, tool_input, tenant_id=tenant_id)`.
That looks up the tool in the unified `TOOLS` registry (k8s +
cloud), passes `_tenant_id`, and the cloud module:

1. Reads the matching `Integration` row from the DB.
2. Lazily instantiates the SDK client + auth (boto3 Session, GCP
   `Credentials`, Azure `ClientSecretCredential`).
3. Caches the client per `(tenant, region)` for the worker's
   lifetime.
4. Calls the SDK from a thread pool (`asyncio.to_thread`) so the
   event loop isn't blocked.
5. Returns a slim JSON string back to the agent.

Errors surface as `error: AWSUnavailable: …` / `GCPUnavailable: …`
/ `AzureUnavailable: …`, with the cause in the message ("no AWS
integration registered for this tenant", "AWS integration is missing
access_key_id", etc.). The agent treats those as a signal that the
provider isn't connected and skips further tool calls.

### Where the code lives

- `src/daalu_automation/core/cloud_aws.py` — AWS handlers + tool specs
- `src/daalu_automation/core/cloud_gcp.py` — GCP handlers + tool specs
- `src/daalu_automation/core/cloud_azure.py` — Azure handlers + tool specs
- `src/daalu_automation/core/kube_tools.py` — `_register_cloud_tools()`
  pulls all three into the master `TOOLS` registry
- `src/daalu_automation/api/routers/onboarding.py` — `_REQUIRED`,
  `_CATALOG`, `_TESTERS` extended for `aws` / `gcp` / `azure`
- `frontend/app/onboarding/page.tsx` — `STEPS` array extended with
  the three new cloud steps

---

## 3. Operator prerequisites

Before a tenant can connect a cloud provider, you (the operator
running the Daalu instance) need to:

### Ship the cloud SDK deps in the API image

`pyproject.toml` already declares them:

```
boto3>=1.34.0                   # AWS — pre-existing
google-cloud-compute>=1.19.0
google-cloud-logging>=3.10.0
google-cloud-monitoring>=2.22.0
google-cloud-functions>=1.16.0
google-api-python-client>=2.140.0
azure-identity>=1.17.0
azure-mgmt-compute>=33.0.0
azure-mgmt-sql>=4.0.0b15
azure-mgmt-web>=7.3.0
azure-monitor-query>=1.4.0
```

The next `build-images` workflow run picks them up. After deploy,
verify with:

```bash
kubectl -n daalu-automation exec deploy/daalu-api -- \
  python -c "import boto3, google.cloud.compute_v1, azure.identity; print('ok')"
```

If you see ImportError, the image hasn't rolled.

### Make sure the API pod has egress

The cloud read tools call out to public cloud APIs:

- AWS: `*.amazonaws.com`
- GCP: `*.googleapis.com`
- Azure: `management.azure.com`, `*.azure.com`

If your cluster's NetworkPolicy restricts egress, allowlist these
hostnames (or 0.0.0.0/0 on port 443) for the `daalu-api`
Deployment.

### (Optional) Pre-seed Integration rows for known tenants

If you're operating on a tenant's behalf, you can insert the row
directly with SQL — same shape as §4 below. The wizard will see it
on next load and show "Already configured."

### (Optional) Audit logging

Every cloud tool call is recorded as an `AlertAction` row tied to
the originating alert. Run:

```sql
SELECT tool_name, status, executed_at, result_output
FROM alert_actions
WHERE tool_name LIKE 'aws_%' OR tool_name LIKE 'gcp_%' OR tool_name LIKE 'azure_%'
ORDER BY executed_at DESC
LIMIT 50;
```

…to see what the agent actually did against any tenant's cloud
account. Read-only by construction in v1.

---

## 4. Per-provider checklists

### AWS

**What the user pastes into the wizard:**

| Field | Required | Notes |
|---|---|---|
| Access key ID | yes | IAM user's key, **or** an STS-issued temporary key. |
| Secret access key | yes | The matching secret. |
| Default region | yes | e.g. `us-east-1`. Tool calls accept a per-call override. |
| Session token | no | Only for STS short-lived creds. |
| Assume-role ARN | no | If set, the session above only needs `sts:AssumeRole` on this ARN. |

**Minimum IAM policy** to attach to the user (or the role at
`role_arn`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "logs:FilterLogEvents",
        "cloudwatch:GetMetricStatistics",
        "rds:DescribeDBInstances",
        "lambda:GetFunction",
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    }
  ]
}
```

The `sts:GetCallerIdentity` entry is what the wizard's **Test
connection** button hits — without it the test 403s even though the
read tools would work fine.

### GCP

**What the user pastes into the wizard:**

| Field | Required | Notes |
|---|---|---|
| Project ID | yes | e.g. `acme-prod`. |
| Service-account key JSON | yes | The full JSON blob downloaded from IAM → Service Accounts → Keys → Add key. |

**Minimum IAM bindings** for the service account:

```bash
PROJECT=acme-prod
SA=daalu-remediation@${PROJECT}.iam.gserviceaccount.com
for role in compute.viewer logging.viewer monitoring.viewer cloudsql.viewer cloudfunctions.viewer; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${SA}" --role="roles/${role}"
done
```

Or, less granular: `roles/viewer` at the project level. Easier but
gives the SA read access to every API in the project, including
ones Daalu doesn't use.

### Azure

**What the user pastes into the wizard:**

| Field | Required | Notes |
|---|---|---|
| Azure AD tenant ID | yes | GUID; same as `--tenant` in `az login`. |
| Application (client) ID | yes | The App Registration's Application ID. |
| Client secret | yes | App Registration → Certificates & secrets → New client secret. Note the expiry. |
| Subscription ID | yes | GUID; one App Registration can span multiple subscriptions, but Daalu scopes to one. |

**Minimum role assignments** for the service principal:

```bash
SP_OBJ_ID=$(az ad sp show --id <client-id> --query id -o tsv)
SUB=/subscriptions/<sub-id>
WORKSPACE=/subscriptions/<sub-id>/resourceGroups/<rg>/providers/Microsoft.OperationalInsights/workspaces/<name>

az role assignment create --assignee-object-id "$SP_OBJ_ID" --assignee-principal-type ServicePrincipal \
  --role Reader --scope "$SUB"
az role assignment create --assignee-object-id "$SP_OBJ_ID" --assignee-principal-type ServicePrincipal \
  --role "Monitoring Reader" --scope "$SUB"
az role assignment create --assignee-object-id "$SP_OBJ_ID" --assignee-principal-type ServicePrincipal \
  --role "Log Analytics Reader" --scope "$WORKSPACE"
```

`Reader` covers VMs, SQL DBs, Function Apps. `Monitoring Reader`
covers Azure Monitor metric queries. `Log Analytics Reader` is per
workspace — assign it on each workspace the agent should query.

---

## 5. Adding cloud write tools later

V1 is read-only on purpose. The path to add a write tool (e.g.
restart an EC2 instance, reboot an RDS DB, redeploy a Lambda alias)
is:

1. Implement the handler in the matching `cloud_<provider>.py`,
   mirroring the existing read tools.
2. In the handler's tool spec, set `requires_approval=True`.
3. Add the tool name to `_WRITE_TOOLS` in `kube_tools.py` so the
   approval-gating logic picks it up.
4. Document the extra IAM permission in §4 above.

The Approve UI in the Remediation tile picks the new tool up
automatically — same flow as `patch_resource` / `delete_pod`. The
agent's `[system]` step will get a real Approve button instead of
the operator-step copy-paste fallback.
