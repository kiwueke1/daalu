# API reference — HTTP routes added by this feature

All routes live under `/api/v1` and require the standard auth (Bearer JWT
or session cookie) unless explicitly noted. Per-tenant scoping is
enforced via `current_tenant_id` — cross-tenant access returns 404
(not 403, by convention).

## ChangeProposals — `/api/v1/change-proposals`

The primary surface for operator approval.

### `GET /change-proposals`

List proposals scoped to the caller's tenant. Most-recent first.

**Query parameters:**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `status` | string | none | Filter to one of `pending` / `approved` / `rejected` / `executed` / `failed` / `stale` |
| `device_id` | string | none | Filter to a single Nautobot device UUID |
| `kind` | string | none | Filter to one of `drift` / `manual` / `intended_change` |
| `limit` | int | 100, max 500 | Cap rows returned |

**Response:** `list[ChangeProposalOut]`

```json
[
  {
    "id": "uuid",
    "device_id": "nautobot-device-uuid",
    "kind": "drift|manual|intended_change",
    "status": "pending|approved|rejected|executed|failed|stale",
    "intended_config": "### /etc/hostname\nweb01\n",
    "observed_config": "### /etc/hostname\nold-name\n",
    "diff": "-old-name\n+web01\n",
    "renderer_version": "linux_ssh.v1",
    "evidence": {
      "triggered_by": "engine|reconciler|user",
      "llm_reasoning": "...",
      "llm_model": "claude-sonnet-4-6",
      "evidence_events": ["event-uuid", ...],
      "evidence_alerts": ["alert-uuid", ...],
      "evidence_metrics": [{"name": "...", "value": "...", "ts": "...", "source": "..."}],
      "confidence": 0.82
    },
    "created_by": "user-uuid-or-null",
    "approved_by": "user-uuid-or-null",
    "approved_at": "2026-05-21T03:00:00Z",
    "executed_at": "2026-05-21T03:00:30Z",
    "executor_result": {
      "success": true,
      "started_at": "...",
      "finished_at": "...",
      "per_step": [{"op": "...", "ok": true}, ...],
      "rollback_performed": false,
      "error": ""
    },
    "created_at": "2026-05-21T02:55:00Z"
  }
]
```

### `GET /change-proposals/{id}`

Single proposal. Cross-tenant ID → 404.

### `POST /change-proposals/{id}/approve`

Flip status `pending → approved`. Records the calling user as `approved_by`
and the timestamp as `approved_at`. The executor will pick it up on the
next beat tick.

**Errors:**
- 404 — proposal not in caller's tenant
- 409 — proposal not in `pending` status

### `POST /change-proposals/{id}/reject`

