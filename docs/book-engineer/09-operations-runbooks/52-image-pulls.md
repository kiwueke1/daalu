# 52. Image pulls and GHCR

We use GitHub Container Registry (GHCR) for images. The
pull pattern has bitten us multiple times. This chapter is
the everything-you-need-to-know reference.

---

## The pattern

- Backend image: `ghcr.io/kiwueke1/daalu-automation:<sha>`.
- Frontend image: `ghcr.io/kiwueke1/daalu-automation-frontend:<sha>`.
- daalu-edge: `ghcr.io/kiwueke1/daalu-edge:<sha>`.
- daalu-vllm: `ghcr.io/kiwueke1/daalu-vllm:<sha>`.

Images are pushed by CI on every commit that touches build
paths. The `<sha>` tag is the git commit SHA. The `latest`
tag tracks main.

---

## Pull secret

The `ghcr` Secret of type `kubernetes.io/dockerconfigjson`
in each namespace that pulls Daalu images.

Created by `install.sh`:

```bash
kubectl -n "$NAMESPACE" create secret docker-registry "$GHCR_SECRET_NAME" \
  --docker-server=ghcr.io \
  --docker-username="$GH_USER" \
  --docker-password="$GH_TOKEN" \
  --docker-email="$GH_USER@users.noreply.github.com" \
  --dry-run=client -o yaml | kubectl apply -f -
```

Both Deployments and Pods reference it via `spec.
imagePullSecrets`.

---

## The read:packages scope trap

This is the single thing we lose the most time on.

A GH token (PAT or `gh auth token`-derived) might have
`repo` scope but not `read:packages`. Both are fine for git
operations. For pulling private GHCR images, **only**
`read:packages` works. With a `repo`-only token:

- The image manifest URL returns 403.
- Kubernetes reports ImagePullBackOff.
- The error in `kubectl describe pod` is "unauthorized" —
  *not* "scope missing" — making it look like the token is
  wrong rather than wrongly-scoped.

The trap: a token can fail open. If the image was previously
cached on the node, the pod starts fine. New pulls fail
silently.

### Diagnosis

```bash
# Check the token's scopes
curl -sI -H "Authorization: Bearer $GH_TOKEN" https://api.github.com/user \
  | grep -i x-oauth-scopes
```

Look for `read:packages` or `write:packages`. Both work for
pull.

### Fix

```bash
gh auth refresh -h github.com -s read:packages
# Or for a PAT:
gh auth login --with-token <new-pat-with-read-packages>
```

Then re-run `install.sh`.

---

## Tag pinning vs `latest`

Production deploys always pin to a specific SHA, never to
`latest`. Reasons:

- `latest` changes invisibly when CI updates it.
- Rollback needs a specific SHA.
- Two replicas during a rolling deploy could land on
  different image versions if one resolves `latest` before
  the new one is uploaded.

The `install.sh` --set pattern sets `image.backend.tag=<sha>`
explicitly.

---

## Rate limits

GHCR has anonymous pull rate limits but authenticated pulls
are higher. Our authenticated pulls don't hit them.

---

## Image vulnerability scanning

CI runs `docker scout`. HIGH/CRITICAL findings are flagged
on the PR. Trivy in a follow-on job double-checks.

Findings are not always actionable (base-image CVEs in
sub-deps we can't update), but we triage them.

---

## Mirrors

For air-gapped or restrictive environments, we have an option
to mirror images to a customer-side registry:

```bash
daalu admin mirror-images --registry registry.acme.io
```

Pulls every Daalu image and re-tags + pushes to the
customer's registry. The customer's Helm values override
the `image.*.repository` keys to point at the mirror.

Not currently shipped in v1; in progress.

---

## What's next

Chapter 53 — cert-manager, TLS, and DNS.
