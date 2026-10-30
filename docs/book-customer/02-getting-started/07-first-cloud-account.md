# 7. Connecting your first cloud account

This chapter walks through connecting AWS, GCP, or Azure for the
first time. By the end of it, your tenant will have a working
cloud integration and the rest of the UI will start showing real
data instead of empty states.

We'll use AWS as the running example. GCP and Azure are very
similar — the screens look the same, only the credential format
changes. The three Cloud-specific sections at the end of the
chapter detail the differences.

---

## Before you start

A few decisions to make. You don't need to commit to all of them
now; you can change any of these later. But thinking about them
once saves you from churning through the connection flow three
times.

### Read-only or read-write?

Daalu can be granted **read-only** or **read-write** access to
your cloud. Read-only is enough for almost all features:
inventory, alerting, the Assistant's investigation, billing
queries. Read-write enables the Assistant to apply changes (e.g.,
restart an ECS service, scale an ASG, terminate an EC2 instance)
after a human approves.

Recommendation for first connection: **read-only.** Get the
inventory flowing. Once your team is comfortable with what Daalu
sees and how it behaves, you can add a separate read-write role
later. Two-step rollouts of this kind are how most teams adopt the
platform without spooking their security review.

### Which account?

If your organization has many AWS accounts (production, staging,
dev, per-team), pick the one you'd most like Daalu to manage
*first*. That's usually production, but if your security review
takes a while, it's also fine to start with staging and add
production once you're satisfied.

You can connect many accounts; there's no limit. The UI groups
them under your tenant, and the Assistant can correlate across
them.

### Which user is doing this?

This is a one-time setup that requires AWS permissions to create
an IAM role. The Daalu user doing the connecting only needs admin
on the tenant. The two identities are independent.

---

## The connection flow at a high level

For every cloud provider, the pattern is the same:

1. You go to *Managed Infra → Cloud Accounts → Add* and pick the
   provider.
2. Daalu shows you a "wizard" that displays an **External ID** and
   a **trust policy** specific to your tenant.
3. You go to your cloud provider's console and create a role that
   trusts Daalu's account, scoped by the External ID.
4. You paste the ARN of that role (or equivalent identifier for
   non-AWS providers) back into Daalu.
5. Daalu validates the credential by making a harmless API call.
   If it succeeds, the row turns **connected**.

This is the standard AWS-cross-account-role pattern, applied
identically to GCP (workload identity federation) and Azure
(service principal). No long-lived API key changes hands.

---

## Step-by-step: AWS

We'll do this in two browser windows: one for Daalu, one for AWS.

### 1. Start the wizard in Daalu

Sidebar → **Managed Infra → Cloud Accounts → Add cloud account.**

A modal opens. Choose **AWS**. Click **Continue.**

The wizard now shows you:

- A **Daalu AWS account ID** — the AWS account number that Daalu
  itself runs in. You'll use this in your trust policy.
- An **External ID** — a long random string unique to your
  tenant. This is the thing that prevents the "confused deputy"
  attack: anyone who somehow knows Daalu's account ID still
  can't assume your role without your specific External ID.
- A pre-rendered **trust policy** JSON blob you can copy.

Keep this tab open.

### 2. Create the IAM role in AWS

Open the AWS console, switch to the account you're connecting,
and go to **IAM → Roles → Create role.**

- **Trusted entity type:** AWS account.
- **An AWS account:** Another AWS account, then paste the
  Daalu AWS account ID from the wizard.
- Tick **Require external ID** and paste the External ID from the
  wizard.
- Leave MFA unticked.

Click **Next.**

On the **permissions** page, attach the policy that matches what
you want Daalu to see:

- For read-only: attach the AWS-managed `ReadOnlyAccess` policy.
- For read-write: read-only plus the
  `AmazonEC2ContainerRegistryPowerUser`, `AmazonEC2FullAccess`,
  and similar service-specific policies that match the operations
  you want the Assistant to perform.

A custom policy with only the actions your team uses is fine; we
publish a recommended minimum-scope policy at
`https://docs.daalu.io/aws-minimum-policy.json`. Many customers
attach that to their read-write role.

Click **Next**, give the role a clear name (e.g.,
`daalu-readonly-prod`), and **Create role.**

Copy the **Role ARN** from the new role's summary page —
it looks like `arn:aws:iam::123456789012:role/daalu-readonly-prod`.

### 3. Finish the wizard in Daalu

Back in the wizard, paste the Role ARN into the **Role ARN**
field. Pick a friendly name (e.g., "Acme production AWS"). Click
**Connect.**

Daalu calls AWS STS to assume the role. The wizard waits
synchronously for the response — usually 1–2 seconds.

Three outcomes are possible:

- **Success.** The row appears in *Cloud Accounts* with status
  **connected**, and you're done.
