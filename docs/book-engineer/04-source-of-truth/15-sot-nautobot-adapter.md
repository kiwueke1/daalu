# 15. The Nautobot adapter

The Nautobot adapter is the supported SoT. This chapter explains how it
reaches Nautobot, how it reads inventory, what it writes back, and the design
choices we made. It stays focused on the **adapter** — the auth machinery it
relies on (gateway JWT, token-exchange, the tool proxy that serves the *human*
UI) lives in the auth-integrations chapters and is cross-referenced, not
duplicated here.

> **Which Nautobot?** The adapter no longer assumes a single hand-wired
> Nautobot. `core/sot/nautobot.py::_load_credentials` now **prefers the
> NV-CM-bundled Nautobot** — the `Integration(provider="config_manager")`
> carrying `nautobot_url` + `nautobot_token_ciphertext` — and only falls back
> to a standalone `nautobot` integration (BYO or the legacy
> `nautobot-controller`, chapter 19). Same adapter, same GraphQL/REST shape;
> only where it points, and *how it dials*, changed. See chapters 20 §20.6,
> 21, and 23 §23.1.

Code: `src/daalu_automation/core/sot/nautobot.py`. The class is
`NautobotSoT(SourceOfTruth)` — there is no `BaseAdapter`; reads go over
Nautobot's **GraphQL** endpoint and writes go over the **REST**
config-context endpoint (GraphQL mutations are not enabled in stock Nautobot).

---

## Adapter shape

`NautobotSoT` implements the `SourceOfTruth` interface
(`core/sot/base.py`). The methods that matter:

```python
class NautobotSoT(SourceOfTruth):
    provider: ClassVar[str] = "nautobot"

    async def list_devices(db, tenant_id, *, platform=None) -> list[Device]: ...
    async def get_device(db, tenant_id, device_id) -> Device | None: ...
    async def get_intended_config(db, tenant_id, device_id) -> IntendedConfig | None: ...
    async def put_intended_config(db, tenant_id, device_id, intended) -> SoTRevision: ...
    async def record_observed(db, tenant_id, observed) -> None: ...
    async def health(db, tenant_id) -> tuple[bool, str]: ...
```

Every method is tenant-scoped: it takes a `tenant_id`, builds a per-call
`httpx.AsyncClient` via `_client()`, and tears it down with `async with`. There
is no long-lived connection pool — credentials and the dial path are resolved
fresh on each call so a re-pointed integration or a re-keyed tunnel takes
effect immediately.

**Reads** (`list_devices`, `get_device`, `get_intended_config`) issue GraphQL
queries (`_DEVICES_QUERY` / `_DEVICE_QUERY`) against `/api/graphql/`. The
device's transport is read from the `daalu_transport` custom field (or a
platform-name heuristic in `_platform_bucket`) so the right fact parser —
`_parse_facts` (Linux), `_parse_redfish_facts`, or `_parse_network_facts`
(junos / iosxr / eos) — gets dispatched via `_dispatch_parse`.

**Writes** (`put_intended_config`, `record_observed`) find-or-create a Nautobot
**Config Context** attached to the device: `daalu_intent` for intended state,
`daalu_observed` for the last observed snapshot. The JSON body shape matches
`core/sot/models.py::LinuxFacts` / `NetworkFacts` / `RedfishFacts`.

---

## Credentials

Configured at integration-creation time and stored encrypted:

- **Base URL** — for the bundled Nautobot this is the in-cluster
  `http://svc-nautobot.<base_hostname>` (plain HTTP, no public DNS); for a BYO
  Nautobot it's a public `https://nautobot.acme.io`.
- **API token** — a Nautobot token with read on `dcim`/`ipam`/`extras` and
  write on `extras.config-contexts` and (for the device-onboarding routes)
  `dcim`/`ipam` object create.

