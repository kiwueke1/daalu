# 28. Cloud providers (AWS, GCP, Azure)

Chapter 7 walked through the first-time AWS setup with detailed
hand-holding. This chapter is the reference: every cloud
provider, every credential option, and what each gives you.

---

## AWS

### Setup

Pattern: cross-account IAM role with External ID. Chapter 7 has
the step-by-step.

### Capabilities

- **Inventory:** EC2, RDS, S3, Lambda, ECS/EKS clusters,
  CloudFront, Route53, ELB/ALB/NLB, Auto Scaling Groups, IAM
  users and roles (read-only).
- **Cost:** Pulled from Cost Explorer API; daily granularity,
  per-service / per-region / per-tag breakdowns.
- **Investigation tools (Assistant):** describe-instance,
  list-functions, get-log-events (CloudWatch), get-metrics.
- **Action tools (write only):** restart-ecs-service, scale-asg,
  terminate-instance, update-launch-template, restart-rds-
  instance. Each gated on a proposal.

### Permissions

- **Minimum read-only:** AWS-managed `ReadOnlyAccess`.
- **Recommended read-only:** custom policy at
  `https://docs.daalu.io/aws-minimum-policy.json` (smaller
  surface area, same coverage).
- **Read-write add-on:** separate role with the actions you
  want the Assistant to perform.

### Regions

The wizard asks for a list of regions to scope to. Defaults to
all enabled regions. Add or remove later from the integration
detail page.

### Troubleshooting

- **AccessDenied on AssumeRole** — External ID missing from
  trust policy, or Daalu account ID typoed.
- **AccessDenied on individual APIs** — role policy doesn't
  grant the action. Widen the policy or use ReadOnlyAccess.
- **Throttled** — Daalu retries with backoff; if it persists,
  AWS has per-account rate limits you may be hitting.

---

## GCP

### Setup

Pattern: workload identity federation with a Google service
account.

1. Create a service account in your GCP project.
2. Grant it `roles/viewer` for read-only access; add other
   roles for write capabilities.
3. Add Daalu's service account as a member with
   `roles/iam.workloadIdentityUser`.
4. Paste the service-account email and project ID into Daalu.

### Capabilities

- **Inventory:** Compute Engine, Cloud SQL, GCS, GKE clusters,
  Cloud Functions, Cloud Run services, Load Balancers, DNS
  zones.
- **Cost:** Pulled from Cloud Billing API.
- **Tools:** Equivalent to AWS — describe, list, scale,
  restart.

### Per-project vs organization-wide

For multi-project orgs, two options:

- Add each project separately. Tidy but tedious.
- Add one "platform" service account with org-wide viewer.
  Less granular but easier to operate.

### Troubleshooting

- **"Project not found"** — the SA can't see the project.
  Check IAM bindings.
- **"Permission denied"** — same as above but for a specific
  resource type. Add the needed role.

---

## Azure

### Setup

Pattern: app registration with federated workload identity.

1. Register an app in Azure AD.
2. Add a federated credential whose issuer is Daalu's IdP and
   whose subject is your tenant string.
3. Assign **Reader** role at the subscription level.
4. Paste app ID, tenant ID, and subscription ID into Daalu.

### Capabilities

- **Inventory:** Compute, Storage, SQL DB, Azure Functions,
  AKS, App Service, Front Door.
- **Cost:** Pulled from Cost Management API.
- **Tools:** Equivalent surface area to AWS.

### Scope: subscription vs management group

If you have many subscriptions, grant Reader at the
management-group level for unified visibility. Per-subscription
is fine if you want each scoped separately.

### Troubleshooting

- **"InvalidAuthenticationToken"** — federated credential
  misconfigured. Compare issuer and subject to what Daalu
  showed in the wizard.
- **"AuthorizationFailed"** — RBAC doesn't have Reader.

---

## Adding write access

The recommended pattern for all three clouds:

1. Start read-only.
2. Get comfortable with what Daalu sees and how the
   Assistant uses it.
3. Add a *separate* write-capable role/SP as a second
   integration.
4. The Assistant detects both and uses each appropriately.

---

## Disconnection

Two-step revocation:

1. *Managed Infra → Cloud Accounts → row → Disconnect.*
   Daalu stops calling immediately.
2. In your cloud, delete the role / SA / SP. Belt and braces.

Existing inventory and cost data ages out after 30 days.

---

## What's next

Chapter 29 covers notification integrations — Slack, PagerDuty,
email.