- **AccessDenied.** Almost always either (a) the External ID
  doesn't match, (b) the trust policy is missing the Daalu
  account number, or (c) the role doesn't have the
  `sts:AssumeRole` permission for the Daalu principal. The error
  message includes the exact AWS reason code; click "Show
  details."
- **Permission denied (post-assume).** You assumed the role, but
  the role's policy doesn't grant the actions Daalu tried.
  Re-attach a broader policy or use `ReadOnlyAccess`.

The most common failure is forgetting to paste the External ID
into the trust condition. AWS will let the role be created without
it, but the assume will fail.

---

## Step-by-step: GCP

The GCP flow uses **workload identity federation** with Google's
service-account impersonation. From your side:

1. In *Managed Infra → Cloud Accounts → Add → GCP*, Daalu shows
   the **Daalu service-account email** that your project will
   trust, plus a unique **audience** string.
2. In the GCP console, create a service account in your project
   (e.g., `daalu-readonly@acme-prod.iam.gserviceaccount.com`)
   and grant it the roles you want Daalu to use. A starting
   point is the `roles/viewer` role.
3. Add the Daalu service account as a member with the
   `roles/iam.workloadIdentityUser` role on the service account
   you just created, restricted by the audience string.
4. Paste the service-account email and the project ID into the
   Daalu wizard and **Connect.**

The wizard tests the integration with `gcloud compute instances
list` (or its API equivalent) — a benign call that requires
viewer-level access.

For organizations with many projects, repeat the flow per
project, or use a single "platform" service account that has
cross-project access via folder-level IAM.

---

## Step-by-step: Azure

Azure uses **federated workload identity** plus a service
principal. The high-level flow is:

1. In *Managed Infra → Cloud Accounts → Add → Azure*, Daalu shows
   the **tenant ID** and **issuer URL** that will identify it.
2. In the Azure portal, register an app registration in your
   tenant; under **Federated credentials**, add a credential whose
   issuer is the URL Daalu provided and whose subject is the
   string Daalu provided.
3. Assign the **Reader** role on the subscription (or wider
   scope) to the service principal.
4. Paste the **Application (client) ID**, **Directory (tenant)
   ID**, and **Subscription ID** into the Daalu wizard and
   **Connect.**

The "wider scope" choice matters: if you want Daalu to see all
your subscriptions, grant Reader at the management-group level.
If you want a per-subscription split, grant it on the
subscription.

---

## What populates after connection

Once the row turns **connected**, several things happen
asynchronously over the next 5–15 minutes:

- An inventory crawler walks your cloud and records every
  significant resource (EC2/Compute instance, ASG/MIG/VMSS,
  S3/GCS/Blob bucket, RDS/CloudSQL/SQL DB instance, etc.).
  Counts and aggregations show up on *Managed Infra → Cloud
  Accounts → row → Details*.
- The Assistant gains the ability to answer questions about that
  account ("how many EC2 instances do we have in us-east-1?").
- Cost data starts pulling from the provider's billing API and
  shows up on the *Billing* page (Chapter 22). The first 24 hours
  of cost data tends to be sparse — billing APIs are eventually
  consistent.
- If you enabled the Daalu Assistant's auto-recommendations,
  the agent will start surfacing things like unused EBS volumes
  and orphaned snapshots within a day or two.

If the row is **connected** but inventory doesn't show up within
30 minutes, check *Managed Infra → Cloud Accounts → row →
Health* — the page shows the last successful crawl and any
crawler errors.

---

## Granting write access later

The recommended path is to start read-only and add write later.
To do that:

1. Create a *separate* role in your cloud with the
   write-capable policy attached. Don't widen the existing
   read-only one — keeping them separate makes incident
   forensics much easier.
2. Add it as a *second* connection in the Daalu wizard, named
   e.g., "Acme production AWS (write)."
3. The Assistant detects both connections; it'll use the
   read-only one for queries and the write one when proposing
   mutations.

Both connections share the same External ID and the same
underlying Daalu identity — only the role and its policy differ.

---

## Disconnecting

To disconnect a cloud account at any time:

*Managed Infra → Cloud Accounts → row → **⋯** → Disconnect.*

Daalu stops calling your cloud immediately. Existing inventory
and cost history remain in your tenant for 30 days, then ages
out. To revoke from the cloud side as well — recommended — go
into your IAM and delete the role/service principal.

---

## What's next

You now have at least one cloud account connected. The home
page's briefing tomorrow morning will mention it; the *Managed
Infra → Cloud Accounts* page will show its inventory; and the
Assistant can answer real questions about your environment.

Part III starts the core-concepts chapters. Chapter 9 covers
tenants, users, and access control in more depth — relevant
because right now you're an admin of a one-person tenant, and the
patterns scale interestingly.