**The bundled token is auto-stamped — nobody pastes it** (PR #121). The chart
generates a `nautobot-token` Secret at install; the config-manager controller
**mirrors that Secret onto the row** (encrypted into the environment's secrets
bag) and reports its ciphertext in the progress payload; the hub's integration
upsert persists it as `nautobot_token_ciphertext` on the `config_manager`
Integration. This closed the long-standing "populated in a follow-up step"
TODO — and it had to, because the previous UX was a trap: the only Nautobot
URL an NV-CM tenant *had* was the `*.cmtools.example.com` one, which is the hub's
**browser** tool proxy — it authenticates with the Daalu session cookie and
*overwrites* `Authorization`, so an API token pasted into the BYO wizard could
never pass through it and every attempt 401'd. The BYO connection tester now
rejects `*.cmtools.example.com` URLs outright with an explanation instead of
surfacing that misleading 401. Environments created before the auto-stamp
need one provision-progress poll (or a re-trigger) to pick up the token.

Tokens are stored encrypted via `core/secrets.py` / `core/crypto.py`. The
ciphertext is decrypted only when a call is made, inside `_load_credentials`:

```python
cm_token = decrypt_secret(cm_token_ct) if cm_token_ct else cm_token_pt
```

`_load_credentials` accepts two on-disk shapes for the standalone integration —
plaintext `token` (what the onboarding wizard PUTs, matching how
`slack.webhook_url` is stored) and encrypted `token_ciphertext` (what the
hosted-provisioning route and operators PUT). The encrypted form wins when both
are present. For the bundled path it reads `nautobot_token_ciphertext` (or
`nautobot_token`) off the `config_manager` integration's `config`.

---

## How the adapter reaches Nautobot

`_load_credentials` returns a **four-tuple** — `(url, token, proxy_url,
forward_host)` — and the last two elements are the important parts. There are
two distinct paths to the same Nautobot, and the adapter is on the **machine**
path:

```
MACHINE path (this adapter)                HUMAN path (browser, NOT this adapter)
───────────────────────────                ──────────────────────────────────────
hub reconciler / executor                  ops.example.com user clicks a tool link
   │                                            │
   │ httpx.AsyncClient(proxy=proxy_url)         │ *.cmtools.example.com  (flat host)
   ▼                                            ▼
daalu-edge forward proxy  ─── WG tunnel ───  daalu-edge forward proxy
   │  (deploy/edge/proxy.py, :8888)             │
   ▼                                            ▼
http://svc-nautobot.<base>  (in-cluster)    tenant Envoy gateway → Nautobot UI
```

### The machine path (dial the gateway, route on `svc-nautobot`)

The bundled Nautobot lives behind a private `10.x` ClusterIP on the tenant's
workload cluster. Its `svc-nautobot.<base_hostname>` host only resolves
*inside* that cluster — worse, that flat hostname is **not in the edge proxy's
allowlist** (which permits `*.svc(.cluster.local)` + RFC1918 literals), so
dialing it directly through the tunnel can *never* work. PR #121 replaced the
direct dial with the **same dial-the-gateway pattern the tool proxy uses**
(`api/tool_proxy.py`):

1. `_load_credentials` picks the environment's `gateway_internal_url` (the
   Envoy proxy's in-cluster Service address — allowlisted) as the dial target
   and extracts `svc_host = urlsplit(nautobot_url).hostname` as the
   **routing host** (`core/sot/nautobot.py:145-155`).
2. `cluster_tunnel_id` resolves through
   `core/cluster_proxy.py::get_proxy_url(db, cluster_tunnel_id)`: `connected` →
   `http://<tunnel_ip>:8888` (the daalu-edge forward proxy, chapters 24/25);
   no tunnel / down → `None`.
3. `_build_http_client(url, token, proxy_url=…, forward_host=…)`
   (`core/sot/nautobot.py:64-105`) sends the routing host as
   **`X-Daalu-Forward-Host`** on the proxied dial (the edge strips `Host` as
   hop-by-hop and re-applies this header — `deploy/edge/proxy.py`) or as a
   plain `Host` header on a direct dial. The tenant gateway host-routes
   `svc-nautobot.<base>` to the Nautobot machine face. Verified 200 e2e
   against the prod tunnel.
4. BYO/public Nautobots keep the old shape: `forward_host=None`, direct dial
   (or plain proxy dial), which also keeps the `httpx.MockTransport` tests
   working.

```python
# core/sot/nautobot.py:_build_http_client
headers["Host" if proxy_url is None else "X-Daalu-Forward-Host"] = forward_host
return httpx.AsyncClient(base_url=url.rstrip("/"), timeout=15.0,
                         proxy=proxy_url, headers=headers)
```

So the adapter authenticates to Nautobot with a **Nautobot API token** (the
`Authorization: Token …` header), not a Keycloak JWT — the gateway's JWT
`SecurityPolicy` is `optional`, so token-authed machine calls pass through.
The JWT story below is about the gateway/UI in front of Nautobot on the human
path.

REST-level callers — bulk import, device create/update/delete, the topology
inventory — share this exact resolution via the public
`core/sot/nautobot.py::nautobot_client` helper (PR #121), instead of only
reading the standalone BYO row as they used to.

### The human path (browser → tool proxy)

A logged-in `ops.example.com` user does **not** use this adapter to view Nautobot.
The "Network & servers" page links to a flat `nautobot-<slug>.cmtools.example.com`
host, the hub reverse-proxies that request over the same WG tunnel, and the
user lands in the Nautobot UI logged in as themselves. That path — the cmtools
tool proxy and the gateway `SecurityPolicy` (**chapter 68**) plus the per-user
Keycloak token-exchange (**chapter 69**) — is documented in section 13. Do not
look for it in this adapter; the two paths only share the tunnel and the same
backing Nautobot.

---

## Gateway JWT auth and the internal JWKS

This matters for the *human* path, and it's why the bundled Nautobot is RBAC-gated.
The tenant's Envoy gateway in front of Nautobot runs a `SecurityPolicy` that:

- validates an **optional** JWT (`jwt.optional: true`) extracted from either
  `Authorization: Bearer …` or the `NVConfigManagerAccessToken` cookie, and
- copies claims to headers for the Nautobot `nv_config_manager_auth` plugin,
  whose `user_provider` provisions a real Django user from the JWT.

The gateway's `issuer`/`audiences` stay the **external** realm (`auth.example.com`
/ `nv-config-manager`), but the **JWKS fetch follows the target cluster**
(`render_values`, keyed on `target_cluster_tunnel_id`):

- **Operator cluster** (`NULL`) → `oidc.jwksUri →
  keycloak.daalu-automation.svc.cluster.local:8080/.../certs`. The external
  `auth.example.com` 404s from inside the cluster, so signing keys come over the
  in-cluster Service.
- **Customer cluster** (`NOT NULL`, reached over WireGuard) → the **external**
  certs URL. That cluster runs no Keycloak, so the in-cluster name doesn't
  resolve there.

Getting this wrong fails *silently*: the Nautobot plugin swallows the JWKS
error at `log.debug`, and its `JWTCookieMiddleware` only considers providers
with `user_provider: true` — so the browser lands on Nautobot's own login page
while machine `svc-*` calls (validated by Envoy separately) keep working.

The interactive OIDC
redirect is disabled for hub-proxied tenants (`oidc.interactiveRedirect=false`)
— the hub injects the token, the gateway only verifies it.

The wiring for all of that — `config_manager_controller/values.py::render_values`
(jwksUri / interactiveRedirect / flat hostnames), the chart's
`templates/security-policy.yaml`, and the Keycloak clients — is **chapters
68–71** and the security chapter 59 (Keycloak hub SSO). This
chapter only needs you to know: the JWT in front of Nautobot is verified
against the internal JWKS, and its `roles` claim drives RBAC.

---

## Nautobot RBAC reality

> If a user opens the bundled Nautobot and sees almost no menus — a "scanty
> sidebar" with most plugins missing — this is the cause, not a broken deploy.

Nautobot's `nv_config_manager_auth.jwt_authentication` reconciles Django
privileges from the JWT on **every login**. The control is
`nautobot.rbac.superuserGroups` in the chart
(`values.yaml`, surfaced as `NV_CONFIG_MANAGER_SUPERUSER_GROUPS` in
`templates/_nautobot-configmap-data.tpl`):

- It maps the JWT **`roles` claim** (the default groups claim) to Django
  `is_superuser` + `is_staff`. If the user's `roles` claim contains one of the
  configured group names, they're promoted to superuser on login.
- **`superuserGroups: []` (the chart default) disables the feature entirely.**
  No promotion happens; a freshly JWT-provisioned user lands as an ordinary,
  near-unprivileged Django account → the scanty sidebar. Only the bootstrap
  admin user remains a superuser.
- It reconciles **both ways**: a role removed in the IdP demotes the user on
  their next login. Promotions and demotions in Keycloak take effect without
  touching Nautobot.

Since PR #127 this is wired end-to-end: `render_values` populates
`superuserGroups` from `settings.keycloak_nvcm_superuser_roles`
(`config_manager_controller/values.py:320-321`, default `nvcm-superuser`), and
`ensure-keycloak-sso` **creates those realm roles** and grants the service
roles to the `daalu-hub-nvcm` service account — previously the chart matched
against role names nothing ever created, so *every* SSO'd user (including the
service-account fallback identity) hit Access Denied. See
[70 §70.5](../13-auth-integrations/70-keycloak-realm-clients-and-provisioning.md).