Flip status `pending → rejected`. Records the calling user as
`approved_by` (yes, even on reject — it's "who decided", not "who said
yes"). The executor never touches rejected rows.

**Errors:** same as approve.

### NOT in the API

- **`POST /change-proposals` (create)** — no HTTP route. Proposals are
  created in-process by the engine (`propose_change` tool), the
  reconciler, or future Python-side importers. External integrators
  needing to create proposals would need their own auth + audit shape
  designed.
- **`POST /change-proposals/{id}/execute`** — no HTTP route. Execution
  is the executor service's job; doing it via HTTP would break the
  identity-boundary model.

## SoT Devices — `/api/v1/sot/devices`

Thin proxy through the `SourceOfTruth` abstraction so the daalu UI
doesn't need direct Nautobot access. Backs the `/devices` list +
detail pages and the *Add device* form.

### `GET /sot/devices`

List the tenant's devices. Optional `?platform=linux` filter.

**Response:** `list[DeviceOut]` — `{id, name, primary_ip, platform, transport, tags, extra}`

### `GET /sot/devices/{id}`

Single device. 404 when not found.

### `GET /sot/devices/{id}/intent`

The device's current `daalu_intent` Config Context, with the facts
deserialised into a typed blob whose shape depends on `transport`. 404
when intent is unset (different from "device doesn't exist", which is
also 404 from `GET /sot/devices/{id}`).

**Response:** `{device_id, revision, transport, facts, fetched_at}`

### `GET /sot/devices/_catalog/list`

Reference data needed to populate the *Add device* form's dropdowns.
Returns sites, device types, roles, and platforms in one batch.

**Response:** `{sites, device_types, device_roles, platforms}` —
each is `list[{id, name, slug}]`.

### `POST /sot/devices` (admin)

Create a Nautobot Device row pre-stamped with `daalu_transport`. The
daalu tenant's `Integration(provider="nautobot")` supplies the
`nautobot_tenant_id` so the device lands tagged with the right Tenant.

**Body:**

```json
{
  "name": "web01",
  "primary_ip": "10.0.0.5/24",
  "site_id": "...",
  "device_type_id": "...",
  "device_role_id": "...",
  "platform_id": "..." | null,
  "transport": "linux_ssh" | "redfish" | "junos" | "iosxr" | "eos"
}
```

After the device row lands, the route POSTs an `ipam.ipaddress` and
PATCHes the device to set `primary_ip4`. Errors on the IP assignment
are logged but don't fail the device creation — the operator can
re-attempt via Nautobot's UI.

### `PUT /sot/devices/{id}/intent` (admin)

Write the device's `daalu_intent` Config Context. **Strict pydantic
validation** dispatches by `device.transport` — Linux facts validate
against `LinuxFacts`, Redfish against `RedfishFacts`, Junos / IOS-XR /
EOS against `NetworkFacts`. Malformed payload → 422.

**Body:** `{"facts": {...}}` — shape depends on transport.

**Response:** the same `IntentOut` shape `GET /sot/devices/{id}/intent` returns.

### `DELETE /sot/devices/{id}` (admin)

Removes the device from Nautobot. Dangling references in old
ChangeProposal rows are left alone — the executor will skip them with
`executor.device_not_found` on the next tick.

**Response:** 204 No Content.

## SoT Webhooks — `/api/v1/sot/webhooks/{tenant_slug}`

Receives webhooks from Nautobot when SoT data changes. **No bearer auth**
— authenticated via HMAC-SHA512 signature against the per-tenant webhook
secret.

The route is exempted from `AuthGateMiddleware` via a regex bypass in the
middleware config.

### `POST /sot/webhooks/{tenant_slug}`

**Headers:**

| Name | Required | Description |
| --- | --- | --- |
| `X-Hook-Signature` | yes | HMAC-SHA512 of the raw request body using the tenant's `webhook_secret` (Nautobot signs this) |
| `Content-Type` | yes | `application/json` |

**Body:** Nautobot webhook payload (variable shape — typically
`{event, model, data: {...}}`).

**Response:** 202 Accepted (the event is published asynchronously)

**Errors:**
- 401 — invalid or missing signature
- 404 — tenant slug doesn't exist
- 409 — tenant exists but has no `webhook_secret` set on its Nautobot
  integration
- 400 — body isn't valid JSON

### What happens after a valid webhook

1. Signature is verified against the stored `webhook_secret` (accepts
   both `webhook_secret` plaintext and `webhook_secret_ciphertext`
   encrypted shape; encrypted wins if both present)
2. A `sot.intent.changed` event is published onto the Redis event
   stream with the parsed payload
3. Downstream consumers (e.g., a future "auto-stale pending proposals
   for this device" handler) react to the event

The next reconciler tick will catch any new drift; nothing else is
synchronous.

## Onboarding — `/api/v1/onboarding`

PR 8 extended this surface with Nautobot pieces.

### `GET /onboarding/status`

Per-tenant onboarding status across the 14-step catalog. **Unchanged in
shape**, but the catalog grew from 13 → 14 (added `nautobot` between
`azure` and `cluster`).

```json
{
  "steps": [
    {"id": "slack", "provider": "slack", "configured": false, "status": "disconnected", "missing": ["webhook_url"]},
    ...
    {"id": "nautobot", "provider": "nautobot", "configured": false, "status": "disconnected", "missing": ["url", "token_ciphertext"]},
    {"id": "cluster", "provider": null, "configured": false, "status": "none", "count": 0}
  ],
  "completed": 0,
  "total": 14
}
```

### `POST /onboarding/test/{provider}` (extended)

PR 8 added a `nautobot` tester. POST with `{"config": {"url": "...",
"token": "..."}}` and the route probes `<url>/api/status/` with the
token.

**Response:** `{ok: bool, message: str, latency_ms: int}`

### `POST /onboarding/validate/nautobot` (existing pattern)

Schema-only check. Returns `{ok: bool, missing: list[str]}`. The
required fields for nautobot are `url` and `token_ciphertext`.

### `GET /onboarding/nautobot/hosted-status` (new in PR 8)

Tells the wizard whether to offer the "Provision hosted" button.

**Response:**

```json
{
  "hosted_available": true | false,
  "detail": "explanation when hosted_available=false"
}
```

`hosted_available` is true iff **both** `MANAGED_NAUTOBOT_URL` and
`MANAGED_NAUTOBOT_ADMIN_TOKEN` are set in the platform settings.

### `POST /onboarding/nautobot/provision` (new in PR 8)

Provision a hosted Nautobot slice for the caller's tenant. **Requires
admin auth** (the caller must be is_admin on their tenant).

**Body:** none (the tenant is inferred from the caller's auth)

**Response:**

```json
{
  "ok": true,
  "message": "provisioned Nautobot tenant 'acme'",
  "url": "https://nautobot.daalu.example.com",
  "nautobot_tenant_slug": "acme",
  "nautobot_tenant_id": "tenant-uuid"
}
```

**Side effects:**

- A Nautobot Tenant is created (or reused if exists) with slug = daalu
  tenant slug
- An ObjectPermission `daalu-tenant-<slug>` is created (or reused)
- A fresh APIToken is minted for the service user
- The tenant's `Integration(provider="nautobot")` row is upserted with
  `{url, token_ciphertext, hosted: true, nautobot_tenant_slug,
   nautobot_tenant_id, permission_id}`

**Idempotency:** safe to call multiple times. Tenant + Permission are
find-or-create; token is always fresh (old tokens stay valid until
manually revoked).

**Errors:**

- 503 — hosted mode disabled (`MANAGED_NAUTOBOT_*` not set)
- 502 — Nautobot rejected our admin call (bad admin token, missing
  service user, etc.) — error body includes the upstream message
- 404 — caller's tenant row vanished mid-request
- 401 / 403 — auth failure

## Integrations config — `/api/v1/integrations/config`

PR 5, 8, 9 added new providers to this surface.

### `GET /integrations/config`

List all integration-config rows for the caller's tenant. Sensitive
fields redacted as `"***"`.

### `PUT /integrations/config/{provider}` (existing)

Create or update the config for one provider. Replaces the whole config
dict.

**New providers this feature added:**

| Provider | Module | Sensitive fields (redacted on GET) |
| --- | --- | --- |
| `nautobot` | infra | `token`, `token_ciphertext`, `webhook_secret`, `webhook_secret_ciphertext` |
| `ssh_credentials` | infra | `private_key_ciphertext`, `password_ciphertext` |
| `redfish_credentials` | infra | `password_ciphertext` |
| `network_credentials` | infra | `password_ciphertext`, `enable_password_ciphertext`, `private_key_ciphertext` |

**Config shapes:**

```jsonc
// nautobot (BYO)
{
  "url": "https://nautobot.example.com",
  "token": "...",                // plaintext OK; read path accepts both
  "webhook_secret": "..."        // optional, for webhook signature verification
}

// nautobot (hosted — populated by the provision route)
{
  "url": "https://nautobot.daalu.example.com",
  "token_ciphertext": "...",
  "hosted": true,
  "nautobot_tenant_slug": "acme",
  "nautobot_tenant_id": "uuid",
  "permission_id": "uuid"
}

// ssh_credentials (tenant-wide)
{
  "user": "daalu",
  "port": 22,
  "sudo": true,
  "private_key_ciphertext": "...",   // OR password_ciphertext
}

// ssh_credentials (per-device — PR 9)
{
  "device_id": "nautobot-device-uuid",   // ← the priority key
  "user": "root",
  "private_key_ciphertext": "..."
}

// ssh_credentials (named pool — PR 9)
// Set `name` to something meaningful (e.g. "bastion-pool") and reference
// it from Nautobot via custom field `ssh_credentials_ref="bastion-pool"`
{
  "user": "bastion",
  "private_key_ciphertext": "..."
}

// redfish_credentials
{
  "user": "root",
  "port": 443,
  "password_ciphertext": "...",
  "verify_tls": false                   // BMCs typically have self-signed certs
}

// network_credentials
{
  "user": "automation",
  "port": 22,
  "password_ciphertext": "...",
  "enable_password_ciphertext": "..."   // optional, IOS-XR exec-mode escalation
}
```

### `DELETE /integrations/config/{provider}`

Removes the row. Multi-row providers (per-device / named-pool ssh_creds)
need ID-based delete — not currently supported via HTTP; SQL for now.

## Settings reference (env vars affecting these APIs)

| Env var | Default | Affects |
| --- | --- | --- |
| `EXECUTOR_JWT_SCOPE` | `executor` | Identity gate in `change_proposals.execute()` |
| `EXECUTOR_PERIOD_S` | `30` | How quickly approved proposals execute |
| `EXECUTOR_BATCH_SIZE` | `25` | Rows per executor tick |
| `EXECUTOR_QUEUE_NAME` | `executor` | Celery queue routing |
| `SOT_RECONCILE_PERIOD_S` | `300` | Drift detection cadence |
| `MANAGED_NAUTOBOT_URL` | `""` | If set, enables hosted-Nautobot provisioning |
| `MANAGED_NAUTOBOT_ADMIN_TOKEN` | `""` | Token used by the provisioning route |
| `MANAGED_NAUTOBOT_SERVICE_USER` | `daalu-platform` | Nautobot user our tokens are minted for |
| `COMMIT_CONFIRMED_TIMEOUT_S` | `600` | Junos / IOS-XR rollback window |

## Auth model

Standard daalu auth applies:

- **Bearer JWT** in `Authorization: Bearer <token>` header — issued by
  `/auth/login`
- **Session cookie** for browser flows — same JWT, just in a cookie
- **Tenant context** — automatically scoped from the auth token; every
  query filters by `tenant_id`. Cross-tenant access returns 404
- **`current_admin`** dependency on some routes (e.g.,
  `/onboarding/nautobot/provision` and the integrations PUT) — checks
  the user has `is_admin=true` on their tenant

The executor service does NOT authenticate via HTTP — it mints its
`Actor(kind="executor", ...)` directly in Python after verifying its
mounted env / SA. There's no HTTP route the executor needs to call.

## Error response shape

All routes use FastAPI's standard:

```json
{"detail": "human-readable error message"}
```

With the canonical HTTP status code (404 / 409 / 422 / 500 / etc.).

## Webhook signature reference

If you're implementing a non-Nautobot SoT and want to use the
`/sot/webhooks/{tenant_slug}` receiver:

```python
import hmac, hashlib

body = b'...'  # the raw request body bytes
secret = b'<the webhook_secret>'
sig = hmac.new(secret, body, hashlib.sha512).hexdigest()
# send as X-Hook-Signature: <sig>
```

The route computes the same and uses `hmac.compare_digest()` to avoid
timing attacks.

## Rate limits

Currently none on the new routes. The executor's batch size is the only
implicit limit. If a tenant hammers `/api/v1/change-proposals` (or any
other route) you'd see backpressure from FastAPI's worker pool, not from
a rate limiter.

## OpenAPI

`GET /openapi.json` and `/docs` (Swagger UI) include all new routes.
