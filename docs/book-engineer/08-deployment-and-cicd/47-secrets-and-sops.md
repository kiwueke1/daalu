# 47. Secrets and SOPS

Daalu has three classes of secret it has to manage:

1. **Cluster-side env secrets** — the K8s Secret
   `daalu-automation-secrets` containing all app env vars
   (DB password, LLM API keys, etc.).
2. **Per-tenant secrets** — encrypted in Postgres via the
   per-tenant DEK scheme (Chapter 9).
3. **Infrastructure secrets** — Cloudflare API token,
   Alertmanager webhook URLs, GHCR pull tokens.

This chapter is about how we manage them at rest, in transit,
and in CI.

---

## Local-dev secrets

`.env` at the repo root. Gitignored. Created by copying
`dev/.env.example` and filling in values.

There is no expectation that local-dev secrets be production
secrets. Local-dev uses dummy keys for paid services where
possible.

---

## Cluster-side env secrets

`daalu-automation-secrets` is a K8s Secret that holds every
environment variable the app reads at startup. The keys are
upper-case, snake-case names matching the Pydantic settings
in `config.py`:

```
AUTH_SECRET=<random 32-byte hex>
DATABASE_URL=postgresql://daalu:<pw>@daalu-postgres.daalu-automation.svc:5432/daalu
REDIS_URL=redis://daalu-redis.daalu-automation.svc:6379/0
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
INGEST_API_KEY=<random>
CLOUDFLARE_API_TOKEN=<token>
SERVICE_TOKEN_SECRET_KEY=<random 32-byte hex>   # see note below
# ...
```

> **`SERVICE_TOKEN_SECRET_KEY` is required for hub↔controller auth.**
> daalu-api mints, and the controllers (nautobot / config-manager) verify,
> short-lived service tokens; `core/service_tokens.mint()` *raises* if this
> key is unset, so any controller-driven provisioning fails without it. It
> is easy to forget because it has an empty default in `config.py`. Add it
> once (shared by api + controllers, which all `envFrom` this Secret) — for a
> SOPS-managed secret: `sops set <path>/secrets.enc.yaml
> '["data"]["SERVICE_TOKEN_SECRET_KEY"]' "\"$(openssl rand -hex 32 |
> base64 -w0)\""`.

`install.sh` creates this Secret from `.env` via:

```bash
kubectl -n daalu-automation create secret generic daalu-automation-secrets \
  --from-env-file=$TMP_ENV \
  --dry-run=client -o yaml | kubectl apply -f -
```

The Helm chart's `templates/_helpers.tpl` defines `daalu.
envFrom` to reference this Secret:

```yaml
envFrom:
  - secretRef:
      name: {{ .Values.existingSecret }}
```

Every pod (api, workers, etc.) consumes the same Secret.

---

## SOPS

For secrets we want to commit (encrypted) to the repo, we
use [SOPS](https://github.com/getsops/sops) with AGE
encryption.

The pattern:

- `.sops.yaml` at the repo root configures the AGE recipients.
- Files like `deploy/k8s/monitoring/05-alertmanager-secrets.
  yaml` are SOPS-encrypted YAML.
- An ArgoCD plugin (or pre-apply hook) decrypts on deploy.

Example `.sops.yaml`:

```yaml
creation_rules:
  - path_regex: deploy/.*-secrets\.yaml$
    age: age1abc...,age1def...
```

The AGE private keys are held by:

- Each engineer who needs to encrypt new secrets.
- An out-of-cluster keyserver that the CD pipeline uses for
  decryption during deploy.

---

## Cloudflare DNS-01 token

Lives in cert-manager namespace as the K8s Secret
`cloudflare-api-token`. Set up out-of-band.

`install.sh` copies it to the daalu-automation namespace
(cert-manager v1.16's DNS-01 solver expects the token in
the challenge's namespace, not in cert-manager).

---

## GHCR pull secret

Created by `install.sh` from a GH token with `read:packages`
scope. Stored as the `ghcr` Secret in
`daalu-automation` namespace.

Token rotation: just re-run `install.sh` with a fresh
`gh auth refresh -s read:packages`.

---

## Executor JWT

The executor pod authenticates with a long-lived JWT
(Chapter 17). Issued by a setup script:

```bash
daalu admin issue-executor-jwt > .executor.jwt
kubectl -n daalu-automation create secret generic daalu-executor-jwt \
  --from-file=token=.executor.jwt \
  --dry-run=client -o yaml | kubectl apply -f -
```

Rotation: regenerate, apply, restart the executor
Deployment. Old token is invalidated when its `exp` claim
hits, or immediately if we rotate `AUTH_SECRET` (which all
tokens are signed with).

---

## Per-tenant secrets in Postgres

Per-tenant encryption uses an AES-GCM scheme:

```python
# core/secrets.py
def _tenant_key(tenant_id: UUID) -> bytes:
    # Derive per-tenant key from KEK + tenant_id
    return HKDF(kek, salt=tenant_id.bytes, info=b"daalu-tenant-secret", length=32)

def encrypt_for_tenant(plaintext: bytes, tenant_id: UUID) -> bytes:
    key = _tenant_key(tenant_id)
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad=tenant_id.bytes)
    return nonce + ct

def decrypt_for_tenant(encrypted: bytes, tenant_id: UUID) -> bytes:
    key = _tenant_key(tenant_id)
    nonce, ct = encrypted[:12], encrypted[12:]
    return AESGCM(key).decrypt(nonce, ct, aad=tenant_id.bytes)
```

The KEK is the `SECRETS_KEK` env var (32 bytes hex). Rotating
it requires re-encrypting every per-tenant secret in the
database — there's a `daalu admin rotate-kek` CLI for this,
not used routinely.

---

## What we don't have

- **HashiCorp Vault.** Considered. Adds an operational
  dependency we don't yet need at our scale.
- **K8s External Secrets Operator.** Considered. The
  SOPS+AGE flow has been enough.
- **Per-engineer secret access auditing.** Limited to the
  AGE key holder list and git history.

These come on the Enterprise track.

---

## What's next

Part IX is the operations runbooks. Chapter 49 — common
incidents and their fixes.
