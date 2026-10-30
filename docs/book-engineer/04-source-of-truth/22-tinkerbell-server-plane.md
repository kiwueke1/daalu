# 22. The Tinkerbell server plane

How Daalu provisions bare-metal **servers** by driving an existing
Tinkerbell stack, and fills the gaps Tinkerbell doesn't cover. Read chapters
20 (overview) and 17 (executor identity) first.

## 22.1 What Tinkerbell is, and who deploys it

Tinkerbell (Tink + SMEE + Hegel + Rufio + HookOS) is deployed by the
lower-level `daalu` bare-metal project as part of standing up a site's mgmt
cluster — see that project's `src/daalu/bootstrap/mgmt/tinkerbell_installer.py`.
daalu-automation does **not** deploy it; it **talks to its CRDs** over the
WireGuard tunnel (apply/watch). This is the key asymmetry vs NV-CM (which we
provision).

**One Tinkerbell per customer site — not a shared one.** Tinkerbell is
physically site-local: SMEE serves DHCP/iPXE on the *provisioning LAN* and
Rufio reaches the *BMC network*, and two DHCP authorities can't share a
broadcast domain. So each customer fleet sits behind its own site cluster
with its own Tinkerbell + Rufio; daalu-automation targets *that tenant's*
stack via its `Integration(provider="tinkerbell")`
(`_resolve_tinkerbell_target(db, tenant_id)` resolves per-tenant). Daalu's own
internal mgmt-cluster Tinkerbell (which provisions daalu's *own* hardware) is
a separate instance and is **never** used to manage a customer fleet.

CRDs used:
- `tinkerbell.org/v1alpha1`: **Hardware**, **Template**, **Workflow**.
- `bmc.tinkerbell.org/v1alpha1`: **Machine**, **Job** (Rufio BMC power/boot
  via Redfish/IPMI).

## 22.2 Why it's not a DeviceAdapter

Provisioning is **imperative** (PXE-install an OS image, power-cycle a BMC) —
there's no declarative `DeviceFacts` to render and drift-check. So the
Tinkerbell path does **not** implement the `DeviceAdapter` ABC. Instead:

- `core/tinkerbell/client.py:TinkerbellClient` — async `CustomObjectsApi`
  wrapper (apply/patch/get/delete + `workflow_state()` / `rufio_job_state()`
  status helpers + `build_power_job()`). Targets in-cluster or a tunnel
  kubeconfig, reusing `nautobot_controller.k8s.load_*`.
- `core/device/tinkerbell_provision.py` — `ServerProvisionSpec` (pydantic:
  explicit CR bodies + Rufio power tasks) and `provision(spec, kubeconfig,
  namespace)`, which applies CRs in dependency order (Rufio Machine →
  Hardware → Template → Workflow → power Job) and watches the Workflow to
  `STATE_SUCCESS` and the Rufio Job to `Completed`, returning an
  `ExecutionResult`.

## 22.3 The gate branch

`ChangeProposalKind` gains `provision_op` (migration `0019`). The gate adds
`core/change_proposals.py:execute_provision()`:

1. Same identity invariant — `actor.kind == "executor"` +
   `actor.scope == executor_jwt_scope`.
2. Same status invariant — must be `approved`.
3. **Observed-state compare instead of render-drift**: re-read the device
   from the SoT; if it's gone, flip to `stale`. (A richer "is it already in
   the target state" compare is a follow-up.)
4. Parse `ServerProvisionSpec` from `proposal.intended_config` (JSON),
   resolve the Tinkerbell target (`_resolve_tinkerbell_target`), call
   `provision()`, record the result, publish `proposal.executed`.

`workers/executor.py` branches on `proposal.kind == provision_op` and calls
`execute_provision()` (no DeviceAdapter, no SSH creds). The executor-only
chokepoint is preserved — nothing else may call `provision()`.

## 22.4 Building specs from the shared Nautobot

`core/sot/server_provisioning.py:build_provision_spec(device, …)` turns a
shared-Nautobot server `Device` (role=server; MAC / BMC / disk in
`device.extra`; day-1 OS intent in a `daalu_intent` Config Context) into a
`ServerProvisionSpec` + a BMC `Secret` body. It emits:

- a **Hardware** CR (MAC, disk, DHCP/netboot, instance metadata),
- a Rufio **Machine** CR + BMC Secret (Redfish connection),
- a default **Template** that streams the OS image (`image2disk`) and writes
  the **cloud-init user-data** (`writefile`) so the host boots daalu-ready,
- a **Workflow** binding template↔hardware,
- the Rufio power tasks (off → one-time PXE/UEFI boot → on).

Mirrors the CR shapes the `daalu` project already uses.

## 22.5 The hand-off boundary (day-1 vs day-2)

Tinkerbell provisions the OS and **hands off at boot** — it does no in-band
OS config. Daalu fills both gaps:

- **Day-1** — users / SSH keys / hostname injected as cloud-init user-data in
  the Template (served by Hegel at first boot).
- **Day-2** — ongoing OS config (packages, sysctl, files) is owned by the
  existing **`linux_ssh`** adapter (declarative gate, unchanged). BIOS
  attributes are owned by **`redfish`**. These run on the *same device* via
  their own transports, selected by `core/device/routing.py`.

So a single bare-metal host involves three transports over its lifecycle:
`tinkerbell` (provision/power) → `linux_ssh` (OS config) → `redfish` (BIOS).

## 22.6 The `tinkerbell` integration

`Integration(provider="tinkerbell")` carries `{namespace, cluster_tunnel_id?,
kubeconfig?}`. `_resolve_tinkerbell_target()` resolves the kubeconfig from a
referenced cluster tunnel (sibling `Integration(provider="kubernetes")`) or
an inline blob, or `None` for in-cluster. Registered in the integrations
catalog with `kubeconfig` redacted.

## 22.7 Why not NICo

NVIDIA Infra Controller was evaluated and rejected: it hard-requires a
BlueField DPU on every managed host and does no in-band OS config — useless
for general servers. Tinkerbell has no such constraint, is already in the
`daalu` stack, and its Hardware/Template/Workflow model is clean.
