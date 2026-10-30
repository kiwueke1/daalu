# AWS integration

This guide walks through connecting an AWS account to Daalu so the
alert-chat agent can pull instance state, CloudWatch logs and
metrics, RDS state, and Lambda configuration on demand. The
integration is **read-only in v1** — no resources are modified, no
agents are installed on the tenant's side, and all calls are
outbound from the operator's `daalu-api` pod over HTTPS to the
public AWS API endpoints.

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
10. [Distributed traces (X-Ray) — roadmap](#10-distributed-traces-x-ray--roadmap)
11. [Verifying the integration](#11-verifying-the-integration)
12. [Troubleshooting](#12-troubleshooting)
13. [Security considerations](#13-security-considerations)
14. [Roadmap — write tools](#14-roadmap--write-tools)

---

## 1. What gets collected

After onboarding, the alert-chat agent gains access to five AWS read
tools. Each tool maps to one signal type and surfaces in the
matching evidence tile in the per-alert UI.

| Tool | Signal | Underlying AWS API | Evidence tile |
|---|---|---|---|
| `aws_describe_instances` | EC2 instance state | `ec2:DescribeInstances` | Cloud instances |
| `aws_describe_rds_instances` | RDS database state | `rds:DescribeDBInstances` | Cloud databases |
| `aws_describe_lambda` | Lambda function config + recent error count | `lambda:GetFunction` + `cloudwatch:GetMetricStatistics` (AWS/Lambda Errors) | Serverless functions |
| `aws_get_cloudwatch_logs` | Application / service logs | `logs:FilterLogEvents` | Logs |
| `aws_query_cloudwatch_metric` | Metrics (latency / error rate / saturation / anything in CloudWatch) | `cloudwatch:GetMetricStatistics` | Metrics |

Distributed traces (X-Ray) are on the roadmap — see §10.

The agent decides which tools to call based on the alert payload.
For an alert that mentions an EC2 instance ID, it pulls
`aws_describe_instances` for that ID. For a Lambda alert it
combines `aws_describe_lambda` (config) with
`aws_query_cloudwatch_metric` (`AWS/Lambda Errors` /
`Duration`). For an RDS alert it looks at instance status and the
relevant `AWS/RDS` metric (CPU, free storage, connections).

## 2. Auth model

Daalu authenticates to AWS using one of two patterns:

**Static IAM user keys** — the simplest path. The tenant creates an
IAM user, attaches a read-only policy (see §4), generates an
access-key pair, and pastes both into the wizard. The keys live in
`integrations.config` as JSON and are only ever read into a
process-local `boto3.session.Session`; they never leave the
operator pod's process memory in cleartext.

**Cross-account assume-role** — the tenant creates an IAM role in
their account that trusts the operator's account, with the
read-only policy attached to the role. The tenant provides the
operator a small "bootstrap" key pair that has only
`sts:AssumeRole` on that role ARN. Daalu calls
`sts.assume_role` on first use, caches the temporary credentials,
and uses those for every read tool. This pattern is preferred for
production tenants because the long-lived key the operator holds
has no real permissions — all access is gated through the role's
trust policy.

Both patterns flow through the same wizard / API; the difference is
whether `role_arn` is set in the config.

## 3. Operator prerequisites

These are things the team running Daalu does once, not per tenant.

### 3.1 Ship boto3 in the API image

`boto3>=1.34.0` is already declared in `pyproject.toml` —
verify it shipped:

```bash
kubectl -n daalu-automation exec deploy/daalu-api -- \
  python -c "import boto3; print(boto3.__version__)"
```

Anything ≥ 1.34 is fine. If the import fails, rebuild and roll the
API image.

### 3.2 Allow egress to AWS endpoints

The cloud read tools dial out from the `daalu-api` pod to the
public AWS regional endpoints (`*.amazonaws.com`). If your cluster
applies a NetworkPolicy that restricts egress, allowlist
`*.amazonaws.com` on TCP/443 for the `daalu-api` Deployment.

Verify connectivity with:

```bash
kubectl -n daalu-automation exec deploy/daalu-api -- \
  python -c "import urllib.request; \
             urllib.request.urlopen('https://sts.us-east-1.amazonaws.com/', timeout=5)" \
  && echo "egress OK"
```

You should see `egress OK`. A connect timeout means the egress is
blocked; resolve it before continuing.

### 3.3 (Optional) Per-tenant credential rotation policy

Cloud credentials live in the `integrations.config` JSONB column.
For tenants with strict rotation requirements, schedule a periodic
re-run of the API call below (or have the tenant re-run the wizard)
with a freshly minted key. The wizard's **Edit any field to
update** behavior means re-saving with a new secret rolls the
session forward immediately on the next tool call (the boto3
client cache is keyed on `(tenant, region)` and refreshed on
config changes).

## 4. Tenant prerequisites

The tenant — the team whose AWS account you're connecting —
needs to do this in the AWS console (or via Terraform / CDK / IaC
of their choice).

### Fast-path: `daalu-aws-demo` Terraform module

If you're standing up a demo, a POC, or just want to skip the IAM
console clicking, the companion `daalu-aws-demo` Terraform module
creates the `daalu-remediation` IAM user, attaches the
`DaaluReadOnly` inline policy (the same JSON as §4.2 verbatim, plus
the forward-compatible X-Ray actions from §10), and mints a
long-lived access key. It also spins up always-free-tier workloads
(a `t3.micro` EC2 instance and a scheduled, mildly flaky Lambda
writing to a CloudWatch log group) so the alert-chat agent has real
signal to triage.

From a shell with **admin** AWS credentials exported — a separate
admin-scoped key pair used only by Terraform, *not* the read-only
key daalu will use afterwards:

```bash
export AWS_ACCESS_KEY_ID="AKIA..."        # admin bootstrap key
export AWS_SECRET_ACCESS_KEY="..."
export AWS_REGION="us-east-1"

cd /path/to/daalu-aws-demo

terraform init
terraform apply       # ~60s, 12 resources, mostly the EC2 boot
```

Once apply finishes, pull out the three values the wizard needs:

```bash
terraform output aws_region
terraform output daalu_access_key_id
terraform output -raw daalu_secret_access_key; echo
```

`-raw` prints the secret without surrounding quotes so it copy-pastes
cleanly. The secret is marked `sensitive` in the module's
`outputs.tf`, so it does **not** appear in `terraform apply` output —
it has to be queried explicitly.

Sanity-check you targeted the right account:

```bash
terraform output aws_account_id
```

Resources the agent will see (useful when firing a synthetic alert):

```bash
terraform output ec2_instance_id
terraform output lambda_function_name
terraform output lambda_log_group_name
```

> The secret access key lives in `terraform.tfstate` in plaintext.
> The module's `.gitignore` keeps state out of git — treat the
> directory as sensitive and don't push it to a shared bucket.

If you used this fast-path, **skip §4.1, §4.2, and §4.3** — they're
the AWS-console equivalent of what Terraform just did. Continue
straight to §5 (wizard) or §6 (API).

Teardown when you're done:

```bash
terraform destroy
```

This deletes the IAM user, the access key, and the demo workloads.
After `destroy`, the next daalu tool call returns
`InvalidClientToken`; toggle the AWS step back to **Skipped** in the
wizard, or re-run `terraform apply` to mint a fresh key.

### 4.1 Create the IAM user (or role)

**Static-keys path:**

1. IAM → Users → **Create user**.
2. Username: `daalu-remediation` (or anything that identifies the
   purpose in the audit log).
3. Skip "Provide user access to the AWS Management Console" — this
   is a programmatic identity only.
4. Attach the policy from §4.2 directly, or wrap it in an IAM group
   if you prefer group-based attachment.
5. After creation, **Security credentials → Create access key →
   Application running outside AWS**. Save the access key ID and
   secret. The secret is shown once; if you lose it you have to
   create a new key.

**Assume-role path:**

1. IAM → Roles → **Create role** → **Custom trust policy**.
2. Trust policy — replace `<operator-aws-account-id>` with the AWS
   account ID of the principal that holds the bootstrap keys (the
   operator's account):

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Principal": { "AWS": "arn:aws:iam::<operator-aws-account-id>:root" },
       "Action": "sts:AssumeRole",
       "Condition": {
         "StringEquals": { "sts:ExternalId": "<random-shared-secret>" }
       }
     }]
   }
   ```

   The `sts:ExternalId` condition is optional but strongly
   recommended — it prevents the confused-deputy attack where a
   third party who learns the role ARN tricks the operator account
   into assuming it.

3. Attach the policy from §4.2.
4. Note the role ARN —
   `arn:aws:iam::<tenant-aws-account-id>:role/daalu-remediation`.
5. The operator's bootstrap key (a key in the operator's own
   account) only needs `sts:AssumeRole` on this ARN, nothing else.

### 4.2 Attach the read-only policy

Same policy in both paths. Save as `DaaluReadOnly`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Identity",
      "Effect": "Allow",
      "Action": [
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Compute",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "ec2:DescribeInstanceStatus",
        "ec2:DescribeTags"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": [
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams",
        "logs:FilterLogEvents",
        "logs:GetLogEvents"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Metrics",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:GetMetricData",
        "cloudwatch:ListMetrics",
        "cloudwatch:DescribeAlarms"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Databases",
      "Effect": "Allow",
      "Action": [
        "rds:DescribeDBInstances",
        "rds:DescribeDBClusters",
        "rds:DescribeEvents"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Serverless",
      "Effect": "Allow",
      "Action": [
        "lambda:GetFunction",
        "lambda:GetFunctionConfiguration",
        "lambda:ListFunctions"
      ],
      "Resource": "*"
    }
  ]
}
```

Notes:

- `sts:GetCallerIdentity` is what the wizard's **Test connection**
  button calls — without it the test 403s even if the read tools
  themselves would work.
- Scope `Resource` down from `*` to specific ARNs if your security
  team requires it. The tools accept resource filters at call time,
  so per-tag or per-VPC scoping works fine.
- AWS-managed `ReadOnlyAccess` covers everything above and more,
  but it's broader than necessary. Prefer the policy as written.

### 4.3 Pick a default region

The wizard requires a default region (e.g. `us-east-1`,
`eu-west-1`). All tool calls accept a `region` override at call
time, so the default just needs to be a valid region — typically
the one where most of the tenant's workloads run.

## 5. Setting it up in the UI

1. Open `https://<your-daalu>/onboarding` in a browser. Sign in
   with a tenant-admin account.
2. The wizard's Welcome screen lists every integration. Click
   **Begin** to walk through them in order, or **Manage
   individually** in the top right and pick **AWS account** from
   the catalog.
3. On the **AWS account** step:

   - Toggle the step from **Skipped** to **Enabled**.
   - **Access key ID** — paste the access key from §4.1
     (`AKIA…`), or `terraform output daalu_access_key_id` if you
     used the §4 fast-path.
   - **Secret access key** — paste the secret. The field is
     masked; the value is shown back as `***` after save. Fast-path:
     `terraform output -raw daalu_secret_access_key`.
   - **Default region** — e.g. `us-east-1`. Fast-path:
     `terraform output aws_region`.
   - **Session token** (optional) — only for short-lived STS
     credentials. Leave blank when using the Terraform fast-path
     (the key is long-lived).
   - **Assume-role ARN** (optional) — fill in the role ARN if
     you're using the cross-account path. The access key above
     then only needs `sts:AssumeRole` on this ARN. Leave blank when
     using the Terraform fast-path (it's the static-keys path).

4. Click **Test connection**. The wizard fires
   `POST /api/v1/onboarding/test/aws` against the entered values
   (without saving them); the backend runs
   `sts.get_caller_identity()` and returns the caller ARN. You'll
   see one of:

   - **Green badge** — `AWS accepted; caller arn =
     arn:aws:iam::123456789012:user/daalu-remediation` — proceed.
   - **Red badge** — the exact error from AWS (`InvalidClientToken`
     means the access key is wrong; `SignatureDoesNotMatch` means
     the secret is wrong; `AccessDenied` means the principal can't
     call STS at all — re-check §4.2).

5. Click **Save & continue**. The wizard fires
   `PUT /api/v1/integrations/config/aws` with the entered config;
   the row is upserted into `integrations` keyed by
   `(tenant_id, provider='aws')`.

6. Re-running the wizard later loads the saved row back into the
   form fields (secrets remain masked) and shows the badge
   **Already configured. Edit any field to update.** — non-destructive.

## 6. Setting it up via the API

Skip the UI entirely:

```bash
curl -fsS -X PUT https://<your-daalu>/api/v1/integrations/config/aws \
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

If you used the §4 Terraform fast-path, feed the outputs straight in
(run from the `daalu-aws-demo` directory):

```bash
curl -fsS -X PUT https://<your-daalu>/api/v1/integrations/config/aws \
  -H "Authorization: Bearer $TENANT_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
    --arg ak "$(terraform output -raw daalu_access_key_id)" \
    --arg sk "$(terraform output -raw daalu_secret_access_key)" \
    --arg r  "$(terraform output -raw aws_region)" \
    '{config: {access_key_id: $ak, secret_access_key: $sk, region: $r}}')"
```

For the assume-role path:

```bash
curl -fsS -X PUT https://<your-daalu>/api/v1/integrations/config/aws \
  -H "Authorization: Bearer $TENANT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "config": {
      "access_key_id": "AKIA...",
      "secret_access_key": "...",
      "region": "us-east-1",
      "role_arn": "arn:aws:iam::123456789012:role/daalu-remediation"
    }
  }'
```

Pre-flight with the test endpoint if you want a probe before
saving:

```bash
curl -fsS -X POST https://<your-daalu>/api/v1/onboarding/test/aws \
  -H "Authorization: Bearer $TENANT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"config": {"access_key_id":"AKIA...", "secret_access_key":"...", "region":"us-east-1"}}'
```

## 7. Field reference

| Field | Key in `config` | Type | Required | Notes |
|---|---|---|---|---|
| Access key ID | `access_key_id` | string | yes | The `AKIA…` prefix is normal for long-lived IAM-user keys; `ASIA…` is normal for short-lived STS keys. |
| Secret access key | `secret_access_key` | string | yes | Stored as JSONB column value — encrypted at rest if your Postgres is encrypted (recommended). |
| Default region | `region` | string | yes | Any valid AWS region code. Tool calls can override per call. |
| Session token | `session_token` | string | no | Pair with short-lived STS keys (`ASIA…`). Expires when the STS session expires — re-run the wizard or the API call to refresh. |
| Assume-role ARN | `role_arn` | string | no | When set, the keys above are only used to call `sts:AssumeRole` on this ARN; everything else flows through the role's permissions. The first tool call per worker triggers `assume_role` and caches the temp creds for the hour. |

The `integrations` row stored for a populated config looks like:

```json
{
  "id": "...",
  "tenant_id": "...",
  "provider": "aws",
  "module": "infra",
  "name": "AWS production account",
  "status": "connected",
  "config": {
    "access_key_id": "AKIA...",
    "secret_access_key": "...",
    "region": "us-east-1",
    "role_arn": "arn:aws:iam::123456789012:role/daalu-remediation"
  }
}
```

## 8. How signals are collected

### 8.1 EC2 instance state

The agent calls `aws_describe_instances` when an alert references
an instance ID, a tag selector, or a generic "AWS host" — e.g. a
host-level health alert from Datadog/PagerDuty piped into Daalu.

```python
# Tool input shape
{
  "region": "us-east-1",          # optional, defaults to config
  "instance_ids": ["i-0abc..."],  # optional
  "filters": [                    # optional, boto3 filter syntax
    {"Name": "tag:Environment", "Values": ["prod"]}
  ]
}
```

Returned JSON per instance is slimmed to the fields the agent
typically reads: `InstanceId`, `State`, `InstanceType`,
`LaunchTime`, `PrivateIpAddress`, `PublicIpAddress`,
`AvailabilityZone`, `Tags`, `StateTransitionReason`. Full describe
output would blow the tool-result context budget; if you need a
field that's currently stripped, file an issue against
`src/daalu_automation/core/cloud_aws.py` — adding to the slim list
is a one-line change.

### 8.2 RDS state

`aws_describe_rds_instances` returns one entry per matching DB
instance with `DBInstanceIdentifier`, `Engine`, `EngineVersion`,
`DBInstanceClass`, `DBInstanceStatus`, `Endpoint`,
`AllocatedStorage`, `StorageType`, `MultiAZ`, `AvailabilityZone`,
and `PendingModifiedValues`. The `PendingModifiedValues` field is
the one operators usually want to check during a maintenance-window
incident — it shows whether the instance is mid-modify.

### 8.3 Lambda state + recent errors

`aws_describe_lambda` is unusual in that it makes **two** calls:

1. `lambda:GetFunction` for the function configuration (runtime,
   memory, env, tracing config, last modified, etc.).
2. `cloudwatch:GetMetricStatistics` on `AWS/Lambda/Errors` for the
   last 60 minutes in 5-minute bins.

Combining configuration + recent error rate in a single tool call
saves a turn for the most common Lambda triage flow ("is this
function failing right now, and what's it doing?"). The pre-signed
S3 code URL from `GetFunction.Code.Location` is stripped from the
response — it's a giant signed URL the agent doesn't need.

### 8.4 CloudWatch Logs

`aws_get_cloudwatch_logs` wraps
`logs:FilterLogEvents`. Inputs:

```python
{
  "log_group": "/aws/lambda/api-gateway",    # required
  "log_stream": "2026/05/18/[LATEST]abc123", # optional, filter to one stream
  "filter_pattern": "?ERROR ?WARN",          # optional, CW filter pattern syntax
  "since_minutes": 60,                       # optional, default 60
  "limit": 100,                              # optional, max 1000
  "region": "us-east-1"                      # optional
}
```

Output is one log line per event, timestamp-prefixed in ISO
format, joined with newlines. CloudWatch filter-pattern syntax
supports plain words (`ERROR`), the `?` OR operator (`?ERROR
?WARN`), and JSON-payload filters (`{$.level = "ERROR"}`) — see the
[AWS Logs filter pattern
docs](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/FilterAndPatternSyntax.html)
for the full grammar.

### 8.5 CloudWatch Metrics

`aws_query_cloudwatch_metric` wraps
`cloudwatch:GetMetricStatistics`. Inputs:

```python
{
  "namespace": "AWS/RDS",                    # required, e.g. AWS/Lambda, AWS/ApplicationELB
  "metric_name": "CPUUtilization",           # required
  "dimensions": {                            # optional, dimension name → value
    "DBInstanceIdentifier": "prod-db"
  },
  "stat": "Average",                         # one of Average/Sum/Minimum/Maximum/SampleCount
  "since_minutes": 60,                       # optional, default 60
  "period_seconds": 60,                      # optional, default 60
  "region": "us-east-1"                      # optional
}
```

Output is the metric label plus an array of `{Timestamp,
<Statistic>, Unit}` datapoints, sorted ascending in time. The
agent typically reads the most recent few points and the trend.

## 9. How tool calls flow through the system

When the alert-chat agent decides to call e.g.
`aws_describe_instances`:

1. The Claude API response includes a `tool_use` block with
   `{"name": "aws_describe_instances", "input": {...}}`.
2. The chat router (`src/daalu_automation/api/routers/alert_chat.py`)
   persists this as an `AlertAction` row with `status='pending'`.
3. Because `aws_describe_instances` is a **read** tool (not in
   `_WRITE_TOOLS`), the router doesn't wait for approval — it
   calls `kube_tools.execute_tool(name, input, tenant_id=tenant_id)`
   immediately.
4. The unified `TOOLS` registry dispatches to
   `cloud_aws._aws_describe_instances`. The handler:
   - Looks up the tenant's AWS Integration row.
   - Builds a `boto3.session.Session` (cached per
     `(tenant_id, region)`).
   - If `role_arn` is set, calls `sts.assume_role` and rebuilds the
     session with the temporary credentials.
   - Calls `ec2.describe_instances` via `asyncio.to_thread` so the
     event loop isn't blocked.
   - Slims the response and serialises it as JSON.
5. The action row flips to `status='executed'` with the JSON in
   `result_output`. A `ChatMessage` of role `tool` is appended to
   the transcript so the agent sees the result.
6. The frontend's `groupActions` (in
   `frontend/components/alerts/investigation-tiles.tsx`) routes the
   action into the **Cloud instances** evidence tile based on the
   `KIND_OF` map.

If something fails — typically a missing/malformed Integration row
or an AWS API error — the handler returns `error: <ExceptionType>:
<message>` and the action row flips to `status='failed'`. The
agent treats those failures as a signal that the provider isn't
connected (or that the requested resource doesn't exist) and adapts
its plan accordingly.

## 10. Distributed traces (X-Ray) — roadmap

X-Ray traces aren't wired into a read tool in v1. To prepare for
them and avoid re-running the wizard later, grant the IAM identity
two additional actions now:

```json
{
  "Sid": "XRay",
  "Effect": "Allow",
  "Action": [
    "xray:GetTraceSummaries",
    "xray:BatchGetTraces"
  ],
  "Resource": "*"
}
```

The planned tool will be `aws_query_xray_traces` with inputs
along the lines of:

```python
{
  "filter_expression": "service(\"checkout\") AND error",
  "since_minutes": 60,
  "limit": 50,
  "region": "us-east-1"
}
```

Output will be one row per trace summary with response time, fault
type, and the trace ID — letting the agent then call `BatchGetTraces`
for a deeper dive on the worst offenders. No timeline for shipping;
the IAM grant above is forward-compatible and lets you wire it up
without re-onboarding.

## 11. Verifying the integration

After saving, the simplest end-to-end verification is to fire a
synthetic alert that mentions an AWS-hosted resource and watch the
agent's tool calls land in the cloud evidence tiles.

If you'd rather verify without a real alert:

```bash
# 1. Confirm the integration row landed
curl -fsS -H "Authorization: Bearer $TENANT_TOKEN" \
  https://<your-daalu>/api/v1/integrations/config | jq '.[]|select(.provider=="aws")'

# 2. Re-run the identity probe at any time
curl -fsS -X POST https://<your-daalu>/api/v1/onboarding/test/aws \
  -H "Authorization: Bearer $TENANT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"config":{"access_key_id":"…", "secret_access_key":"…", "region":"us-east-1"}}'

# 3. Inspect what the agent has called recently across the tenant
kubectl -n daalu-automation exec daalu-postgres-0 -- \
  psql -U daalu -d daalu_automation -c "
    SELECT tool_name, status, executed_at, LEFT(result_output, 80)
    FROM alert_actions
    WHERE tool_name LIKE 'aws_%'
    ORDER BY executed_at DESC LIMIT 10;
  "
```

## 12. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Wizard's **Test connection** returns `AWS rejected the credentials: An error occurred (InvalidClientToken) …` | Access key ID typo or deleted in AWS | Re-paste the access key; if confident it's right, check IAM → Users → Security credentials and verify the key is *Active*. |
| `SignatureDoesNotMatch` | Secret access key is wrong (often a copy-paste truncation) | Re-paste the secret. If the original wasn't saved, create a new access key in IAM and use that. |
| Wizard returns `AccessDenied … user is not authorized to perform: sts:GetCallerIdentity` | The IAM principal exists but doesn't have STS permission | Add `sts:GetCallerIdentity` to the policy in §4.2. The read tools may have worked without it, but the wizard test gates on STS specifically. |
| Test passes but the agent's tool call fails with `error: ClientError: User: arn:aws:iam::…:user/… is not authorized to perform: ec2:DescribeInstances` | Identity is valid but the policy is missing the specific action | Add the missing action to the policy. The error message names the action — paste it into a new `Statement` block. |
| `error: AWSUnavailable: no AWS integration registered for this tenant` | The Integration row for `provider='aws'` doesn't exist | The wizard step wasn't saved, or it was saved against a different tenant. Re-open `/onboarding`, toggle the AWS step on, and save. |
| Assume-role path: `AccessDenied … is not authorized to perform: sts:AssumeRole on resource: arn:…/daalu-remediation` | The tenant's trust policy doesn't allow the operator's account, or the `ExternalId` doesn't match | Re-check the trust policy on the role. The trusted Principal must be the operator's account ID. If you set `ExternalId`, both the role's trust policy and the operator's `integrations.config` must use the same value. |
| All tool calls latency-spike to 30+ seconds | Network egress is partially blocked or the DNS resolver in the cluster doesn't have a path to AWS public DNS | Check `kubectl logs deploy/daalu-api` for `ConnectTimeoutError` / `EndpointConnectionError`. Confirm §3.2 egress check still passes. |
| Wizard's status shows `disconnected` even after saving | The status column is only flipped on a successful real-tool call, not on the wizard test. Trigger a real alert that touches AWS to flip it. | This is cosmetic; the integration is functional. The `connected` flip happens on the first non-erroring tool call. |

## 13. Security considerations

- **Credentials at rest**. The wizard stores `secret_access_key`
  (and `session_token`, if used) in the `integrations.config`
  JSONB column. If your Postgres is on encrypted storage (encrypted
  cloud volumes, AWS RDS with KMS-at-rest, etc.) the secret is
  encrypted at rest. If not, add storage-level encryption — Daalu
  does not currently envelope-encrypt this column.
- **Credentials at rest in backups**. Same applies to any DB
  backups you take. Treat the `integrations` table as sensitive.
- **Network exposure**. AWS API calls go from the operator pod to
  `*.amazonaws.com` over HTTPS. No tenant-side agent, no inbound
  surface area opened on the tenant's account.
- **Least privilege**. The read-only policy in §4.2 is minimal.
  Avoid attaching the AWS-managed `ReadOnlyAccess` policy unless
  the tenant explicitly wants the agent to be able to read
  arbitrary services beyond the ones Daalu uses.
- **Audit trail**. Every cloud tool call is recorded as an
  `AlertAction` row tied to the originating alert. The
  CloudTrail entry on the AWS side shows the user identity
  (`daalu-remediation` or the assumed role's session name
  `daalu-remediation`). The two trails are easy to correlate by
  timestamp.
- **Key rotation**. Re-save the wizard step with a new key to
  rotate. The client cache picks up the new credentials on the
  next tool call. If the old key is still active in IAM, both work
  simultaneously — typical "rotate then revoke" pattern.

## 14. Roadmap — write tools

V1 is intentionally read-only. The path to add a write tool (e.g.
restart EC2 instance, redeploy Lambda alias, reboot RDS) is:

1. Implement the handler in `src/daalu_automation/core/cloud_aws.py`
   mirroring the existing read tools.
2. Set `requires_approval=True` in the tool spec.
3. Add the tool name to `_WRITE_TOOLS` in
   `src/daalu_automation/core/kube_tools.py`.
4. Add the matching IAM action to the policy in §4.2 — and
   document the new action in this doc's policy block.

The Approve UI in the Remediation tile picks up the new tool
automatically. The same flow that gates `patch_resource` and
`delete_pod` runs for AWS write tools, with the same per-action
**Approve & run** / **Reject** buttons and execution-result panel.

Likely first writes when added:

- `aws_reboot_instance` — `ec2:RebootInstances`
- `aws_reboot_rds_instance` — `rds:RebootDBInstance`
- `aws_invoke_lambda` — `lambda:InvokeFunction` (for redeploy
  flows that need a manual cold-start)

When that work lands, this section moves into §1 and the IAM
policy in §4.2 picks up the matching actions.

---

## Appendix A — Pushing AWS alerts into Daalu

§1–§13 cover the **pull** side: Daalu has read-only AWS credentials and
fetches state on demand whenever an alert mentions an AWS resource.
This appendix covers the **push** side — making CloudWatch (or anything
that can fire a CloudWatch alarm) hand Daalu a fresh alert so the
alert-chat agent can start triaging it.

Daalu has no AWS-specific webhook. There is one generic ingest
endpoint, and the AWS side is responsible for shaping its alarm
notifications into the envelope Daalu expects.

Contents:

- [A.1 — Daalu's generic event ingest endpoint](#a1--daalus-generic-event-ingest-endpoint)
- [A.2 — Mint a tenant ingest key](#a2--mint-a-tenant-ingest-key)
- [A.3 — Smoke-test the ingest path from your shell](#a3--smoke-test-the-ingest-path-from-your-shell)
- [A.4 — AWS-side option 1: EventBridge → API Destination (recommended)](#a4--aws-side-option-1-eventbridge--api-destination-recommended)
- [A.5 — AWS-side option 2: CloudWatch alarm → SNS → Lambda translator](#a5--aws-side-option-2-cloudwatch-alarm--sns--lambda-translator)
- [A.6 — End-to-end lifecycle of one AWS alert](#a6--end-to-end-lifecycle-of-one-aws-alert)
- [A.7 — Verifying it works](#a7--verifying-it-works)
- [A.8 — Troubleshooting](#a8--troubleshooting)

### A.1 — Daalu's generic event ingest endpoint

```
POST https://<your-daalu>/api/v1/events
Headers:
  X-Daalu-Key: <tenant ingest key>
  Content-Type: application/json
Body: { ...EventEnvelope }
```

The handler lives at `src/daalu_automation/api/routers/events.py` —
function `emit_event`. The body is parsed into `EventEnvelope`
(`src/daalu_automation/core/events.py:29`) and published to the
Redis-Streams event bus. Any tenant-id in the body is dropped — the
tenant is resolved exclusively from `X-Daalu-Key`'s SHA-256 against
`tenants.ingest_api_key_hash`.

`EventEnvelope` is a flat dict — the wire shape Redis Streams allows:

| Field | Required | What it does |
|---|---|---|
| `type` | yes | Drives which agent picks the event up. For CloudWatch firing → `infra.alert.fired`. For ALARM-clear → `infra.alert.resolved`. |
| `module` | yes | Always `infra` for AWS alarms. |
| `source` | yes | Free-form provenance string, e.g. `aws-cloudwatch`, `aws-eventbridge`. Shows up on the Alerts page. |
| `summary` | yes | One-line human title — becomes the alert title. |
| `severity` | no, default `info` | `info` / `warning` / `critical`. |
| `payload` | no | Arbitrary dict — the agent reads `alarm_name`, `region`, `instance_id`, etc. out of here when deciding which `aws_*` read tool to call next. Pass the raw CloudWatch alarm JSON verbatim if you don't want to flatten it. |
| `occurred_at` | no, default now() | ISO-8601 UTC. CloudWatch's `StateChangeTime` slots in nicely. |

The two types the `InfraAgent` is wired to act on (see
`src/daalu_automation/modules/infra/agent.py:31`) are
`infra.alert.fired` and `infra.alert.resolved` — use those.

### A.2 — Mint a tenant ingest key

The ingest key is per-tenant. As a Daalu superuser:

```bash
TENANT_ID=$(curl -fsS -H "Authorization: Bearer $SUPERUSER_TOKEN" \
  https://<your-daalu>/api/v1/tenants | jq -r '.[0].id')

curl -fsS -X POST -H "Authorization: Bearer $SUPERUSER_TOKEN" \
  https://<your-daalu>/api/v1/tenants/${TENANT_ID}/rotate-ingest-key \
  | jq -r '.ingest_api_key'
```

Save the returned string — Daalu stores only its SHA-256 hash, so it
isn't recoverable. Rotating again invalidates the previous key
immediately; you'll need to update every shim before the rotation.

### A.3 — Smoke-test the ingest path from your shell

Before plumbing AWS, prove the wiring with a hand-crafted POST. This
is what AWS will ultimately do — the only difference is who's holding
the curl.

```bash
DAALU_KEY="<the key from §A.2>"

curl -fsS -X POST https://<your-daalu>/api/v1/events \
  -H "X-Daalu-Key: ${DAALU_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "type":     "infra.alert.fired",
    "module":   "infra",
    "source":   "aws-cloudwatch",
    "severity": "warning",
    "summary":  "Synthetic CloudWatch alarm: smoke test",
    "payload": {
      "alarm_name":  "smoke-test",
      "region":      "us-east-1",
      "new_state":   "ALARM",
      "reason":      "Triggered manually from shell"
    }
  }'
```

A successful response is `202 Accepted` with the envelope echoed back.
Within a few seconds you'll see:

- a new row on `/alerts` with title "Synthetic CloudWatch alarm: smoke
  test",
- an `Incident` row on `/operations`,
- a `recommendation` if the AI triage rated this Sev-1.

If you see `401 missing X-Daalu-Key` or `401 invalid X-Daalu-Key` the
key is wrong or wasn't sent — re-mint per §A.2.

### A.4 — AWS-side option 1: EventBridge → API Destination (recommended)

EventBridge has first-class support for calling an arbitrary HTTPS URL
with a custom header. No Lambda required. Three resources:

- a **Connection** that holds the `X-Daalu-Key` value,
- an **API Destination** pointing at `/api/v1/events`,
- a **Rule** that matches CloudWatch alarm state-change events and
  invokes the destination with a templated body that matches
  `EventEnvelope`.

#### A.4.1 — Store the ingest key in Secrets Manager

```bash
aws secretsmanager create-secret \
  --name daalu/ingest-key \
  --secret-string "${DAALU_KEY}"
```

EventBridge Connections will reference this so the key never sits in
the rule definition.

#### A.4.2 — Create the EventBridge Connection

```bash
aws events create-connection \
  --name daalu-ingest \
  --authorization-type API_KEY \
  --auth-parameters 'ApiKeyAuthParameters={ApiKeyName=X-Daalu-Key,ApiKeyValue='"${DAALU_KEY}"'}'
```

(EventBridge stores the value in its own managed Secrets Manager
secret behind the scenes; the Secret you created in §A.4.1 is just for
your bookkeeping if you'd rather pin a known ARN.)

#### A.4.3 — Create the API Destination

```bash
CONN_ARN=$(aws events describe-connection --name daalu-ingest \
  --query ConnectionArn --output text)

aws events create-api-destination \
  --name daalu-events \
  --connection-arn "${CONN_ARN}" \
  --invocation-endpoint "https://<your-daalu>/api/v1/events" \
  --http-method POST \
  --invocation-rate-limit-per-second 50
```

#### A.4.4 — Allow EventBridge to invoke the API Destination

EventBridge needs an IAM role that lets it call its own
`events:InvokeApiDestination`. The trust policy is for
`events.amazonaws.com`:

```bash
aws iam create-role \
  --role-name daalu-eventbridge-invoke \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{
      "Effect":"Allow",
      "Principal":{"Service":"events.amazonaws.com"},
      "Action":"sts:AssumeRole"
    }]
  }'

