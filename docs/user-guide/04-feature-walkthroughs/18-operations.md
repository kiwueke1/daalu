# 18. Operations — devices, drift, change proposals

If you operate a network, *Operations* is your home page. It's
the surface for device inventory, drift detection, the
change-proposal queue, and bulk inventory upload — everything
SoT in one place.

URL: `https://ops.example.com/operations`. Tabs are deep-linkable
via `?tab=devices` etc., so a bookmark or shared link drops you
straight onto the right view.

---

## Top-level layout

Tabs across the top, left to right:

- **Overview** — vocabulary refresher + the three KPIs
  (devices, in-sync, open drift) + jump-offs to the other tabs.
- **Devices** — the inventory page.
- **Drift** — the live-drift list.
- **Proposals** — every change proposal, kinds of all sorts.
- **Bulk import** — upload YAML or Excel to seed your inventory.
- **Routine runs** — history of reconciler and executor
  activity.

We'll cover each.

---

## Devices tab

The Devices tab is your device inventory. Each card is one
device (router, switch, firewall, server, etc.) being watched by
the SoT reconciler.

Each card shows:

- **Name** — the device's hostname.
- **Transport** — how Daalu reaches it (SSH, and so on).
- **Primary IP**.
- **Tags**, if any.
- **Remove** — deletes the device from Nautobot so Daalu stops
  managing it. It asks you to confirm in place (**Remove
  <name>**) and says what happens; the device itself is
  untouched.

Above the cards: a hostname **Search**, a **Transport** filter,
and the **Add device** button.

Click any card to drill into the **device detail page**:

- **Identity** — the device's ID, transport and primary IP.
- **Reconcile now** — trigger an immediate reconciler run for
  this device. It asks you to confirm in place (**Check device
  now**) and says what it will do: connect, compare with the
  intended config, and open a proposal for any difference —
  nothing is pushed.
- **Related proposals** — the proposals scoped to this device.
- **Intended config** — what Daalu keeps this device set to,
  as an editor. **Save & push to device** saves a new revision,
  which Daalu applies on the next reconcile. If you try to close
  or reload the tab with unsaved edits, the browser warns you
  first.

---

## Drift tab

A filtered view of the Devices tab showing only currently-
drifted devices. Each row also shows:

- A diff summary ("3 lines added, 1 removed").
- The age of the drift (when it was first seen).
- How many open proposals the device has, if more than one.
- **Open proposal** and **Open device** links.

If nothing has drifted, the tab says *"No devices are drifted"* —
new drift shows up within 5 minutes.

This is the page a NetOps team's daily standup tends to start
on.

---

## Proposals tab

The full queue of change proposals. *Operations → Proposals*
shows every kind:

- **drift** — the reconciler found the running config differs
  from the intended config.
- **intent** — the intended config changed and needs pushing to
  the device.
- **manual** — opened directly, by a person or by the AI.

Columns:

- **Title** — short description.
- **Kind** — see above.
- **Target** — which device / cluster / resource.
- **Status** — `pending`, `approved`, `executed`, `failed`,
  `stale`, `rejected`.
- **Author** — user, Assistant, reconciler, or workflow.
- **Created**.
Filter the list by status: Pending, Approved, Executed, Failed,
Stale, Rejected, or All.

Click any row for the detail page. If you're eligible to decide,
it has two buttons: **Approve & push**, which queues the change
for the executor, and **Reject**, which asks you to confirm in
place (**Reject proposal**) and tells you nothing will be pushed.
The page also shows:

- **Change for** — the device, and who created the proposal and
  when.
- **Why this change** — what triggered it, the AI's reasoning and
  confidence when the AI wrote it, which facts changed, and links
  to the triggering events and related alerts.
- **Diff** — the change, colour-coded line by line.
- **Executor result** — once it has run: *Pushed successfully*, or
  the reason the push failed.
- **Raw rendered configs** and **Raw evidence JSON**, folded away,
  for when you need the exact text.

