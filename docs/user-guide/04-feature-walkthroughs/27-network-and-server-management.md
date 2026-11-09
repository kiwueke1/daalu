# 27. Network & server management

Daalu can manage the configuration of your **network switches** and the
**lifecycle of your bare-metal servers** — and let its AI agents propose,
and you approve, changes to both. It does this by provisioning a dedicated
**management stack** into your environment: a set of services (a source of
truth, a workflow engine, a config store and renderer, and optional
zero-touch provisioning and DHCP) that Daalu installs, upgrades, heals, and
drives on your behalf. You work entirely in the Daalu UI.

Where to find it: **Managed infra → Network & servers** (sidebar → Infrastructure)
(`https://ops.example.com/managed-infra?tab=network`).

## 27.1 What it manages

| Your equipment | What Daalu does |
|---|---|
| **Network switches** (Cumulus/NVUE, Arista, Juniper, Cisco and other SSH-reachable NOSes) | Stores intended config, renders it, shows you a diff, and deploys it with automatic rollback on failure |
| **Bare-metal servers** | Provisions them from bare metal (network boot + OS install), controls power, and seeds first-boot setup |
| **Server operating systems (day-2)** | Keeps hostname, packages, users, and settings in line with intent |
| **Server hardware (BMC)** | Reads health and manages BIOS settings where supported |

Each piece is optional — you choose what Daalu manages when you create an
environment, component by component.

## 27.2 Environments — named, and more than one

Everything in this feature lives inside a **management environment**. An
environment is one complete management stack with a name you choose —
`Production`, `Lab`, `Staging` — running in its own namespace in your
cluster, with its own set of private web addresses.

You can run several environments side by side, fully independent of each
other. One of them is always marked **Primary** — a small badge on its
tile. The primary environment is the one the rest of Daalu talks to by
default: the *Operations* page, the topology view, and device onboarding
all bind to the primary environment's source of truth.