DEST_ARN=$(aws events describe-api-destination --name daalu-events \
  --query ApiDestinationArn --output text)

aws iam put-role-policy \
  --role-name daalu-eventbridge-invoke \
  --policy-name invoke-daalu \
  --policy-document '{
    "Version":"2012-10-17",
    "Statement":[{
      "Effect":"Allow",
      "Action":"events:InvokeApiDestination",
      "Resource":"'"${DEST_ARN}"'"
    }]
  }'
```

#### A.4.5 — Create the Rule + Target with an EventEnvelope template

This is the load-bearing piece. The rule matches every CloudWatch
alarm state-change in the account, and the target's **InputTransformer**
rewrites AWS's event into `EventEnvelope` JSON:

```bash
aws events put-rule \
  --name daalu-cloudwatch-alarms \
  --event-pattern '{
    "source":      ["aws.cloudwatch"],
    "detail-type": ["CloudWatch Alarm State Change"]
  }' \
  --state ENABLED

ROLE_ARN=$(aws iam get-role --role-name daalu-eventbridge-invoke \
  --query Role.Arn --output text)

aws events put-targets \
  --rule daalu-cloudwatch-alarms \
  --targets '[{
    "Id":          "daalu-events",
    "Arn":         "'"${DEST_ARN}"'",
    "RoleArn":     "'"${ROLE_ARN}"'",
    "InputTransformer": {
      "InputPathsMap": {
        "alarmName":   "$.detail.alarmName",
        "newState":    "$.detail.state.value",
        "oldState":    "$.detail.previousState.value",
        "reason":      "$.detail.state.reason",
        "region":      "$.region",
        "accountId":   "$.account",
        "timestamp":   "$.time"
      },
      "InputTemplate": "{\"type\":\"infra.alert.fired\",\"module\":\"infra\",\"source\":\"aws-cloudwatch\",\"severity\":\"warning\",\"summary\":\"AWS CloudWatch: <alarmName> -> <newState>\",\"occurred_at\":\"<timestamp>\",\"payload\":{\"alarm_name\":\"<alarmName>\",\"new_state\":\"<newState>\",\"old_state\":\"<oldState>\",\"reason\":\"<reason>\",\"region\":\"<region>\",\"account_id\":\"<accountId>\"}}"
    }
  }]'
