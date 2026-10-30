# 69. Per-user token-exchange (log users into tools as themselves)

This chapter is the deep dive on the **downstream Bearer** the cmtools proxy
injects: how a logged-in `ops.example.com` operator lands inside a tenant's NV-CM
tool web UI *as themselves* — `preferred_username`/`email` and all — instead of
as a faceless shared service account. It is the auth half of the cmtools story;
[68. NV-CM tool SSO through the hub](68-nvcm-tool-sso-through-the-hub.md) covers
the host routing, tenant check, tunnel forwarding, and the tenant-gateway
`SecurityPolicy` (§68.6–68.7) that surround the point where this token is minted,
and [70. Keycloak realm, clients & provisioning](70-keycloak-realm-clients-and-provisioning.md)
is the reference for the realm clients and mappers this exchange depends on.

> TL;DR: the proxy decrypts the user's Keycloak refresh token from the session
> (`krt` claim), refreshes it through `daalu-hub-ui`, then runs a **Standard
> Token Exchange** through `daalu-hub-nvcm` (no `audience` param — a client
> audience-mapper stamps `aud=nv-config-manager`). The result is a token that
> *is the user* and that the tenant gateway + NV-CM auth plugin accept. If
> there's no stored refresh token (password login) or the exchange fails, the
> proxy falls back to a `daalu-hub-nvcm` client-credentials **service** token.

The relevant code is `core/keycloak.py:get_user_nvcm_token` (the exchange),
`api/tool_proxy.py:_resolve` (the choose-user-or-service decision),
`api/routers/auth.py:oidc_callback` (where `krt` is stored), and
`deploy/scripts/config-manager/setup-keycloak.sh` + `cli.py:_ensure_keycloak_sso`
+ `core/keycloak_admin.py:ensure_audience_mapper` (the Keycloak wiring).

---

## 1. Why a per-user token is required at all

The bundled Nautobot ships NV-CM's `nv_config_manager_auth` plugin as its
`user_provider`. When a request arrives carrying a validated JWT (in the
`Authorization` header or the `NVConfigManagerAccessToken` cookie), the plugin
reads identity claims off that token and **creates / logs in the matching
Django user**. That is the whole point: the tool gets a real, named session.

A shared service identity cannot drive that. The `daalu-hub-nvcm` service
account has *no* human identity — its client-credentials token carries
`preferred_username=service-account-daalu-hub-nvcm`, no `email`, and the realm
roles of the *machine* client. Inject that and every tenant operator becomes the
same anonymous service-account user inside Nautobot, with whatever (typically
broad, machine-grade) RBAC that account carries. That is wrong on both identity
(audit trails collapse to one user) and authorization (you cannot RBAC-gate the
sidebar per-operator — see [68 §68.9](68-nvcm-tool-sso-through-the-hub.md) on
`nautobot.rbac.superuserGroups`).

So the downstream Bearer **must** carry the human's identity. The hub session
JWT (`daalu_session`, HS256, see
[05. Authentication](../02-backend-architecture/05-auth-system.md)) cannot be
that token: it is signed with daalu's own HS256 secret, not by Keycloak, so the
tenant gateway's `remoteJWKS` validation (RS256 against the realm) would reject
it, and its audience is daalu, not `nv-config-manager`. We need a token that is
**(a)** signed by the realm, **(b)** audienced for `nv-config-manager`, and
**(c)** subject = the human. Keycloak Token Exchange is exactly the machine for
turning "I hold this user's session" into such a token.

---

## 2. The flow end to end

```
 OIDC login (one time, api/routers/auth.py:oidc_callback)
 ────────────────────────────────────────────────────────
   Keycloak ── refresh_token ──▶ encrypt_secret() ──▶ session claim "krt"
                                                       (HS256 daalu_session cookie)

 Every cmtools request (api/tool_proxy.py:_resolve → core/keycloak.get_user_nvcm_token)
 ──────────────────────────────────────────────────────────────────────────────────────
   1. claims["krt"] ── decrypt_secret() ──▶ user refresh token
   2. POST /token  grant=refresh_token  client_id=daalu-hub-ui
            └──▶ fresh USER access token  (sub = the human)
   3. POST /token  grant=token-exchange  client_id=daalu-hub-nvcm
                   subject_token = the user access token
                   (NO audience param)
            └──▶ daalu-hub-nvcm audience-mapper stamps aud=nv-config-manager
            └──▶ USER token, aud=nv-config-manager, preferred_username/email intact
   4. inject as  Authorization: Bearer <token>  +  NVConfigManagerAccessToken cookie
```

