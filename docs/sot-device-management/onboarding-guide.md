# Onboarding guide — connecting a customer's environment

This document is the per-tenant flow: how a customer goes from "fresh
daalu account" to "the executor is pushing config changes I approved."

For platform-operator setup (deploying daalu + Nautobot itself), see
[`setup-guide.md`](setup-guide.md) and [`nautobot-guide.md`](nautobot-guide.md).

## Two onboarding paths

There are two ways a customer can wire up the SoT/device-management feature:

### Path 1 — the dedicated SoT wizard at `/onboarding/sot` (recommended)

A 3-step focused flow designed for customers new to Nautobot:

1. **Pick a mode** — BYO Nautobot vs hosted (the wizard auto-detects whether
   hosted is available on this deploy by hitting
   `GET /api/v1/onboarding/nautobot/hosted-status`)
2. **Wire the connection** — paste URL + token for BYO, or click
   *Provision* for hosted (the button POSTs to
   `/api/v1/onboarding/nautobot/provision`)
3. **Seed SSH credentials** — minimal form for tenant-wide `ssh_credentials`

After step 3, the wizard funnels the customer to `/devices` to add their
first device.

### Path 2 — the generic onboarding wizard at `/onboarding`

The pre-existing 14-step wizard treats Nautobot as one of the standard
integrations alongside Slack / SMTP / Prometheus / etc. Operators who
already know what they're doing can wire Nautobot there in one form; the
SoT-specific wizard adds the hosted-vs-BYO branching and the credential
step that the generic one doesn't.

Either path works — both write to the same `Integration(provider="nautobot")`
row.

## The 14-step generic wizard catalog

The onboarding wizard lives at `/onboarding`. It walks the customer
through 14 steps, in order:

| # | Step | Provider | Required for SoT? |
| - | --- | --- | --- |
| 1 | Slack notifications | `slack` | No |
| 2 | Email (SMTP) | `smtp` | No |
| 3 | Prometheus / Alertmanager | `prometheus` | No |
| 4 | Loki (logs) | `loki` | No |
| 5 | Thanos (long-history metrics) | `thanos` | No |
| 6 | OpenSearch (logs) | `opensearch` | No |
| 7 | PagerDuty | `pagerduty` | No |
| 8 | Customer kubeconfig | `kubernetes` | No |
| 9 | AWS account | `aws` | No |
| 10 | Google Cloud project | `gcp` | No |
| 11 | Azure subscription | `azure` | No |
| **12** | **Nautobot (Source of Truth)** | **`nautobot`** | **Yes** |
| 13 | Cluster tunnel (VPN) | (no row — `cluster_tunnels` table) | Optional |

Steps 1–11 are pre-existing. Step 12 is what PR 8 added. Step 13 is the
WireGuard tunnel.

Only **step 12 (Nautobot)** is required for the SoT/device-management
feature to function. The cluster tunnel + cloud integrations enrich the
LLM's observation surface but don't gate device management.

## Wiring the Nautobot integration

### BYO path (customer brings their own Nautobot)

In the wizard, on step 13:

1. **URL** — full HTTPS URL to the customer's Nautobot
2. **API token** — write-enabled token from a Nautobot user with the
   permission scope detailed in [`nautobot-guide.md`](nautobot-guide.md)
3. **Webhook secret** (optional but recommended) — same string the
   customer will configure on the Nautobot webhook side
4. Click **Test connection** — daalu probes `<url>/api/status/` with
   the token. Green check + ms latency = good
5. **Save**

What's stored: the wizard PUTs to `/api/v1/integrations/config/nautobot`
which writes a row with `{url, token, webhook_secret}`. The PR-8 dual-
shape support means plaintext from the wizard works alongside the
`token_ciphertext` shape that hosted provisioning lands.

### Hosted path (you host Nautobot for them)

1. Customer goes to step 13. If hosted mode is configured (see
   [`setup-guide.md`](setup-guide.md)), a **"Provision hosted Nautobot"**
   button is available on the page (UI for this is a follow-up to PR 8;
   currently `curl` it).
