# 44. Troubleshooting

When something doesn't work, this chapter is the first place to
look. It's organized by symptom rather than by root cause —
how things present to you, in the order you'd encounter them.

For each symptom you'll find the most likely cause, the quick
check, and the fix. For deeper issues, the engineer book's
Part IX has full runbooks.

---

## "I can't log in"

Symptoms: the login form rejects your credentials, or you log
in but get bounced back to the login screen.

Likely causes (most-common first):

1. **Wrong sign-in method.** If your account came in through
   Google, use **Continue with Google** — it has no Daalu
   password, so the email+password form will say "Invalid
   email or password" no matter what you type.
2. **Unverified account.** A self-signed-up account must click
   its email-verification link before it can sign in ("This
   account is inactive"). Use **Resend the link** on the
   sign-up confirmation screen.
3. **Wrong password** on a local account. There's no
   self-service reset yet — ask a tenant admin to re-invite
   you, or write to support.
4. **Browser cookies disabled** for `ops.example.com`. Daalu uses
   an HttpOnly session cookie; without cookies, no auth.
5. **Your session expired** during a long-running action. The
   bounce is normal; log in again.

If you've been removed from the tenant, you'll see a
specific "Account no longer has access" message. Ask an admin
to re-invite you.

---

## "An integration row is red"

Symptoms: a Cloud Account / Source of Truth / Notification
row shows status `error`.

Quick check: click the row → **Health** tab. The most recent
probe response is shown verbatim.

Common errors and fixes:

- **AWS: AccessDenied** — IAM trust policy or external ID
  changed. Re-verify the role.
- **GCP: 401 invalid_grant** — workload identity binding was
  removed. Re-bind in IAM.
- **Slack: token revoked** — the workspace admin removed
  Daalu's app. Re-authorize.
- **Nautobot: 401 Unauthorized** — API token expired or was
  rotated. Mint a new one.
- **Anything with timeouts** — the target system is slow or
  unreachable. Check from your side first.

---

## "I'm not getting Slack notifications"

Quick checks, in order:

1. **Did the alert fire?** *Alerts* page — confirm there's a
   matching firing alert.
2. **Are notifications enabled for you?** *Settings →
   Preferences → Per-channel toggles.*
3. **Is Slack the destination for this alert?** Click the
   alert → "Routing decision."
4. **Is the Slack channel allowed?** *Managed infra →
   Integrations → Slack* — verify the channel is in the
   allow list and Daalu has been invited (`/invite @daalu`).
5. **Has the alert been snoozed?** Look at the alert's pill
   bar.

If all those pass and you still don't get Slack, fire a test
notification from *Settings → Alerts → Send test alert* to
isolate which step is broken.

---

## "The Assistant is hanging / timing out"

Quick checks:

1. **Is the LLM router stuck on fallback?** *Usage & Pricing →
   Local GPU* — if the pill is offline, calls fall through to
   the next tier (typically slower).
2. **Is your prompt too long?** If you're pasting megabytes
   of logs, truncate.
3. **Is the Daalu deployment itself degraded?** Ask your
   operator to check the platform's health.

If hung for >2 minutes, refresh the page and ask again. The
underlying call should have timed out and been retried by
then.

---

## "My GPU has said 'Deploying…' for ages"

A first vLLM boot is genuinely long: a ~10 GB serving image,
~5 GB of model weights, then GPU-kernel compilation — three
multi-minute waits that look identical from outside.

Open the **Deploy progress** panel on the Local GPU card
(*Usage & Pricing*). It shows the pod, each container's state
with the blocking reason, cluster events, and the vLLM logs
once they exist. What to look for:

- **Pulls progressing in the events** — it's working; wait.
- **`ImagePullBackOff` / `ErrImagePull`** — the node can't
  reach the registry; check cluster egress.
- **`Pending` + `Insufficient nvidia.com/gpu`** — the GPU
  Operator isn't advertising the card on that node.
- **`CrashLoopBackOff` + CUDA/VRAM errors in the logs** — the
  model doesn't fit the card; pick a smaller model.

---

## "The management-stack install is stuck"

Open the environment's tile (*Managed infra → Network &
servers*) — the install-progress view shows every pod and the
**notable events** that explain a stall.

- **Pods in `Init:…` / `ContainerCreating` / `Pending` for a
  few minutes** — normal on a first install; image pulls
  dominate.
- **`FailedScheduling` + `Insufficient cpu`** — your **Size**
  pick is too big for the cluster. Small fits a single-node
  cluster; Medium/Large raise CPU requests substantially.
