# Nautobot guide — running it, integrating it, operating it

This is the single most important external dependency this feature
introduces. **Nautobot is the canonical state of every managed device** —
without it (or a BYO equivalent), the SoT pipeline has nothing to compare
observed reality against.

Read [`architecture.md`](architecture.md) first for the abstract picture.
This document is the operational manual.

## Two deployment modes

| Mode | Who runs Nautobot | Who configures the integration | When to use |
| --- | --- | --- | --- |
| **BYO** (Bring Your Own) | The customer | The customer pastes URL + token in the onboarding wizard | Customers who already run Nautobot. Common in network-heavy shops |
| **Hosted** | You (the platform operator) | The customer clicks "Provision hosted Nautobot" (or you `curl` the route) | Customers who don't have Nautobot. The hosted instance is multi-tenant (one install, per-tenant ObjectPermissions) |

You can support both simultaneously. Each tenant independently chooses.

## Hosted-mode setup (one-time, by you)

### 1. Spin up Nautobot

**Recommended:** Nautobot Helm chart (Network to Code's official):
https://github.com/nautobot/helm-charts

```bash
helm repo add nautobot https://nautobot.github.io/helm-charts
helm repo update

# Minimal values.yaml — extend for prod (Postgres + Redis durability, ingress)
cat > nautobot-values.yaml <<'EOF'
nautobot:
  superUser:
    username: admin
    password: <set-something-strong>
    email: admin@example.com
  config:
    ALLOWED_HOSTS: ["nautobot.daalu.example.com"]
  ingress:
    enabled: true
    hosts:
      - host: nautobot.daalu.example.com
        paths: ["/"]
    tls:
      - secretName: nautobot-tls
        hosts: ["nautobot.daalu.example.com"]
postgresql:
  auth:
    password: <set-something-strong>
redis:
  auth:
    password: <set-something-strong>
EOF

helm install nautobot nautobot/nautobot -n nautobot --create-namespace -f nautobot-values.yaml
```

On a single-node k3s cluster the same chart works; bind the ingress to traefik via the existing
`ingressClassName: traefik`.

### 2. Configure DNS + TLS

Add `nautobot.daalu.example.com` to your cluster's Cloudflare A-record set
pointing at the traefik LB IP (`<ingress-ip>`). cert-manager (already in the cluster) issues the cert.

### 3. Create the daalu-platform service user

The hosted-provisioning route mints per-tenant API tokens **for a specific
Nautobot user** that exists across all tenants. The per-tenant
ObjectPermission attached to that user is what scopes each token's reach.

Create the user one time, in Nautobot's admin UI (`/admin/users/user/add/`)
OR via the Nautobot CLI:

```bash
kubectl exec -n nautobot deploy/nautobot -- nautobot-server createsuperuser
# Username: daalu-platform
# Email: admin@example.com
# Password: <not used by our flow but Nautobot requires it; store somewhere>
```

**Important:** this user does NOT need to be a superuser. Demote it after
creation to a regular user — the ObjectPermissions are what grant access.

```python
# In nautobot-server shell:
from django.contrib.auth import get_user_model
User = get_user_model()
u = User.objects.get(username="daalu-platform")
u.is_superuser = False
u.is_staff = False
u.save()
```

### 4. Mint the admin token (for provisioning)

The provisioning route needs an admin token to create Tenants /
ObjectPermissions / APITokens. Create a separate user for *this* (so the
admin token isn't bound to the daalu-platform user):

```bash
# Via the admin UI: Users → Add → username "daalu-provisioner"
# Then Tokens → Add → user "daalu-provisioner", write_enabled=True
# Copy the token value (shown once).
```

The `daalu-provisioner` user **does** need superuser (or at least full
permissions on Tenant, ObjectPermission, APIToken models).

### 5. Configure the platform settings

```bash
# In daalu-automation's secret:
MANAGED_NAUTOBOT_URL=https://nautobot.daalu.example.com
MANAGED_NAUTOBOT_ADMIN_TOKEN=<the daalu-provisioner token from step 4>
MANAGED_NAUTOBOT_SERVICE_USER=daalu-platform   # default, matches step 3
```

Roll the API + worker + executor pods. Verify hosted mode is enabled:

```bash
curl https://daalu.example.com/api/v1/onboarding/nautobot/hosted-status \
  -H "Authorization: Bearer <admin token>"
# {"hosted_available": true, "detail": ""}
```

## Per-tenant provisioning (hosted mode)

Once hosted mode is enabled, tenants self-onboard via:

```bash
curl -X POST https://daalu.example.com/api/v1/onboarding/nautobot/provision \
  -H "Authorization: Bearer <tenant admin token>"
# {"ok": true, "message": "provisioned Nautobot tenant 'acme'", ...}
```

This **idempotently** creates:

1. A Nautobot **Tenant** with `slug=<daalu tenant slug>` and `name=<daalu tenant name>`
2. An **ObjectPermission** named `daalu-tenant-<slug>`:
   - `actions = [view, add, change, delete]`
   - `object_types = [dcim.device, dcim.interface, dcim.devicetype, dcim.devicerole, dcim.platform, dcim.location, dcim.rack, dcim.cable, ipam.ipaddress, ipam.prefix, ipam.vlan, ipam.vlangroup, extras.configcontext, extras.tag, extras.note]`
   - `constraints = {"tenant__slug": "<slug>"}` ← **the security boundary**
   - `users = [<daalu-platform user id>]`
3. An **APIToken** for the daalu-platform user

The token (write-enabled, cleartext returned once) lands in the tenant's
`Integration(provider="nautobot")` row as `token_ciphertext`. The tenant
can now read/write devices, IPs, VLANs, etc. — but only ones tagged with
their own Tenant slug.

Re-running the call is safe: it reuses the existing Tenant + Permission but
mints a fresh token. The previous token stays valid until you manually
revoke it in Nautobot (no auto-revoke in v1).

## BYO-mode setup (each tenant does this for themselves)

The customer:

1. Stands up their own Nautobot (or already has one)
2. Creates a user + APIToken with the same permissions outlined above
3. Goes to the daalu onboarding wizard → Nautobot step → pastes URL + token
4. Tests the connection; saves

That's it. No provisioning route involved.

**BYO checklist for the customer:**
- Nautobot URL is HTTPS and reachable from the daalu cluster
- API token has at minimum: view + add + change + delete on DCIM device,
  IPAM IP/prefix/VLAN, extras configcontext
- (Optional but recommended) Webhook secret configured so daalu can verify
  webhook signatures
- (Optional) A Nautobot Tenant tagging convention if their Nautobot is
  multi-tenant on their side

## The Nautobot data model we expect

Nautobot's data model is rich; this feature uses a small subset.

### Devices (`dcim.device`)

The unit of management. We don't create devices ourselves — the customer
populates Nautobot however they want (manual entry, scripts, importers).

What we read:
- `id` (UUID, opaque to us)
- `name` (display)
- `primary_ip4.address` (CIDR — we strip to bare IP for the adapter)
- `platform.name` (used for transport-bucket heuristic)
- `tags[].name`
- `config_context` (where `daalu_intent` and `daalu_observed` live)
- `custom_field_data` (where our per-device overrides live)

What we expect on each device:
- `primary_ip4` set (no IP = we can't reach it)
- `platform` set OR `custom_field_data.daalu_transport` set
- A `daalu_intent` Config Context attached, with content matching the
  facts schema for its transport (see [`adapter-reference.md`](adapter-reference.md)
  for shapes)

### Config Contexts (`extras.configcontext`)

Two Config Contexts per device:

- **`daalu_intent`** — the *desired* state. JSON body matches the
  `LinuxFacts` / `RedfishFacts` / `NetworkFacts` schema. This is what the
  reconciler compares observed state against.
- **`daalu_observed`** — last observed snapshot. The reconciler writes
  this on every successful collect. Useful for ad-hoc inspection; not
  load-bearing for the pipeline.

Example `daalu_intent` for a Linux server:

```json
{
  "hostname": "web01",
  "authorized_keys": [
    {"user": "deploy", "key": "ssh-ed25519 AAAA..."}
  ],
  "sysctl": [
    {"name": "vm.swappiness", "value": "10"},
    {"name": "net.core.somaxconn", "value": "4096"}
  ],
  "packages": [
    {"name": "curl", "state": "present"},
    {"name": "telnet", "state": "absent"}
  ]
}
```

Example for a Redfish-managed server BMC:

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

Example for a Juniper Junos switch:

```json
{
  "hostname": "leaf01",
  "interfaces": [
    {"name": "ge-0/0/0", "description": "uplink to spine01", "enabled": true, "mtu": 9216}
  ],
  "vlans": [
    {"vlan_id": 100, "name": "prod-web"}
  ],
  "static_routes": [
    {"prefix": "10.0.100.0/24", "next_hop": "10.0.0.1"}
  ]
}
```

### Custom fields we expect

Add these custom fields in Nautobot once (Admin → Custom Fields):

| Field name | Type | On model | Purpose |
| --- | --- | --- | --- |
| `daalu_transport` | text | dcim.device | Override the platform-name heuristic. Valid values: `linux_ssh`, `redfish`, `junos`, `iosxr`, `eos` |
| `ssh_user` | text | dcim.device | Per-device SSH user override (otherwise tenant-wide default) |
| `ssh_port` | text | dcim.device | Per-device SSH port override |
| `ssh_credentials_ref` | text | dcim.device | Name of an `ssh_credentials` Integration row to use for this device |
| `redfish_user` | text | dcim.device | Per-device Redfish user |
| `redfish_port` | text | dcim.device | Per-device Redfish port |
| `redfish_credentials_ref` | text | dcim.device | Name of a `redfish_credentials` Integration row |
| `network_user` | text | dcim.device | Per-device network user |
| `network_port` | text | dcim.device | Per-device network port |
| `network_credentials_ref` | text | dcim.device | Name of a `network_credentials` Integration row |

The `*_credentials_ref` fields are how named-credential pools are pointed
at (see PR 9 docs).

### Webhooks (Nautobot → daalu)

When SoT intent changes, Nautobot pushes a webhook so daalu reacts
faster than the next reconciler tick. Configure in Nautobot
(Extensibility → Webhooks):

| Field | Value |
| --- | --- |
| Name | `daalu-sot-intent-changed` |
| Content types | `dcim.device`, `extras.configcontext` |
| Triggers | Update + Delete |
| URL | `https://daalu.example.com/api/v1/sot/webhooks/<tenant-slug>` |
| HTTP method | POST |
| HTTP content type | application/json |
| Secret | (a long random string — also paste this into the daalu integration's `webhook_secret` field) |
| Enabled | ✓ |

Nautobot signs the request body with HMAC-SHA512 using the secret;
daalu's webhook handler verifies. Mismatched secret → 401, request
dropped.

The URL tenant slug is part of the path because Nautobot's webhook
config doesn't let us inject arbitrary auth headers. It's safe — the
HMAC is the auth.

## Operating Nautobot

### Backups

`pg_dump` of Nautobot's Postgres is sufficient for v1. The `daalu_intent`
Config Contexts are the operationally critical data — if you lose them,
intent has to be re-authored from scratch, which is expensive.

### Upgrades

Helm-chart upgrades are usually a `helm upgrade` away. Watch the release
notes for breaking ORM changes — Config Context schemas and ObjectPermission
constraint semantics are stable but the underlying tables move occasionally.

After any upgrade, smoke-test:
1. `curl https://nautobot/api/status/` returns 200
2. `curl https://daalu.example.com/api/v1/onboarding/test/nautobot` (for
   any one tenant) returns ok
3. Pick a known device, fetch via daalu's reconciler manually (or wait
   for the next tick), confirm a proposal lands as expected (or doesn't,
   if nothing has drifted)

### Monitoring

The Nautobot pod's own `/api/status/` + Nautobot's RQ workers are the
key health surfaces. Use Network to Code's recommended Prometheus
exporter if you want metrics into your stack.

What to alert on:
- Nautobot pod not ready for > 5 minutes — blocks every tenant's reconciler
- Nautobot Postgres connection pool saturation — slow drift detection
- More than ~3 failed webhook deliveries to daalu in 1 hour — token rotation
  drift; signal an investigation

### Capacity planning

The hosted Nautobot is **multi-tenant** — every customer's devices are in
the same Postgres. Scale concerns:

- ~1000 devices per tenant: fine, single Postgres
- ~10,000 devices total across all tenants: still fine, default Helm
  config
- > 50,000 devices total: consider Postgres tuning (connection pooling
  via pgbouncer) or sharding into multiple Nautobot instances per
  customer segment. The `SourceOfTruth` ABC was kept specifically so
  this shard is "use a different URL+token per tenant" not "rewrite
  every consumer."

### When NOT to touch Nautobot directly

Don't modify a customer's `daalu-tenant-<slug>` ObjectPermission by hand.
Re-running `POST /api/v1/onboarding/nautobot/provision` is the supported
path — it'll find-or-create the row with the canonical shape.

Don't share the `daalu-provisioner` admin token with anyone — anyone with
it can mint tokens for any tenant.

## Manual ops cookbook

### Adding a new device to a tenant's SoT

The customer does this themselves in Nautobot's UI. Nothing to do on
daalu's side — the next reconciler tick picks up the new device. If they
also add a `daalu_intent` Config Context, the reconciler immediately
proposes any drift; otherwise no proposal until they author intent.

### Removing a tenant

Hosted mode:

1. Revoke the tenant's API token (Admin → Tokens → find by description
   `daalu tenant <slug>` → delete)
2. Delete the `daalu-tenant-<slug>` ObjectPermission
3. Delete the `<slug>` Tenant (this cascades — all their devices /
   IPs / etc. go with it)
4. On daalu's side, delete the tenant's `Integration(provider="nautobot")`
   row, or just let the next reconciler tick log "nautobot unavailable"
   and skip

BYO mode: the customer manages their own Nautobot; daalu just needs the
integration row cleaned up.

### Adding a new transport (e.g., FortiOS)

This is a per-PR follow-up. Outline:

1. Add a new `FortiOSFacts` Pydantic model to `core/sot/models.py`
2. Add `FortiOSFacts` to the `DeviceFacts` union
3. In `core/sot/nautobot.py`:
   - Add `_parse_fortios_facts(blob)` and `_fortios_facts_to_blob(facts)`
   - Extend `_dispatch_parse` and `_dispatch_to_blob`
   - Extend `_platform_bucket` to recognize fortios platform names
4. In `core/device/`:
   - Create `fortios.py` with `FortiOSAdapter(DeviceAdapter)`
   - Register at module bottom via `register_device_adapter(FortiOSAdapter)`
   - Side-effect import from `core/device/__init__.py`
5. Add `fortios → fortios_credentials` to `_CREDS_PROVIDER_BY_TRANSPORT`
   in `core/change_proposals.py`
6. Add `fortios_credentials` to `_REDACT_FIELDS` and `_ADAPTERLESS` in
   `api/routers/integrations.py`
7. Tests in `tests/test_fortios_adapter.py` mirroring the Redfish test
   layout

## Common Nautobot problems

### "Nautobot accepted the token" but the reconciler skips the tenant

The integration test endpoint just probes `/api/status/`, which any
authenticated token can hit. Your token may not have the per-tenant
ObjectPermission scope. Check:

```bash
curl -H "Authorization: Token <tenant token>" \
  "<nautobot url>/api/dcim/devices/?limit=1"
# Should return your tenant's devices, not 403 or empty.
```

If empty: the ObjectPermission isn't attached to your token's user, or its
constraint doesn't match your tenant slug.

### "GraphQL errors: insufficient permissions"

The user behind the token needs `dcim.view_device` (and friends) explicitly
granted via ObjectPermission. The provisioning route grants this; BYO
tokens need it manually.

### Webhook signatures keep failing

Most common cause: the secret in Nautobot doesn't match the secret in the
daalu Integration row. Both must be the same cleartext string. Re-paste
both and try again.

### `_parse_facts` warnings in the worker log

The blob in `daalu_intent` doesn't match the expected schema. Look at the
`raw` field in the log entry — usually a typo in a key name, or a string
where a list was expected. Fix the Config Context in Nautobot's UI.
