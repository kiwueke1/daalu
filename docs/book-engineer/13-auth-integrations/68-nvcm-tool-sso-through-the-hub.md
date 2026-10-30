# 68. NV-CM tool SSO through the hub (cmtools)

This is the flagship chapter of the auth-integrations section. It documents how
a logged-in `ops.example.com` operator clicks **"Open in Nautobot"** (or Render,
Workflow, Config-store, the config-browser UI) on the Network-&-servers page and
lands *inside that tool, logged in as themselves*, even though the tool runs on
the tenant's **private** cluster behind a `10.x` ClusterIP that no external
browser can reach.

> **Read [59. Keycloak hub SSO](../10-security/59-keycloak-hub-sso.md) first.**
> Chapter 59 establishes the realm (`daalu`), `auth_mode=oidc`, the
> `daalu_session` cookie, and the internal-JWKS / audience-mapper gotchas.
> This chapter is about the *reverse-proxy plane* that carries that identity
> across the WireGuard tunnel into the tenant's tool UIs — the piece chapter 59
> deliberately left as "the tool gateways do OIDC against the same realm."

This chapter consolidates the cmtools design. It does **not** re-explain the JWT
machinery ([5. Auth system](../02-backend-architecture/05-auth-system.md),
[56. Auth flows](../10-security/56-auth-flows.md)), tenant isolation
([57. Tenant isolation](../10-security/57-tenant-isolation.md)), the machine
token path ([21. The NV-CM network plane](../04-source-of-truth/21-nvcm-network-plane.md)),
the NV-CM operator internals ([23. NV-CM internals and operator](../04-source-of-truth/23-nvcm-internals-and-operator.md)),
the tunnel ([24. The WireGuard hub](../05-cluster-federation/24-wireguard-hub.md),
[25. daalu-edge](../05-cluster-federation/25-daalu-edge.md)), or the TLS/DNS
substrate ([53. Cert-manager, TLS, and DNS](../09-operations-runbooks/53-tls-and-dns.md)).
It cross-references them.

---

## 68.1 The problem: private tool UIs

A tenant's NV-CM release deploys a clutch of web tools on the tenant cluster's
Envoy Gateway:

| Tool | What it is |
|---|---|
| Nautobot | the SoT web UI (dcim/ipam) |
| config-browser UI | the NV-CM front-end |
| Render | a config-render backend |
| Workflow | a Temporal-backed workflow engine |
| Config-store | a config storage backend |

All of them sit behind a **private in-cluster gateway** — a `10.x` ClusterIP,
or at best a tenant-internal address that the hub can only reach over the
WireGuard tunnel. From any external browser they are simply unroutable. The hub
already talks to the *machine* faces of these services (`svc-nautobot`, …) over
the tunnel with a service JWT — see
[21. The NV-CM network plane](../04-source-of-truth/21-nvcm-network-plane.md) §21.2
— but the *human* tool UIs had no front door at all.

Two sub-problems, not one:

1. **Reachability.** Get a browser request from the public internet to a
   private in-cluster gateway over the tunnel.
2. **Identity.** Make the operator land in the tool authenticated *as
   themselves*, reusing the hub login — no second password, no second OIDC
   round-trip that the tenant gateway can't complete (it has no public OIDC
   surface, and its own redirect loops; see §68.6).

cmtools solves both.

---

## 68.2 The flat `*.cmtools.example.com` scheme

Every human tool URL is a **flat, single-label host** under one wildcard zone:

```
https://nautobot-<slug>.cmtools.example.com       # Nautobot
https://render-<slug>.cmtools.example.com         # Render
https://workflow-<slug>.cmtools.example.com        # Workflow (API)
https://config-store-<slug>.cmtools.example.com    # Config-store
https://<slug>.cmtools.example.com                 # config-browser UI (bare host)
```

These are produced by `config_manager_controller/values.py:compute_urls`
(the `*_human` keys), where `slug, _, zone = base_hostname.partition(".")` and
the inner `human(tool)` helper emits `https://<tool>-<slug>.<zone>`. The
config-browser UI keeps the bare `<slug>.<zone>` host (the `"ui"` key). The
frontend "Network & servers" page (`frontend/app/network/page.tsx`) links the
tool buttons to these `*_human` URLs.

Why flat and single-label, not nested `nautobot.<slug>.cmtools.example.com`?