> **The name is permanent.** The name you type becomes the environment's
> identifier — it is derived into the Kubernetes namespace and the public
> hostnames of the stack's consoles, all of which are baked into live URLs.
> The create form shows you the derived identifier live as you type
> (*"Identifier: `production` — this becomes the namespace and the public
> hostnames, and cannot be changed after creation"*). Pick deliberately.

## 27.3 Where it runs (and why your data stays put)

The management stack runs **in your environment** — your own connected
cluster — never inside Daalu's shared service. When you create an
environment, Daalu automatically targets **your tenant's connected cluster
tunnel**: if you have a cluster joined over the secure WireGuard link
(chapter 34), the stack installs there without you having to say so.
Daalu's hub then reaches it only over that tunnel. Nothing in your cluster
is exposed to the public internet.

## 27.4 Creating an environment

From **Managed infra → Network & servers**, click **Create environment**
(the page opens straight onto the create form if you have none yet; if you
already have environments, use **+ Add environment**). Only tenant admins
can create environments — members see the environment tiles read-only.

The form:

1. **Name.** The one irreversible choice, so it comes first. Client-side
   checks cover length and characters; a few names are reserved by the
   platform, and if you hit one the server tells you exactly why.
2. **Components.** Tick what you need:
   - **Render service** — renders intended config to vendor syntax.
   - **Config store** — stores intended config and serves the config API.
   - **Workflow engine** — orchestrates and retries deploys.
   - **Source of truth** — inventory, IPAM, intended config.
   - **Zero-touch provisioning** — onboard new switches automatically.
   - **DHCP** — bundled DHCP for zero-touch provisioning.

   The first four are on by default; ZTP and DHCP are opt-in.
3. **Size** — *Small*, *Medium*, or *Large*. Small runs a single-instance
   database with minimal CPU requests and **fits a single-node cluster**.
   Medium and Large raise CPU requests and replica counts across the
   workflow tier — the form warns you that on a small cluster those pods
   will sit Pending with `Insufficient cpu`. When in doubt, start Small;
   you can resize later.
4. **Break-glass admin login (optional, collapsed).** See §27.8.

Click **Create environment**. The request returns immediately —
*"Scheduling…"* flashes for a moment and you land directly on the new
environment's live install progress. The install itself runs in the
background and takes a few minutes on the first run (image pulls dominate);
you can leave the page and come back.

## 27.5 Watching the install — live progress

While an environment is coming up, its detail view shows **Building your
management stack**: the target namespace, the chart version, and a live
picture of the rollout —

- **Workloads** — every pod in the environment with its phase and
  ready-count (`3/3`). Pods running init containers are styled as
  *progress*, not failure — sitting in `Pending` for a few minutes on a
  first install is the install working.
- **Notable events** — only the events that explain a stall
  (`FailedScheduling`, `ErrImagePull`, …). If the install isn't finishing,
  the reason is here. The classic one: an oversized **Size** pick on a
  small cluster shows `Insufficient cpu`.

Environments are also self-healing: if an install is interrupted partway
(for example by a platform restart), Daalu detects the wedged state on its
next pass and recovers it automatically — a stuck install is retried, and a
stuck upgrade is rolled back to the last good version. You don't have to
do anything.

## 27.6 The environment tiles and detail view

Once you have environments, the page shows a **tile per environment**:
name, status badge (`active`, `provisioning`, `error`…), namespace, chart
version, and a one-line summary (*"4 components running"*, or *"5/9 pods
ready"* mid-install). Click a tile to open it.

An **active** environment's detail view shows **All components are
ready** — the green completion banner — and then four tabs:
**Consoles**, **Devices**, **Observability** and **Workloads**.

The **Consoles** tab holds:

- **Stack consoles** — link cards into the stack's own web UIs, each named
  for what it does:
  - **Source of truth** — inventory, addressing, and intended state.
  - **Workflows** — watch deploys run, retry, and roll back.
  - **Config store** — stored intended configuration and history.
  - **Rendering** — turn intended state into device syntax.
  - **Config browser** — browse the stack's own console (workflow
    catalog, diagnostics).

  These consoles are optional and read-mostly — you sign in with your
  Daalu identity (§27.7) and Daalu drives them for you day to day.
- **In Daalu** — jump-offs to **Topology** (see the live fabric, chapter
  49) and **Operations** (onboard devices, review drift, approve changes).
  Topology isn't in the sidebar; this card is how you reach it.

The other tabs:

- **Devices** — on the primary environment, the inline
  device-onboarding panel (§27.9).
- **Observability** — the environment's observability stack.
- **Workloads** — the pod list again, now that everything is
  healthy.

## 27.7 One login for everything

The stack consoles honour your Daalu session. When you click **Source of
truth** or any other console card, you arrive signed in as *yourself* —
same identity, same login, no second password. This holds for every member
of your team, and it survives ordinary working patterns: you can browse
Daalu for an hour, then open a console, and you're still you.

If a console ever drops you on its own login page, that's a fault, not a
step you're supposed to complete — see the Troubleshooting chapter.

## 27.8 The break-glass admin

There is exactly one exception to "no second password": the source-of-truth
console keeps a **local administrator account** for the day single sign-on
itself is unavailable — which is precisely the day you can't go and create
an account.

- You can choose the username and password at create time (the collapsed
  **Break-glass admin login** section of the form; passwords must be at
  least 12 characters), or leave it blank and let Daalu generate a strong
  one.
- Either way the credential is shown **exactly once**, in an amber banner
  right after the environment is created, with a **Copy password** button.
  Daalu stores it encrypted and will not display it again — save it in
  your password manager before dismissing the banner.
- You and your team never need it for day-to-day work; everyone is signed
  in automatically from Daalu.

## 27.9 Onboarding your devices

Device onboarding lives right on the primary environment's detail view,
on its **Devices** tab — the **Onboard your devices** panel. It's a short checklist, and the first
item is already done for you:

1. **Source of truth: connected.** The environment's bundled source of
   truth is wired to Daalu automatically — the platform stamps the API
   credential itself and reaches the service over your cluster tunnel.
   There is **no URL or token to paste**. The panel shows a live status:
   *connected*, *wired, not answering yet* (normal mid-install; it keeps
   retrying), or *not wired yet*.
2. **Device SSH credentials.** Enter the SSH user your devices share
   (Cumulus/NVUE devices: user `cumulus`) and either a password or a
   private key, then **Save credential**. This one tenant-wide credential
   is what Daalu uses for config push, drift checks, the live topology
   probe, and the in-browser device terminal. It is encrypted at rest the
   moment you save it, and it is never echoed back — reads return it
   redacted.
3. **Bulk import.** Upload your inventory as a **YAML or Excel** file.
   Daalu validates every row against the environment's catalog (sites,
   device types, roles, platforms) and shows a green/red row-by-row
   preview — nothing is written until you apply. Chapter 13 has the file
   schema.