```

Notes:

- The `type` is hardcoded to `infra.alert.fired` for any state change.
  If you want `infra.alert.resolved` when an alarm clears, split into
  two rules with different `event-pattern.detail.state.value` matchers
  and different `InputTemplate` literals.
- The `payload` carries `alarm_name`, `region`, `reason`, and the
  account ID. The alert-chat agent reads `alarm_name` / `region` /
  any resource hints in `reason` when deciding which `aws_*` tool to
  call. **Put the EC2 instance ID, RDS identifier, Lambda function
  name, etc. into the alarm name** — the agent picks them up from
  there.
- `InputTransformer` validation is strict; bad placeholders fail at
  `put-targets` time, not at runtime.

#### A.4.6 — Test by forcing an alarm

If you have the `daalu-aws-demo` Terraform module running (§4), force
the demo Lambda's error metric high enough to trip an alarm — or
create a no-op alarm and flip it manually:

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name daalu-push-smoke \
  --namespace AWS/Lambda \
  --metric-name Errors \
  --dimensions Name=FunctionName,Value=daalu-demo-flaky \
  --statistic Sum \
  --period 60 \
  --evaluation-periods 1 \
  --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --treat-missing-data notBreaching

aws cloudwatch set-alarm-state \
  --alarm-name daalu-push-smoke \
  --state-value ALARM \
  --state-reason "Manual push test"
```

