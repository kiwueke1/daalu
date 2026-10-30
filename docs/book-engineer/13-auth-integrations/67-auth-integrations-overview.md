# 67. Auth integrations — overview & map

This is the navigational opener for Part XIII. If you're trying to answer
"*which* identity authenticates *which* surface, and where does it come from?",
start here — then jump to the chapter that owns the detail. This chapter
**maps**; it deliberately does not re-document any single flow. Every box below
already has a home chapter, and the rest of Part XIII (68–71) covers the one
piece that didn't have a home: the **cmtools** path that lets a logged-in
`ops.example.com` user open a tenant's private NV-CM tool UIs *as themselves*.

> If you're investigating a 401/403 in production: identify the surface in the
> table below, then open its owning chapter. The cross-references are
> load-bearing — most "auth bugs" are actually a mismatch between two of these
> integrations (cookie scope vs. zone, JWKS reachability vs. issuer, tenant
> `tid` vs. slug).

---

## 67.1 The integrations, and where each lives

The platform has **seven** distinct authentication/identity integrations. Five
predate this section and have their own chapters; the sixth and seventh (the
cmtools tool-proxy and its per-user token-exchange) are new and live entirely in
Part XIII.

| # | Integration | What it authenticates | Credential | Owning chapter |
|---|---|---|---|---|
| 1 | **Hub session + PATs** | A human or script against the daalu API (`ops.example.com`) | HS256 `daalu_session` cookie / bearer; `dpat_` PATs; `X-Daalu-Key` ingest keys | [5. Auth system](../02-backend-architecture/05-auth-system.md), [56. Auth flows](../10-security/56-auth-flows.md) |
| 2 | **Hub OIDC SSO** | The *login itself* — password check delegated to Keycloak (`auth_mode=oidc`) | Authorization-code+PKCE against realm `daalu`, client `daalu-hub-ui` | [59. Keycloak hub SSO](../10-security/59-keycloak-hub-sso.md) |
| 3 | **Executor identity** | Background jobs acting on the platform's behalf | Platform JWT `sub:"executor"`, `scope:"executor"`, `tenant_id:null` | [17. Executor identity](../04-source-of-truth/17-executor-identity.md) |
| 4 | **Tenant isolation** | That a request's claimed tenant matches the resource | `tid` claim in the session JWT vs. resource `tenant_id` | [57. Tenant isolation](../10-security/57-tenant-isolation.md) |
| 5 | **Edge / WG tunnel trust** | A workload cluster to the hub (and the hub's traffic into it) | One-shot bootstrap token → durable WireGuard pubkey identity | [24. WireGuard hub](../05-cluster-federation/24-wireguard-hub.md), [25. daalu-edge](../05-cluster-federation/25-daalu-edge.md) |
| 6 | **cmtools tool SSO** *(NEW)* | A browser reaching a tenant's private NV-CM tool UI through the hub | Hub session cookie at the cmtools zone → injected downstream Bearer | **[68](68-nvcm-tool-sso-through-the-hub.md)** (flagship), **[70](70-keycloak-realm-clients-and-provisioning.md)** (realm wiring) |
| 7 | **Per-user token-exchange** *(NEW)* | The tool seeing the *real user* (not a shared service account) | Keycloak Standard token-exchange minting a user-scoped downstream JWT | **[69](69-per-user-token-exchange.md)** |

Two machine-identity paths from elsewhere also feed into this picture and are
**not** re-documented here:

- The **hub→NV-CM machine token** (`svc-*` hosts, client-credentials JWT via
  `core/keycloak.py:get_machine_token`) — see
  [21. NV-CM network plane §21.2](../04-source-of-truth/21-nvcm-network-plane.md).
  This same `daalu-hub-nvcm` service identity is the cmtools proxy's **fallback**
  when a session has no user refresh token (§67.4).
- The **NV-CM gateway OIDC/JWT install internals** —
  [23. NV-CM internals & operator](../04-source-of-truth/23-nvcm-internals-and-operator.md)
  and [20. Config-management planes](../04-source-of-truth/20-config-management-planes.md).

The TLS/DNS substrate every externally-reachable surface above depends on
(`auth.example.com`, `*.cmtools.example.com`, the LE wildcard, the
Cloudflare-token-in-challenge-namespace gotcha) lives in
[53. Cert-manager, TLS, and DNS](../09-operations-runbooks/53-tls-and-dns.md).

---

## 67.2 Who authenticates what (one-paragraph table)

Read this as "principal → surface → with what → verified where":

| Principal | Reaches | Presenting | Verified by |
|---|---|---|---|
| Human (browser) | `ops.example.com` (hub API/UI) | `daalu_session` cookie | `core/auth.py:decode_token`; OIDC login mints it (`api/routers/auth.py:oidc_callback`) |
| Script / CI | hub API | `dpat_…` PAT (`Authorization`) | `api/deps.py:_resolve_pat` (sha256 lookup) |
| Webhook (SoT) | `/api/v1/sot/webhooks/<slug>` | HMAC over body | per-tenant secret (slug → tenant) |
| Executor (Celery) | hub API (internal) | platform JWT `scope:"executor"` | `core/auth.py:verify_executor_token` (`scp == executor_jwt_scope`) → `core/change_proposals.py` `actor.scope` gate |
| Hub (machine) | NV-CM `svc-*` over tunnel | client-credentials JWT (`daalu-hub-nvcm`) | NV-CM gateway JWT filter, internal JWKS |
| **Human (browser)** | **tenant tool UI at `*.cmtools.example.com`** | **hub `daalu_session` cookie (zone-scoped)** | **`api/tool_proxy.py:_resolve` → tenant Envoy `SecurityPolicy` JWT filter** |
| Workload cluster | hub (federation) | WG pubkey (bootstrapped) | `24/25` peer config |

The cmtools row is the new one: the **same** hub session cookie that
authenticates `ops.example.com` also authenticates the tool zone — because
`auth_cookie_domain` is set to the parent domain (e.g. `.example.com`) in a
production deployment (a deployment value; the
`config.py:auth_cookie_domain` default is `""`/host-only), the cookie is sent to
`*.cmtools.example.com`. The proxy decodes it, enforces tenant isolation
(`str(claims["tid"]) == str(tenant.id)`), and then mints/forwards a **downstream**
Keycloak token so the tool itself sees an authenticated user.

---

## 67.3 The cmtools request flow at a glance

This is the spine that 68–71 unpack. The browser side stays **flat**
(`<tool>-<slug>.cmtools.example.com`, with the config-browser UI at the bare
`<slug>.cmtools.example.com`); one wildcard DNS record and one LE wildcard cert
serve the whole zone.

```
                     ops.example.com login (Keycloak OIDC, auth_mode=oidc)   → ch 59
                                      │  mints daalu_session (HS256), extra: krt = enc(KC refresh token)
                                      ▼
 browser ──https──▶ Cloudflare ──▶ hub Traefik (<ingress-ip>) ──▶ cmtools Ingress ──▶ daalu-api
   GET https://nautobot-<slug>.cmtools.example.com/…                                            │
   (cookie daalu_session, scoped .daalu.io)                                                  │
                                                                                             ▼
                                              api/main.py:CmtoolsHostRouter  (OUTER ASGI mw, ch 68)
                                              • Host endswith ".cmtools.example.com" → tool router
                                              • NOT a Starlette Host route (prometheus reads route.path → 500)
                                                                                             │
                                                                                             ▼
                                              api/tool_proxy.py:_resolve  (ch 68)
                                              • _claims_from_cookie(daalu_session) → decode_token
                                              • tenant isolation: str(claims.tid) == str(tenant.id)  → ch 57
                                              • load Integration(provider="config_manager"):
                                                    cluster_tunnel_id, gateway_internal_url, KC client/secret
                                                                                             │
                                 ┌───────────────────────────────────────────────────────────┘
                                 ▼  downstream Bearer (ch 69)
                  PREFER per-user:  decrypt claims.krt → core/keycloak.get_user_nvcm_token
                     (refresh daalu-hub-ui token → Keycloak STANDARD token-exchange via
                      daalu-hub-nvcm, NO audience param; client audience-mapper stamps
                      aud=nv-config-manager) → token carries the USER's email/username
                  FALL BACK (no krt: password/invite sessions): get_machine_token
                     (daalu-hub-nvcm client-credentials = shared service identity)  → ch 21 §21.2
                                 │
                                 ▼  forward over WireGuard tunnel (ch 24/25)
                  httpx via edge forward proxy (deploy/edge/proxy.py)
                  • Authorization: Bearer <token>
                  • Cookie NVConfigManagerAccessToken=<token>   (_forward_cookie)
                  • X-Daalu-Forward-Host = <flat public host>   (host stays FLAT end-to-end)
                                 │
                                 ▼
                  tenant Envoy Gateway  SecurityPolicy  (ch 68 §68.6–68.7)
                  • jwt.optional + claimToHeaders; oidc.interactiveRedirect = FALSE
                    (no oauth2 redirect — the filter loops on flat sibling hosts)
                  • remoteJWKS follows the TARGET CLUSTER (values.py::render_values):
                      operator cluster  → keycloak.daalu-automation.svc:8080 (external 404s in-cluster)
                      customer cluster  → external auth.example.com (no Keycloak of its own)
                  • token iss stays external (auth.example.com); only the JWKS *fetch* varies
                                 │
                                 ▼
                  NV-CM tool (Nautobot nv_config_manager_auth plugin, …)  → logs in the REAL user
```

The key correctness facts encoded above — each owned by a later chapter:

- **The router is outer ASGI, not a Host route** — a Starlette `Host` route has
  no `route.path`, which `prometheus-fastapi-instrumentator` reads → `AttributeError`
  → 500. (`api/main.py:CmtoolsHostRouter`, ch 68 §68.3.)
- **Host is flat end-to-end.** `tool_proxy` forwards the flat `public_host`
  unchanged via `X-Daalu-Forward-Host`; the gateway was re-provisioned to route
  flat hosts. There is no flat→nested rewrite. (ch 68 §68.6.)
- **Per-user is preferred, service token is the fallback.** `krt` is only present
  on OIDC-login sessions; password-login and invite-redeem sessions carry no
  refresh token, so those silently use the shared `daalu-hub-nvcm` service
  identity. (ch 69 §4.)
- **JWKS is internal, issuer is external.** The gateway validates against
  in-cluster Keycloak, but the token's `iss` and audiences must still match the
  external realm. (ch 68 §68.7, ch 69 §3.)

---

## 67.4 How the new path reuses the old ones

cmtools is intentionally thin — it composes existing identities rather than
inventing one:

- **The session cookie is unchanged.** Same HS256 `daalu_session`, same
  `core/auth.py:issue_token`/`decode_token`. cmtools only relies on it being
  *zone-scoped* (`.daalu.io`) and on two extra claims the OIDC callback now
  stuffs in: `krt` (encrypted KC refresh token) and `kit` (id_token, for RP-initiated
  logout). See [5](../02-backend-architecture/05-auth-system.md) and
  [56](../10-security/56-auth-flows.md) for the cookie/JWT mechanics;
  [59](../10-security/59-keycloak-hub-sso.md) for where `krt`/`kit` are populated.
- **Tenant isolation is the same check.** The proxy's `tid == tenant.id` gate is
  the same principle Part X documents — see
  [57](../10-security/57-tenant-isolation.md).
- **The fallback identity is the executor's machine token.** When per-user
  exchange isn't available, cmtools forwards the *same* `daalu-hub-nvcm`
  client-credentials token the hub already uses for `svc-nautobot /api/` — see
  [21 §21.2](../04-source-of-truth/21-nvcm-network-plane.md).
- **The transport is the federation tunnel.** Nothing new on the wire: requests
  ride the daalu-edge forward proxy over WireGuard — see
  [24](../05-cluster-federation/24-wireguard-hub.md) and
  [25](../05-cluster-federation/25-daalu-edge.md).
- **The Keycloak realm and clients are the SSO realm.** Realm `daalu`,
  browser-login client `daalu-hub-ui`, the new confidential exchange client
  `daalu-hub-nvcm`, and the gateway client `nv-config-manager-ui` — the realm
  wiring is the subject of [70. Keycloak realm, clients & provisioning](70-keycloak-realm-clients-and-provisioning.md)
  (building on [59](../10-security/59-keycloak-hub-sso.md)), and the exchange that
  consumes it is unpacked in [69. Per-user token-exchange](69-per-user-token-exchange.md).

In other words: ch 5/56/59 own *the hub login*, ch 17/21 own *machine identity*,
ch 24/25 own *the tunnel*, ch 57 owns *isolation*, and ch 53 owns *the TLS/DNS
substrate*. Part XIII's job is to show how cmtools **glues them into one click**.

---

## 67.5 What's next

Read Part XIII in order if you're new to cmtools; jump straight to the relevant
chapter if you're debugging:

- **[68. NV-CM tool SSO through the hub (cmtools)](68-nvcm-tool-sso-through-the-hub.md)** —
  the flagship chapter. Why private tenant tool UIs were unreachable, the flat
  `*.cmtools.example.com` URL design (`compute_urls()` + the Network-&-servers
  links), the `CmtoolsHostRouter` outer ASGI middleware and `tool_proxy.py`
  host-parse/authz/resolve, the tenant-ownership check, tunnel forwarding via
  `X-Daalu-Forward-Host`, the per-tenant gateway values
  (`render_values`/`_daalu_values_overlay`, `interactiveRedirect=false`,
  HTTP-only listener, target-cluster-keyed JWKS, flat CORS origins), the
  known-open items (Render/Config-store console links → `/docs`; Workflow →
  the Temporal web UI; Nautobot RBAC roles now bootstrapped by PR #127), and
  the live-infra durability checklist.
- **[69. Per-user token-exchange](69-per-user-token-exchange.md)** —
  `get_user_nvcm_token` (refresh through `daalu-hub-ui` → Standard exchange
  through `daalu-hub-nvcm`, no `audience` param), where `krt` is stored (an
  OFFLINE refresh token since PR #119), the
  service-token fallback (`get_machine_token`), token caching, the Keycloak
  version story (26.2 GA floor, PR #120; V1 impersonation NPE on the old
  instance), and a failure-mode cheat sheet.
- **[70. Keycloak realm, clients & provisioning](70-keycloak-realm-clients-and-provisioning.md)** —
  the single source of truth for what must exist in realm `daalu` for tool SSO:
  the `daalu-hub-nvcm` exchange client, the `nv-config-manager-ui` gateway
  client, the two audience-mappers, `standard.token.exchange.enabled`,
  `revokeRefreshToken=false`, redirect URIs, and how `setup-keycloak.sh` /
  `_ensure_keycloak_sso` make them survive a realm rebuild.
- **[71. Reachability: DNS, TLS, firewall & the tunnel](71-auth-reachability-dns-tls-tunnel.md)** —
  the network/PKI substrate every token, cookie, and JWKS fetch assumes: the
  `*.cmtools.example.com` wildcard DNS + LE wildcard cert, `auth.example.com` proxied
  (NAT-hairpin fix), the host firewall, and the restart-safe
  `daalu-edge` (persistent PVC, the `409` re-peer dance).
