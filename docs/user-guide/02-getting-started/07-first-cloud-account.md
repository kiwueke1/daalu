# 7. Connecting your first cloud account

This chapter walks through connecting AWS, GCP, or Azure for the
first time. By the end of it, your tenant will have a working
cloud integration: the AI can look at your cloud when it
investigates an alert, and (for AWS) firing CloudWatch alarms
arrive in Daalu as alerts.

We'll use AWS as the running example. GCP and Azure use the same
form — only the credential fields change. The cloud-specific
sections later in the chapter detail the differences.

---

## Before you start

### Read-only, always

Daalu's cloud connections are **read-only**. The AI uses them to
look things up while it investigates — describe instances, read
logs and metrics, check a database or a function — and never to
change anything in your cloud. Give Daalu a credential that can
only read; the permissions each provider needs are listed below
and in the connection form itself.

### Which account?

You connect **one account per provider** (one AWS account, one GCP
project, one Azure subscription) per tenant. If your organization
has many, pick the one you'd most like Daalu to look at first —
usually production, though starting with staging is fine while
your security review runs. To point Daalu at a different account
later, click **Edit** on the row and change the credential.

### Which user is doing this?

You need someone who can create a read-only credential in the
cloud account. The Daalu user doing the connecting only needs
admin on the tenant. The two identities are independent.

---

## The connection flow at a high level

For every cloud provider, the pattern is the same:

1. You go to *Managed infra → Clusters & observability*, find the
   provider under **Cloud accounts**, and click **Connect**.
2. In your cloud, you create a read-only credential with the
   permissions the form lists.
3. You paste it into the form and click **Test connection**.
   Daalu makes one harmless call to prove the credential works.
4. You click **Connect …** to save it. Credentials are encrypted
   the moment they're saved and are never shown back to you.

---

## Step-by-step: AWS

We'll do this in two browser windows: one for Daalu, one for AWS.

### 1. Open the form in Daalu

Sidebar → **Managed infra**, on the **Clusters & observability**
tab. Under **Cloud accounts**, find the **AWS account** row and
click **Connect.** A dialog opens with these fields:

- **Access key ID** and **Secret access key**.
- **Default region** (e.g. `us-east-1`).
- **Session token** (optional) — only for short-lived STS
  credentials.
- **Assume-role ARN** (optional) — if set, the access key only
  needs `sts:AssumeRole` on that role, and everything else is
  granted by the role's policy.

Keep this tab open.

### 2. Create a read-only credential in AWS

In the AWS console, for the account you're connecting, create an
IAM user (or a role, if you'll use **Assume-role ARN**) whose
policy allows only what Daalu reads:

- `ec2:Describe*`
- `logs:FilterLogEvents`
- `cloudwatch:GetMetricStatistics`
- `rds:Describe*`
- `lambda:GetFunction`

For CloudWatch alarms to show up as Daalu alerts, also allow the
CloudWatch call that lists alarms (`cloudwatch:DescribeAlarms`).
The AWS-managed `ReadOnlyAccess` policy covers all of these if you
prefer a managed policy.

Create an access key for the user, and copy the **Access key ID**
and **Secret access key**.

### 3. Test and connect in Daalu

Back in the dialog, paste the keys, set the default region, and
fill in the role ARN if you're using one. Click **Test
connection.** Daalu calls AWS STS *GetCallerIdentity* — the
cheapest "do these credentials work" check there is; it touches
no billable service.

- **Success** shows *"AWS accepted"* with the identity the keys
  belong to. Click **Connect AWS account** to save.
- **AWS rejected the credentials** shows AWS's own reason. Usually
  a mistyped key, a deactivated key, or a role the key isn't
  allowed to assume.

---

## Step-by-step: GCP

1. In the GCP console, create a service account in your project
   and grant it these roles: `roles/compute.viewer`,
   `roles/logging.viewer`, `roles/monitoring.viewer`,
   `roles/cloudsql.viewer`, `roles/cloudfunctions.viewer`.
2. Create a JSON key for the service account and download it.
3. In Daalu, click **Connect** on the **Google Cloud project** row.
   Enter the **Project ID** and paste the whole key file into
   **Service-account key JSON**.
4. Click **Test connection.** Daalu reads the project's metadata
   from Compute Engine — a call with no side effects that proves
   both the key and the project ID are valid. Then click
   **Connect Google Cloud project**.

To cover several projects, give one service account viewer access
to the others through folder-level IAM.

---

## Step-by-step: Azure

1. In the Azure portal, create an app registration (service
   principal) and a client secret for it.
2. Assign it **Reader** at subscription scope, **Monitoring
   Reader**, and **Log Analytics Reader** on your workspace.
3. In Daalu, click **Connect** on the **Azure subscription** row
   and fill in **Azure AD tenant ID**, **Application (client)
   ID**, **Client secret** and **Subscription ID**.
4. Click **Test connection.** Daalu asks Azure for a management
   token with the service principal — no resources are touched.
   Then click **Connect Azure subscription**.

---

## What happens after connection

- **The AI can look at your cloud.** When an alert touches cloud
  infrastructure, the AI can describe instances, read logs and
  metrics, and inspect databases and functions (EC2, CloudWatch,
  RDS and Lambda on AWS; Compute Engine, Cloud Logging, Cloud
  Monitoring, Cloud SQL and Cloud Functions on GCP; VMs, Log
  Analytics, Azure Monitor, SQL databases and Functions on Azure).
  You can ask the Assistant about them too ("which EC2 instances
  are running in us-east-1?").
- **CloudWatch alarms become alerts (AWS).** Every minute, Daalu
  checks for CloudWatch alarms in the *ALARM* state and brings them
  in as alerts on the *Alerts* page.
- **The row shows its status.** Daalu probes each integration
  regularly; if the credential stops working, the row's status
  turns red.

---

## Disconnecting

On the account's row under *Managed infra → Clusters &
observability → Cloud accounts*, click the bin icon. It asks you
to confirm in place (**Disconnect AWS account**, for example) and
says what happens: the agent loses read access.

Daalu stops calling your cloud immediately. To revoke from the
cloud side as well — recommended — delete or deactivate the key,
service account or app registration you created.

---

## What's next

You now have at least one cloud account connected. *Managed
infra → Clusters & observability* shows it under **Cloud
accounts**, and the Assistant can answer real questions about
your environment.

Part III starts the core-concepts chapters. Chapter 9 covers
tenants, users, and access control in more depth — relevant
because right now you're an admin of a one-person tenant, and the
patterns scale interestingly.