Within ~15 seconds the alert lands on `/alerts`. Run the DB query from
§11 (`SELECT … FROM alerts ORDER BY created_at DESC`) to confirm.

### A.5 — AWS-side option 2: CloudWatch alarm → SNS → Lambda translator

EventBridge can't reach Daalu through a corporate VPN or a custom CA?
The Lambda path gives you more control: the alarm publishes to SNS,
SNS triggers a small Lambda, the Lambda crafts the `EventEnvelope` and
POSTs it.

Pseudocode for the translator (Python 3.12, ~30 lines):

```python
import json, os
import urllib.request

DAALU_URL = os.environ["DAALU_URL"]          # https://ops.example.com/api/v1/events
DAALU_KEY = os.environ["DAALU_KEY"]          # from §A.2

def handler(event, context):
    # SNS wraps the CloudWatch alarm in event["Records"][0]["Sns"]["Message"]
    msg = json.loads(event["Records"][0]["Sns"]["Message"])
    envelope = {
        "type":     "infra.alert.fired"
                    if msg["NewStateValue"] == "ALARM"
                    else "infra.alert.resolved",
        "module":   "infra",
        "source":   "aws-cloudwatch-sns",
        "severity": "warning",
        "summary":  f"AWS CloudWatch: {msg['AlarmName']} -> {msg['NewStateValue']}",
        "occurred_at": msg["StateChangeTime"],
        "payload": {
            "alarm_name": msg["AlarmName"],
            "new_state":  msg["NewStateValue"],
            "reason":     msg["NewStateReason"],
            "region":     msg["Region"],
            "raw":        msg,
        },
    }
    req = urllib.request.Request(
        DAALU_URL,
        data=json.dumps(envelope).encode(),
        headers={"X-Daalu-Key": DAALU_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return {"status": resp.status}
```