4. **See the imported fabric in the topology view.** One click and your
   devices appear as a live graph — chapter 49.

## 27.10 How a change happens — find → suggest → approve → apply

This is the part that makes it Daalu and not just a config tool. The loop
is the same for a switch config change, a server reconfigure, or a power
action:

1. **Find** — an agent notices something (an alert, drift from intent, a
   capacity signal, or you asking for a change).
2. **Suggest** — the AI explains the issue and proposes a specific fix,
   with its reasoning and a confidence score.
3. **Approve** — you see the proposal (and, for config changes, the exact
   diff) in the Daalu UI and click **Approve** or **Reject**. Nothing
   touches a device until you approve.
4. **Apply** — Daalu carries out the approved change on the device and
   reports back: success, or failure with the reason. Network config
   changes are applied with **commit-confirm and automatic rollback**, so
   a bad change backs itself out rather than stranding a device.

You stay in control at the approval step every time. The AI can find and
propose all day; only an approval turns a proposal into a real change.

Devices with **private management addresses** — the normal case for a
fabric management network — are fully reachable for all of this. Daalu
dials them through your cluster's secure tunnel, so the executor, the
drift checks, the topology probe, and the device terminal all work
without you exposing a single device to the internet.

## 27.11 Upgrades

Each environment shows its current stack version as a chip on the detail
view (*Chart v1.3.0*). When Daalu ships a newer version, an **Upgrade
available** badge and an **Upgrade** button appear next to it
(admins only).

Click **Upgrade**. It asks you to confirm in place (**Upgrade now**)
and says what happens: the stack rolls to the new version, and its
tools may restart for a few minutes. Once you confirm, the rollout is
scheduled immediately — like the install, it runs in the background for a few minutes, the environment tile
shows the rollout live, and you can leave the page. Each environment
upgrades independently, so you can move `Lab` first and `Production` a
week later.

## 27.12 Setting it up fresh — what you provide

A first-time setup is mostly Daalu's job. What *you* provide:

1. **A connected cluster.** Complete the one-time cluster onboarding
   (chapter 34): your platform team runs one `helm install` and the
   cluster joins over an outbound WireGuard tunnel.
2. **Cluster prerequisites (installed once).** Your cluster needs a small
   set of standard platform pieces present before the first environment:
   - an **ingress gateway** (Envoy Gateway) for the per-service web
     addresses,
   - **cert-manager** (+ an issuer) so certificates issue automatically,
   - the **CloudNativePG** operator (the stack runs its databases as
     Postgres clusters),
   - a **default storage class** that supports persistent volumes.

   The pre-provision check tells you, by name, anything that's missing.
   If you don't run these already, Daalu's onboarding team installs them
   with you — they're cluster-wide, shared by every environment, set up
   once.
3. **Capacity.** One environment is roughly **30 pods plus up to four
   small Postgres databases**. Make sure the nodes you're dedicating have
   room for that *alongside your other workloads* — in particular the
   per-node pod count (kubelet `max-pods`, commonly defaulted to 110)
   must have headroom, not just CPU and memory. The install-progress
   panel will show you if scheduling is blocked, and the **Size** hint on
   the create form is there for exactly this.
4. **Device credentials + inventory** — the onboarding panel (§27.9).

### For server provisioning (either location)

Server lifecycle management needs bare-metal hosts with a **BMC**
(iDRAC / iLO / Redfish) reachable on your management network, and a site
cluster with the provisioning service. This is set up per **site** (it
can't be shared across locations) — Daalu's team stands it up with you. If
a server step is greyed out, it's telling you a hardware/site prerequisite
isn't met yet.

## 27.13 Frequently asked

**Do I have to use the stack consoles?** No. Daalu is the primary surface;
the consoles are optional and read-mostly, and you're signed into them
with your Daalu identity when you do open one.

**Can I manage only servers, or only network?** Yes — pick the components
you need per environment.

**Why can't I rename an environment?** The name is derived into the
namespace and the public hostnames, which are baked into live URLs. Create
a new environment with the right name instead.

**What if I already run my own source of truth (Nautobot)?** Daalu can use
the bundled one, or you can connect your own — see chapter 30. The bundled
one requires zero setup; the BYO path is for teams with an established
instance.

**Is anything applied automatically without me?** No device change is
applied without an approval. (Read-only discovery, the topology probe, and
health monitoring run on their own; they don't change anything.)
