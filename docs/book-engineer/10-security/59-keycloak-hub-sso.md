# 59. Keycloak hub SSO (single sign-on across the hub and the NV-CM tools)

This chapter is the operator + engineer reference for **hub SSO**: logging
into `ops.example.com` through Keycloak so a logged-in operator is *themselves*
in the bundled NV-CM tools (Nautobot, Render, Workflow, Config Store) with
**no second login**. It complements [56. Auth flows](56-auth-flows.md)
(which documents the legacy local password login) and
[04-source-of-truth/23](../04-source-of-truth/23-nvcm-internals-and-operator.md)
(the NV-CM gateway OIDC + JWT plumbing). This chapter establishes the realm and
the browser login; the **hub reverse-proxy plane** that carries that identity
across the WireGuard tunnel into a tenant's private tool UIs — the
`*.cmtools.example.com` tool proxy and the per-user Keycloak token-exchange — is
[Part XIII — Auth integrations](../13-auth-integrations/README.md), chapters
[68](../13-auth-integrations/68-nvcm-tool-sso-through-the-hub.md)–[70](../13-auth-integrations/70-keycloak-realm-clients-and-provisioning.md).

> TL;DR: set `auth_mode=oidc` on daalu-api and the browser login becomes a
> Keycloak redirect. Because the NV-CM tool gateways already do OIDC against
> the **same** realm (`daalu`), the operator's Keycloak session is reused —
> the "Open in Nautobot" links just work. Identity stays daalu-owned; Keycloak
> is the credential + SSO authority.

---

## 1. Why this exists

Before this, there were **two unrelated login systems**:

| Surface | Auth |
|---|---|
| `ops.example.com` (the hub) | daalu's own email+password → HS256 `daalu_session` cookie |
| `*.cm.example.com` (NV-CM tools) | Envoy Gateway OIDC → Keycloak realm `daalu` |

So clicking a tool link bounced you to a Keycloak login your hub session knew
nothing about — and the realm had **no human users** at all. Hub SSO unifies
the front door on Keycloak so one login covers everything.

**Design rule that did *not* change:** daalu remains the **system of record**
for *who* a user is — the `users` row carries `tenant_id` + `is_admin`, and the
session is still the same `daalu_session` HS256 cookie that every downstream
authz check already trusts. OIDC only changes *how the password is checked* and
*the fact the browser also holds a Keycloak session*. Keycloak is the
credential/SSO authority; it is **not** the tenant/role authority.

---

## 2. The two modes (`auth_mode`)

`auth_mode` (setting / `AUTH_MODE` env, Helm `config.authMode`) selects the
hub's front door:

- **`password`** (default): the legacy local form (`POST /auth/login`,
  bcrypt). This is the dev default and the **rollback** target.
- **`oidc`**: `POST /auth/login` is disabled (403); the browser is redirected
  to Keycloak. `GET /auth/config` reports the mode so the login page either
  renders the form or bounces to `/auth/oidc/login`.

The cutover is a single setting flip + a daalu-api restart — **no image
rebuild**. See §7.

---

## 3. The OIDC login flow (authorization code + PKCE)

Implemented in `core/oidc.py` (relying-party helper) + the routes in
`api/routers/auth.py`. Public paths: `/auth/config`, `/auth/oidc/login`,
`/auth/oidc/callback`, `/auth/oidc/logout` (see `auth_public_paths`).

```
Browser → GET /api/v1/auth/oidc/login?next=/some/page
  API: mint state + nonce + PKCE verifier; set them as short-lived HttpOnly
       cookies (oidc_state / oidc_nonce / oidc_verifier / oidc_next)
  API → 302 to Keycloak authorize_endpoint (client_id=daalu-hub-ui, S256)
Browser → Keycloak: logs in (password or "Sign in with Google")
Keycloak → 302 GET /api/v1/auth/oidc/callback?code=…&state=…
  API: state == cookie? exchange code (PKCE verifier) at token_endpoint
  API: validate id_token — RS256 vs realm JWKS (by kid), iss, aud=daalu-hub-ui,
       exp, and nonce == cookie (replay defence)
  API: email = claims.email → SELECT users WHERE email = $1
  API: issue_token(user_id, tenant_id, is_admin) → same daalu_session cookie
  API → 302 to the (sanitised, local-only) `next`
```