2. The button POSTs to `/api/v1/onboarding/nautobot/provision`.
3. That route idempotently:
   - Creates a Nautobot Tenant with the customer's slug
   - Creates an ObjectPermission constrained to that slug
   - Mints an APIToken for the platform service user
   - Writes the resulting `{url, token_ciphertext, hosted: true,
     nautobot_tenant_slug, nautobot_tenant_id, permission_id}` into the
     customer's `Integration(provider="nautobot")` row
4. The customer can now log into the Nautobot instance with their
   credentials; their token only sees their own tenant's data thanks to
   the ObjectPermission constraint.

Programmatic version:

```bash
curl -X POST https://daalu.example.com/api/v1/onboarding/nautobot/provision \
  -H "Authorization: Bearer <customer admin token>"
# {"ok": true, "message": "provisioned Nautobot tenant 'acme'", ...}
```

Re-running is safe — find-or-create on Tenant + Permission, fresh
token each time.

## Adding the credential rows

Devices need credentials beyond the SoT integration:

| Device type | Credential provider | Required fields |
| --- | --- | --- |
| Linux server | `ssh_credentials` | `user`, `private_key_ciphertext` or `password_ciphertext`, optional `port` (default 22) |
| Server BMC | `redfish_credentials` | `user`, `password_ciphertext`, optional `port` (default 443), optional `verify_tls` |
| Network device | `network_credentials` | `user`, `password_ciphertext`, optional `enable_password_ciphertext` (for IOS-XR / EOS exec-mode escalation), optional `port` |

Currently no UI for SSH / Redfish / Network credential rows. The customer
either:

- `PUT /api/v1/integrations/config/{provider}` directly with a JSON config
- Or asks you to seed via SQL / a one-off script

Example: seeding tenant-wide SSH creds via the API:

```bash
# 1. Generate a key pair on the customer's bastion if they don't have one.
# 2. Encrypt the private key. The daalu API has a helper, but for now:
kubectl exec -n daalu-automation deploy/daalu-api -- python -c "
from daalu_automation.core.crypto import encrypt_secret
import sys
print(encrypt_secret(sys.stdin.read()))
" < ~/.ssh/daalu_id_ed25519

# 3. PUT the integration row:
curl -X PUT https://daalu.example.com/api/v1/integrations/config/ssh_credentials \
  -H "Authorization: Bearer <tenant admin token>" \
  -d '{
    "name": "tenant-wide ssh creds",
    "config": {
      "user": "daalu",
      "private_key_ciphertext": "<the encrypted blob>",
      "port": 22,
      "sudo": true
    }
  }'
```

Same shape for `redfish_credentials` (password instead of key) and
`network_credentials`.

## Adding devices

### Via the daalu UI (recommended)

Go to **`/devices`** and click *Add device*. The form pulls Nautobot's
catalog (sites, device types, roles, platforms) so you pick from
dropdowns instead of pasting UUIDs. Submitting:

1. POSTs to `/api/v1/sot/devices` which creates the device row in
   Nautobot with `daalu_transport` pre-stamped from the dropdown
2. Creates an `ipam.ipaddress` row for the primary IP and links it
3. Returns the new device so the list refreshes immediately

Then click the new device row to author its intended config in the
**form-based editor** (Linux) or **JSON editor** (Redfish / network gear
for now). Save lands a `daalu_intent` Config Context in Nautobot. The
next reconciler tick picks up any drift.

### Via Nautobot directly

If you prefer Nautobot's own UI (it's richer for advanced cases — device
bays, virtual chassis, interface cabling), three things have to be true
for a device to be picked up by the daalu reconciler:

1. **It exists in Nautobot** as a `dcim.device` with `primary_ip4` set
2. **Its platform or custom field tells us its transport** (see the
   transport mapping in [`nautobot-guide.md`](nautobot-guide.md))
3. **A `daalu_intent` Config Context is attached** with the right facts
   schema for its transport

### Example: onboarding a Linux server

In Nautobot:

1. Devices → Add Device:
   - Name: `web01`
   - Device Role: `Server` (any)
   - Manufacturer/Type: any
   - Tenant: the customer's tenant slug (set by hosted provisioning)
   - **Primary IPv4**: `10.0.0.5/24`
   - Platform: `Ubuntu` (or anything in the linux bucket: `linux`,
     `ubuntu`, `debian`, `rhel`, `centos`, `fedora`)
