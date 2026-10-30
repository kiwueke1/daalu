# daalu-aws-demo

Terraform module that spins up a tiny, always-free-tier AWS footprint so the
[daalu-automation](https://github.com/) alert-chat agent has real resources
to monitor and triage. It also creates the IAM user + read-only policy that
daalu's onboarding wizard expects, so the only manual step left is pasting
the generated access key into the wizard.

## What gets created

| Resource | Why |
|---|---|
| `t3.micro` EC2 instance (Amazon Linux 2023, no public IP, no SSH) | Target for `aws_describe_instances` and CloudWatch `AWS/EC2` metrics |
| Lambda function (`python3.12`, 128 MB) that errors ~10% of the time | Target for `aws_describe_lambda` + `AWS/Lambda Errors` metric |
| EventBridge rule, `rate(5 minutes)` | Drives the Lambda so there are continuously fresh logs and error metrics |
| CloudWatch log group (`/aws/lambda/daalu-demo-flaky`, 7-day retention) | Target for `aws_get_cloudwatch_logs` |
| IAM user `daalu-remediation` + `DaaluReadOnly` policy + access key | The credentials you paste into daalu's AWS onboarding step |

All defaults stay inside the AWS free tier:

- EC2 t3.micro: 750 hrs/month free for the first 12 months of a new account. One
  always-on instance is ~720 hrs/month.
- Lambda: 1 M requests/month always-free. `rate(5 minutes)` is ~8.6k/month.
- CloudWatch Logs: 5 GB/month ingest always-free. The Lambda emits a handful
  of small JSON lines per invocation; 7-day retention keeps storage trivial.
- EventBridge, EBS root volume, default VPC: all free at this scale.

> Caveat: free-tier limits apply per-account. If this is a fresh account
> with nothing else in it, you're safe. If it isn't, check your billing
> dashboard after the first day.

## Prerequisites

- Terraform >= 1.5 (`terraform --version`)
- An AWS account you control, ideally brand new
- An AWS access key pair for an **admin** identity in that account — used
  by Terraform only, separate from the read-only key daalu will use later

You do **not** need the AWS CLI installed; Terraform talks to AWS directly.

## Step 1 — provide admin credentials to Terraform

Terraform needs AWS credentials with permission to create IAM users, EC2
instances, Lambda functions, etc. Pick one of:

### Option A — environment variables (simplest)

In a fresh terminal:

```bash
export AWS_ACCESS_KEY_ID="AKIA..."
export AWS_SECRET_ACCESS_KEY="..."
export AWS_REGION="us-east-1"
```

These only live for the lifetime of the shell — close the terminal and
they're gone. Re-export when you come back.

### Option B — shared credentials file

If you already use the AWS CLI elsewhere, drop a profile into
`~/.aws/credentials`:

```ini
[daalu-demo]
aws_access_key_id     = AKIA...
aws_secret_access_key = ...
```

Then either set `AWS_PROFILE=daalu-demo` before running Terraform or add
`profile = "daalu-demo"` to the `provider "aws"` block in `providers.tf`.

### How to mint these admin keys

In the AWS console, with the root user of your new account:

1. IAM → Users → **Create user** → name `terraform-bootstrap`.
2. Permissions → Attach policies directly → `AdministratorAccess`.
3. After creation → Security credentials → **Create access key** →
   *Application running outside AWS* → save both halves.

Delete this user once you're done with the demo if you want to be tidy
about it. The `daalu-remediation` user that Terraform creates is read-only
and is safe to leave in place.

## Step 2 — apply the module

```bash
cd /path/to/daalu-aws-demo

# Optional: customise via a tfvars file.
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars

terraform init
terraform plan
terraform apply
```

12 resources are planned. Apply takes ~60 seconds — most of it is the EC2
instance booting.

### Architecture

```
                       ┌──────────────────────────────────────────────────────┐
                       │  AWS account (us-east-1)                             │
                       │                                                      │
                       │   ┌────────────────────────────────────────────┐     │
                       │   │  Default VPC                               │     │
                       │   │                                            │     │
                       │   │   ┌──────────────────────────────────┐     │     │
                       │   │   │  Default subnet (first AZ)       │     │     │
                       │   │   │                                  │     │     │
                       │   │   │   ┌───────────────────────────┐  │     │     │
                       │   │   │   │ aws_instance.demo         │  │     │     │
                       │   │   │   │ "daalu-demo-ec2"          │  │     │     │
                       │   │   │   │ t3.micro / AL2023 x86_64  │  │     │     │
                       │   │   │   │ 8 GB gp3, encrypted       │  │     │     │
                       │   │   │   │ IMDSv2 required           │  │     │     │
                       │   │   │   │ no public IP              │  │     │     │
                       │   │   │   └─────────────▲─────────────┘  │     │     │
                       │   │   │                 │                │     │     │
                       │   │   │     ┌───────────┴────────────┐   │     │     │
                       │   │   │     │ aws_security_group     │   │     │     │
                       │   │   │     │ ingress: none          │   │     │     │
                       │   │   │     │ egress:  TCP/443       │   │     │     │
                       │   │   │     └────────────────────────┘   │     │     │
                       │   │   └──────────────────────────────────┘     │     │
                       │   └────────────────────────────────────────────┘     │
                       │                                                      │
                       │   ┌──────────────────────────┐                       │
                       │   │ EventBridge rule         │  rate(5 minutes)      │
                       │   │ daalu-demo-flaky-        │                       │
                       │   │   schedule               │                       │
                       │   └────────────┬─────────────┘                       │
                       │                │ InvokeFunction                      │
                       │                │ (gated by aws_lambda_permission)    │
                       │                ▼                                     │
                       │   ┌──────────────────────────┐                       │
                       │   │ aws_lambda_function      │                       │
                       │   │ "daalu-demo-flaky"       │                       │
                       │   │ runtime  python3.12      │                       │
                       │   │ memory   128 MB          │                       │
                       │   │ timeout  10 s            │                       │
                       │   │ env      ERROR_PROB=0.1  │◀── aws_iam_role       │
                       │   │ tracing  PassThrough     │    "lambda-exec"      │
                       │   └────────────┬─────────────┘  + AWSLambdaBasic-    │
                       │                │ stdout/stderr   ExecutionRole       │
                       │                ▼                                     │
                       │   ┌──────────────────────────┐                       │
                       │   │ CloudWatch Log Group     │                       │
                       │   │ /aws/lambda/             │                       │
                       │   │   daalu-demo-flaky       │                       │
                       │   │ retention 7 days         │                       │
                       │   └──────────────────────────┘                       │
                       │                                                      │
                       │   ┌──────────────────────────────────────────────┐   │
                       │   │ IAM user  /daalu/daalu-remediation           │   │
                       │   │   inline policy: DaaluReadOnly               │   │
                       │   │     sts:GetCallerIdentity                    │   │
                       │   │     ec2:Describe{Instances,Status,Tags}      │   │
                       │   │     logs:{Describe*,FilterLogEvents,GetLog*} │   │
                       │   │     cloudwatch:{GetMetric*,ListMetrics}      │   │
                       │   │     rds:Describe{DBInstances,DBClusters,…}   │   │
                       │   │     lambda:{GetFunction,ListFunctions,…}     │   │
                       │   │     xray:{GetTraceSummaries,BatchGetTraces}  │   │
                       │   │   access key  AKIA...  (long-lived)          │   │
                       │   └─────────────────────┬────────────────────────┘   │
                       └─────────────────────────┼────────────────────────────┘
                                                 │  HTTPS, SigV4
                                                 ▼
                                ┌────────────────────────────────────┐
                                │  daalu-api pod (operator cluster)  │
                                │  alert-chat agent calls AWS read   │
                                │  tools using these credentials     │
                                └────────────────────────────────────┘
```

### Resource inventory

The 12 resources Terraform creates, grouped by purpose:

#### Compute target — what daalu's `aws_describe_instances` will see

| # | Terraform address | AWS resource | Notes |
|---|---|---|---|
| 1 | `aws_security_group.demo` | EC2 security group `daalu-demo-ec2` | No ingress rules. Egress restricted to TCP/443 (so the instance can still reach SSM and yum mirrors if you ever attach a profile). |
| 2 | `aws_instance.demo` | EC2 instance `daalu-demo-ec2` | `t3.micro`, Amazon Linux 2023, AMI resolved at plan time from `/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64`. 8 GB encrypted gp3 root. `associate_public_ip_address = false`. IMDSv2 token required (`http_tokens = "required"`). Detailed monitoring **off** — basic 5-min `AWS/EC2` metrics are free; detailed is not. |

#### Serverless target — what `aws_describe_lambda` + `aws_query_cloudwatch_metric` will see

| # | Terraform address | AWS resource | Notes |
|---|---|---|---|
| 3 | `aws_iam_role.lambda_exec` | IAM role `daalu-demo-lambda-exec` | Trust policy lets `lambda.amazonaws.com` assume it. |
| 4 | `aws_iam_role_policy_attachment.lambda_basic` | Managed policy attachment | Attaches the AWS-managed `AWSLambdaBasicExecutionRole` — just enough to write to CloudWatch Logs. |
| 5 | `aws_cloudwatch_log_group.lambda` | Log group `/aws/lambda/daalu-demo-flaky` | 7-day retention so ingest stays well under the 5 GB/month always-free tier. Created explicitly (not via Lambda's auto-create) so retention is set from day one. |
| 6 | `aws_lambda_function.flaky` | Lambda `daalu-demo-flaky` | Python 3.12, 128 MB, 10s timeout. Code is `lambda/handler.py`, packaged into `lambda/build.zip` by `archive_file.lambda_zip` at apply time. `ERROR_PROBABILITY` env var (default `0.1`) controls how often it raises. X-Ray tracing is `PassThrough` (off, but inherits any caller's trace context). |
| 7 | `aws_cloudwatch_event_rule.lambda_schedule` | EventBridge rule `daalu-demo-flaky-schedule` | `schedule_expression = "rate(5 minutes)"` → ~8.6k invocations/month vs the 1 M/month free tier. |
| 8 | `aws_cloudwatch_event_target.lambda_schedule` | EventBridge target | Wires the rule to the Lambda. |
| 9 | `aws_lambda_permission.events_invoke` | Lambda resource policy statement | Lets `events.amazonaws.com` actually call `InvokeFunction` on the Lambda (rules can't invoke without this even if the target points at them). |

#### Daalu integration identity — the credentials you paste into the wizard

| # | Terraform address | AWS resource | Notes |
|---|---|---|---|
| 10 | `aws_iam_user.daalu` | IAM user `/daalu/daalu-remediation` | Programmatic-only. The `path = "/daalu/"` keeps it grouped in the IAM tree, separate from human users. |
| 11 | `aws_iam_user_policy.daalu_read_only` | Inline policy `DaaluReadOnly` | Verbatim copy of the policy in `daalu-automation/docs/cloud integrations/aws.md` §4.2 — `sts:GetCallerIdentity` (for the wizard's Test connection probe), EC2 / RDS / Lambda / CloudWatch Logs / CloudWatch Metrics describes, plus the forward-compatible X-Ray actions from §10 so the policy doesn't need re-editing when X-Ray ships. |
| 12 | `aws_iam_access_key.daalu` | Long-lived access key | Exposed as the `daalu_access_key_id` (plain) and `daalu_secret_access_key` (sensitive) outputs. The secret lives in `terraform.tfstate` in plaintext — the bundled `.gitignore` keeps state out of git. |

#### Data sources (not created, but read at plan time)

| Terraform address | What it reads | Why |
|---|---|---|
| `aws_caller_identity.current` | The AWS account ID Terraform authenticated as | Surfaced via the `aws_account_id` output so you can double-check you targeted the right account before pasting keys into daalu. |
| `aws_vpc.default` | The default VPC for the region | The EC2 instance and security group live in it. |
| `aws_subnets.default` | All default subnets in that VPC | The EC2 instance is placed in the first one. |
| `aws_ssm_parameter.al2023_ami` | Latest AL2023 AMI ID | Avoids hard-coding an AMI ID that would rot. |

### How the pieces map to daalu's read tools

Once Terraform finishes and you paste the keys into daalu, this is what each
read tool from `aws.md` §1 will actually hit:

| daalu tool | Hits this resource | Useful in a synthetic alert because… |
|---|---|---|
| `aws_describe_instances` | `aws_instance.demo` | Returns instance state, tags, AZ — typical EC2 host-down triage shape. |
| `aws_describe_rds_instances` | *(no resource — returns empty)* | Demonstrates the empty-result path. Add an `aws_db_instance` later if you want real RDS signal. |
| `aws_describe_lambda` | `aws_lambda_function.flaky` | Combines config + last 60 min of `AWS/Lambda Errors` in one call. Because the function errors ~10% of the time on a 5-min cadence, this metric is always non-zero. |
| `aws_get_cloudwatch_logs` | `aws_cloudwatch_log_group.lambda` | The handler writes structured JSON lines tagged `level: ERROR / WARN / INFO`, so `filter_pattern` queries (`?ERROR ?WARN`) return real hits. |
| `aws_query_cloudwatch_metric` | Both — EC2 `AWS/EC2` basic metrics and Lambda `AWS/Lambda` metrics | `AWS/Lambda Errors / Invocations / Duration` and `AWS/EC2 CPUUtilization` are all populated. |

### Cost shape at idle

After the first apply, the steady-state monthly footprint (assuming nothing
else in the account) is:

- EC2 t3.micro × 720 hrs ≈ **750-hour 12-month free tier** (≈ $7.50/mo after that).
- Lambda invocations: 8,640/mo, GB-seconds ≈ 86 → **always-free** (1 M req + 400 k GB-s).
- CloudWatch Logs ingest: a few MB/mo → **always-free** (5 GB).
- EventBridge: 8,640 custom-bus events/mo → **always-free** (rules-on-default-bus are free).
- EBS gp3 root, 8 GB → **30 GB-month free tier for 12 months**.
- Data transfer: zero outbound (no public IP) → **free**.

## Step 3 — grab the daalu credentials

Terraform creates the IAM user, attaches the `DaaluReadOnly` policy (copied
verbatim from `daalu-automation/docs/cloud integrations/aws.md` §4.2), and
mints an access key. Pull the values out:

```bash
terraform output aws_region
terraform output daalu_access_key_id
terraform output -raw daalu_secret_access_key; echo
```

The secret is marked `sensitive` in `outputs.tf`, so it doesn't print
during `terraform apply`. The `-raw` flag prints it without quotes so you
can copy-paste cleanly.

> **State warning.** The secret access key lives in `terraform.tfstate` in
> plaintext. The bundled `.gitignore` keeps state out of git. Don't push
> state to a public bucket or share the directory.

## Step 4 — connect to daalu

Follow `daalu-automation/docs/cloud integrations/aws.md` §5:

1. Open `https://<your-daalu>/onboarding` and sign in with a tenant-admin
   account.
2. Click **Manage individually** (top right) → pick **AWS account** from
   the catalog. Or click **Begin** to walk through every integration in
   order.
3. On the **AWS account** step:
   - Toggle the step from **Skipped** to **Enabled**.
   - **Access key ID** — paste `terraform output daalu_access_key_id`.
   - **Secret access key** — paste `terraform output -raw daalu_secret_access_key`.
   - **Default region** — paste `terraform output aws_region` (i.e.
     `us-east-1`).
   - **Session token** — leave blank (the key is long-lived, not STS).
   - **Assume-role ARN** — leave blank (this demo uses the static-keys
     path, not cross-account assume-role).
4. Click **Test connection**. You should see a green badge:
   `AWS accepted; caller arn = arn:aws:iam::<account-id>:user/daalu/daalu-remediation`.
   - If you see `InvalidClientToken` → access key wasn't copied
     correctly; re-paste.
   - If you see `SignatureDoesNotMatch` → secret wasn't copied
     correctly; re-paste from `terraform output -raw ...`.
   - If you see `AccessDenied` on `sts:GetCallerIdentity` → the policy
     drifted; `terraform apply` again.
5. Click **Save & continue**.

## Step 5 — verify daalu can see things

Once saved, daalu's alert-chat agent can call the five AWS read tools on
demand. The cheapest way to confirm end-to-end wiring is from the
operator side, per `aws.md` §11:

```bash
# Confirm the integration row landed (replace TENANT_TOKEN + host).
curl -fsS -H "Authorization: Bearer $TENANT_TOKEN" \
  https://<your-daalu>/api/v1/integrations/config | jq '.[]|select(.provider=="aws")'

# After the agent has called something:
kubectl -n daalu-automation exec daalu-postgres-0 -- \
  psql -U daalu -d daalu_automation -c "
    SELECT tool_name, status, executed_at, LEFT(result_output, 80)
    FROM alert_actions
    WHERE tool_name LIKE 'aws_%'
    ORDER BY executed_at DESC LIMIT 10;"
```

Helpful pointers when firing a synthetic alert against the demo:

```bash
terraform output ec2_instance_id        # use in an EC2-flavoured alert
terraform output lambda_function_name   # use in a Lambda-flavoured alert
terraform output lambda_log_group_name  # use as a CloudWatch Logs target
```

Because the Lambda errors ~10% of the time on a 5-minute cadence, the
`AWS/Lambda Errors` metric and the log group both have continuous fresh
signal — there's always something for the agent to triage.

## Tearing it down

```bash
terraform destroy
```

This deletes the EC2 instance, Lambda, log group, EventBridge rule, IAM
user, policy, and access key. The `daalu-remediation` access key in daalu
will start returning `InvalidClientToken` on the next tool call; toggle the
AWS step back to **Skipped** in the daalu wizard, or re-run `terraform
apply` to mint a fresh key.

## Costs after the 12-month free tier

If you keep this running past month 12 of the AWS account, the EC2
instance becomes the only non-trivial cost (~$7.50/month for t3.micro
on-demand in us-east-1). Everything else stays in the always-free tier.
Set a billing alert at $1 in the AWS console if you want a tripwire.

## Files

```
.
├── README.md                  # this file
├── versions.tf                # required_providers
├── providers.tf               # aws provider + default tags
├── variables.tf               # inputs
├── main.tf                    # data sources (default VPC, AMI, caller identity)
├── ec2.tf                     # t3.micro + security group
├── lambda.tf                  # lambda, log group, schedule
├── lambda/handler.py          # the python handler
├── iam.tf                     # daalu-remediation user + DaaluReadOnly + access key
├── outputs.tf                 # values to paste into the daalu wizard
├── terraform.tfvars.example   # copy to terraform.tfvars to customise
└── .gitignore                 # keeps state + secrets out of git
```