Wire it up:

```bash
aws sns create-topic --name daalu-cloudwatch-alarms
TOPIC_ARN=$(aws sns list-topics --query 'Topics[?contains(TopicArn,`daalu-cloudwatch-alarms`)].TopicArn | [0]' --output text)

aws lambda create-function \
  --function-name daalu-cloudwatch-translator \
  --runtime python3.12 \
  --role arn:aws:iam::<acct>:role/<lambda-exec-role> \
  --handler index.handler \
  --zip-file fileb://translator.zip \
  --environment "Variables={DAALU_URL=https://<your-daalu>/api/v1/events,DAALU_KEY=<the-key>}"

aws lambda add-permission \
  --function-name daalu-cloudwatch-translator \
  --statement-id sns-invoke \
  --action lambda:InvokeFunction \
  --principal sns.amazonaws.com \
  --source-arn "${TOPIC_ARN}"

aws sns subscribe \
  --topic-arn "${TOPIC_ARN}" \
  --protocol lambda \
  --notification-endpoint "$(aws lambda get-function --function-name daalu-cloudwatch-translator --query Configuration.FunctionArn --output text)"

# Then on each alarm, set its --alarm-actions to ${TOPIC_ARN}.
```

Trade-offs vs. option 1:

- **More code, more failure modes** (Lambda concurrency, cold starts,
  permissions on the SNS topic).
