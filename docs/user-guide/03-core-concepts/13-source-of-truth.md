# 13. Source of Truth — your network devices

If you operate a real network — routers, switches, firewalls,
load balancers — you've already discovered that keeping your
inventory in sync with what's actually on the boxes is a hard
problem. The inventory lives in Nautobot, Netbox, or a wiki.
The live state lives on the devices. Reality and intent drift
apart, slowly and then quickly, and nobody catches it until a
config change goes wrong.

Daalu's **Source of Truth (SoT)** features address this problem
directly. This chapter explains the model and walks you through
setup. Chapter 18 covers the page-by-page UI for the
*Operations* page; Chapter 30 covers the deeper Nautobot
integration mechanics (webhooks, custom fields, vendor drivers).

---

## The vocabulary

Three terms come up everywhere:

- **Source of Truth (SoT).** Your authoritative inventory
  system — Nautobot in the current product. It says what each
  device *should* look like.
- **Live state.** What the device *actually* looks like, right
  now, on the box.
- **Drift.** A difference between the two. Daalu's reconciler
  detects drift on a schedule.

Drift isn't inherently bad. Sometimes drift is a planned change
someone made on the device that hasn't yet been written back to
Nautobot. Sometimes it's a misconfiguration. Sometimes it's an
outage in progress. The point of SoT is to surface every
instance so a human can decide which it is.

---

## Setting it up

Before any of the reconciler / drift / proposals machinery does
anything for you, four things have to be in place. The
*Operations* page in the sidebar will show empty tabs and a
"Couldn't load devices" error until they are.

Where you do this depends on which Nautobot you use — and the
common path now involves **no wizard at all**.

### 1. Connect a Nautobot

Two options:

- **Use the bundled source of truth (the common path).** If your
  tenant runs a management environment (Chapter 27), it already
  includes a Nautobot — and it is **wired to Daalu
  automatically**. The platform stamps the API credential itself
  and reaches the service over your cluster tunnel; there is no
  URL and no token to paste, ever. The **Onboard your devices**
  panel on the environment's detail page (*Managed infra →
  Network & servers → your primary environment*) shows the live
  wiring status and is where the rest of this checklist lives.
