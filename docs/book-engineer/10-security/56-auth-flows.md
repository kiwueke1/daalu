# 56. Auth flows in depth

Chapter 5 covered the auth mechanics. This chapter covers
the actual flows — login, refresh, logout, PAT use, executor
auth, webhook auth — each as a sequence.

> The local email+password login below is the `auth_mode=password`
> path. When the hub runs `auth_mode=oidc`, the browser login is a
> Keycloak redirect instead — see [59. Keycloak hub SSO](59-keycloak-hub-sso.md)
> for the OIDC flow, Google sign-in, user mirroring, and cutover/rollback.

---

## Login flow

```
User → Browser: enters email + password, hits submit
Browser → API: POST /api/v1/auth/login { email, password }
API → DB: SELECT * FROM users WHERE email = $1 LIMIT 1
API: verify_password(plain, user.password_hash)
API: issue_token(user_id, tenant_id, is_admin) → (jwt, expiry)
API → Browser: Set-Cookie: daalu_session=<jwt>; HttpOnly; Secure; SameSite=Lax
              Body: { user, token, expires_at }
Browser → /
```

Notes:
- The token is returned in the body too, so curl-style
  callers can use it without parsing cookies.
- Constant-time password comparison via passlib.
- Failed login (wrong password) returns a generic 401 with
  no user-existence leak.

---

## Subsequent request (cookie)

```
Browser → API: GET /api/v1/alerts
              Cookie: daalu_session=<jwt>
API.AuthGate: cookie present, decode JWT, OK; forward
API.current_user: decode JWT again, load User from DB
API handler: handle the request scoped to user.tenant_id
API → Browser: 200 + JSON
```

The double-decode is the price of separating gate (cheap,
no DB) from handler (loads the user). Negligible cost.

---

## Subsequent request (PAT)

```
Script → API: GET /api/v1/alerts
             Authorization: Bearer dpat_xxxxxxxxxxxxxxxxxxxxxxxx
API.AuthGate: detect "dpat_" prefix, skip JWT decode, forward
API.current_user._resolve_pat:
    hash = sha256(token)
    pat = SELECT * FROM personal_access_tokens WHERE token_hash = $1
    check revoked_at, expires_at, user.is_active
    stamp pat.last_used_at = now (fire-and-forget)
    return user
API handler: same as above
```

Notes:
- The `dpat_` prefix is the gate's cheap dispatcher.
- The hash lookup is indexed; O(1).
- `last_used_at` stamping is fire-and-forget; a failed
  commit there doesn't break the request.

---

## Token refresh

JWTs expire in 60 minutes. The frontend silently refreshes:

```
Frontend (periodic): GET /api/v1/auth/me
API: if cookie valid and expires_at < now + 10 min, mint new token
API → Frontend: Set-Cookie (new) + body { expires_at: new }
```

If the cookie is already expired, /auth/me returns 401 and
the frontend redirects to /login.

---

## Logout

```
Browser → API: POST /api/v1/auth/logout
API → Browser: Set-Cookie: daalu_session=; Max-Age=0; HttpOnly; Secure
              Body: {}
```

The cookie is cleared client-side. The JWT itself is *not*
invalidated server-side — it remains valid until its
exp claim. JWT revocation would require a server-side
blacklist; we don't have one.

If you need real revocation (e.g., compromised cookie), use
the **"Sign out everywhere"** button which rotates the user's
session-secret salt and invalidates all their JWTs at once.

---

## "Sign out everywhere"

```
User → API: POST /api/v1/auth/sign-out-everywhere
API → DB: UPDATE users SET session_salt = random() WHERE id = user.id
API → Browser: clear cookie
```

The `session_salt` is part of the JWT payload. When it changes,
old JWTs decode but the salt-check fails. Future logins use
the new salt.

This is a "nuclear option" for compromised sessions. Cheaper
than rotating AUTH_SECRET (which affects all users).

---

## Webhook authentication

```
External → API: POST /api/v1/sot/webhooks/<slug>
                Body: <json>
                X-Daalu-Signature: <hex>
API.AuthGate: bypass JWT/PAT check for webhook path
API.handler:
    sig = request.header["X-Daalu-Signature"]
    secret = look up by slug
    expected = hmac_sha256(body, secret)
    if not constant_time_eq(expected, sig): raise 401
    process the event
```

The slug determines tenant. The HMAC is the auth.

---

## Executor authentication

```
Executor pod startup:
    DAALU_EXECUTOR_JWT = read from env (mounted Secret)
    jwt.decode(DAALU_EXECUTOR_JWT) — validates locally

Executor doing a write tool call:
    _require_executor_scope(jwt.decode(DAALU_EXECUTOR_JWT))
    → raises if scope != "executor"
    → succeeds otherwise; the tool proceeds
```

The executor's JWT has no user_id; it's a "platform"
identity. Tenant isolation in executor calls comes from the
proposal's tenant_id (the proposal table is tenant-scoped).

---

## Federation bootstrap

Covered in Chapter 26 — sequence diagram of the join flow.
The auth piece is:

```
Edge → API: POST /api/v1/clusters/<slug>/callback
            Body: { token, public_key }
API: hash(token) match against cluster_tunnels.bootstrap_token_hash
API: check expires_at, bootstrap_consumed
API: mark consumed; respond with WG config
```

The bootstrap token is the one-shot credential. After
consumption, the WireGuard pubkey is the long-lived identity.

---

## What's next

Chapter 57 — tenant isolation guarantees.