- **Interrupted install** ("another operation is in
  progress") — self-healing: Daalu detects and recovers a
  wedged install or upgrade on its next pass, without your
  intervention.

---

## "A stack console shows its own login page"

Opening **Source of truth** / **Workflows** / any console from
an environment card should sign you in automatically with your
Daalu session — a console presenting its own username/password
form is a fault, not a step. Sign out of Daalu and back in
(this refreshes the session the consoles inherit); if it
persists, write to support. The only account that intentionally
uses a console-local login is the **break-glass admin**
(Chapter 27), for when single sign-on itself is down.

---

## "The Topology page is empty or disabled"

1. **"Live topology isn't enabled for this workspace yet"** —
   the `network_topology_enabled` feature flag is off for your
   tenant. Ask your Daalu contact.
2. **"No devices in the source of truth yet"** — onboard your
   fabric first: *Managed infra → Network & servers → your
   environment → Onboard your devices*.
3. **All links grey ("unknown")** — the LLDP probe can't reach
   your devices. Check that the tenant-wide **SSH credential**
   is saved in the onboarding panel and that the devices'
   management IPs are right in the source of truth. Private
   management IPs are fine — they're dialled through your
   cluster tunnel — but the tunnel must be **connected**.
4. **A spine↔server link marked "Miscabled" that you know is
   fine** — check whether both endpoints are management ports;
   genuine management-network adjacency renders as the faint
   **Mgmt** style, not Miscabled. If a data-plane link is
   marked Miscabled, believe the graph first: it means LLDP
   sees a neighbour that doesn't match the intended cabling.

---

## "Bulk import won't accept my YAML file"

The upload accepts `.yaml`, `.yml`, and `.xlsx`. Some desktop
file dialogs filter by MIME type and can hide YAML files even
though they're accepted — switch the dialog's filter to "All
files" and pick the file; the import itself handles it fine.

---

## "The onboarding panel says Nautobot is 'not wired yet'"

The platform stamps the bundled source-of-truth credential
automatically when the environment finishes installing. If the
environment is still provisioning, wait for **All components
are ready**. *"Wired, not answering yet"* is the normal
mid-install state — the panel keeps retrying on its own. If an
old environment predates auto-wiring, open its progress view
once (*Managed infra → Network & servers → the environment*) to
trigger it.

---

## "A change proposal won't approve"

Symptoms: the approve button is grayed out, or clicking it
returns an error.

Reasons:

- **You proposed it.** Four-eyes rule. Someone else needs to
  approve.
- **It's already approved/applied/denied.** Check the
  proposal's current status.
- **The integration the proposal targets is in error state.**
  Daalu won't execute against an unhealthy integration. Fix
  the integration first.
- **Workflow approval gate.** If the proposal is part of a
  multi-step workflow with a specific approver, only that
  approver can advance it.

---

## "Cluster federation is stuck on 'awaiting handshake'"

Quick checks (from the cluster side):

1. **`kubectl -n daalu-edge get pods`** — is the edge pod
   running? If crashlooping, inspect logs.
2. **`kubectl -n daalu-edge logs deploy/daalu-edge`** —
   look for connection errors. Common: outbound UDP/51820
   blocked by your egress firewall.
3. **`kubectl -n daalu-edge get jobs`** — did the bootstrap
   Job complete? If it failed, the token may have expired
   (1 hour limit). Regenerate the invite.

From the Daalu side:

1. **Did you generate the invite recently?** Tokens expire
   after 1 hour.
2. **Is the cluster row's WireGuard public key set?**
   *Managed infra → Clusters & observability → \[cluster\] →
   Tunnel diagnostics.*

---

## "Devices aren't showing on the Operations page"

Quick checks:

1. **Is the source of truth healthy?** Bundled: the *Onboard
   your devices* panel should read **Nautobot connected**.
   BYO: the row under *Managed infra → Integrations → Source
   of Truth* should be green.
2. **Has the initial sync completed?** Detail page shows
   "Last sync."
3. **Did the devices match Daalu's filters?** *Settings →
   Reconciliation → Device filters* — Daalu may be
   intentionally excluding sites/roles.

---

## "Drift won't clear"

Symptom: you approved a drift proposal, the executor
reported success, but the next reconciler cycle still shows
drift.

Likely causes:

1. **Normalization mismatch.** The intended config and live
   config differ in a way Daalu thinks is significant but
   shouldn't. Check the diff details; tune *Settings →
   Reconciliation → Normalization rules.*
2. **The change didn't actually apply.** Check the
   proposal's audit log — did the executor's API call
   actually return success?
3. **The device reverted.** Some devices have configs
   loaded from external sources (TFTP, RANCID) that
   override running config. Daalu pushed; the next reload
   pulled back.

---

## "I see a 'rate limit exceeded' error"

Most common when a script or workflow is calling the API at
high volume.

Quick checks:

1. **Per-PAT rate limit:** 60 requests/minute by default.
2. **Per-IP rate limit:** 600 requests/minute by default.

Raise via *Settings → API → Rate limits* (admin only). Or
batch your requests.

---

## "The home page is empty"

If you've connected sources and the home page is still
showing empty states:

1. Hard-refresh the browser (Cmd-Shift-R / Ctrl-Shift-R).
2. Check the briefing-time setting; if you're looking before
   today's briefing has generated, it'll be yesterday's.
3. Click "Regenerate" on the briefing card.

---

## When to write to support

Open a ticket from *Help & Feedback* if:

- An integration is green per the health check but visibly
  not working.
- The Assistant gives an answer that's confidently wrong on
  a topic that matters.
- Anything billing-related that doesn't match your
  expectation.
- A production-impacting outage of any kind.

Include in the ticket:

- The version + commit from the *Help* page.
- The URL where the symptom appeared.
- Screenshots if the UI is involved.
- Approximate time the symptom started.

Tickets with these details typically resolve within hours;
without, the back-and-forth takes days.

---

## What's next

Chapter 45 is the FAQ — the questions we get asked most.
Chapter 46 is the glossary.
