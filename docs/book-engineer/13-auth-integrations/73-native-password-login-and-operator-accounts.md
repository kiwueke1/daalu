# 73. Native password login & operator accounts

A typical deployment (`ops.example.com`) runs in **`AUTH_MODE=oidc`** — Google sign-in brokered by
Keycloak, restricted to your `@example.com` workspace domain. That restriction is
correct for staff, but it blocks two things the platform needs:

- **Invited users** on non-workspace email (a customer's Gmail, a contractor)
  can never complete Google SSO, so an accepted invite was useless to them.
- **Operator break-glass accounts** that must work even if Google/Keycloak is
  unreachable.

The fix is a **native email+password login that runs alongside Google SSO**,
without opening the door to anonymous self-registration. This chapter documents
that posture, the flag that controls it, and the accounts provisioned under it.

> Owning code: `core/auth.py` (bcrypt hashing, JWT issue), `api/routers/auth.py`
> (`/login`, `/config`, `/redeem-invite`), `config.py` (the flags). Login UX:
> `frontend/app/login/page.tsx`. See also ch. [59](../10-security/59-keycloak-hub-sso.md)
> (Keycloak hub SSO) and [56](../10-security/56-auth-flows.md) (auth flows).

---

## 73.1 The three independent switches

Native login used to be coupled to public signup — you couldn't have one
without the other. They are now three independent settings:

| Setting | Env var | Controls |
|---|---|---|
| `auth_mode` | `AUTH_MODE` | `"oidc"` advertises the Google button and the OIDC callback. `"password"` is local-only mode. |
| `native_password_login_enabled` | `NATIVE_PASSWORD_LOGIN_ENABLED` | Allows `POST /auth/login` (email+password) **even while `auth_mode=oidc`**, *without* enabling public signup. |
| `self_signup_enabled` | `SELF_SIGNUP_ENABLED` | Opens the **public** `POST /auth/signup` page so anyone can self-register (isolated tenant, email-verified). Also implies native login (back-compat). |

**Recommended production posture:**

```
AUTH_MODE=oidc                      # Google SSO stays on
NATIVE_PASSWORD_LOGIN_ENABLED=true  # native login on
SELF_SIGNUP_ENABLED  (unset/false)  # public CANNOT self-register
```

Result: the login page shows **both** the *Sign in with Google* button **and**
the email+password form, but **no** "create an account" link. `GET /auth/config`
returns `password_login_enabled=true`, `self_signup_enabled=false`, and the
frontend renders accordingly (`frontend/app/login/page.tsx`).

The login gate lives in `api/routers/auth.py::login`:

```python
native_login_ok = settings.self_signup_enabled or settings.native_password_login_enabled
if settings.auth_mode == "oidc" and not native_login_ok:
    raise HTTPException(403, "password login is disabled; sign in via SSO …")
```

SSO-only users (no `hashed_password`) still can't use the password path — the
`hashed_password` / `verify_password` checks 401 them.

---

## 73.2 How accounts are created (public cannot self-register)

With `self_signup_enabled=false`, the only two ways a native-password account
comes into existence are:

1. **Operator-provisioned** — `daalu create-admin` (CLI, `cli.py`). Idempotent:
   creates or updates a user in the default operator tenant, `is_active=true`,
   `hashed_password` set, `is_admin=true`. Run it inside the API pod:

   ```bash
   export KUBECONFIG=<operator-cluster-kubeconfig>
   POD=$(kubectl -n daalu-automation get pod -l app=daalu-api -o jsonpath='{.items[0].metadata.name}')
   kubectl -n daalu-automation exec "$POD" -c api -- \
     daalu create-admin --email someone@example.com --password '<generated>' --full-name 'Their Name'
   ```

2. **Accepted invite** — an admin creates an invite (`POST /api/v1/invites`,
   ch. on invites); the invitee opens `https://ops.example.com/accept-invite/<token>`,
   sets a display name + password, and `POST /auth/redeem-invite` creates the
   user (`is_active=true`, password hashed) and signs them in. This works for
   **any** email, including non-workspace addresses, because it never touches Google.

`email_verified_at` is **not** checked at login — only `is_active`. Admin-created
and invite-redeemed accounts are active immediately.

> **Invite link must be a public route.** `/accept-invite/<token>` is served to
> an unauthenticated invitee, so it bypasses the authenticated app shell
> (`frontend/components/layout/app-shell.tsx`, `PUBLIC_ROUTE_PREFIXES`). If it
> doesn't, the page renders the full app, fires authed API calls that 401, and
> dies with "Application error: a client-side exception has occurred".

---

## 73.3 Operator accounts

Seed native-password admin accounts on the default operator tenant. Generate
passwords and keep them in a password manager — **never** commit them. Rotate with
`daalu create-admin` (same email re-runs the update path).

| Login email | Who | Why native (not Google) |
|---|---|---|
| `admin@example.com` | operator on a non-workspace address | Google SSO only accepts the workspace domain. |
| `admin@local.example.com` | break-glass admin | Non-routable identifier (`local.` subdomain). No mailbox; nothing is emailed to it. Chosen over `admin@example.com` so it can **never** collide with a Google-SSO identity (SSO only mints the apex workspace domain, never a subdomain). |

To rotate a password:

```bash
kubectl -n daalu-automation exec "$POD" -c api -- \
  daalu create-admin --email admin@local.example.com --password '<new-generated>'
```

---

## 73.4 Why Keycloak is *not* in this path

Native login is entirely app-local: bcrypt in `core/auth.py`, the user row in
the platform Postgres, a JWT session cookie. Keycloak stays exactly as ch.
[70](70-keycloak-realm-clients-and-provisioning.md) describes — the broker for
**Google** federation and the cmtools token-exchange — and is untouched by the
password path. This keeps native login working even when Keycloak is down, and
means "the identity provider the operator controls" is simply the platform's own
user table.

---

## 73.5 Files

- `src/daalu_automation/config.py` — `auth_mode`, `native_password_login_enabled`, `self_signup_enabled`.
- `src/daalu_automation/api/routers/auth.py` — `login` (gate), `auth_config` (`/config`), `redeem_invite`.
- `src/daalu_automation/core/auth.py` — `hash_password`, `verify_password`, `issue_token`.
- `src/daalu_automation/cli.py` — `create-admin`.
- `frontend/app/login/page.tsx` — renders Google button + password form off `/auth/config`.
- `frontend/components/layout/app-shell.tsx` — `PUBLIC_ROUTE_PREFIXES` (the `/accept-invite` fix).
- `deploy/k8s/api/deployment.yaml` — `AUTH_MODE`, `NATIVE_PASSWORD_LOGIN_ENABLED`.