- **One DNS record, one cert.** A single Cloudflare wildcard `*.cmtools.example.com
  A → <ingress-ip>` and a single Let's Encrypt **wildcard** cert on the hub's
  Traefik serve every tenant × every tool. A wildcard cert covers exactly *one*
  DNS label of depth, so `nautobot-acme.cmtools.example.com` is covered but
  `nautobot.acme.cmtools.example.com` would need a second-level wildcard (and a
  per-tenant cert). Flat keeps it to one of each, forever.
- **No per-tenant DNS/cert provisioning** in the onboarding path.

Contrast the **machine** URLs from the same `compute_urls`: `http://svc-<tool>.<base_hostname>`
— plain HTTP, JWT-only, dialed by the executor/SoT over the tunnel, never
browser-facing. Keep those stable; they are a different plane
([21](../04-source-of-truth/21-nvcm-network-plane.md) §21.2).

---

## 68.3 The full request path

```
browser
  → Cloudflare (proxied)
  → hub Traefik LB (<ingress-ip>)
  → cmtools Ingress
  → daalu-api :: CmtoolsHostRouter (outer ASGI middleware)
  → api/tool_proxy.py :: tool_proxy_http / tool_proxy_ws
  → core/cluster_proxy :: edge forward proxy URL  (over WG tunnel)
  → daalu-edge forward proxy (deploy/edge/proxy.py)
  → tenant Envoy Gateway (HTTP-only listener)
  → the tool (Nautobot / Render / …)
```

### CmtoolsHostRouter — why it's an outer ASGI middleware, not a route

`api/main.py:CmtoolsHostRouter` is the **outermost** middleware (`create_app`
adds it *last*, after the Prometheus instrumentator, lines 388-396, only in
`mode=="hub"`). On each `http`/`websocket` scope it reads the raw `Host` header;
if `host.endswith("." + zone)` (zone = `settings.cmtools_base_domain.lower()`,
default `cmtools.example.com`) it hands the scope to the wrapped tool router and
returns. Otherwise the FastAPI app runs unchanged.

It is deliberately **not** a Starlette `Host` route in `app.routes`. The
docstring (main.py:171-181) spells out why: `prometheus-fastapi-instrumentator`
iterates `app.routes` and reads `route.path` to name the metric, and a `Host`
route has **no `.path`** → `AttributeError` → 500 on *every* request. So the
host dispatch lives outside the route table entirely, and the wrapped
`_cmtools_tool_router` (main.py:319-338) is a plain Starlette `Router` with a
catch-all `Route("/{path:path}", tool_proxy_http, …)` + `WebSocketRoute(…, tool_proxy_ws)`.

A second, independent skip lives in `api/main.py:AuthGateMiddleware.dispatch`
(lines 116-124): it bypasses the hub cookie gate for `host == cm_zone or
host.endswith("." + cm_zone)`, because cmtools traffic is authenticated by the
tool proxy itself, not the hub gate. (Minor asymmetry: the auth gate also
matches the *bare* zone, where `CmtoolsHostRouter` matches only subdomains —
harmless, the bare zone has no tool route.)

### tool_proxy: parse, authenticate, authorize, resolve

`api/tool_proxy.py:tool_proxy_http` (and `tool_proxy_ws`) does, in order:

1. **`_parse_host`** (tool_proxy.py:139-159) splits `<tool>-<slug>.<base>` →
   `(tool, slug)`, returning `(None, slug)` for the bare UI host and `None` if
   the host isn't under the base or the label contains a `.`. Tool prefixes are
   `_TOOL_PREFIXES = ("config-store", "nautobot", "render", "workflow")`,
   matched by `label.startswith(tool + "-")`.

   > Implementation note: the code comment claims the tuple is "longest-first so
   > `config-store` is matched before any shorter prefix." The tuple as written
   > is **not** length-ordered, and ordering is in fact immaterial — every
   > prefix requires a literal `-` separator and no prefix is a `<other-prefix>-…`
   > string, so the split is unambiguous regardless of order. The outcome is
   > correct; the stated rationale is not.

2. **`_claims_from_cookie`** (tool_proxy.py:162-177) pulls `daalu_session`
   (`settings.auth_cookie_name`) out of the raw `Cookie` header and
   `decode_token`s it (HS256, `core/auth.py:decode_token`).

3. **`_resolve`** (tool_proxy.py:180-259) is the authz + target resolution core
   — covered in §68.4 and §68.5.

---

## 68.4 Hub-session auth + the tenant-ownership check

`_resolve` (tool_proxy.py:180-259):