- **More flexibility** — you can dedupe, batch, suppress, or rewrite
  payloads before hitting Daalu. Useful when the CloudWatch alarm
  name doesn't carry enough context and you want the Lambda to
  enrich it (e.g. resolve the instance's `Name` tag before sending).
- **Survives custom CA / private-link setups** that EventBridge API
  Destinations don't talk to natively.

### A.6 — End-to-end lifecycle of one AWS alert

```
┌──────────────────────────────────────────────────────────────────────┐
│ 1. AWS-side: CloudWatch alarm state OK → ALARM                       │
│      (metric threshold breached, e.g. AWS/Lambda Errors > 1 in 1 min)│
└────────────────────────────────┬─────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 2. EventBridge default bus receives "CloudWatch Alarm State Change". │
│    Rule "daalu-cloudwatch-alarms" matches (§A.4.5).                  │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │ InputTransformer rewrites payload
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 3. API Destination "daalu-events" POSTs the templated EventEnvelope  │
│    JSON to https://<your-daalu>/api/v1/events with the X-Daalu-Key   │
│    header pulled from the Connection.                                │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │ HTTPS, traefik → daalu-api pod
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 4. daalu-api : routers/events.py:emit_event                          │
│      • verify_ingest_key hashes the header, looks up the tenant      │
│      • parses body into EventEnvelope                                │
│      • publish() → Redis Streams event bus                           │
│      • persists a row in the events table                            │
│      • returns 202 with the envelope echoed                          │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │ Redis pub/sub
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 5. daalu-agents : InfraAgent.handle(event)                           │
│    Subscribed to infra.alert.fired (see modules/infra/agent.py).     │
│    Calls Claude with the INCIDENT_TRIAGE_SYSTEM prompt to get        │
│    {likely_root_cause, remediation, blast_radius, confidence}.       │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 6. Incident row inserted (operations + alerts pages render it).      │
│    emit_alert() → an Alert row, severity = blast_radius mapping.     │
│    If Sev-1, emit_recommendation() → "Escalate, page on-call".       │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │ user opens the alert page
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 7. alert-chat agent triages the alert (auto-triage on open, or       │
│    POST /alerts/{id}/triage). Reads the alert body + payload         │
│    (which carries alarm_name, region, etc. from §A.4.5).             │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │ tool_use blocks emitted by Claude
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 8. routers/alert_chat.py dispatches each tool_use to TOOLS[name].    │
│    For an EC2-flavoured alarm: aws_describe_instances, then          │
│    aws_get_cloudwatch_logs on the relevant log group, then           │
│    aws_query_cloudwatch_metric on the metric the alarm watches.      │
│    Each call → boto3 → real AWS API → JSON result.                   │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 9. Each tool result becomes an AlertAction row + a "tool" ChatMessage│
│    appended to the transcript. The frontend's groupActions() in      │
│    investigation-tiles.tsx routes them into the matching evidence    │
│    tile (Cloud instances / Logs / Metrics / Serverless functions).   │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 10. The agent posts a final assistant message — root cause +         │
│     remediation grounded in the live evidence it just pulled. If a   │
│     write tool is suggested, it lands in the Remediation tile with   │
│     Approve & run / Reject buttons.                                  │
└──────────────────────────────────────────────────────────────────────┘
```

### A.7 — Verifying it works

After plumbing either option, verify in three places:

```bash
# 1. The event landed on the bus (events table).
kubectl -n daalu-automation exec daalu-postgres-0 -- \
  psql -U daalu -d daalu_automation -c "
    SELECT occurred_at, type, source, severity, summary
    FROM events
    WHERE source LIKE 'aws-%'
    ORDER BY occurred_at DESC
    LIMIT 10;"

# 2. The InfraAgent picked it up (alerts table).
kubectl -n daalu-automation exec daalu-postgres-0 -- \
  psql -U daalu -d daalu_automation -c "
    SELECT created_at, severity, title, status
    FROM alerts
    WHERE metadata_json::text LIKE '%aws%'
    ORDER BY created_at DESC
    LIMIT 10;"

# 3. The alert-chat agent ran aws_* tools against the alert.
kubectl -n daalu-automation exec daalu-postgres-0 -- \
  psql -U daalu -d daalu_automation -c "
    SELECT executed_at, tool_name, status, LEFT(result_output, 80)
    FROM alert_actions
    WHERE tool_name LIKE 'aws_%'
    ORDER BY executed_at DESC
    LIMIT 10;"
```

And in the UI: `https://<your-daalu>/alerts` shows the new alert at
the top; click it to see the evidence tiles populate as the agent
calls the tools.

### A.8 — Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `401 missing X-Daalu-Key` on the smoke test | Header dropped (proxy stripping unknown headers, or curl typo) | Curl with `-v` to confirm the header lands; check ingress/proxy config. |
| `401 invalid X-Daalu-Key` | Key was rotated after the AWS-side secret was set | Rotate again per §A.2 and update the EventBridge Connection (`aws events update-connection`) or the Lambda env var. |
| Smoke test passes but EventBridge rule never fires | Rule's `event-pattern` too specific, or the alarm's region doesn't match the bus you put the rule on | `aws events test-event-pattern --event-pattern '…' --event '…'` to verify the pattern; remember EventBridge is regional, the rule must be in the alarm's region. |
| EventBridge fires but Daalu returns 5xx | InputTemplate produced invalid JSON (missing escape, stray comma) | Look at the rule's CloudWatch metrics — `FailedInvocations` is non-zero. Inspect with `aws events describe-rule --name daalu-cloudwatch-alarms`. Compare the generated body against §A.1 schema. |
| Event lands on `/events` but no Alert appears | Event `type` isn't one of the `InfraAgent.subscribed_event_types` | Confirm `type` is exactly `infra.alert.fired` (or `infra.alert.resolved`). Anything else is persisted but not triaged. |
| Alert appears but no `aws_*` tool runs | Auto-triage didn't pick the alert up (e.g. agent crashed mid-prompt), or the alert's payload doesn't carry resource hints | Click **Triage** on the alert page to re-run, or hit `POST /alerts/{id}/triage?force=true`. If hints are missing, enrich the EventBridge InputTransformer or the Lambda translator. |
| `error: ClientError: User … is not authorized to perform: ec2:DescribeInstances` in `alert_actions.result_output` | Push path is fine; pull path is the issue (IAM policy from §4.2 missing actions) | Add the missing action to the `DaaluReadOnly` policy. |

### A.9 — Forward compatibility

When a Daalu-managed AWS alert receiver ships (a `/webhooks/aws` route
that speaks SNS natively and handles the subscription-confirmation
handshake), it'll deprecate the EventBridge/Lambda shimming above —
but the underlying `EventEnvelope` contract is what the rest of the
pipeline reads, so the shim's payload will still be valid input. No
rip-and-replace.
