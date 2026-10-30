# Cloud integrations

These docs describe how to connect a tenant's public-cloud account
to Daalu Automation so the alert-chat agent can pull logs, metrics,
traces, and resource state when an incident touches cloud-hosted
infrastructure. The integration model is the same for all three
providers: one row in the `integrations` table per tenant per
provider, populated through the onboarding wizard at
`https://<your-daalu>/onboarding` or with a single API call.

| Provider | Per-provider guide | Auth model |
|---|---|---|
| Amazon Web Services | [aws.md](./aws.md) | IAM user keys, or assume-role from operator-side keys |
| Google Cloud Platform | [gcp.md](./gcp.md) | Service-account JSON key |
| Microsoft Azure | [azure.md](./azure.md) | Azure AD service principal (client credentials) |

The provider guides are self-contained — pick the one that matches
the tenant's cloud and follow it end-to-end. The high-level shape
that's identical across all three:

1. The **operator** (the team running Daalu) ensures the API image
   ships the relevant SDK and has egress to the cloud's public
   endpoints. That's a one-time setup per Daalu instance, covered
   in each guide's *Operator prerequisites* section.
2. The **tenant** (the team whose cloud you're connecting) creates
   a least-privilege credential — IAM user / service account / app
   registration — with read-only permissions on the resources
   they want the agent to investigate.
3. The credential goes into Daalu through `/onboarding` (UI) or
   `PUT /api/v1/integrations/config/{provider}` (API). The wizard
   runs a no-side-effect identity probe before saving so a typo
   surfaces immediately.
4. From that point on, any alert the agent investigates can call
   into the cloud — results land in the per-alert evidence tiles.

## Signals collected

All three providers expose the same four signal types through the
alert chat. The tool names follow the pattern `<provider>_<verb>`:

| Signal | AWS | GCP | Azure |
|---|---|---|---|
| Compute state | `aws_describe_instances` | `gcp_list_instances` | `azure_list_vms` |
| Managed database state | `aws_describe_rds_instances` | `gcp_describe_sql_instance` | `azure_describe_sql_db` |
| Serverless / function state | `aws_describe_lambda` | `gcp_describe_function` | `azure_describe_function` |
| Logs | `aws_get_cloudwatch_logs` | `gcp_query_logging` | `azure_query_log_analytics` |
| Metrics | `aws_query_cloudwatch_metric` | `gcp_query_monitoring` | `azure_query_metrics` |
| Distributed traces | (planned, see aws.md §10) | (planned, see gcp.md §10) | Already covered by `azure_query_log_analytics` against an App Insights workspace; see azure.md §8.3 |

All current cloud tools are **read-only**. Write tools (restart an
instance, redeploy a function, reboot a database) ship through the
same Approve gate as `patch_resource` and `delete_pod` once added —
see each provider guide's *Roadmap* section for the path.

## How to set up the wizard (high level)

1. Sign in at `https://<your-daalu>/login`.
2. Click **Onboarding** in the sidebar (or visit `/onboarding`
   directly).
3. The Welcome step lists every integration the wizard knows about.
   The three cloud steps live near the bottom, after the Kubernetes
   step and before the cluster-tunnel step:
   - **AWS account**
   - **Google Cloud project**
   - **Azure subscription**
4. Click **Begin** to walk through the steps in order, or **Manage
   individually** in the top-right to jump straight to one
   integration.
5. For each cloud step:
   1. Toggle **Enabled**.
   2. Fill in the fields — placeholders + help text describe each
      one; secrets are masked.
   3. Click **Test connection**. A green badge with latency means
      the credentials work. A red badge surfaces the exact error.
   4. Click **Save & continue**. The credential is persisted in the
      `integrations` table.
6. After all the steps you care about are saved, open any alert
   and click **Re-triage** — the agent will use the new tools on
   its next investigation pass.

Each provider guide walks through this flow in detail and covers
what to do when something goes wrong.

## Where this lives in the codebase

- `src/daalu_automation/core/cloud_aws.py` — AWS handlers + tool
  specs
- `src/daalu_automation/core/cloud_gcp.py` — GCP handlers + tool
  specs
- `src/daalu_automation/core/cloud_azure.py` — Azure handlers + tool
  specs
- `src/daalu_automation/core/kube_tools.py` — `_register_cloud_tools()`
  merges all three into the master `TOOLS` registry
- `src/daalu_automation/api/routers/onboarding.py` — wizard backend
  (`_REQUIRED`, `_CATALOG`, `_TESTERS`)
- `frontend/app/onboarding/page.tsx` — wizard frontend (`STEPS`
  array)

## Versioning

These docs cover the integration surface as of the v1 release —
read-only tools, IAM-credential auth, no agent-side install on the
tenant cloud. Future revisions will add cloud write tools, support
for workload-identity federation (no static keys), and per-region
scoping; check each provider guide's *Roadmap* section for current
status.