Key properties:

- **PKCE S256** + `state` + `nonce` are all verified; `next` is constrained to
  a local path (open-redirect guard, `_safe_next`).
- The id_token is verified against the realm **JWKS** (cached, refreshed on a
  `kid` miss) — no shared secret needed for verification.
- **No daalu User row → 403** (`"no daalu account for <email>"`). OIDC
  authenticates; it does not silently create tenants. (Set
  `oidc_auto_provision_users=true` only for a single-tenant dev box; it creates
  a passwordless User on the default tenant.)
- **Seamless tools:** because the same browser now holds a Keycloak realm-`daalu`
  session, opening `nautobot.<tenant>.cm.example.com` (gateway OIDC, same realm)
  needs no prompt. That is the whole point.

**Logout** (`GET /auth/oidc/logout`, also used in password mode): clears
`daalu_session` and, in OIDC mode, RP-initiated-logout to Keycloak's
`end_session_endpoint` so the SSO session ends too, landing on
`keycloak_hub_post_logout_url` (default `/login`).

---

## 4. Identity mapping & user mirroring

daalu users live in the `users` table; Keycloak realm `daalu` must hold a user
with the **same email** for login to resolve. Three ways a Keycloak user
appears, so a fresh install works out of the box:

1. **Invite redemption** (`POST /auth/redeem-invite`): when `oidc_mirror_users`
   is true, the new user is mirrored into Keycloak with the password they just
   chose (`_mirror_user_to_keycloak` → `core/keycloak_admin.py`). Best-effort —
   a mirror failure never breaks redemption.
2. **Backfill** existing users: `daalu backfill-keycloak-users`
   (`--temporary`/`--no-temporary`) — sets a fresh password per user
   (temporary forces a reset at first login; Google sign-in users never need
   it).
3. **Google sign-in**: brokered users arrive with a verified email; they still
   need a matching daalu `users` row (invite first, or auto-provision on a dev
   box).

`core/keycloak_admin.py` (`KeycloakAdmin`) is the idempotent Admin-REST helper:
`ensure_user`, `set_password`, `ensure_client`, `ensure_identity_provider_google`.
It authenticates either as a **service-account client**
(`keycloak_admin_client_id`/`_secret`) or a **realm admin user**
(`keycloak_admin_username`/`_password` via `admin-cli`, realm
`keycloak_admin_realm`, default `master`).

> Gotcha (learned in prod): Keycloak's realm has **Verify Profile** enabled, so
> a mirrored user with no first/last name gets "Account is not fully set up" and
> cannot log in. `ensure_user` always sets a `firstName`/`lastName` (falling
> back to the email local-part) and clears `requiredActions` to avoid this.

---

## 5. "Sign in with Google"

A Google **identity provider** on realm `daalu` adds the Google button to the
Keycloak login page and brokers logins.

1. In Google Cloud Console → APIs & Services → Credentials → **OAuth client ID**
   (type: Web application). Authorized redirect URI **exactly**:
   `https://auth.example.com/realms/daalu/broker/google/endpoint`.
2. Put the client id/secret into config:
   - `keycloak_google_client_id` (Helm `config.keycloakGoogleClientId`,
     non-secret),
   - `KEYCLOAK_GOOGLE_CLIENT_SECRET` (a key in `daalu-automation-secrets`).
3. `daalu ensure-keycloak-sso` registers the IdP (`providerId=google`,
   `trustEmail=true`, `syncMode=IMPORT`). Idempotent.

`trustEmail=true` is deliberate — Google verifies the address, and the verified
`email` claim is the key that maps onto the daalu `users` row.

---

## 6. Provisioning that survives re-installs

The hub-SSO Keycloak objects (the `daalu-hub-ui` client + the Google IdP) are
provisioned by **daalu-automation itself**, not the platform repo, so the whole
feature lives in one repo and re-applies on every install:

- `daalu ensure-keycloak-sso` (CLI) — idempotently ensures the client (redirect
  URI = `keycloak_hub_redirect_url`) + the Google IdP (when creds are set).
- A Helm **post-install/post-upgrade hook Job**
  (`templates/keycloak-sso-job.yaml`, gated by `config.ensureKeycloakSsoJob`,
  default on) runs it on every `helm install/upgrade`. So a fresh
  `daalu` + Keycloak bring-up comes up with hub SSO working — no manual
  `kcadm`.

The platform repo only needs to provide realm `daalu` (it already does) and the
admin credentials the Job uses.

### Secrets (`daalu-automation-secrets`, SOPS-managed)

Add these keys (the non-secret siblings are in `values.yaml` `config.*`):

| Secret key | What |
|---|---|
| `KEYCLOAK_HUB_CLIENT_SECRET` | secret of the `daalu-hub-ui` confidential client |
| `KEYCLOAK_ADMIN_PASSWORD` | realm-admin password (if using password-grant admin) |
| `KEYCLOAK_ADMIN_CLIENT_SECRET` | service-account secret (if using client-credentials admin) |
| `KEYCLOAK_GOOGLE_CLIENT_SECRET` | Google OAuth client secret (only for Google sign-in) |

Set them in your app Secret (e.g. SOPS-encrypted, chapter 47); they arrive in the pod via
the chart's `envFrom: secretRef` (`daalu.envFrom`).

---

## 7. Cutover & rollback

**Cutover to SSO:**

1. Ensure the `daalu-hub-ui` client exists with a secret (the Job, or
   `daalu ensure-keycloak-sso`), and that secret is in `daalu-automation-secrets`
   as `KEYCLOAK_HUB_CLIENT_SECRET`.
2. Mirror users: `daalu backfill-keycloak-users` (and/or set
   `oidc_mirror_users=true` going forward), or rely on Google sign-in.
3. Flip `config.authMode: oidc` and roll daalu-api
   (`kubectl rollout restart deploy/daalu-api -n daalu-automation`).
4. Verify: open `https://ops.example.com` → redirected to Keycloak → after login,
   land in the hub; click "Open in Nautobot" → no second prompt.

**Rollback (if SSO misbehaves):** set `config.authMode: password` and restart
daalu-api. The local password path is intact; no data migration. Because the
toggle is config-only, rollback is seconds, not a rebuild.

---

## 8. Settings reference (`config.py`)

| Setting | Env | Default | Purpose |
|---|---|---|---|
| `auth_mode` | `AUTH_MODE` | `password` | `password` \| `oidc` front door |
| `keycloak_issuer_url` | `KEYCLOAK_ISSUER_URL` | — | realm issuer (login + machine tokens) |
| `keycloak_hub_client_id` | `KEYCLOAK_HUB_CLIENT_ID` | `daalu-hub-ui` | hub OIDC client |
| `keycloak_hub_client_secret` | `KEYCLOAK_HUB_CLIENT_SECRET` | — | hub client secret (SOPS) |
| `keycloak_hub_redirect_url` | `KEYCLOAK_HUB_REDIRECT_URL` | derived | OIDC callback (registered redirect URI) |
| `keycloak_hub_post_logout_url` | `KEYCLOAK_HUB_POST_LOGOUT_URL` | `/login` | RP-logout landing |
| `oidc_auto_provision_users` | `OIDC_AUTO_PROVISION_USERS` | `false` | create User on first OIDC login (dev) |
| `oidc_mirror_users` | `OIDC_MIRROR_USERS` | `false` | mirror users on invite redemption |
| `keycloak_admin_client_id/_secret` | `KEYCLOAK_ADMIN_CLIENT_ID/_SECRET` | — | admin via service account |
| `keycloak_admin_username/_password` | `KEYCLOAK_ADMIN_USERNAME/_PASSWORD` | — | admin via password grant |
| `keycloak_admin_realm` | `KEYCLOAK_ADMIN_REALM` | `master` | realm the admin authenticates against |
| `keycloak_realm` | `KEYCLOAK_REALM` | derived | managed realm (`daalu`) |
| `keycloak_admin_base_url` | `KEYCLOAK_ADMIN_BASE_URL` | derived | admin REST base (`https://auth.example.com`) |
| `keycloak_google_client_id` | `KEYCLOAK_GOOGLE_CLIENT_ID` | — | Google IdP client id |
| `keycloak_google_client_secret` | `KEYCLOAK_GOOGLE_CLIENT_SECRET` | — | Google IdP secret (SOPS) |

