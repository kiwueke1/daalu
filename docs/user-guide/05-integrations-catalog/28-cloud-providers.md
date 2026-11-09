# 28. Cloud providers (AWS, GCP, Azure)

Chapter 7 walked through the first-time AWS setup with detailed
hand-holding. This chapter is the reference: every cloud
provider, the credentials it takes, and what each gives you.

All three live in the same place: *Managed infra → Clusters &
observability → Cloud accounts*. Each provider has one row.
Click **Connect** (or **Edit** once it's connected), fill in the
dialog, click **Test connection** to check the credentials, then
save with **Connect \[provider\]**. You can also set them up in
order on the *Onboarding* tab.

Credentials are stored encrypted at rest. Cloud access is
**read-only**: the agent uses it to look at what an alert
touches while it investigates. Changes the AI proposes go
through the approval flow (Chapter 52), not through these
credentials.

---

## AWS

### Setup

The **AWS account** dialog asks for:

- **Access key ID** and **Secret access key**.
- **Default region** — for example `us-east-1`.
- **Session token** — optional, only for short-lived STS
  credentials.
- **Assume-role ARN** — optional. If you set it, the access
  keys above only need `sts:AssumeRole` on that role;
  everything else is granted by the role's policy.

**Test connection** asks AWS who the credentials belong to and
shows the caller ARN when they're accepted.

### What the agent can read

- EC2 instances (describe).
- CloudWatch log events and metric statistics.
- RDS instances (describe).
- Lambda functions (configuration).

### Permissions

Grant read-only access covering:
`ec2:Describe*`, `logs:FilterLogEvents`,
`cloudwatch:GetMetricStatistics`, `rds:Describe*`,
`lambda:GetFunction`. The AWS-managed `ReadOnlyAccess` policy
covers all of these if you'd rather not write your own.

### Troubleshooting

- **"AWS rejected the credentials"** on test — the keys are
  wrong, inactive, or the session token has expired.
- **AccessDenied while investigating** — the policy doesn't
  grant that action. Widen the policy, or use `ReadOnlyAccess`.

---

## GCP

### Setup

The **Google Cloud project** dialog asks for:

- **Project ID** — for example `acme-prod`.
- **Service-account key JSON** — paste the full key file your
  IAM admin downloaded.

**Test connection** fetches the project's metadata with that
key.

### What the agent can read

- Compute Engine instances.
- Cloud Logging entries.
- Cloud Monitoring metrics.
- Cloud SQL instance state.
- Cloud Functions configuration.

### Permissions

Bind these roles to the service account:
`roles/compute.viewer`, `roles/logging.viewer`,
`roles/monitoring.viewer`, `roles/cloudsql.viewer`,
`roles/cloudfunctions.viewer`.

### Troubleshooting

- **"GCP rejected the credentials"** on test — the key is
  malformed, deleted, or for a different project.
- **Permission errors while investigating** — the service
  account is missing one of the roles above.

---

## Azure

### Setup

The **Azure subscription** dialog asks for a service principal
(an Azure AD app registration with a client secret):

- **Azure AD tenant ID**.
- **Application (client) ID**.
- **Client secret**.
- **Subscription ID**.

**Test connection** checks that Azure issues a management token
for the service principal.

### What the agent can read

- Virtual machines.
- Log Analytics (Kusto queries).
- Azure Monitor metrics.
- SQL databases.
- App Service / Functions.

### Permissions

Assign **Reader** at subscription scope, **Monitoring Reader**,
and **Log Analytics Reader** on the workspace.

### Troubleshooting

- **"Azure rejected the service principal"** on test — the
  tenant ID, client ID or secret is wrong, or the secret has
  expired.
- **AuthorizationFailed while investigating** — a role
  assignment above is missing.

---

## Disconnection

Two-step revocation:

1. *Managed infra → Clusters & observability → Cloud accounts*,
   then the delete icon on the provider's row. It asks you to
   confirm in place ("the agent loses read access"). Daalu
   stops calling immediately.
2. In your cloud, delete or disable the access key, service
   account key, or service principal secret. Belt and braces.

---

## What's next

Chapter 29 covers notification integrations — Slack and
email — and PagerDuty as an alert source.
