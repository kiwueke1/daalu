# 58. Secrets management

Chapter 47 covered the mechanical "where each secret lives."
This chapter is the lifecycle: how we generate, rotate, audit,
and destroy secrets.

---

## Categories

1. **Platform-side static secrets** — AUTH_SECRET, SECRETS_KEK,
   Stripe webhook secret, GH pull token.
2. **Tenant-side dynamic secrets** — integration API tokens,
   device passwords, webhook HMAC keys.
3. **Customer-issued secrets** — PATs.

Each has its own lifecycle.

---

## Static secrets

| Secret | Where stored | Rotation cadence | Notes |
|---|---|---|---|
| AUTH_SECRET | k8s Secret | Yearly | Invalidates all JWTs and PATs |
| SECRETS_KEK | k8s Secret | Yearly | Re-encrypts all per-tenant DEKs |
| Postgres password | k8s Secret | Quarterly | Update in DB + Secret |
| Redis password | k8s Secret | Quarterly | Same |
| Stripe API key | k8s Secret | Yearly | Rotate in Stripe + Secret |
| GHCR pull token | k8s Secret | Yearly | gh auth refresh -s read:packages |
| Cloudflare API token | k8s Secret | Yearly | Scoped narrowly |
| Anthropic API key | k8s Secret | Yearly | If billing is on Daalu's account |
| OpenAI API key | k8s Secret | Yearly | Same |

Rotation procedure for each documented in the runbooks.

---

## Tenant-side dynamic secrets

When a customer connects an integration:

```
Customer enters token in UI
  → API receives over TLS
  → encrypted with per-tenant DEK
  → stored in integration.credentials_encrypted column
  → plaintext memory zeroized immediately after encrypt
```

Decryption only happens in process at call time. We never
log the plaintext (structlog has a blocklist of fields
named `*_token`, `*_password`, `credentials_*`, `secret_*`).

Rotation: customer-driven. UI has a "Rotate credential"
button per integration that prompts for a new value and
re-encrypts. The old value is overwritten in place.

---

## PATs

Generation:

- 24 bytes of entropy from `secrets.token_urlsafe`.
- Prefixed `dpat_` for visibility.
- Cleartext shown exactly once; only sha256-hex stored.

Rotation:

- "Rotate" button on the token gives a new cleartext.
- Old token continues to work for 24 hours.
- Tokens auto-expire if `expires_at` set.

Revocation:

- "Revoke" sets `revoked_at = now`.
- Effective immediately on next request.

Hard-delete:

- 7 days after revocation, a background job hard-deletes the
  row. Until then, reactivation is possible.

---

## Memory hygiene

Where we can, we zeroize secret bytes after use:

```python
def encrypt_for_tenant(plaintext: bytes, tenant_id: UUID) -> bytes:
    try:
        key = _tenant_key(tenant_id)
        ...
        return ct
    finally:
        # zero out the key reference (limited utility in
        # Python but worth doing where memory pressure is a
        # concern)
        del key
```

Python's GC and short-lived bytes objects mean this isn't a
strong guarantee. For threat models where it matters
(government clients), we run with `PYTHONHASHSEED=random`
and recommend memory-locked OS configurations.

---

## Backups

Backups of Postgres include the encrypted secrets. They're
encrypted at rest by the backup target (S3-compatible with
KMS). The KEK is **not** in the backup — secrets in a
backup can only be decrypted with the KEK held separately.

This means a backup leak alone doesn't expose secrets.
It's a deliberate two-store architecture.

---

## Audit

Access to secrets is logged:

- API decryption of a tenant credential → audit row.
- Admin shell access → platform audit row.
- KEK rotation → platform audit row.

Audit logs are append-only. The compliance team reviews
quarterly.

---

## Detecting leaks

- **Git pre-commit hook** runs `gitleaks` to catch
  accidental secret commits.
- **CI runs `trufflehog`** on every PR for the same purpose.
- **GitHub secret scanning** is on for the repo; it
  catches PATs that match our `dpat_` regex.
- **DLP scanning** on logs from production filters out
  patterns that match secret shapes.

When a leak is found:

1. Rotate the leaked secret immediately.
2. Audit recent activity for misuse.
3. File an incident in the security tracker.
4. Post-mortem in the next security sync.

---

## What's next

Part XI starts the testing chapters. Chapter 59 — test
layout.
