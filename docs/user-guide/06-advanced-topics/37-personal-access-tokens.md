# 37. Personal access tokens — the automation API

Daalu has an HTTP API. Anything you can do in the UI you can
do over the API, with the same permissions. The credential for
scripting use is a **personal access token (PAT)**.

This chapter covers minting, using, and rotating PATs.

---

## Minting a PAT

Chapter 24 covered the UI. In short:

1. *Settings → API tokens → New token.*
2. Pick a name, expiry, optional description.
3. **Copy the token now.** It's shown exactly once.

The token looks like `dpat_xxxxxxxxxxxxxxxxxxxxxxxx`. The
`dpat_` prefix is intentional — it's recognizable in env files
and grepable in case you ever wonder "is this a Daalu token?"

---

## Using a PAT

Send it in the `Authorization` header:

```http
GET /api/v1/alerts HTTP/1.1
Host: ops.example.com
Authorization: Bearer dpat_xxxxxxxxxxxxxxxxxxxxxxxx
```

Every authenticated endpoint accepts both PATs and the
browser's session cookie. The PAT route skips the JWT decode
path and looks up the token by hash directly.

`curl` example:

```bash
TOKEN="dpat_xxxxxxxxxxxxxxxxxxxxxxxx"
curl -H "Authorization: Bearer $TOKEN" \
  https://ops.example.com/api/v1/alerts
```

---

## What a PAT can do

The same as the user it was minted for. If you're a regular
user, your PAT can investigate but cannot mutate integrations.
If you're an admin, your PAT can do everything an admin can.

PATs do *not* bypass the four-eyes approval rule. A PAT-driven
proposal cannot be self-approved by another PAT belonging to
the same user.

---

## Expiry and rotation

When you minted, you picked an expiry: never, 30 / 90 / 365
days, or custom. Best practice:

- **365 days** for human-managed scripts. Calendar a rotation.
- **30–90 days** for CI pipelines. Automate rotation through
  the secret store.
- **Never** is allowed but discouraged. Tokens you never rotate
  are tokens that eventually leak.

To rotate:

1. *Settings → API tokens → \[token\] → ⋯ → Rotate.*
2. A new cleartext is shown. Update wherever the old token
   was used.
3. The old token continues to work for 24 hours, giving you a
   transition window.

To revoke:

1. *Settings → API tokens → \[token\] → ⋯ → Revoke.*
2. Effective immediately. Any in-flight request gets 401 on
   its next call.

A revoked token can be reactivated within 7 days. After that
it's gone.

---

## The API surface

A short tour:

### Auth

- `POST /api/v1/auth/login` — exchange email+password for JWT.
- `POST /api/v1/auth/logout` — invalidate session.
- `GET /api/v1/auth/me` — current user info.
- `GET /api/v1/auth/tokens` — list your PATs (PAT-auth).
- `POST /api/v1/auth/tokens` — mint a PAT.
- `DELETE /api/v1/auth/tokens/<id>` — revoke a PAT.

### Alerts

- `GET /api/v1/alerts` — list, with filters.
- `GET /api/v1/alerts/<id>` — detail.
- `POST /api/v1/alerts/<id>/acknowledge` — assign to you.
- `POST /api/v1/alerts/<id>/snooze` — snooze.

### Integrations

- `GET /api/v1/integrations` — list configured integrations.
- `POST /api/v1/integrations` — admin only.
- `GET /api/v1/integrations/<id>/health` — current health.

### Events

- `POST /api/v1/events` — webhook ingest.
- `GET /api/v1/events` — query the timeline.

### SoT

- `GET /api/v1/sot/devices` — list devices.
- `GET /api/v1/sot/devices/<id>/config` — read config.
- `POST /api/v1/sot/proposals` — open a proposal.

### Change proposals

- `GET /api/v1/proposals` — list, with kind filters.
- `POST /api/v1/proposals/<id>/approve` — approve.
- `POST /api/v1/proposals/<id>/deny` — deny.

### Assistant

- `POST /api/v1/copilot/chat` — interactive chat turn.
- `GET /api/v1/copilot/history` — your conversation history.

### Clusters

- `GET /api/v1/clusters` — federated clusters.
- `POST /api/v1/clusters/invite` — admin: mint a bootstrap
  invite.

A full OpenAPI spec is at
`https://ops.example.com/openapi.json`. Live Swagger UI at
`https://ops.example.com/docs`.

---

## Patterns

A few that come up:

### CI declares a deploy

```bash
curl -X POST https://ops.example.com/api/v1/events \
  -H "Authorization: Bearer $DAALU_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"source":"github-actions","kind":"deploy.started","severity":"info","labels":{"service":"api","env":"prod"},"payload":{"commit":"abc"}}'
```

### Nightly sweep for stuck proposals

```python
import requests
TOKEN = os.environ["DAALU_TOKEN"]
r = requests.get("https://ops.example.com/api/v1/proposals?status=proposed&older_than=24h",
                 headers={"Authorization": f"Bearer {TOKEN}"})
for p in r.json()["items"]:
    print(p["id"], p["title"])
    # ping Slack about it
```

### Export every Daalu event to your warehouse

A workflow with an outbound webhook (Chapter 33) is the right
shape for this, but if you want to pull rather than push, the
events endpoint is paginated.

---

## Best practices

- **Store tokens in a secret store.** Not in code, not in
  config files committed to git. Vault, AWS Secrets Manager,
  GCP Secret Manager, Doppler — any of them.
- **One token per use case.** Don't share a single token
  across CI, ops scripts, and a personal one-off. The
  blast-radius on rotation is then per-use-case.
- **Watch the last-used column.** If a token hasn't been used
  in 60 days, you probably don't need it. Revoke.
- **Use admin-minted PATs for shared automation accounts.**
  See Chapter 6 — admin can mint on behalf of a service-style
  user.

---

## What about JWT?

The browser session uses JWTs. They're short-lived (1 hour by
default; refreshes silently). For scripting use, PATs are
strictly better:

- Don't expire mid-job.
- Rotatable independently.
- Recognizable by prefix.
- Revocable individually.

Use PATs.

---

## What's next

Part VIII covers support. Chapter 44 starts with
troubleshooting.
