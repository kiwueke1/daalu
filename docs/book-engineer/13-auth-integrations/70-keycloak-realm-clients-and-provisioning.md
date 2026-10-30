# 70. Keycloak realm, clients & provisioning (for tool SSO)

This chapter is the **single source of truth for what must exist in the Keycloak
realm `daalu`** for per-tenant NV-CM tool SSO through the hub to work. If you are
debugging a 401 from a tenant's Nautobot tile, a `400` from a token-exchange, or
standing up a fresh `auth.example.com`, this is the reference for *which clients,
mappers, roles, and redirect URIs have to be there* and *how they get created so
they survive a realm rebuild*.

> Read [59. Keycloak hub SSO](../10-security/59-keycloak-hub-sso.md) first — it
> explains the *browser login* half (`auth_mode=oidc`, `daalu-hub-ui`, identity
> mapping, the `ensure-keycloak-sso` Job). This chapter covers the *tool SSO*
> half: the `daalu-hub-nvcm` exchange client and the `nv-config-manager-ui`
> gateway client, plus the token-exchange wiring that links all three.

This chapter is deliberately a **reference**, not a flow walkthrough. The
request flow (browser → hub `CmtoolsHostRouter` → `tool_proxy` → WG tunnel →
tenant Envoy gateway) lives in
[68. NV-CM tool SSO through the hub](68-nvcm-tool-sso-through-the-hub.md). For the
per-user-vs-service Bearer decision see
[69. Per-user token-exchange](69-per-user-token-exchange.md), and for the
machine (`svc-*`) client-credentials path see
[21. The NV-CM network plane](../04-source-of-truth/21-nvcm-network-plane.md) §21.2
and [23. NV-CM internals and operator](../04-source-of-truth/23-nvcm-internals-and-operator.md).

---

## 70.1 The three clients at a glance

Everything below lives in **realm `daalu`** on the Keycloak instance
(`auth.example.com`, issuer `https://auth.example.com/realms/daalu`), image
`quay.io/keycloak/keycloak:26.2` (§70.7 explains why 26.2 is a hard floor). Three clients
matter for tool SSO. They are *not* interchangeable — each plays one role in a
chain, and the chain only closes if all three mappers exist.

| Client | Type | Used by | What it carries / does |
|---|---|---|---|
| `daalu-hub-ui` | **confidential**, authorization-code + PKCE | the operator's *browser* login to `ops.example.com` (the hub backend holds the secret) | Mints the hub user session (id_token + refresh_token). Its audience-mapper stamps `aud=daalu-hub-nvcm` so `daalu-hub-nvcm` is *permitted* to token-exchange the user's token. The RP-logout `post_logout_redirect_uri` must be on its allow-list. |
| `daalu-hub-nvcm` | confidential, service account + token-exchange | the hub backend (`core/keycloak.py`) | The exchange client. `standard.token.exchange.enabled=true`. Its audience-mapper stamps `aud=nv-config-manager` onto the *exchanged* token. Its service account holds the `nvcm-deploy-execute` realm role (the machine/fallback identity). |
| `nv-config-manager-ui` | public, standard flow + PKCE | the per-tenant Envoy gateway's OIDC config | The `clientId`/`audiences` the tenant gateway `SecurityPolicy` validates against. `redirectUris` cover `*.cmtools.example.com`. |

The single fact that makes the whole design hang together: **`daalu-hub-nvcm`
runs a Keycloak *Standard* Token Exchange on a `daalu-hub-ui` user token, and
the exchanged token's `aud` is `nv-config-manager`** — which is exactly the
audience the tenant gateway and NV-CM's Nautobot auth plugin require. There are
therefore **two audience-mappers on two different clients**, and confusing them
is the #1 cause of a silent fallback to the shared service identity:

```
daalu-hub-ui   --(mapper: aud += daalu-hub-nvcm)-->  permits the exchange
daalu-hub-nvcm --(mapper: aud  = nv-config-manager)--> the exchanged token the gateway accepts
```

---

## 70.2 `daalu-hub-ui` — browser login + exchange-permission