- `claims is None` → `_Unauthorized`. For an HTML `GET`/`HEAD`, `tool_proxy_http`
  turns this into a **302 to the hub login** with `?next=` back to the tool URL
  (`_login_redirect`, lines 262-270, forcing `https` so the post-login round-trip
  doesn't bounce to `http://`); otherwise a bare `401`.
- Load `Tenant` by slug; absent → `_ProxyUnavailable` ("no such tenant", → 502).
- **Tenant ownership:** `if str(claims.get("tid")) != str(tenant.id): raise
  _Forbidden()` (lines 196-198). The session's tenant id — baked into the JWT by
  `core/auth.py:issue_token` as `tid`, derived from `user.tenant_id` at OIDC
  login (`api/routers/auth.py:oidc_callback`) — **must** match the tenant that
  owns the requested slug. This is the cross-tenant guard for the whole plane;
  it complements [57. Tenant isolation](../10-security/57-tenant-isolation.md).
- Load `Integration(provider="config_manager", tenant_id=tenant.id)`; absent →
  `_ProxyUnavailable`. From it: `gateway_internal_url` (the in-cluster gateway
  base) out of `integ.config`, `cluster_tunnel_id` off the column, and
  `proxy_url = await get_proxy_url(db, cluster_tunnel_id)` (`core/cluster_proxy`)
  — the edge forward-proxy address for this tenant's tunnel.
- Build `conn = conn_from_integration_config(cfg)`
  (`core/configmgr/client.py:conn_from_integration_config`) — Keycloak client id,
  decrypted client secret, optional audience.

For the cookie to even reach the cmtools zone, it must be set with
`domain=.daalu.io`. The code default is host-only (`config.py:auth_cookie_domain`
defaults to `""`); the **deployment** sets `AUTH_COOKIE_DOMAIN=.daalu.io`
(`core/auth.py:_set_session_cookie`). Without that env override the cmtools
hosts never see `daalu_session` and the proxy 401/302-loops — this was the root
cause of the "buffers forever" stale-cookie incident during bring-up. Cookie
mechanics live in [5](../02-backend-architecture/05-auth-system.md) and
[56](../10-security/56-auth-flows.md).

---

## 68.5 Downstream identity: per-user token-exchange, service fallback

The Bearer the proxy injects downstream is, **preferentially, the user's own
token** — and only falls back to a shared service identity when it can't mint
one. `_resolve` (tool_proxy.py:229-258):

1. **Per-user (preferred).** If the session has `krt` (the user's Keycloak
   refresh token, encrypted), decrypt it (`core/crypto.decrypt_secret`) and call
   `core/keycloak.get_user_nvcm_token(refresh_token=…, nvcm_client_id=conn.keycloak_client_id,
   nvcm_client_secret=conn.keycloak_client_secret)`. On `KeycloakAuthError`/`ValueError`
   it logs `tool_proxy.user_exchange_failed` and falls through.

2. **Service (fallback).** `core/keycloak.get_machine_token(client_id, client_secret,
   audience=conn.audience)` — the `daalu-hub-nvcm` client-credentials token, the
   *same* identity the executor/SoT machine path uses
   ([21](../04-source-of-truth/21-nvcm-network-plane.md) §21.2).

`get_user_nvcm_token` (`core/keycloak.py:137-206`) is two Keycloak calls:

1. `grant_type=refresh_token`, `client_id=settings.keycloak_hub_client_id`
   (`daalu-hub-ui`), optional `client_secret` → a fresh **user** access token.
   Refresh-token rotation is off in the realm (`revokeRefreshToken=false`), so
   the stored `krt` is reusable.
2. `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`,
   `client_id=nvcm_client_id` (`daalu-hub-nvcm`), `client_secret`,
   `subject_token=<user access token>`,
   `subject_token_type=…:access_token`. **No `audience` param is sent** — the
   `daalu-hub-nvcm` client's audience-mapper stamps `aud=nv-config-manager`. The
   result carries the *user's* `preferred_username`/`email` with the audience
   the tenant gateway + NV-CM auth plugin accept.

Cached per refresh token (`_USER_CACHE`, keyed on `sha256(refresh_token)`).

### Where `krt` comes from (and where it doesn't)

`api/routers/auth.py:oidc_callback` stores, on the session JWT's `extra`:
`extra["krt"] = encrypt_secret(refresh_token)` and `extra["kit"] = id_token`
(the id-token hint, replayed at logout by `oidc_logout` as `id_token_hint`).
`core/auth.py:issue_token` merges `extra` into the signed cookie.

**`krt` is OIDC-login-only.** Password login (`auth.py:login`) and invite-redeem
(`redeem_invite`) call `issue_token` with no `extra` — no `krt` — so for those
sessions the per-user branch is skipped and the proxy injects the shared service
token. That is by design and matches the fallback comment in `_resolve`.

**`krt` is an OFFLINE refresh token** (PR #119). The login authorize URL requests
`scope=openid email profile offline_access`
(`core/oidc.py:build_authorize_url`, `core/oidc.py:169`). Before that, `krt`
was an *online* refresh token bound to the realm's 30-minute SSO-session idle
timeout: a user who browsed Daalu for a while, then clicked a tool tile, hit
`invalid_grant: Token is not active` on the refresh — and the proxy silently
fell back to the service token, which is not a user, so the tool showed its own
login page. Offline tokens are governed by `offlineSessionIdleTimeout`
(~30 days, no max lifespan on the realm) instead, so the per-user path survives
ordinary idle. `offline_access` is an optional client scope with
`consentRequired=false` on `daalu-hub-ui`, so no consent prompt appears.
Sessions issued *before* the change carry online `krt`s and still degrade after
30 idle minutes until the user re-logs-in.

### Why two audience mappers (the exchange-permission vs output-audience split)

This trips people up, so be explicit. There are **two** distinct
`oidc-audience-mapper`s, on two different clients, doing two different jobs:

| Mapper on client | Stamps | Purpose |
|---|---|---|
| `daalu-hub-ui` (via `core/keycloak_admin.py:ensure_audience_mapper`, called from `cli.py:_ensure_keycloak_sso` with `audience_client="daalu-hub-nvcm"`) | `aud=daalu-hub-nvcm` onto the **user's** token | *Permits* `daalu-hub-nvcm` to run the Standard Exchange on the user's token |
| `daalu-hub-nvcm` (via `deploy/scripts/config-manager/setup-keycloak.sh`) | `aud=nv-config-manager` onto the **exchanged** token | The audience the tenant gateway + NV-CM plugin validate |

The first is the *input* side (exchange permission); the second is the *output*
side (downstream audience). The realm must run **Keycloak 26.2+**, where Standard Token
Exchange (RFC 8693) is **GA** — enabled server-wide, gated per client by
`standard.token.exchange.enabled=true`, which `daalu-hub-nvcm` has on. On 26.1
that per-client switch was *inert* and the legacy preview feature was off, so
the exchange 400'd with `unsupported_grant_type` and every operator dropped to
the tools' own login — the reason for the 26.1→26.2 bump (PR #120; one-way DB
schema migration on first start, `Recreate` strategy → ~1 min auth downtime).
(On the earlier workload-cluster instance — 26.5.1, pre-rehome — the V1
*impersonation* exchange NPE'd, another reason we use only the **Standard**
exchange shape.) Keycloak realm +
client provisioning details are in
[59](../10-security/59-keycloak-hub-sso.md) §8; here we only care that the two
mappers exist and which is which.

> **Stale-docstring warning.** The `api/tool_proxy.py` module docstring
> (lines 19-39) and the comment at lines 94-98 still describe a
> *service-token-only* design with per-user exchange as a "future upgrade /
> known tradeoff." That is **out of date** — the implemented `_resolve` does
> per-user *first*. Trust the code (lines 229-258), not the docstring.

---

## 68.6 Forwarding over the tunnel — the host bridge and the cookie

`tool_proxy_http` (tool_proxy.py:301-340) dials
`httpx.AsyncClient(proxy=target.proxy_url, …)` at the gateway base + the original
path/query, and injects:

- `Authorization: Bearer <target.bearer>` (line 309).
- `X-Forwarded-Host: <public_host>`, `X-Forwarded-Proto: https`.
- the **`NVConfigManagerAccessToken` cookie** via `_forward_cookie`
  (tool_proxy.py:102-117): it keeps the browser's *tool* cookies (Nautobot's
  `sessionid`/`csrftoken` etc.), **drops** the hub session cookie and any prior
  `NVConfigManagerAccessToken`, and appends `NVConfigManagerAccessToken=<bearer>`.
  NV-CM's tool auth plugins read the gateway-validated JWT from this cookie.

