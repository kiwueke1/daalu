# 55. Threat model

This chapter is the structured "what could go wrong" view.
We use it to drive design decisions and to brief security
reviewers.

The format follows STRIDE — Spoofing, Tampering, Repudiation,
Information disclosure, Denial of service, Elevation of
privilege.

---

## Assets

What we're protecting:

1. **Customer data at rest.** Alerts, events, integration
   credentials, audit logs, change-proposal history.
2. **Customer data in transit.** API calls, federation
   traffic, LLM inference round trips.
3. **The integrity of write operations.** A change proposal
   approved by a user must execute exactly that change.
4. **Tenant isolation.** No tenant sees another tenant's data
   or actions.
5. **Daalu's operational secrets.** The KEK, AUTH_SECRET,
   GHCR pull token, Cloudflare API token.

---

## Threat actors

- **External attacker.** Internet, no credentials.
- **Authenticated user, wrong tenant.** Valid Daalu account
  trying to access another tenant.
- **Authenticated user, same tenant.** Valid account trying
  to escalate privileges (non-admin → admin actions).
- **Malicious Daalu engineer.** Insider with platform access.
- **Compromised customer cluster.** Edge pod or vLLM pod
  compromised from inside the customer's network.

---

## STRIDE — each axis

### Spoofing

| Threat | Mitigation |
|---|---|
| Forged JWT | HS256 with strong AUTH_SECRET; signature check at gate and current_user |
| Forged PAT | sha256-hashed lookup; high entropy (192 bits) |
| Forged HMAC on webhook | Per-tenant secret, constant-time compare |
| Forged WireGuard peer | Public-key cryptography; bootstrap token single-use |

### Tampering

| Threat | Mitigation |
|---|---|
| In-flight modification of API request | TLS 1.3 |
| In-flight modification of federation tunnel | WireGuard auth + encryption |
| Database row tampered with | DB locked down by Postgres auth; data not interactively modifiable from the network |
| Audit log tampered with | Append-only by application contract; superuser DB access required to modify |

### Repudiation

| Threat | Mitigation |
|---|---|
| User denies they approved a change | Per-row audit log records actor, timestamp, request-id |
| Agent denies it proposed | Same |
| Daalu engineer denies access | Platform audit log records every admin command |

### Information disclosure

| Threat | Mitigation |
|---|---|
| Wrong-tenant reads | Every query filters by tenant_id; tested |
| LLM leakage | Prompt context is tenant-scoped; LLM provider doesn't see other tenants |
| Pod logs leak credentials | structlog never logs secret values; we audit log output |
| Backups expose data | Backups encrypted at rest; retention bounded |
| Browser XSS reads JWT | HttpOnly cookie; no JS access |

### Denial of service

| Threat | Mitigation |
|---|---|
| API flood | Per-PAT and per-IP rate limits at the gateway |
| Cloud LLM bill explosion | Per-tenant token quotas + alarms |
| Reconciler runaway | Concurrency limits + exponential backoff |
| Worker queue overflow | Redis broker memory limit; oldest tasks rejected |
| Federation tunnel exhaustion | Per-tenant subnet caps |

### Elevation of privilege

| Threat | Mitigation |
|---|---|
| User mints admin PAT | Only admins can mint others'; UI gates |
| Non-admin self-approve | Four-eyes rule at API |
| Worker pool runs write tool | Queue routing separates executor; JWT scope gate inside tools |
| Approved proposal modified | Status check at execute-time; non-approved is rejected |
| Daalu engineer assumes user | Platform-side actions logged separately; can't impersonate without an explicit shell command (which is logged) |

---

## Out-of-scope

A few threats we explicitly don't mitigate:

- **Compromised customer laptop/browser.** If a user's session
  is hijacked, we can't fix it. We mitigate impact (HttpOnly
  cookies, short TTLs, audit logs to detect after the fact).
- **Subverted CI/CD.** If our GitHub Actions is taken over,
  a malicious deploy is possible. We mitigate with signed
  commits, branch protection, and review requirements.
- **Physical access to the hosting hardware.** Disk encryption at
  rest mitigates passive access; active tampering is in
  the hosting provider's scope.
- **Quantum computing breaking our crypto.** We use AES-256
  and HS256 today; will migrate to post-quantum when stable
  algorithms ship.

---

## Pen test cadence

External pen test once a year by a reputable firm. Internal
red team exercises quarterly. Findings tracked in a
private GitHub repo.

---

## What's next

Chapter 56 — auth flows in depth.