2. Configuration → Config Contexts → Add:
   - Name: `daalu_intent`
   - Weight: 1000
   - Active: ✓
   - Devices: web01
   - Data:
     ```json
     {
       "hostname": "web01",
       "authorized_keys": [
         {"user": "deploy", "key": "ssh-ed25519 AAAA... ops@daalu"}
       ],
       "sysctl": [
         {"name": "vm.swappiness", "value": "10"}
       ]
     }
     ```

Within `sot_reconcile_period_s` (default 300s), the daalu reconciler:

1. Pulls the device list, finds `web01`
2. Notes its platform → bucket `linux` → transport `linux_ssh`
3. Resolves SSH creds (tenant-wide row, or per-device override)
4. SSH'es to `10.0.0.5:22` as `daalu`, runs `hostname`, reads
   `/etc/sysctl.d/99-daalu.conf`, reads `/home/deploy/.ssh/authorized_keys`
5. Compares to intent. If different → creates a `ChangeProposal(kind="drift")`

A pending proposal appears at `/proposals` for the operator to review.

### Example: onboarding a server BMC

In Nautobot:

1. Devices → Add Device:
   - Name: `bmc-prod-01`
   - Primary IPv4: the BMC's management IP, e.g. `10.0.5.10/24`
   - Platform: `Dell iDRAC` (or any in the redfish bucket: `redfish`,
     `idrac`, `ilo`, `xcc`, `irmc`, `ipmi-redfish`)
2. `daalu_intent` Config Context:
   ```json
   {
     "bios_attributes": [
       {"name": "BootMode", "value": "Uefi"},
       {"name": "LogicalProc", "value": "Enabled"}
     ],
     "boot_override": {"target": "None", "enabled": "Disabled"},
     "power": {"desired_state": "On"}
   }
   ```

Make sure a `redfish_credentials` Integration row exists for the tenant
with `{user: "root", password_ciphertext: <encrypted>, verify_tls: false}`
(BMCs typically have self-signed certs).

### Example: onboarding a network device

In Nautobot:

1. Devices → Add Device:
   - Name: `leaf01`
   - Primary IPv4: management IP, e.g. `10.0.255.10/32`
   - Platform: `Juniper Junos` (or `juniper-junos`, `junos`)
2. `daalu_intent` Config Context:
   ```json
   {
     "hostname": "leaf01",
     "interfaces": [
       {"name": "ge-0/0/0", "description": "uplink to spine01", "enabled": true, "mtu": 9216}
     ],
     "vlans": [
       {"vlan_id": 100, "name": "prod-web"}
     ]
   }
   ```

Make sure a `network_credentials` Integration row exists with
`{user: "automation", password_ciphertext: <encrypted>}`.

## Per-device credential overrides (PR 9)

Most tenants will use one credential row per provider (the tenant-wide
default). For special cases:

### Scenario A: One specific server has a different key

Use a **per-device row** — an integration row whose `config.device_id`
matches the Nautobot device UUID. The PR-9 resolver picks per-device rows
ahead of the tenant-wide one.

```bash
# Find the Nautobot device UUID first:
curl -H "Authorization: Token <tenant nautobot token>" \
  "<nautobot url>/api/dcim/devices/?name=web01" | jq '.results[0].id'
# → "ad7c6f12-..."

curl -X PUT https://daalu.example.com/api/v1/integrations/config/ssh_credentials \
  -H "Authorization: Bearer <tenant admin token>" \
  -d '{
    "name": "web01 high-trust key",
    "config": {
      "device_id": "ad7c6f12-...",
      "user": "root",
      "private_key_ciphertext": "<encrypted high-trust key>",
      "sudo": false
    }
  }'
```

The same daalu tenant now has two `ssh_credentials` rows. The resolver
picks the per-device one for `web01` and the tenant-wide one for
everything else.

### Scenario B: A named pool used by a group of devices

Use a **named pool** — a credential row given a meaningful `name`, and
each device that should use it gets the matching `*_credentials_ref`
custom field set in Nautobot.