So "which Nautobot menus/plugins a user sees" is governed by
`nautobot.rbac.superuserGroups` ⨯ the `roles` claim Keycloak stamps on that
user's token — not by anything in `core/sot/nautobot.py`. The adapter's
machine path is unaffected: it uses a Nautobot API token with its own
Nautobot permissions, independent of JWT RBAC.

---

## Inventory read

`list_devices` runs `_DEVICES_QUERY` against `/api/graphql/` and maps each raw
device through `_to_device`:

```python
async def list_devices(self, db, tenant_id, *, platform=None):
    async with await self._client(db, tenant_id) as client:
        data = await self._graphql(client, _DEVICES_QUERY)
    out = []
    for raw in data.get("devices") or []:
        dev = self._to_device(raw)
        if platform and dev.platform != platform:
            continue
        out.append(dev)
    return out
```

`_to_device` derives the platform bucket (`linux` / `redfish` / `junos` /
`iosxr` / `eos` / `unknown`) from `platform.name`, and resolves the device's
**transport** by preferring the `daalu_transport` custom field, falling back to
the bucket. `_graphql` raises `NautobotUnavailable` on any GraphQL `errors`
array so a partial response never gets silently mapped.

The reconciler (chapter 14) is what calls these on a schedule; the adapter
itself holds no crawl loop or mirror tables.