Every approval, rejection and execution is also recorded in the
audit log (*Insights → Audit log*).

---

## How a drift proposal flows

End-to-end, here's the lifecycle:

1. Reconciler runs at 09:00. It reads device config from
   `edge-router-3`, reads the Nautobot intended config, sees
   `acl-10` has been edited on the device but not in Nautobot.
2. Reconciler writes a proposal: title "Drift on edge-router-3
   (ACL acl-10)", kind=drift, target=device-id, plan="apply
   intended config to device."
3. Proposal appears on the Drift and Proposals tabs.
4. Operator opens it and reads the diff. Two possible answers:
   - **The device is wrong.** Click **Approve & push** (someone
     other than the proposal's author must approve). The executor
     pushes the intended config to the device, re-rendered fresh
     at execution time, and records the result.
   - **The device change was intentional; the source of truth is
     out of date.** Update the intended config to match — in your
     source of truth, or under **Intended config** on the device's
     page — and **Reject** the proposal. Once the intent changes,
     an approved-but-unexecuted proposal flips to `stale` instead
     of pushing.
5. Proposal status → `executed` (or `rejected`). The reconciler's
   next run sees no drift and the device drops off the Drift tab.

The whole loop is auditable. The post-mortem question "why does
this device have this config?" is always answerable from the
proposals history.

---

## Bulk import tab

If you're onboarding a brand-new tenant, manually adding 200
devices through *Add device* is not going to be your friend.
The Bulk import tab lets you upload your whole inventory in one
shot. (The same importer is embedded in the **Onboard your
devices** panel on your management environment — *Managed infra
→ Network & servers* — alongside the SSH-credential step; use
whichever is closer to hand.)

Accepted formats:

- **YAML** — a top-level `devices:` list (or a bare top-level
  list) of records.
- **Excel (`.xlsx`)** — first sheet, first row = headers
  (case-insensitive). Empty rows are skipped.

Required columns: `name`, `primary_ip`, `transport`, `site`,
`device_type`, `role`. The `platform` column is optional.

Names in the catalog fields (site, device_type, role, platform)
resolve case-insensitively against your Nautobot — you don't
have to look up UUIDs.

The flow is always two-phase:

1. **Upload.** Daalu parses + validates against your catalog
   and shows a row-by-row preview. Valid rows are green; rows
   with errors are red, with the reason next to them. *Nothing
   has been written to Nautobot at this point.*
2. **Apply.** When you're happy, click *Apply N devices*. It asks
   you to confirm in place (*Apply to Nautobot*) and says how many
   objects it will write, and that there is no undo. Daalu then
   creates each valid row sequentially, skipping the bad ones.
   The preview table updates in place to show which rows were
   created and any per-row errors that surfaced during the
   actual Nautobot calls.

Tip: re-uploading the same file is harmless if you only fixed
the rows that errored — Nautobot will 4xx on duplicate names,
which surfaces as a per-row error rather than blowing away your
batch. Edit the file to only include the rows you want to
retry, or rename the duplicates.

---

## Routine runs tab

A history of every automated action that touched the Source of
Truth, newest first, merged into one timeline:

- **Reconciler snapshots** — each pass over your devices.
- **Drift proposals opened** — with the device they're about.
- **Executor runs** — approved changes being pushed.

Useful when you're debugging "why didn't the reconciler catch
X?" On a fresh tenant it's empty until the reconciler has run a
few cycles; it runs every 5 minutes.

---

## On-demand reconciliation

To check a device now rather than waiting for the next pass, open
it and click **Reconcile now**, then **Check device now** to
confirm. The result shows inline.

The reconciler checks every device every 5 minutes. That interval
is the same for the whole tenant and isn't set in the app; if you
need it changed, ask your operator.

---

## What's next

Chapter 19 covers the *Agents* page — the autonomous helpers
that run on schedules and triggers. The reconciler is an agent,
but only one of many.