### The host bridge: `X-Daalu-Forward-Host`

Two host representations must be reconciled:

- **Flat (browser/cert side):** `nautobot-<slug>.cmtools.example.com` — what the
  wildcard cert covers and the browser sent.
- The **edge forward proxy** (`deploy/edge/proxy.py`) resets `Host` to the
  dialed URL's host by default.

So when `target.proxy_url` is set, the proxy sets `X-Daalu-Forward-Host =
public_host` (tool_proxy.py:315-317) and the edge restores `Host` to that value
on the upstream request. When there's no tunnel (direct dial), the proxy sets
`Host: public_host` itself (lines 318-320).

> **A note on "flat vs nested."** Earlier design notes describe
> `X-Daalu-Forward-Host` as carrying a *nested* gateway host
> (`<tool>.<slug>.cmtools.example.com`) while the browser/cert side stayed flat —
> i.e. a flat→nested rewrite at the proxy. **That is not what the current code
> does.** `tool_proxy.py` forwards `public_host` (the **flat** host)
> *unchanged*; the tenant gateway was re-provisioned to route the flat hosts
> (`render_values` emits flat component hostnames — see §68.7). The plane is
> **flat end-to-end**; there is no host rewrite in `tool_proxy.py`.

WebSockets (`tool_proxy_ws`, tool_proxy.py:346-487) ride a raw `CONNECT` through
the edge proxy when on a tunnel (plain-HTTP forward can't carry an `Upgrade`),
else dial directly; the same Bearer is injected in the upstream handshake
headers.

### Why the gateway's interactive OIDC redirect is DISABLED

The tenant Envoy Gateway runs `oidc.interactiveRedirect=false`. The reason is a
**cookie loop on flat sibling hosts**: Envoy's oauth2 filter scopes its session
cookie to the gateway's `baseHostname`, but the flat tool hosts
(`<tool>-<slug>.<zone>`) are *siblings*, not children, of that hostname — so the
oauth2 cookie is never sent back on a tool host, the filter sees "no session,"
redirects again, and loops forever. The chart comment
(`templates/security-policy.yaml:351-358`) states exactly this.

So instead of an interactive redirect, the gateway runs **JWT-only**:

- `templates/security-policy.yaml` SecurityPolicy: `jwt.optional: true`,
  provider `oidc-provider`, `extractFrom` both `Authorization: Bearer ` **and**
  the `NVConfigManagerAccessToken` cookie (lines 337-342), plus `claimToHeaders`.
- The interactive `oidc:` block is gated on
  `{{- if .Values.oidc.interactiveRedirect }}` (line 351) — false for
  hub-proxied tenants, so the whole oauth2 redirect filter is **omitted**.

And the **JWKS source follows the target cluster**: `remoteJWKS.uri` resolves
`oidc.jwksUri` → `internalIssuerUrl/.../certs` → `issuerUrl/.../certs`
(security-policy.yaml:328-335). `render_values` always sets `jwksUri` explicitly
— it is keyed on `target_cluster_tunnel_id`:

| `target_cluster_tunnel_id` | Installs into | `jwksUri` |
| --- | --- | --- |
| `NULL` | the operator's own cluster | `keycloak.daalu-automation.svc.cluster.local:8080/.../certs` |
| set | a customer cluster over WireGuard | `https://auth.example.com/.../certs` |

In the operator's cluster the external `auth.example.com` 404s from inside, so the
keys must come over the in-cluster Service. A customer cluster runs no Keycloak
of its own, so that same name is unresolvable there and the public issuer is the
only working source. Getting this backwards fails *silently* — the Nautobot auth
plugin swallows the JWKS error at `log.debug` and drops the browser to Nautobot's
own login page, while machine `svc-*` calls keep working. Note
the token **`iss` stays external** (`auth.example.com`) — the provider `issuer:` is
`oidc.issuerUrl`; only the *JWKS fetch URL* is internal, so audience/issuer
matching against the external realm still holds. This internal-JWKS gotcha is
also called out in [59](../10-security/59-keycloak-hub-sso.md) §8a.

### The NV-CM Nautobot `user_provider` login

Once the JWT cookie is validated by the gateway, NV-CM's Nautobot plugin
(`nv_config_manager_auth`, the `user_provider` path) reads the JWT and logs in
the **real user** — `preferred_username`/`email` from the per-user exchanged
token — so the operator is themselves inside Nautobot, no second password.

### The break-glass local admin (the one exception)

Everything above depends on Keycloak being up. When it isn't, there is no way
in — the tools have no other identity source, and you cannot mint an account in
an IdP that is down. Nautobot is the only component in the stack that *also*
ships its own Django login (`/login/`) and bootstraps a superuser for it; an
audit of the 1.3.0 chart found nothing comparable elsewhere (Temporal Web,
config-store, render, ZTP, DHCP, MCP and the config-browser UI are
gateway-JWT-only, and the NATS/Redis passwords have no login surface).

Upstream generates that superuser password per-install and never surfaces it,
so it was effectively unusable. `config_manager_controller/local_admin.py` lets
the person creating the environment choose it instead:

| Layer | What it carries |
| --- | --- |
| `POST …/environments` | `admin_username`, `admin_password` — both optional; `422` on a value Nautobot would reject |
| Row storage | `secrets_ciphertext`, a JSON bag (`nautobot_admin_username`/`_password`) |
| Helm values | `nautobot.admin.username` → `NAUTOBOT_SUPERUSER_NAME` (`_nautobot-configmap-data.tpl`) |
| Installer | `secrets.k8s.nautobot_app.values.adminPassword` → `NAUTOBOT_SUPERUSER_PASSWORD` |

Defaults: username `admin`, password generated (28 chars). The password is
returned **once**, on the create response only — `list`/`get` never carry it,
and the UI shows it in a dismissable banner with a copy button. It stays
recoverable by an operator from the encrypted row, but nothing re-serves it.

Rows written before this bag existed hold a bare token rather than JSON;
`local_admin.from_row` reports an empty password for those, which every caller
reads as "leave the installer's generated value alone" — so old environments
are untouched.

> This is a break-glass path, not the normal one. Ordinary users never see it:
> they are already signed in to Daalu and are SSO'd into the tools by the proxy.

---

## 68.7 Per-tenant gateway values: where flat/HTTP-only/JWKS get set

The gateway is shaped by `config_manager_controller/values.py:render_values`:

- `gateway`: an **HTTP-only** listener (`{name:http, protocol:HTTP, port:80}`),
  `certificates.enabled=false`, `createGatewayClass=false`,
  `className=envoy-gateway` (`SHARED_GATEWAY_CLASS`). The gateway has no HTTPS
  listener and no cert — the **hub** terminates TLS at its wildcard and reverse-
  proxies plain HTTP over the tunnel.
- `gateway.cors.allowOrigins`: an explicit `https://<flat-host>` list (the
  chart's default `https://*.<base>` wouldn't match the flat siblings).
- `oidc`: `issuerUrl` / `internalIssuerUrl`, **`jwksUri`** = the certs URL for
  whichever cluster this environment installs into (internal Service here,
  external `auth.example.com` for a customer cluster — see the table above),
  `audiences=[keycloak_token_audience]`, `clientId=nv-config-manager-ui`, and
  **`interactiveRedirect=False`**.
- `nautobot.admin.username`: the break-glass superuser name, emitted only when
  the creator chose something other than the chart default `admin`.
- Flat per-component hostname overrides for render/nautobot/configStore;
  Temporal sets `gateway.api.hostname=workflow-<slug>` **and**
  `gateway.devUi.hostname=temporal-<slug>` — two distinct hosts (see §68.8).

In production the **Deployer** path applies all of this as an overlay:
`config_manager_controller/deployer_runner.py:_daalu_values_overlay` re-derives
from `render_values(row)`, forces `createGatewayClass=false` /
`className=GATEWAY_CLASS` / `nodePort.enabled=false` /
`oidc.interactiveRedirect=false`, copies `internalIssuerUrl`/`jwksUri`, and sets
the flat component hostnames; it's `_deep_merge`d in
`_build_daalu_deployer._generate_values`. `_DaaluDeployer._create_secrets`
pre-creates the `oidc-client-secret`. NV-CM operator/Deployer internals are in
[23. NV-CM internals and operator](../04-source-of-truth/23-nvcm-internals-and-operator.md).

---

## 68.8 Sequence diagram

```
 ┌─────────┐   ┌──────────┐   ┌───────┐   ┌──────────────┐   ┌──────────┐   ┌──────────┐   ┌───────────────┐   ┌──────┐
 │ Browser │   │Cloudflare│   │Traefik│   │ daalu-api    │   │ Keycloak │   │daalu-edge│   │ tenant Envoy  │   │ Tool │
 └────┬────┘   └────┬─────┘   └───┬───┘   │ CmtoolsRouter│   │ (realm   │   │ fwd proxy│   │ Gateway       │   │(Naut-│
      │             │             │       │ + tool_proxy │   │  daalu)  │   │ (tunnel) │   │ JWT-only      │   │ obot)│
      │             │             │       └──────┬───────┘   └────┬─────┘   └────┬─────┘   └──────┬────────┘   └──┬───┘
      │  1. GET https://nautobot-<slug>.cmtools.example.com  (Cookie: daalu_session=.daalu.io)       │              │
      │────────────>│────────────>│──────────────>│              │              │                 │              │
      │             │             │   2. CmtoolsHostRouter: host endswith ".cmtools.example.com" → tool_proxy        │
      │             │             │       3. _parse_host → (nautobot, <slug>)                      │              │
      │             │             │       4. _claims_from_cookie(daalu_session) → claims           │              │
      │             │             │       5. _resolve: claims.tid == tenant.id ?  ── no ──> 403     │              │
      │             │             │          load Integration(config_manager): gateway_url, tunnel │              │
      │             │             │       6. krt present?                                          │              │
      │             │             │             │  6a. refresh_token grant (daalu-hub-ui)          │              │
      │             │             │             │─────────────>│  → user access token             │              │
      │             │             │             │  6b. token-exchange (daalu-hub-nvcm, no aud)     │              │
      │             │             │             │─────────────>│  → USER token, aud=nv-config-mgr  │              │
      │             │             │             │  (fallback: get_machine_token = service token)   │              │
      │             │             │       7. forward over tunnel: Authorization: Bearer <user tok> │              │
      │             │             │          Cookie: NVConfigManagerAccessToken=<user tok>         │              │
      │             │             │          X-Daalu-Forward-Host: nautobot-<slug>.cmtools.example.com (FLAT)        │
      │             │             │             │              │   8.──────────>│                  │              │
      │             │             │             │              │       9. edge restores Host = flat host          │
      │             │             │             │              │                │──────10.────────>│              │
      │             │             │             │              │   11. SecurityPolicy: jwt(optional) validates    │
      │             │             │             │              │       cookie/Bearer vs INTERNAL JWKS             │
      │             │             │             │              │       (NO interactive OIDC redirect)             │
      │             │             │             │              │                │      claimToHeaders             │
      │             │             │             │              │                │                 │──── 12. ────>│
      │             │             │             │              │                │   13. nv_config_manager_auth    │
      │             │             │             │              │                │       user_provider → logs in   │
      │             │             │             │              │                │       the REAL user             │
      │<───────────────────────── 14. tool UI, authenticated as the operator ──────────────────────────────────│
```

---

## 68.9 Known-open items

These are documented **design** decisions / current limitations, not bugs:

- **Render and Config-store are backend APIs**, not web apps — their `/` is
  `{"detail":"Not Found"}`. Since PR #127, `compute_urls` points their console
  links at **`/docs`** (the FastAPI Swagger surface) instead of the bare root
  (`config_manager_controller/values.py:155-156`), so the tiles land somewhere
  useful. Related: the env detail card resolves console URLs from the
  environment row / progress payload first, and `compute_urls()` stores the
  config-browser UI under the bare `ui` key there — the frontend's tile
  resolver originally accepted only the integration-config spellings
  (`ui_human`/`ui_url`), which made the Config-browser tile silently vanish;
  fixed by PR #135 (accept the bare `ui` key too).
- **Workflow should point at the Temporal *web* UI**, not the workflow API. The
  values emit *both* `workflow-<slug>` (the API host,
  `gateway.api.hostname`) and `temporal-<slug>` (the Temporal web UI,
  `gateway.devUi.hostname`). The Network-page button currently targets the API
  host; the operator-facing link should be `temporal-<slug>.cmtools.example.com`
  (the `temporal-web` route).
- **Nautobot sidebar is RBAC-gated — now bootstrapped automatically.** Nautobot
  maps the JWT `roles` claim → `is_superuser` via `nautobot.rbac.superuserGroups`.
  Historically the chart matched against realm role names that *nothing ever
  created*, so every SSO'd user — including the `daalu-hub-nvcm`
  service-account fallback identity — landed on Nautobot's **Access Denied**.
  Since PR #127 `ensure-keycloak-sso` creates the roles itself:
  `keycloak_nvcm_superuser_roles` (default `nvcm-superuser`, `config.py:412`)
  feeds `render_values`' `superuserGroups`
  (`config_manager_controller/values.py:320-321`), and
  `keycloak_nvcm_service_roles` (default `nvcm-deploy-execute,nvcm-superuser`,
  `config.py:420`) are additionally **granted to the `daalu-hub-nvcm` service
  account** (`cli.py:574-586` via
  `core/keycloak_admin.py:ensure_realm_role` /
  `grant_realm_role_to_service_account` — tolerant when the NV-CM client
  doesn't exist yet; the next Job run converges). A fresh install converges
  on its own.

---

## 68.10 Durability checklist (must be automated on a fresh deploy)

Several pieces were applied live during bring-up and **must** be re-applied (or
automated) for a fresh cluster, or the plane silently fails:

- **Cloudflare DNS.** `*.cmtools.example.com A → <ingress-ip>` (**DNS-only** so
  Traefik terminates the LE wildcard via cert-manager DNS-01). Separately,
  `auth.example.com` was flipped to **proxied** (orange cloud) — it was DNS-only to
  the operator's own public IP, unreachable behind NAT (a NAT-hairpin). Keycloak
  is proxy-ready (`KC_PROXY=edge`, `KC_HOSTNAME=auth.example.com`). See
  [53. Cert-manager, TLS, and DNS](../09-operations-runbooks/53-tls-and-dns.md).
- **Cloud firewall.** Open `80/443` to `0.0.0.0/0` on the hub node (was
  Cloudflare-IPs-only).
- **cert-manager.** The `cloudflare-api-token` secret must live in the
  **challenge namespace** (`daalu-automation`), not just `cert-manager`, for the
  `*.cmtools.example.com` DNS-01 to issue.
- **daalu-edge state on a PVC.** `persistence.enabled=true` — an `emptyDir`
  restart wiped the WG key and crash-looped the bootstrap on a 409; the
  entrypoint now tolerates the 409 (restart-safe). The edge image must carry
  `deploy/edge/proxy.py` (the `X-Daalu-Forward-Host` handler). A hub re-peer
  after a key change = edit the `daalu-wireguard-peers` secret + roll
  `daalu-wireguard-hub`. See [25. daalu-edge](../05-cluster-federation/25-daalu-edge.md)
  and [24. The WireGuard hub](../05-cluster-federation/24-wireguard-hub.md).
- **`AUTH_COOKIE_DOMAIN=.daalu.io`** on daalu-api (§68.4) — without it the cookie
  never reaches the cmtools zone.
- **Keycloak provisioning** (`deploy/scripts/config-manager/setup-keycloak.sh` +
  `cli.py:ensure_keycloak_sso`): the two clients (`daalu-hub-nvcm`,
  `nv-config-manager-ui`), both audience mappers (§68.5), the NV-CM realm
  roles + service-account grants (PR #127, §68.9), and the
  `redirectUris` including `*.cmtools.example.com`. Note the `-s "redirectUris=[…]"`
  CLI form **replaces** existing URIs — easy to clobber.
- **Cloudflare Browser Integrity Check must be off for the OIDC endpoints.**
  `auth.example.com` is proxied (orange cloud), and Cloudflare's BIC 403-blocks
  requests with non-browser `User-Agent`s — **error 1010** — which is exactly
  what a customer gateway's JWKS fetch and the hub's `httpx` token calls look
  like. The symptom stacks with the offline-token issue above (two independent
  causes, same "tool shows its own login" symptom). Fixed out-of-band with a
  **zone Configuration Rule** disabling BIC for
  `auth.example.com/protocol/openid-connect/*`. A fresh Cloudflare zone must
  recreate that rule; nothing in this repo provisions it. See
  [71 §71.2](71-auth-reachability-dns-tls-tunnel.md).

---

## What's next

[69. Per-user token-exchange](69-per-user-token-exchange.md) is the deep dive on
the downstream Bearer summarized in §68.5;
[70. Keycloak realm, clients & provisioning](70-keycloak-realm-clients-and-provisioning.md)
covers the realm/client/mapper setup behind §68.5;
[71. Reachability — DNS, TLS, firewall & the tunnel](71-auth-reachability-dns-tls-tunnel.md)
expands the §68.10 substrate;
[67. Auth integrations — overview & map](67-auth-integrations-overview.md) opens the section.
If you're debugging a cmtools 401/403/502 in
production, walk §68.3 top-to-bottom: 401/302 = no/invalid `daalu_session`
(check `AUTH_COOKIE_DOMAIN`); 403 = `claims.tid != tenant.id`; 502 = no tenant /
no `config_manager` integration / no `gateway_internal_url` / tunnel down / token
mint failed. A blank page that *loads* but shows a scanty Nautobot sidebar is
§68.9 RBAC, not auth.
