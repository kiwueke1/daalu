# 20. Config-management planes: NV-CM (network) + Tinkerbell (servers)

This chapter is the engineering reference for how Daalu manages device and
server configuration by **wrapping two mature, external tools** instead of
maintaining bespoke drivers for everything:

- **Network fabric → NVIDIA Config Manager (NV-CM).** Switch/fabric config
  (Cumulus / NVOS / MLNX-OS / Arista / Junos / IOS-XR) is rendered, stored,
  and deployed by NV-CM. Daalu drives it over the WireGuard tunnel.
- **Servers → Tinkerbell.** Bare-metal provisioning + BMC power/boot is done
  by Tinkerbell (already deployed by the lower-level `daalu` project). Daalu
  applies its CRs over the tunnel and fills the gaps Tinkerbell doesn't cover
  (in-band OS config via `linux_ssh`, BIOS via `redfish`).
- **One shared source of truth.** The Nautobot bundled in the NV-CM stack is
  the SoT for **both** planes.

This chapter is the code-level tour. Read chapters 14–17 first — the reconciler,
SoT/Nautobot adapter, change-proposal gate, and executor identity are the
substrate these planes build on.

## 20.1 The principle

> Daalu is the **brain** (AI find→triage→recommend, the human-approval gate,
> the multi-tenant SaaS hub, events, billing). NV-CM and Tinkerbell are
> **execution substrates**. Where a mature tool already does a job, we defer
> to it and only fill genuine gaps.

We do **not** fork or vendor either tool's source. Only NV-CM's *Helm chart*
is vendored (pinned) under `deploy/charts/`; its *images* are mirrored to
Harbor; its *REST API* is called via thin clients. Tinkerbell is deployed by
the `daalu` project — we only talk to its CRDs.

## 20.2 Responsibility matrix (who owns what)

| Capability | Owner | Daalu code |
|---|---|---|
| Source of truth (inventory, IPAM, intent) | **Shared Nautobot** (NV-CM-bundled) | `core/sot/nautobot.py` (prefers the NV-CM Nautobot, §20.6) |
| Switch/fabric config | **NV-CM** | `core/device/config_manager.py` |
| Server provisioning (PXE/OS install) | **Tinkerbell** | `core/device/tinkerbell_provision.py` |
| Server BMC power/boot | **Tinkerbell/Rufio** | same |
| In-band Linux OS config (day-2) | **Daalu `linux_ssh`** | `core/device/linux_ssh.py` (unchanged) |
| Server BIOS / firmware | **Daalu `redfish`** | `core/device/redfish.py` (unchanged) |
| AI, approval UX, SaaS, events, billing | **Daalu** | the rest of the platform |

The feature is **off by default** and gated per tenant:
`tenant.feature_flags["config_manager_enabled"]` (network) and
`["server_management_enabled"]` (servers). Tenants without a flag are
byte-for-byte unaffected — every new transport/executor is additive.

## 20.3 Code map

| Area | Path |
|---|---|
| NV-CM API clients | `core/configmgr/{client,provision}.py` |
| NV-CM executor (DeviceAdapter) | `core/device/config_manager.py` |
| NV-CM controller (provisions the stack) | `config_manager_controller/{app,values,deployer_runner,deployer_config,progress,prechecks,env_slug,local_admin,chart_versions,helm_runner}.py` |
| NV-CM tenant state | `models/config_manager_tenant.py` + migrations `0019`, `0030` (named environments), `0031` (`gateway_internal_url`) |
| Vendored pinned chart | `deploy/charts/nv-config-manager-<ver>/` |
| Tinkerbell CR client | `core/tinkerbell/client.py` |
| Tinkerbell provisioning executor | `core/device/tinkerbell_provision.py` |
| Server spec builder (Nautobot → CRs) | `core/sot/server_provisioning.py` |
| Transport router | `core/device/routing.py` |
| Keycloak machine-token helper | `core/keycloak.py` |
| NATS→Redis event bridge | `workers/nats_bridge.py` |
| Gate change (imperative ops) | `core/change_proposals.py:execute_provision` |
| Onboarding wizard route | `api/routers/onboarding.py:provision_config_manager` |

## 20.4 How the gate stays intact

The approval gate (`core/change_proposals.py`) is unchanged in spirit:
`execute()` for **declarative** changes (render → drift re-check → push) and
the new `execute_provision()` for **imperative** server ops (executor
identity + `approved` + observed-state compare). Both keep the executor-only
chokepoint — `DeviceAdapter.execute()` and `tinkerbell_provision.provision()`
are still only reachable through the gate, invoked by the executor worker
(`workers/executor.py`), which branches on `ChangeProposalKind.provision_op`.

## 20.5 Two new device transports

`core/device/routing.py` is the single source of truth for transport
selection:

- `select_config_transport(device, config_manager_enabled=…)` → returns
  `"config_manager"` for switches on enabled tenants, else the device's
  native transport (`linux_ssh` / `redfish` / `junos` / …) — **no change for
  non-enabled tenants**.
- `select_provision_transport(device, server_management_enabled=…)` → returns
  `"tinkerbell"` for server hosts on enabled tenants, else `None` (daalu's
  own provisioning).

`config_manager` is registered as a real `DeviceAdapter`
(`core/device/config_manager.py`) and added to
`_CREDS_PROVIDER_BY_TRANSPORT`; `tinkerbell` is *not* a DeviceAdapter (it's
imperative — see chapter 22).

## 20.6 The shared Nautobot

`core/sot/nautobot.py:_load_credentials` now prefers the NV-CM-bundled
Nautobot: if the tenant has an `Integration(provider="config_manager")` row
with `nautobot_url` + `nautobot_token_ciphertext`, the SoT points there;
otherwise it falls back to the standalone `nautobot` integration (BYO /
legacy `nautobot_controller`). Both the token and the gateway address are
**auto-stamped** by the controller at provision time (PR #121 — see chapters
15 and 23 §23.2), and the adapter reaches the bundled Nautobot by dialing the
environment's gateway with `X-Daalu-Forward-Host: svc-nautobot.<base>` rather
than the unresolvable svc host directly (chapter 15). One Nautobot, both
planes — switches and servers are both `Device` rows, distinguished by
role/platform. The `nautobot_controller` is retired for
`config_manager_enabled` tenants.

Continue to chapter 21 (the NV-CM network plane in detail) and chapter 22
(the Tinkerbell server plane).