---

## Intended config (golden state)

Intended config is a Nautobot **Config Context** named `daalu_intent` attached
to the device. `get_intended_config` reads it out of the same GraphQL response
(`config_context` field) — no second round-trip — and dispatches to the right
fact parser by transport:

```python
ctx = raw.get("config_context") or {}
intent_blob = ctx.get("daalu_intent")
...
return IntendedConfig(
    device_id=device_id,
    revision=_revision_of(intent_blob),       # sha256(canonical-json)[:16]
    facts=_dispatch_parse(dev.transport, intent_blob),
    fetched_at=datetime.now(tz=timezone.utc),
)
```

The revision is a content hash (`_revision_of`), so a no-op write doesn't churn
the revision string. Fact parsers are tolerant: one malformed `authorized_key`
/ `sysctl` / `interface` entry is logged (`nautobot.intent.bad_*`) and skipped
rather than poisoning the whole device's intent.

---

## Writing back

Two writes, both via the REST config-context endpoint
(`/api/extras/config-contexts/`). The adapter never mutates device or IP
objects — only the two daalu-owned contexts.

### Intended config (`put_intended_config`)

Serialize the facts (`_dispatch_to_blob`), find the existing `daalu_intent`
context for the device (`_find_intent_context_id`), then `PATCH` its `data` or
`POST` a new one:

```python
# create
{"name": "daalu_intent", "weight": 1000, "is_active": True,
 "devices": [device_id], "data": body_blob}
# or update
PATCH /api/extras/config-contexts/<ctx_id>/  {"data": body_blob}
```

The returned `SoTRevision` uses Nautobot's `last_updated` (falling back to the
context `id`, then the content hash).

### Observed snapshot (`record_observed`)

Identical find-or-create against the `daalu_observed` context (weight `100`, so
intent at weight `1000` wins on merge). This records "what the box actually
looked like last time we saw it," distinct from intent.

There is no "patch Nautobot's golden-config plugin" path — intent lives in
plain config contexts, which keeps the write surface to one REST resource and
avoids depending on the golden-config plugin being installed.

---

## Nautobot 2.x compatibility (the bundled Nautobot is 2.4)

The adapter and the device-onboarding routes were originally written against
Nautobot 1.x shapes. The first real end-to-end onboarding against the
NV-CM-bundled Nautobot 2.4 (2026-07-22) surfaced four incompatibilities, all
fixed in PRs #122–#126 — keep them in mind when touching this code or when a
BYO tenant shows up with a 1.x instance:

- **GraphQL custom fields renamed** (PR #126). Nautobot 2.x exposes
  custom-field data as `_custom_field_data`; querying the old
  `custom_field_data` name is a **400**, which broke `GET /sot/devices` (500)
  and the topology inventory (`topology.inventory_failed`). Both device
  queries alias it back — `custom_field_data: _custom_field_data`
  (`core/sot/nautobot.py:378-407`) — so the response shape and every
  downstream reader are unchanged.
- **Roles moved to `extras`** (PR #123). Roles live at `/api/extras/roles/`
  since 2.0; `/api/dcim/roles/` is the 1.x location, and its 404 silently
  yielded an *empty* role list — the bulk-import dry run then flagged every
  row "unknown role". The catalog fetch lists **both** paths and 404-skips on
  whichever the target version lacks
  (`api/routers/sot_devices.py:310-317`).
- **Device types are matched by `model`** (PR #123). Device types carry
  `model`, not `name`, and their `display` is `"<manufacturer> <model>"` —
  not what an operator types in a spreadsheet. The catalog prefers
  `name or model or display` (`api/routers/sot_devices.py:326-334`), so a row
  saying `MSN3700` resolves.
- **IP assignment is a four-step flow** (PR #122). The 1.x
  `assigned_object_type` shortcut and direct `primary_ip4` patch both 400 on
  2.x: an IP now needs a **namespace** (or parent prefix), and `primary_ip4`
  refuses any IP not attached to one of the device's interfaces. So
  `_create_one_device` (`api/routers/sot_devices.py:368-480`) creates the IP
  in the **Global namespace**, adds a **virtual `mgmt0` interface**, binds via
  `/api/ipam/ip-address-to-interface/`, then PATCHes `primary_ip4` — sequence
  verified against the live bundled Nautobot. Each later step degrades to a
  logged warning rather than failing the device create.

Two Apply-time prerequisites live in Nautobot itself, not our code: a
**Global-namespace prefix** covering the management network, and the
**`daalu_transport` custom field** must exist — seed both before the
first Apply.

One frontend footnote (PR #124): Linux file dialogs (GTK /
xdg-desktop-portal) filter by MIME type and `.yaml` often has no
shared-mime-info mapping — an extension-only `accept` list made the
bulk-import picker look xlsx-only. The picker lists the YAML MIME types
alongside the extensions.

### Where onboarding lives in the UI (PR #121)

Device onboarding moved out of the standalone Integrations page into
**Managed infra → Network & servers → (primary environment) → "Onboard your
devices"**: live Nautobot wiring status (backed by the new
`GET /sot/devices/_status` probe, `api/routers/sot_devices.py:212`), an inline
SSH credential form, bulk import, and a topology link — no URL/token pasting.
The Integrations page itself left the sidebar and became a tab in
`/managed-infra` (`?tab=`-synced; `/integrations` redirects).

---

## Health

`health()` does the cheapest possible authenticated probe — `GET /api/status/`
through the same `_client` (and therefore the same tunnel dial) — and reports
`(False, reason)` on `NautobotUnavailable` or any exception, never raising. The
reconciler uses it to mark an integration red without crashing a run.

---

## Limitations

What the adapter doesn't do today:

- **Structural diff.** Intent/observed are compared as parsed facts; deep
  per-field structural diffing is partial.
- **Object write-back.** Writes are scoped to the `daalu_intent` /
  `daalu_observed` config contexts. Arbitrary device / interface / custom-field
  edits via proposals aren't supported.
- **GraphQL mutations.** Stock Nautobot doesn't enable them; that's why every
  write is a REST config-context call.
- **Tunnel-down fallback.** If the tunnel isn't `connected`, `get_proxy_url`
  returns `None` and the adapter direct-dials the `svc-*` host, which won't
  resolve from the hub — the call fails fast rather than hanging. Bring the
  tunnel up (chapter 24/25) before crawling.

---

## What's next

Chapter 16 — the change-proposal lifecycle in detail. For the *human* side of
the bundled Nautobot — the browser tool proxy and the gateway `SecurityPolicy`
in chapter 68, the per-user token-exchange in chapter 69, and the realm-level
SSO foundation in chapter 59 (Keycloak hub SSO); for the machine token / network
plane, chapter 21 §21.2; for the tunnel itself, chapters 24–25.