### 2.1 Storing `krt` at login

Per-user exchange is only possible because the OIDC callback squirrels the
user's Keycloak **refresh** token into the session. `oidc_callback`
(`api/routers/auth.py`) does, after it has validated the id_token and mapped the
verified `email` to a daalu `User`:

```python
extra: dict = {}
refresh_token = tokens.get("refresh_token")
if refresh_token:
    from daalu_automation.core.crypto import encrypt_secret
    extra["krt"] = encrypt_secret(refresh_token)
if id_token:
    extra["kit"] = id_token            # for RP-initiated logout, see ch. 59
token, _ = issue_token(..., extra=extra or None)
```

`issue_token` (`core/auth.py`) merges `extra` into the HS256 `daalu_session`
claims, so `krt` (Fernet-encrypted refresh token) and `kit` (raw id_token) ride
inside the same cookie every other authz check already trusts. The refresh token
is encrypted at rest in the cookie via `core/crypto.encrypt_secret`; it is never
stored server-side.

> **`krt` must be an OFFLINE refresh token (PR #119).** The authorize URL
> requests `scope=openid email profile offline_access`
> (`core/oidc.py:build_authorize_url`, the scope literal at `core/oidc.py:169`).
> Without `offline_access`, Keycloak returns an *online* refresh token bound to
> the realm's **SSO-session idle timeout (30 minutes)** — so a user who browsed
> Daalu for a while and then opened a tool hit
> `invalid_grant: Token is not active` on the refresh step, the proxy fell back
> to the service token, and the tool showed its own login. With
> `offline_access` the token is governed by `offlineSessionIdleTimeout`
> (~30 days, no max lifespan), so the per-user path survives ordinary idle.
> On the `daalu` realm `offline_access` is an assigned **optional** client
> scope on `daalu-hub-ui` with `consentRequired=false`, so no consent screen
> appears. Sessions minted before the change keep their online `krt` until the
> user re-logs-in.

> **`krt` is OIDC-login-only.** `auth.py:login` (local password) and
> `redeem_invite` call `issue_token` with no `extra`, so those sessions carry no
> `krt`. They are *valid* hub sessions and reach the proxy fine — they just take
> the service-token fallback (§4). If you log into `ops.example.com` with a
> password rather than through Keycloak, the tools log you in as the service
> account. This is by design and is the reason §5 of [59](../10-security/59-keycloak-hub-sso.md)
> insists `auth_mode=oidc` for the cmtools experience to be "as yourself".

### 2.2 The exchange itself

`core/keycloak.py:get_user_nvcm_token` is two HTTP POSTs to the realm token
endpoint:

**Step 1 — refresh.** The stored token may be minutes or hours old; refresh it
through the *login* client to get a current user access token:

```python
refresh_form = {
    "grant_type": "refresh_token",
    "client_id": settings.keycloak_hub_client_id,        # daalu-hub-ui
    "refresh_token": refresh_token,
}
if settings.keycloak_hub_client_secret:                  # confidential client; env often unset → sent only if present
    refresh_form["client_secret"] = settings.keycloak_hub_client_secret
```

This is reusable only because realm **`revokeRefreshToken=false`** (§3): with
rotation off, the same refresh token keeps working for the life of the SSO
session, so we don't have to persist a rotating value back into the cookie on
every request.

**Step 2 — Standard Token Exchange.** Trade the user access token for an
NV-CM-audienced token through the *service* client:

```python
exchange_form = {
    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
    "client_id": nvcm_client_id,                         # daalu-hub-nvcm
    "client_secret": nvcm_client_secret,
    "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
    "subject_token": user_at,                            # from step 1
}
```

Note what is **absent**: there is no `audience` (and no `requested_token_type`)
parameter. We deliberately do *not* ask Keycloak for a specific audience. The
audience is supplied by a **client audience-mapper** on `daalu-hub-nvcm`
(`config."included.client.audience"=nv-config-manager`, §3), which stamps
`aud=nv-config-manager` onto every token that client emits — including the one
produced by the exchange. Passing an explicit `audience` param here is the path
that drags in V1-style audience handling, which is broken on this Keycloak
(§6). Letting the mapper do it keeps us on the Standard (V2) code path.

The returned token's `sub`/`preferred_username`/`email` are the **user's**
(exchange preserves subject identity), and its `aud` is `nv-config-manager`.
That is exactly the shape the tenant gateway validates and the NV-CM
`user_provider` reads. Compare this to the *machine* path in
[21. NV-CM network plane §21.2](../04-source-of-truth/21-nvcm-network-plane.md),
where `get_machine_token` deliberately *does* send `audience` on a
client-credentials grant — different grant, different reason.

---

## 3. The Keycloak configuration this depends on

Three pieces of realm config make the exchange legal and correctly-audienced.
They are provisioned by `deploy/scripts/config-manager/setup-keycloak.sh`,
`core/keycloak_admin.py:ensure_audience_mapper`, and the realm-level setting
below; the durable-install path is `cli.py:_ensure_keycloak_sso` (also wired as
a Helm post-install Job — see [59 §6](../10-security/59-keycloak-hub-sso.md)).

> The table below is the *exchange's-eye view* — which realm settings this flow
> needs and why. [70. Keycloak realm, clients & provisioning](70-keycloak-realm-clients-and-provisioning.md)
> is the full reference for the three clients, every mapper, the redirect URIs,
> and how each one survives a realm rebuild; read it rather than treating this
> table as exhaustive.

| Where | Setting | Why |
|---|---|---|
| `daalu-hub-nvcm` client | `standard.token.exchange.enabled=true` | Permits this confidential client to *run* a Standard exchange. Set by `setup-keycloak.sh` (`-s 'attributes."standard.token.exchange.enabled"=true'`). |
| `daalu-hub-nvcm` client | `oidc-audience-mapper`, `included.client.audience=nv-config-manager` | The **output**-side mapper. Stamps `aud=nv-config-manager` on the exchanged token so we don't pass an `audience` param. Set by `setup-keycloak.sh` (`nvcm-audience` mapper). |
| `daalu-hub-ui` client | `oidc-audience-mapper`, `included.client.audience=daalu-hub-nvcm` | The **permission**-side mapper. The user access token (step 1) must carry `aud=daalu-hub-nvcm` so the realm permits `daalu-hub-nvcm` to exchange it. Set by `cli.py:_ensure_keycloak_sso` → `ensure_audience_mapper(client_id=keycloak_hub_client_id, audience_client="daalu-hub-nvcm")`. |
| realm `daalu` | `revokeRefreshToken=false` | Refresh-token rotation OFF, so the `krt` stored once at login keeps refreshing for the whole SSO session. With rotation on, the first refresh would invalidate the stored token and every subsequent request would 400. |
| `daalu-hub-ui` client | `offline_access` assigned as an **optional** client scope, `consentRequired=false` | Lets the login request `offline_access` (§2.1) without a consent prompt, so `krt` is an OFFLINE token that outlives the 30-minute SSO idle timeout. |
| server version | Keycloak **26.2+** | Standard Token Exchange is **GA** only in 26.2; on 26.1 the per-client switch is inert and the exchange returns `400 unsupported_grant_type` (§6). |

The two audience-mappers are easy to conflate; they sit on **different clients**
and do **different jobs**:

- `daalu-hub-ui` mapper → `aud=daalu-hub-nvcm` on the *user's login* token. This
  is the **"may I exchange this?"** gate — Keycloak only lets `daalu-hub-nvcm`
  exchange a subject token that lists it as an audience.
- `daalu-hub-nvcm` mapper → `aud=nv-config-manager` on the *exchanged output*
  token. This is the **"who will accept this?"** value — the tenant gateway's
  `oidc.audiences` and NV-CM's `user_provider` both expect `nv-config-manager`.

Miss the first and the exchange 403s ("client not permitted"). Miss the second
and the exchange succeeds but the tenant gateway rejects the token on audience
mismatch.

> **Issuer stays external.** The exchanged token's `iss` is the public realm
> issuer (`auth.example.com`), even though the tenant gateway fetches JWKS from the
> *in-cluster* Keycloak service. Only the JWKS *fetch URL* is internal; the
> `iss` and `aud` the gateway validates are the public realm values. That
> asymmetry — internal JWKS fetch, external issuer/audience — is the subject of
> [68 §68.6–68.7](68-nvcm-tool-sso-through-the-hub.md).

---

## 4. The service-token fallback

`api/tool_proxy.py:_resolve` chooses per-user first, service second:

```python
bearer: str | None = None
krt = claims.get("krt")
if krt:
    try:
        bearer = await get_user_nvcm_token(
            refresh_token=decrypt_secret(krt),
            nvcm_client_id=conn.keycloak_client_id,        # daalu-hub-nvcm
            nvcm_client_secret=conn.keycloak_client_secret,
        )
    except (KeycloakAuthError, ValueError) as e:
        logger.warning("tool_proxy.user_exchange_failed", slug=slug, error=str(e))
        bearer = None                                      # fall through
if bearer is None:
    bearer = await get_machine_token(
        client_id=conn.keycloak_client_id,
        client_secret=conn.keycloak_client_secret,
        audience=conn.audience,                            # nv-config-manager
    )
```

Fallback fires in three cases:

1. **No `krt`** — password / invite session (§2.1).
2. **`decrypt_secret` raises `ValueError`** — the encryption key rotated since
   login, so the stored `krt` is undecryptable. Treated as "no user token".
3. **`get_user_nvcm_token` raises `KeycloakAuthError`** — the refresh token
   expired (SSO session ended), the exchange was rejected (a Keycloak mapper
   regressed), or the realm is unreachable.

The fallback is `get_machine_token` (client-credentials, `audience` sent
explicitly — the machine path of [21 §21.2](../04-source-of-truth/21-nvcm-network-plane.md)).
The operator still reaches the tool; they're just logged in as the service
account for that request, and `tool_proxy.user_exchange_failed` is logged so the
degradation is visible. The proxy never hard-fails on a missing user token —
only on a missing *service* token (which means the integration is unwired), in
which case `_resolve` raises `_ProxyUnavailable`.

> **Stale docstrings.** The module docstring of `api/tool_proxy.py` and the
> comment block near the injection site still describe a service-token-**only**
> design with per-user as a "future upgrade / known tradeoff." That text is out
> of date: `_resolve` does per-user-first today. Trust the code, not those
> comments.

---

## 5. Token caching

Both token helpers cache in-process to keep a burst of tool requests (every
asset, every XHR a tool UI fires) from hammering the realm token endpoint.

- **`get_user_nvcm_token`** caches in `_USER_CACHE`, keyed by
  `sha256(refresh_token)` — so the cache is *per user*, and two users sharing a
  pod never collide. Entries are dropped `_EXPIRY_SKEW_S` (30 s) before
  `expires_in`, so an in-flight request never races the expiry boundary.
- **`get_machine_token`** caches in `_CACHE`, keyed by
  `(token_url, client_id, audience)` — *per machine client/audience*, shared
  across all requests.

Both caches are process-local (no Redis); a daalu-api restart re-mints. The key
choice matters: hashing the *refresh* token (not the user id) means the cache
naturally invalidates when a user re-logs-in (new SSO session → new refresh
token → new cache key), and never leaks one user's exchanged token to another.
`clear_token_cache()` drops both and is called by tests + on credential
rotation.

---

## 6. Why V1 impersonation exchange is unusable here

Keycloak historically had **two** token-exchange flavours:

- **V1 / legacy exchange** — the older, "preview" `token-exchange` feature,
  which is also how *impersonation*-style exchange (one subject acting as
  another, audience-driven via the `audience`/`requested_subject` params) was
  expressed.
- **V2 / Standard Token Exchange** — the GA flow gated by the per-client
  `standard.token.exchange.enabled` attribute, with **no** `audience`/subject
  params; the target client's mappers shape the output token.

The version story has two acts, and both argue for the same code shape:

- **On the pre-rehome workload-cluster instance (26.5.1,
  `KC_FEATURES=token-exchange`)**, the **V1 impersonation exchange threw a
  `NullPointerException`** server-side — simply broken for our parameters. That
  is why §2.2's exchange sends *no* `audience` and relies on the client
  audience-mapper: that combination keeps us strictly on the V2 Standard path
  (`standard.token.exchange.enabled`). Any attempt to "just pass
  `audience=nv-config-manager` on the exchange" silently reroutes into the V1
  handling and NPEs.
- **On Keycloak 26.1**, Standard Token Exchange is not yet GA: the
  per-client `standard.token.exchange.enabled` switch exists but is **inert**
  and the legacy preview feature is disabled, so the exchange returns
  `400 {"error":"unsupported_grant_type"}` and the proxy falls back to the
  service token — operators drop to each tool's own login. Use **26.2+**
  (`quay.io/keycloak/keycloak:26.2`), where Standard
  Token Exchange (RFC 8693) is GA and enabled by default at the server level —
  the client config takes effect with **no further changes**. Note the
  bump runs a one-way DB schema migration on first start; the Deployment's
  `Recreate` strategy means ~1 min of auth downtime per rollout.

The mapper-stamps-audience design is not a stylistic choice; it is the only
shape that works across both instances. The realm/version reference — the
per-client `standard.token.exchange.enabled` gate, the 26.2-GA requirement, the
V1-NPE warning — is [70 §70.7](70-keycloak-realm-clients-and-provisioning.md).

---

## 7. Failure-mode cheat sheet

| Symptom | Likely cause |
|---|---|
| Tool logs you in as `service-account-daalu-hub-nvcm` | `auth_mode=password` (no `krt`), or `tool_proxy.user_exchange_failed` in logs → see fallback (§4) |
| Exchange 403 "client not permitted to exchange" | `daalu-hub-ui` audience-mapper missing (`aud=daalu-hub-nvcm` not on the login token) — run `_ensure_keycloak_sso` |
| Exchange 400 on the **refresh** step | `revokeRefreshToken=true` (rotation on) invalidated the stored `krt`, or the SSO session expired |
| Refresh 400 `invalid_grant: Token is not active` after ~30 min idle | Session predates PR #119 (online `krt`, idle-expired) or `offline_access` missing from the login scope / not an optional scope on `daalu-hub-ui` (§2.1) — user must re-login |
| Exchange 400 `unsupported_grant_type` | Keycloak < 26.2 — Standard Token Exchange not GA; the per-client switch is inert on 26.1 (§6, PR #120) |
| Exchange succeeds, **gateway** then 401s | `daalu-hub-nvcm` audience-mapper missing or wrong (`aud != nv-config-manager`); or token `iss`/`aud` ≠ gateway's `oidc` config (see [68 §68.6–68.7](68-nvcm-tool-sso-through-the-hub.md)) |
| Exchange succeeds, gateway JWKS fetch 403s (Cloudflare **error 1010**) | Cloudflare Browser Integrity Check blocking non-browser UAs on `auth.example.com` — needs the zone Configuration Rule disabling BIC for `/protocol/openid-connect/*` (see [71 §71.2](71-auth-reachability-dns-tls-tunnel.md)) |
| Exchange NPE 500 | something is passing `audience` on the exchange → V1 path (26.5.1-era instance, §6) |
| User lands on Nautobot **Access Denied** despite a valid user token | realm roles behind `nautobot.rbac.superuserGroups` never created — run `ensure-keycloak-sso` (PR #127; see [70 §70.5](70-keycloak-realm-clients-and-provisioning.md)) |
| `decrypt_secret` ValueError in logs, then fallback | encryption key rotated since the user's login; user must re-login to refresh `krt` |

---

## What's next

[70. Keycloak realm, clients & provisioning](70-keycloak-realm-clients-and-provisioning.md) —
the single source of truth for *what must exist in the realm* for this exchange
to be legal: the `daalu-hub-ui`/`daalu-hub-nvcm`/`nv-config-manager-ui` clients,
both audience-mappers, the `setup-keycloak.sh` / `ensure-keycloak-sso` durability
path, and the Keycloak version specifics (26.2 GA floor). For what the tenant Envoy gateway *does* with
the token this chapter mints — JWT validation against the **in-cluster** JWKS,
`interactiveRedirect=false`, `claimToHeaders`, and the NV-CM `user_provider`
login — see [68. NV-CM tool SSO through the hub §68.6–68.7](68-nvcm-tool-sso-through-the-hub.md).
For the hub login that produces `krt` in the first place,
[59. Keycloak hub SSO](../10-security/59-keycloak-hub-sso.md); and for the
machine-identity sibling of this flow,
[21. The NV-CM network plane §21.2](../04-source-of-truth/21-nvcm-network-plane.md).