- **Bring your own Nautobot.** If you already operate one, add
  it under *Managed infra → Integrations → Source of Truth*:
  paste its base URL and an API token. The token needs
  view+add+change+delete on `dcim.*` and `ipam.*` for your
  Nautobot tenant — a token for a user with those scoped
  ObjectPermissions is the safest shape. The "Test connection"
  button hits `/api/status/` so you can see "Nautobot accepted
  the token" before saving. Use your Nautobot's **real** base
  URL here — not the address of a Daalu-provisioned console
  (the tester will refuse those with an explanation, since a
  browser console can't carry an API token).

You can optionally add a **webhook secret** in the BYO flow. If
you set it (and configure the matching Webhook in Nautobot —
Chapter 30 has the exact steps), Daalu's mirror stays
event-driven instead of polling.

### 2. Set up device credentials

Daalu's executor needs credentials to reach your devices. The
**Onboard your devices** panel covers the most common case — a
single **tenant-wide SSH credential** (user, plus either a
password or a private key) reused for every SSH-reachable device
you onboard. For Cumulus/NVUE switches the user is typically
`cumulus`. The same credential powers config push, drift checks,
the topology probe, and the in-browser device terminal.

For non-Linux transports — server BMCs (Redfish), Juniper Junos,
Cisco IOS-XR, Arista EOS — credentials live under separate
provider rows on the Integrations tab (`redfish_credentials`,
etc.). You can add those any time before you onboard a device
using that transport.

All credentials are **encrypted the moment they're saved** —
whether entered in the UI or via the API — and never appear in
logs. Reading a credential row back returns it redacted; there
is no way to retrieve a stored secret in plaintext.

### 3. Add your devices

Two paths, same outcome:

- **One at a time.** *Operations → Devices → Add device.* You
  fill in name, transport, primary IP, and the catalogue fields
  (site, device type, role, optional platform). Daalu creates
  the row in your Nautobot tenant.
- **Bulk.** Upload a YAML or Excel file — from the **Onboard
  your devices** panel (bundled path) or *Operations → Bulk
  import* (same view). See
  [Bulk import](#bulk-import--getting-started-fast) below for
  the schema.

After this step, devices show up in the *Devices* tab — but the
reconciler is **not yet doing anything with them**. They're
inventory rows; you've told Daalu they exist. The next step is
what turns reconciliation on.

### 4. Author intent per device

For each device, open its detail page from the *Devices* tab and
fill in the **intended config** editor. This writes a
`daalu_intent` Config Context onto the Nautobot device — the
"what *should* be true" half of the drift comparison.

Until a device has `daalu_intent` set, the reconciler skips it
silently (it has nothing to compare against) and the "Reconcile
now" button on the detail page returns `device has no
daalu_intent set — author it from the device page first`. This
is by design: an empty intent shouldn't be interpreted as "the
device should be empty."

Once intent is set, the reconciler picks the device up on its
next sweep (every 5 minutes by default). The *Drift* tab is
where you'll see results.

---

## The reconciler

A background agent — the **reconciler** — runs on a schedule
(default: every 5 minutes per device, configurable per tenant).
For each device on its watch list, it:

1. Pulls the intended config from Nautobot.
2. Connects to the device and reads the live config.
3. Compares them, normalizing for things like comment lines and
   whitespace that don't matter.
4. If they match, records a "in sync" timestamp and moves on.
5. If they don't match, writes a **change proposal** with
   `kind=drift` describing the diff and proposing the corrective
   action.

The proposal lands on the *Operations → Proposals* page. You
read it, you decide whether to apply Nautobot's intended config
to the device (the most common path) or to update Nautobot to
match the device (the "we made the change directly on the box
and forgot to update SoT" path). Either way, a single approval
closes the gap.

---

## What you see in the UI

Everything SoT lives on a single page — *Operations* in the
sidebar — organised into tabs:

- **Overview** — vocabulary refresher plus the three KPIs you
  care about (devices in SoT, devices in sync, open drift).
- **Devices** — every device the reconciler is watching.
  Filterable by transport, with a hostname search.
- **Drift** — currently-drifted devices. This is the tab you
  visit first thing in the morning if you operate a network.
- **Proposals** — the full change-proposal queue (drift,
  ai-suggested, manual, workflow).
- **Bulk import** — upload your inventory as YAML or Excel.
  See "Bulk import" below.
- **Routine runs** — history of reconciler activity and recent
  drift detections. Useful for "why didn't it catch X?"

Each device's detail page shows:

- Its identity (hostname, transport, primary IP, tags).
- A "Reconcile now" button to trigger an on-demand check —
  surfaces the result inline (in sync / drift / skipped / error).
- The intended config editor.
- A jump-off link to any open proposals scoped to that device.

---

## Bulk import — getting started fast

Most teams don't want to add 200 routers one at a time. The
*Operations → Bulk import* tab takes a YAML or Excel file and
creates the device rows for you in your tenant's Nautobot.

The flow is two-phase by design:

1. Upload your file. Daalu parses it and resolves the names you
   used for site / device type / role / platform against your
   actual Nautobot catalogue — case-insensitively.
2. You see a row-by-row preview: green for valid, red for the
   rows that won't import (with the reason). Nothing has been
   written yet.
3. Click **Apply** and Daalu creates each valid row. Rows with
   errors are skipped without aborting the batch.

The expected YAML shape:

```yaml
devices:
  - name: web01
    primary_ip: 10.0.0.5/24
    transport: linux_ssh
    site: dc1
    device_type: generic-server
    role: server
    platform: linux            # optional
```

For Excel: first sheet, first row = headers (case-insensitive).
Columns: `name`, `primary_ip`, `transport`, `site`,
`device_type`, `role`, `platform` (the last one optional).

Notes that save a support ticket:

- **Name things the way your catalogue does.** `site`,
  `device_type`, `role`, and `platform` are matched
  case-insensitively against your Nautobot's catalogue — for
  device types, use the *model* name (e.g. `MSN3700`), which is
  what the dry run resolves.
- **The file picker accepts both formats.** The dialog lists
  `.yaml`/`.yml` and `.xlsx` files; if your desktop's file
  dialog seems to hide YAML files, switch its filter to "All
  files" — the upload itself accepts them regardless.

Whether you bring your own Nautobot or use the bundled
instance, this works the same way — the upload writes through
your Nautobot integration. Once the import lands, the devices
appear in the *Devices* tab **and** on the live topology map
(Chapter 49).

---

## Why this is a big deal

Most network teams have a stack like:

- Nautobot for inventory.
- Ansible / Nornir / scripts for config push.
- A wiki for the runbooks.
- An on-call rotation for the human side.

This stack has no closed loop. The push direction works
(Nautobot → device), but there's no automated "did it actually
take effect, and is it staying that way?" check. Drift goes
undetected until something breaks.

Daalu's reconciler is the missing piece. It runs continuously,
files structured proposals when it spots something, and gives
your team a single page where the state of the world is
inspected.

A few patterns customers tell us they spot within weeks of
turning this on:

- Devices where someone made a "quick" CLI change six months ago
  that was never rolled into Nautobot. Now revealed.
- ACLs that have drifted because someone applied a per-incident
  exception and never reverted.
- Firmware versions that diverge across a fleet because the
  upgrade was applied to "all of prod" but missed three boxes.
- Phantom devices in Nautobot that no longer exist on the
  network.

None of this is glamorous, but it's exactly the kind of latent
operational debt that turns a small change window into an
all-night incident.

---

## Approval and execution

The drift workflow is the same as every other change proposal:

1. Reconciler writes the proposal.
2. The proposal goes to *Operations → Proposals*.
3. A human (not the reconciler, which has no human identity)
   approves or denies.
4. If approved, the **executor** — a special service that has
   the credentials to push to devices — runs the change.
5. The proposal is marked complete with a timestamped audit
   trail.

The executor is the only identity in Daalu with credentials to
mutate device state. The split between "reconciler that
proposes" and "executor that acts on approved proposals" is what
allows the safety guarantee: an automated detector can never
also be the thing that fixes drift without humans in the loop.

---

## Confidence and trust

Customers reasonably worry about giving an automated system the
ability to push to network devices. Two specific safety
mechanisms:

- **No self-approval.** The reconciler has no human user
  account. Its proposals always need a human approver.
- **No drift-on-drift.** Once a drift proposal is open, the
  reconciler won't write more proposals for the same device
  until the first one closes. So if you're slow to approve, you
  don't get a queue of duplicates.

A useful rollout pattern: enable the reconciler in
**read-only** mode for the first few weeks. Daalu will detect
drift and write proposals, but the executor will refuse to apply
any of them. You read the proposals, mentally walk through what
*would* have happened, and only then enable execution. This is
the same staged-rollout pattern as the cloud-account
read-then-write story in Chapter 7.

---

## What Daalu is not trying to be

A clarification that comes up often:

- **Daalu is not your config push tool.** Ansible/Nornir/your
  scripts can stay. Daalu writes the proposal; the executor's
  back-end can be your scripts.
- **Daalu is not your inventory authority.** Nautobot remains
  the SoT. Daalu mirrors it and can write back, but it doesn't
  try to *be* the SoT.
- **Daalu is not a CMDB replacement.** A full CMDB models
  applications, services, dependencies, ownership — much more
  than network inventory. Daalu's SoT is scoped to "devices and
  their config." For the wider world, Daalu integrates with
  your CMDB rather than replacing it.

---

## What's next

Chapter 14 covers cluster federation — the other major
infrastructure-facing feature, this one for Kubernetes clusters.
Chapter 18 in Part IV is the UI walkthrough for the
SoT-related pages.