```bash
# Create the named pool integration row:
curl -X PUT https://daalu.example.com/api/v1/integrations/config/ssh_credentials \
  -H "Authorization: Bearer <tenant admin token>" \
  -d '{
    "name": "bastion-pool",
    "config": {
      "user": "bastion",
      "private_key_ciphertext": "<encrypted bastion key>"
    }
  }'
```

Then in Nautobot, on each device that should use this pool, set the
custom field `ssh_credentials_ref = "bastion-pool"`. The resolver looks
up the row by that name. A typo in the custom field raises `LookupError`
(loud failure — visible in executor logs and in proposal failures).

### Scenario C: Per-device user / port override

Set the custom field `ssh_user` / `ssh_port` (or `redfish_user` /
`redfish_port` / `network_user` / `network_port`) on the device in
Nautobot. These override the row's defaults but the row selection logic
is unchanged.

Useful for: "10 devices share one key, but each runs SSH on a different
port."

## Testing the chain end-to-end

After everything's wired:

```bash
# 1. Confirm the Nautobot integration works
curl -X POST https://daalu.example.com/api/v1/onboarding/test/nautobot \
  -H "Authorization: Bearer <tenant admin token>" \
  -d '{"config": {}}'
# Expect: ok=true, message="Nautobot accepted the token"

# 2. Trigger a drift detection manually (or wait for the next tick).
# Easiest path: edit a managed setting on the real device (e.g.
# `sudo hostnamectl set-hostname different-name`) and wait
# sot_reconcile_period_s seconds.

# 3. Confirm a drift proposal appears
curl -H "Authorization: Bearer <token>" \
  https://daalu.example.com/api/v1/change-proposals?status=pending

# 4. Open the proposal in /proposals UI, review the diff, click Approve.

# 5. Within executor_period_s, the row flips to "executed" and
#    the device is back to the SoT intent.
curl -H "Authorization: Bearer <token>" \
  https://daalu.example.com/api/v1/change-proposals/<id>
# {"status": "executed", "executor_result": {"success": true, ...}, ...}

# 6. Confirm on the device:
ssh daalu@10.0.0.5 'hostname'
# → web01
```

## LLM-authored proposals

Once observability integrations (Prometheus / Loki / AWS / GCP / etc.)
are wired, the alert-chat agent will start calling `propose_change`
during investigations. Operators see these in `/proposals` with:

- A "AI engine" badge (vs "Drift reconciler" for auto-drift)
- The model's `llm_reasoning` rendered as the body
- Confidence as a percentage
- Linked events and alerts that triggered the proposal
- A metrics-evidence list

Tunes worth knowing:

- The `requires_approval=False` on the `propose_change` tool means the
  tool call lands a `pending` proposal immediately — there's no chat-side
  approval. The Proposals UI is the single approval surface.
- The agent will only propose changes to devices it can see in the SoT.
  If a device isn't in Nautobot yet, the LLM can't author a proposal
  for it.
- Confidence is the model's self-report. Treat it as a tie-breaker for
  long approval queues, not as a gate.

## When something goes wrong

- **Proposal stuck in `pending` forever** — nobody's clicked Approve.
  Check the operator's notifications / Slack channel. (A future
  follow-up could escalate stale-pending proposals; not in v1.)
- **Proposal stuck in `approved`, never `executed`** — see the
  troubleshooting section in [`setup-guide.md`](setup-guide.md).
- **Proposal in `failed` status** — read `executor_result.error` and the
  `per_step` array. Common causes: SSH connection refused (device
  unreachable), authentication failure (wrong creds), adapter rejected
  the payload (unexpected device response).
- **Proposal in `stale` status** — the SoT intent changed between
  Approve and Execute. Re-render and re-author. Check
  `executor_result.stale.reason` for which file changed.

## Tearing down a managed device

In Nautobot:

1. Delete the `daalu_intent` Config Context (or remove the device from
   it). The reconciler now sees no intent and won't propose drift.
2. Optionally delete the device from Nautobot. The reconciler skips
   non-existent devices automatically.

On the device itself, you'll need to manually remove daalu's managed
files (`/etc/sysctl.d/99-daalu.conf`, etc.) — the adapter doesn't
have a "tear down" verb in v1. Files stay where they are until
something cleans them up. Document this in your customer-facing
offboarding runbook.