---

## 8a. Known gotcha — workload-cluster JWKS must be internal

The NV-CM tool gateways (Envoy) validate the human session JWT by fetching the
realm **JWKS**. On the workload cluster the *external* issuer host
(`auth.example.com`, a private LB on the platform cluster) is **not reachable**, so
a JWKS URL of `https://auth.example.com/realms/daalu/protocol/openid-connect/certs`
fails and the post-login request dies with Envoy **"OAuth flow failed"** — even
though the Keycloak login itself succeeded.

The controller already sets `KEYCLOAK_INTERNAL_ISSUER_URL`, but it was
only applied to the machine `*-svc` SecurityPolicies; the **human**
`*-gateway-security` / `*-auth-bypass` policies kept the external JWKS URL.

The fix — point **every** SecurityPolicy `jwt` provider's `remoteJWKS.uri` at a
JWKS source the cluster can actually reach. The token `iss` stays the external
issuer (that's what the token carries); only the JWKS *fetch* varies.

> ✅ **This is now durable — no hand-patching.** `render_values` emits
> `oidc.jwksUri` on every install, keyed on `target_cluster_tunnel_id`:
> `keycloak.daalu-automation.svc.cluster.local:8080/.../certs` for the
> operator's own cluster, the external certs URL for a customer cluster over
> WireGuard (which runs no Keycloak). The namespace assumes Keycloak runs in
> `daalu-automation` on the operator cluster.
> Cover: `tests/config_manager_controller/test_oidc_jwks_target_cluster.py`.

### Then: "Audiences in JWT are not allowed"

The next wall after JWKS: Envoy's `oidc-provider` only allows `aud:
nv-config-manager`, but a human login token from client `nv-config-manager-ui`
has `aud: account` (Keycloak default) unless the client has an **audience
mapper**. Add one so the token also carries the API audience (don't instead add
`account` to Envoy's allowed list — every realm token has `account`, so that
would accept any client's token):

```bash
# kcadm inside the keycloak pod, realm daalu
cid=$(kcadm.sh get clients -r daalu -q clientId=nv-config-manager-ui --fields id --format csv --noquotes)
kcadm.sh create clients/$cid/protocol-mappers/models -r daalu \
  -s name=nv-config-manager-audience -s protocol=openid-connect \
  -s protocolMapper=oidc-audience-mapper \
  -s 'config."included.client.audience"=nv-config-manager' \
  -s 'config."access.token.claim"=true' -s 'config."id.token.claim"=false'
# token aud becomes ["nv-config-manager","account"] → Envoy accepts.
```

Both (1) and (2) belong in the controller's per-tenant NV-CM provisioning so a
re-provision stays healthy (tracked separately).

---

## 9. Files

- `src/daalu_automation/core/oidc.py` — RP helper (discovery, PKCE, code
  exchange, id_token validation, RP-logout URL).
- `src/daalu_automation/core/keycloak_admin.py` — idempotent Admin REST
  (users, client, Google IdP).
- `src/daalu_automation/api/routers/auth.py` — `/auth/config`,
  `/auth/oidc/{login,callback,logout}`; `/auth/login` gated by `auth_mode`;
  invite-redemption mirror hook.
- `src/daalu_automation/cli.py` — `daalu ensure-keycloak-sso`,
  `daalu backfill-keycloak-users`.
- `frontend/app/login/page.tsx`, `frontend/lib/auth.tsx`, `frontend/lib/api.ts`
  — login page bounces to SSO in oidc mode; logout via the backend.
- `deploy/helm/daalu-automation/` — `templates/keycloak-sso-job.yaml`,
  `_helpers.tpl` (`commonEnv`), `values.yaml` (`config.*`).