The browser login client. Its OIDC code+PKCE flow and id_token validation are
[59](../10-security/59-keycloak-hub-sso.md)'s subject; here we record only what
tool SSO adds to it.

**Required config**

- `publicClient=false` (**confidential** — the hub backend holds
  `keycloak_hub_client_secret` and sends it on the refresh that
  `get_user_nvcm_token` performs), `standardFlowEnabled=true`, authorization-code
  + `pkce.code.challenge.method=S256`. Chapter 59 (its settings table) likewise
  calls this the "`daalu-hub-ui` confidential client".
- `redirectUris` include the hub callback (`keycloak_hub_redirect_url`, by
  convention `https://ops.example.com/api/v1/auth/oidc/callback`).
- **Post-logout redirect URI on the realm allow-list.** `api/routers/auth.py:oidc_logout`
  reads the `kit` (id_token) claim from the session and passes it, plus a
  `post_logout_redirect_uri` (from `keycloak_hub_post_logout_url`, default the
  hub `/login`), as `id_token_hint` to `core/oidc.end_session_url`; Keycloak only
  honours that redirect if it is on the client's allow-list. **Note:** nothing in
  this repo provisions `post.logout.redirect.uris` — `ensure_client` (§70.5) sets
  only `redirectUris`. The post-logout URI must be present on the client by some
  other means (the platform's declarative realm definition, or a manual add) or
  RP-logout falls back to a local redirect without ending the Keycloak session.
- **Audience-mapper `nvcm-exchange-audience`** (an `oidc-audience-mapper`) with
  `included.client.audience=daalu-hub-nvcm`, `access.token.claim=true`,
  `id.token.claim=false`. This is the *exchange-permission* mapper. Keycloak's
  Standard Token Exchange will only let `daalu-hub-nvcm` exchange a
  subject token whose `aud` already lists `daalu-hub-nvcm`; this mapper puts it
  there on every `daalu-hub-ui` access token. Provisioned by
  `core/keycloak_admin.py:ensure_audience_mapper` (see §70.5).

**Realm refresh-token policy.** `revokeRefreshToken` must be **off** for the
realm (or at least for this client). The hub stores the user's Keycloak refresh
token (encrypted, session claim `krt`) and reuses it on every tool click to mint
a fresh access token before exchanging. With rotation on, the second click
would 400 with `invalid_grant`. `core/keycloak.py:get_user_nvcm_token` documents
this assumption ("rotation is off, so the stored refresh token is reusable").

---

## 70.3 `daalu-hub-nvcm` — confidential exchange/service client

The hub's machine identity *and* the per-user exchange client. Its id/secret
land on each tenant's `Integration(provider="config_manager")`
(`keycloak_client_id` + `keycloak_client_secret_ciphertext`) — no longer by
hand: put the secret in `KEYCLOAK_NVCM_CLIENT_SECRET` (SOPS) and the NV-CM
progress poll stamps and encrypts it on every provision
(`_keycloak_service_creds` in `api/routers/onboarding.py`).

**Required config** (`deploy/scripts/config-manager/setup-keycloak.sh` lines 73–90):

- `publicClient=false`, `serviceAccountsEnabled=true`,
  `standardFlowEnabled=false`, `directAccessGrantsEnabled=false` — a pure
  service/exchange client, no browser flow.
- **`attributes."standard.token.exchange.enabled"=true`** — opts this client
  into *Standard* token exchange (GA on KC 26.2+; the switch is inert on 26.1 —
  §70.7). Without it the exchange call 400s
  and `tool_proxy` falls back to the shared service token. (V1 impersonation
  exchange is **not** used — see §70.7.)
- **Audience-mapper `nvcm-audience`** (an `oidc-audience-mapper`) with
  `included.client.audience=nv-config-manager` (`$AUDIENCE`),
  `access.token.claim=true`. This stamps `aud=nv-config-manager` onto the
  *output* of the exchange, which is why `core/keycloak.py:get_user_nvcm_token`
  sends **no `audience` parameter** on the exchange request — the mapper, not
  the request, sets the audience.
- **Service-account role `nvcm-deploy-execute`** — a realm role granted to
  `service-account-daalu-hub-nvcm`. It lands in the token's `roles` claim and
  maps to NV-CM's RBAC *execute* role for `DeployWorkflow`. This same client,
  via `core/keycloak.py:get_machine_token` (client-credentials grant), is the
  fallback Bearer when no per-user `krt` is present and is the machine identity
  documented in [21](../04-source-of-truth/21-nvcm-network-plane.md) §21.2.

**The two roles this one client plays**

```
per-user (preferred):  refresh(daalu-hub-ui) -> standard-exchange via daalu-hub-nvcm -> aud=nv-config-manager, sub=the user
service (fallback):    client_credentials on daalu-hub-nvcm        -> aud=nv-config-manager, sub=service-account, roles=[nvcm-deploy-execute]
```

---

## 70.4 `nv-config-manager-ui` — the tenant gateway OIDC client

The client the **tenant's Envoy gateway** is configured against. The hub never
drives a browser flow through it (hub-proxied tenants run with
`oidc.interactiveRedirect=false`); it exists so the gateway `SecurityPolicy`'s
JWT provider has a `clientId`/`audiences` to validate, and so any *direct*
(non-hub) browser OIDC against a tenant resolves to a registered client.

**Required config** (`setup-keycloak.sh` lines 95–99):

- `publicClient=true`, `standardFlowEnabled=true`,
  `pkce.code.challenge.method=S256`.
- **`redirectUris=["https://*.cmtools.example.com/*"]`** (`$UI_REDIRECT`) — covers
  every flat tenant tool host `<tool>-<slug>.cmtools.example.com` and the bare
  `<slug>.cmtools.example.com` config-browser UI under one wildcard.
- Its `aud` must equal `keycloak_token_audience` / the gateway's
  `oidc.audiences` (default `nv-config-manager`). In the gateway `SecurityPolicy`
  the JWT provider's `issuer` stays the **external** `https://auth.example.com/...`
  and only the **JWKS fetch URL** varies by target cluster — in-cluster
  `keycloak.daalu-automation.svc.cluster.local:8080` for environments installed
  here, the external certs URL for a customer cluster over WireGuard. Either
  way the token `iss`/`aud` must
  still match the external realm. (See `config_manager_controller/values.py:render_values`
  and the chart `templates/security-policy.yaml`.)

> ⚠ **Durability caveat:** `setup-keycloak.sh` sets `redirectUris` with the
> `-s 'redirectUris=[...]'` form, which **replaces** the existing list rather
> than appending. Re-running with a narrower `UI_REDIRECT` will silently drop
> previously-registered URIs. Always pass the full wildcard.

---

## 70.5 How each is provisioned durably

There are three provisioning surfaces, layered weakest-to-strongest. The goal
is that a fresh `daalu` + `auth.example.com` bring-up has all three clients +
mappers + roles without manual clicking, and a realm rebuild self-heals.

### 1. `deploy/scripts/config-manager/setup-keycloak.sh` — the authoritative client definitions

The declarative source for `daalu-hub-nvcm` and `nv-config-manager-ui`. Run via
`kcadm.sh` (ships in the Keycloak container). Idempotent: re-running `update`s in
place via `upsert_client`. It creates, in order:

1. realm role `nvcm-deploy-execute`;
2. `daalu-hub-nvcm` (confidential, `standard.token.exchange.enabled=true`) + its
   `nvcm-audience` mapper (`aud=nv-config-manager`) + grants the execute role to
   its service account;
3. `nv-config-manager-ui` (public, `redirectUris=*.cmtools.example.com/*`, PKCE).

It then prints the `keycloak_client_id` / `keycloak_client_secret`. Put that
secret in `KEYCLOAK_NVCM_CLIENT_SECRET` (SOPS) rather than onto a tenant
Integration by hand — the provision path stamps every tenant from there.
**It does *not* create `daalu-hub-ui` or its
exchange-permission mapper** — that is the SSO Job's job (next).

```bash
KC_URL=https://auth.example.com KC_REALM=daalu \
KC_ADMIN=admin KC_ADMIN_PASSWORD=… \
UI_REDIRECT='https://*.cmtools.example.com/*' \
./deploy/scripts/config-manager/setup-keycloak.sh
```

### 2. `core/keycloak_admin.py:ensure_audience_mapper` — the exchange-permission mapper

The idempotent Admin-REST helper that puts the *exchange-permission* mapper on
`daalu-hub-ui`. It looks the client up by `clientId`, checks for an existing
mapper by name (`nvcm-exchange-audience`), and only POSTs if absent — POSTing
the `oidc-audience-mapper` with `included.client.audience=<audience_client>`,
`access.token.claim=true`, `id.token.claim=false`. Tolerates `409` (race /
already-present). This is the helper that closes the "Keycloak permits the
exchange" half of the chain.

### 3. `cli.py:ensure_keycloak_sso` + the `keycloak-sso-job` — self-healing on every install

`daalu ensure-keycloak-sso` (CLI) calls, against the realm Admin REST:

- `keycloak_admin.ensure_client` for `daalu-hub-ui` (self-heals the browser
  client + its redirect URIs if the realm was rebuilt without it). This is a
  **minimal** self-heal: it sets `clientId`, `enabled`, `protocol=openid-connect`,
  `publicClient` (the CLI passes the default → **confidential**),
  `standardFlowEnabled=true`, `redirectUris=[keycloak_hub_redirect_url]`, and the
  secret. It does **not** set the PKCE attribute or the post-logout allow-list —
  those remain the platform's declarative-realm responsibility (see §70.2);
- `keycloak_admin.ensure_audience_mapper(client_id=keycloak_hub_client_id,
  audience_client="daalu-hub-nvcm")` — the §70.2 exchange-permission mapper;
- **the NV-CM realm roles** (PR #127, `cli.py:566-586`): every role named in
  `keycloak_nvcm_superuser_roles` (default `nvcm-superuser`, `config.py:412`)
  and `keycloak_nvcm_service_roles` (default `nvcm-deploy-execute,nvcm-superuser`,
  `config.py:420`) is created via
  `keycloak_admin.ensure_realm_role` (`core/keycloak_admin.py:323`), and the
  *service* roles are additionally granted to the **`daalu-hub-nvcm` service
  account** via `grant_realm_role_to_service_account`. The grant returns
  `False` (printed as `–`, a skip) while the `daalu-hub-nvcm` client doesn't
  exist yet — the next Job run converges once `setup-keycloak.sh` has created
  it. Rationale: the chart's `nautobot.rbac.superuserGroups`
  (`config_manager_controller/values.py:320-321`) matches JWT `roles` against
  realm role names that previously *nothing* created, so every SSO'd user —
  including the service-account fallback identity — hit Nautobot's Access
  Denied;
- `ensure_identity_provider_google` if Google creds are set (the "Sign in with
  Google" IdP, `trustEmail=true`).

It is wired as a **Helm post-install / post-upgrade hook** Job
(`deploy/helm/daalu-automation/templates/keycloak-sso-job.yaml`, gated on
`config.ensureKeycloakSsoJob`):

```yaml
annotations:
  "helm.sh/hook": post-install,post-upgrade
  "helm.sh/hook-weight": "5"
  "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
spec:
  backoffLimit: 3
  activeDeadlineSeconds: 300   # don't wedge an upgrade if Keycloak is briefly down
```

So **every** `helm upgrade` of daalu re-asserts `daalu-hub-ui` + the
exchange-permission mapper + Google IdP. It is a no-op for objects it already
finds. Note the division of labour: the Job owns `daalu-hub-ui` and the
exchange-permission mapper; `setup-keycloak.sh` owns `daalu-hub-nvcm`,
`nv-config-manager-ui`, the output audience mapper, and the execute role. Both
must run for a fresh realm to be complete.

---

## 70.6 Provisioning responsibility matrix

| Object | `setup-keycloak.sh` | `ensure_keycloak_sso` Job | Notes |
|---|---|---|---|
| `daalu-hub-ui` client | — | ✓ `ensure_client` | confidential; sets `redirectUris` only (not PKCE / post-logout — §70.5) |
| `daalu-hub-ui` audience-mapper (`aud=daalu-hub-nvcm`) | — | ✓ `ensure_audience_mapper` | exchange permission |
| `daalu-hub-nvcm` client (`standard.token.exchange.enabled`) | ✓ | — | confidential service |
| `daalu-hub-nvcm` audience-mapper (`aud=nv-config-manager`) | ✓ | — | exchange output |
| realm role `nvcm-deploy-execute` + grant | ✓ | ✓ (`keycloak_nvcm_service_roles`) | machine/exec identity; both paths idempotent |
| realm role `nvcm-superuser` + service-account grant | — | ✓ (PR #127) | feeds `nautobot.rbac.superuserGroups`; grant skips until `daalu-hub-nvcm` exists |
| `nv-config-manager-ui` client (`*.cmtools.example.com`) | ✓ | — | gateway OIDC |
| Google IdP | — | ✓ (if creds) | optional |

If a tile 401s, walk this table top-to-bottom against the live realm. A missing
**`daalu-hub-ui` audience-mapper** silently demotes every user to the service
token; a missing **`standard.token.exchange.enabled`** 400s the exchange; a
wrong **`nv-config-manager-ui` redirectUri** breaks any direct tenant login.

---

## 70.7 Keycloak version specifics

The version facts are load-bearing and easy to regress on a Keycloak upgrade or
re-deploy:

- **Keycloak 26.2 is the floor** (pin
  `quay.io/keycloak/keycloak:26.2` or later in your Keycloak deployment). Standard Token Exchange (RFC 8693) went **GA in 26.2** — enabled
  by default at the server level, gated per client by
  `standard.token.exchange.enabled`. On **26.1** that per-client switch was
  *inert* and the legacy preview feature was disabled, so the exchange returned
  `400 unsupported_grant_type` and the proxy silently fell back to the
  service-account token — operators dropped to each tool's own login. No
  `KC_FEATURES` flag is needed on 26.2+ (the prod Deployment sets none). The
  bump runs a **one-way DB schema migration** on first start; with the
  `Recreate` strategy that is ~1 min of auth downtime per rollout, and there is
  no schema rollback to 26.1.
- **V1 (legacy / impersonation) token exchange is not used — ever.** On the
  pre-rehome workload-cluster instance (26.5.1, started with
  `KC_FEATURES=token-exchange`) it NPE'd outright. We use **Standard** token
  exchange exclusively: refresh the
  `daalu-hub-ui` token, then exchange it through `daalu-hub-nvcm` with
  `grant_type=urn:ietf:params:oauth:grant-type:token-exchange` and
  `subject_token_type=...:access_token`, relying on the audience-mappers (not
  request params) to set audiences. `core/keycloak.py:get_user_nvcm_token`
  implements exactly this; do not "simplify" it back to an `audience`-param or
  `requested_subject` (impersonation) form.
- **`revokeRefreshToken=false`** (§70.2) — required so the stored `krt` refresh
  token is reusable across tool clicks.
- **`offline_access` as an optional client scope on `daalu-hub-ui`**
  (`consentRequired=false`) — the login requests it (`core/oidc.py:169`, PR
  #119) so `krt` is an OFFLINE refresh token that survives the 30-minute
  SSO-session idle timeout; see
  [69 §2.1](69-per-user-token-exchange.md).

---

## 70.8 What this chapter does *not* cover (cross-references)

This chapter intentionally stops at "what exists in Keycloak". The surrounding
machinery is documented elsewhere — read these rather than re-deriving them:

- The browser login flow, `auth_mode`, identity mirroring, cutover/rollback —
  [59. Keycloak hub SSO](../10-security/59-keycloak-hub-sso.md).
- Local password / PAT / cookie auth and the auth gate —
  [5. Authentication](../02-backend-architecture/05-auth-system.md),
  [56. Auth flows in depth](../10-security/56-auth-flows.md).
- The hub-proxy request path (`CmtoolsHostRouter`, `tool_proxy`, the WG forward) —
  [68. NV-CM tool SSO through the hub](68-nvcm-tool-sso-through-the-hub.md); the
  per-user-vs-service Bearer decision —
  [69. Per-user token-exchange](69-per-user-token-exchange.md).
- Tenant isolation / the `claims.tid == tenant.id` check —
  [57. Tenant isolation](../10-security/57-tenant-isolation.md).
- The machine (`svc-*`) client-credentials path and NV-CM RBAC —
  [21. The NV-CM network plane](../04-source-of-truth/21-nvcm-network-plane.md) §21.2,
  [20. Config-management planes](../04-source-of-truth/20-config-management-planes.md),
  [23. NV-CM internals and operator](../04-source-of-truth/23-nvcm-internals-and-operator.md).
- The tenant gateway `SecurityPolicy`, `oidc.interactiveRedirect=false`, the
  internal-JWKS rewrite — `config_manager_controller/values.py:render_values`,
  `deployer_runner.py:_daalu_values_overlay`, and the vendored chart
  `templates/security-policy.yaml` (also summarised in
  [68. NV-CM tool SSO through the hub](68-nvcm-tool-sso-through-the-hub.md) §68.6–68.7).
- The WireGuard tunnel + edge forward proxy these tokens ride over —
  [24. WireGuard hub](../05-cluster-federation/24-wireguard-hub.md),
  [25. Deploying daalu-edge](../05-cluster-federation/25-daalu-edge.md).
- TLS/DNS for `auth.example.com` and `*.cmtools.example.com` (LE wildcard, DNS-01,
  the Cloudflare-token-in-challenge-namespace gotcha, `auth.example.com` PROXIED
  for NAT-hairpin) — [53. Cert-manager, TLS, and DNS](../09-operations-runbooks/53-tls-and-dns.md).

---

## 70.9 Files

- `deploy/scripts/config-manager/setup-keycloak.sh` — `daalu-hub-nvcm`,
  `nv-config-manager-ui`, `nvcm-audience` mapper, `nvcm-deploy-execute` role.
- `src/daalu_automation/core/keycloak_admin.py` — `ensure_client`,
  `ensure_audience_mapper`, `ensure_identity_provider_google`,
  `ensure_realm_role` (line 323) + `grant_realm_role_to_service_account`
  (PR #127).
- `src/daalu_automation/cli.py` — `daalu ensure-keycloak-sso`.
- `deploy/helm/daalu-automation/templates/keycloak-sso-job.yaml` — the
  post-install/upgrade self-heal Job (`config.ensureKeycloakSsoJob`).
- `src/daalu_automation/core/keycloak.py` — `get_user_nvcm_token` (Standard
  exchange, no `audience` param), `get_machine_token` (client-credentials
  fallback).
- `src/daalu_automation/config.py` — `keycloak_hub_client_id`
  (`daalu-hub-ui`), `keycloak_hub_client_secret`, `keycloak_token_audience`
  (`nv-config-manager`), `keycloak_ui_client_id` (`nv-config-manager-ui`),
  `keycloak_issuer_url` / `keycloak_internal_issuer_url`,
  `keycloak_nvcm_superuser_roles` (line 412) /
  `keycloak_nvcm_service_roles` (line 420).

## What's next

[71. Reachability — DNS, TLS, firewall & the tunnel](71-auth-reachability-dns-tls-tunnel.md) —
how `auth.example.com` and `*.cmtools.example.com` actually get a DNS record, an LE
wildcard cert, and a route through the firewall and WireGuard tunnel so the
clients and tokens this chapter defines are reachable end-to-end.

The per-user token-exchange path in the hub tool proxy (`tool_proxy._resolve`,
the `krt` session claim, and the service-token fallback) is
[69. Per-user token-exchange](69-per-user-token-exchange.md).
