# 51. Tenant operations

Things you do to or for specific tenants: seeding, deleting,
rotating keys, exporting data.

These are not routine. Each is a controlled operation with a
checklist.

---

## Creating a tenant manually

The normal flow is self-serve sign-up. Manual creation
(for hands-on onboarding, demos, etc.):

```bash
daalu admin create-tenant \
  --name "Acme Robotics" \
  --admin-email admin@acme.io \
  --admin-name "Acme Admin"
```

Generates a one-shot login link, prints it. The admin sets
their password on first login.

---

## Seeding demo data

For demo / staging tenants:

```bash
daalu seed-demo --tenant-id <id>
```

Writes synthetic alerts, events, mock cloud accounts, an
example device fleet. Idempotent — repeated runs adjust to
match the target shape rather than duplicating.

---

## Exporting a tenant's data

For a customer asking under GDPR or a paid export:

```bash
daalu admin export-tenant --tenant-id <id> --output tenant-<id>.json.gz
```

Generates a JSON archive of: users, integrations (with
credentials masked), events, alerts, proposals, audit logs.

Note: credentials are intentionally masked — exporting
plaintext API keys is a separate operation that requires
a documented request.

---

## Deleting a tenant

Hard delete:

```bash
daalu admin delete-tenant --tenant-id <id> --confirm
```

What this does:

- Cascades to all tenant-scoped tables.
- Revokes all PATs.
- Disables all integrations (no last-gasp probe).
- Removes from the WireGuard hub (if any tunnels).
- Removes Stripe subscription (if any).
- Logs the action with the operator's email.

There is no soft-delete recovery beyond 30 days. The
self-serve flow gives users a 30-day grace; this admin
command bypasses it. Use carefully.

---

## Rotating the executor JWT

The executor's JWT is long-lived. Rotation:

```bash
daalu admin issue-executor-jwt > /tmp/new-jwt
kubectl -n daalu-automation create secret generic daalu-executor-jwt \
  --from-file=token=/tmp/new-jwt \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n daalu-automation rollout restart deploy/daalu-executor
shred -u /tmp/new-jwt
```

Old JWT becomes invalid when the executor pod restarts (it
only knows the new token from its mounted Secret).

---

## Rotating AUTH_SECRET

This is the JWT signing secret. Rotating it invalidates
every user's session and PAT.

```bash
# 1. Generate new
NEW_AUTH=$(openssl rand -hex 32)

# 2. Update .env
sed -i "s/^AUTH_SECRET=.*/AUTH_SECRET=$NEW_AUTH/" .env

# 3. Re-run install.sh
bash deploy/scripts/install.sh
```

Users will see "session expired" on next page load and need
to log in again. PATs will need re-issue.

Schedule this maintenance window with a "passwords invalid for
30 minutes" notice to users.

---

## Rotating the SECRETS_KEK

The per-tenant secret KEK is rotated by re-encrypting every
DEK with the new KEK. We have a CLI for this:

```bash
daalu admin rotate-kek --old <old-kek> --new <new-kek>
```

It iterates every tenant, decrypts each tenant's DEK with
the old KEK, re-encrypts with the new, commits.

This is online — no downtime — but slow on big tenants.

---

## Per-tenant troubleshooting

When a customer reports an issue:

```bash
# Get the tenant's slug
daalu admin lookup --email <user-email>

# See the tenant's recent activity
daalu admin tenant-summary --tenant-id <id>

# Tail logs filtered to this tenant
kubectl logs deploy/daalu-api --tail=200 | grep <tenant-id>

# Open a shell as if logged in as the tenant's admin
daalu admin shell --tenant-id <id>
```

The shell is the most useful debugging tool — it puts you
in an IPython with a session scoped to the tenant.

---

## Audit

Every admin command writes to the **platform audit log**
(separate from per-tenant audit logs). Includes:

- Operator email.
- Command.
- Arguments.
- Result.
- Timestamp.

Stored in Postgres in the `platform_audit_logs` table.
Surfaced only to superusers.

---

## What's next

Chapter 52 — image pulls and GHCR-specific quirks.
