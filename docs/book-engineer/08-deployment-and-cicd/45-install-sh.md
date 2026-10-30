# 45. install.sh and rollout mechanics

`deploy/scripts/install.sh` is the operational rollout
script. Despite the name, it's not a one-shot installer —
it's the canonical "upgrade the cluster to a new image
tag" command. Re-running it is the deploy operation.

This chapter explains what it does, in order, and where its
behaviour might surprise you.

---

## The signature

```bash
bash deploy/scripts/install.sh
```

Environment knobs:

```bash
NAMESPACE=daalu-automation        # default
RELEASE=daalu-automation          # default
HOST=ops.example.com                 # default
IMAGE_TAG=<sha>                   # default: walks back from HEAD
ENV_FILE=.env                     # default
```

The defaults match production. Override for staging or for
pinning a specific image.

---

## What it does, step by step

### 1. Sanity checks

Verifies kubeconfig is set, current context is what was
expected, .env exists. Bails early if anything's off.

### 2. Namespace

Creates `daalu-automation` namespace if absent. Idempotent.

### 3. cert-manager DNS-01 secret

Copies the Cloudflare API-token secret from `cert-manager`
namespace to `daalu-automation`:

```bash
kubectl -n cert-manager get secret cloudflare-api-token -o yaml \
  | sed "s/namespace: cert-manager/namespace: ${NAMESPACE}/" \
  | kubectl apply -n "$NAMESPACE" -f -
```

This is because cert-manager v1.16's DNS-01 solver reads the
token secret from the *challenge's* namespace, not from
`--cluster-resource-namespace`.

### 4. GHCR pull secret

Creates a `kubernetes.io/dockerconfigjson` Secret for pulling
images from GHCR.

This step is finicky:

- Reads GitHub user and token from gh CLI.
- Validates the token has `read:packages` scope by hitting
  the GitHub API and parsing the `X-OAuth-Scopes` header.
- Bails if scope is missing — we've burned hours on
  ImagePullBackOff caused by silently-wrong-scope tokens.

### 5. App secret from .env

Creates the `daalu-automation-secrets` Secret from `.env`,
with URL rewriting for local-dev URLs:

```bash
grep -Ev '^\s*(#|$)' "$ENV_FILE" \
  | sed -E "s|@localhost:5432/|@daalu-postgres.${NAMESPACE}.svc.cluster.local:5432/|g" \
  | sed -E "s|redis://localhost:6379/|redis://daalu-redis.${NAMESPACE}.svc.cluster.local:6379/|g" \
  > "$TMP_ENV"
```

The rewriting catches a common dev-vs-prod mistake.

### 6. Image-tag guardrail

Walks back from HEAD to the last commit that touched
build-relevant paths (the `paths:` filter from
`.github/workflows/build-images.yml`):

```bash
IMAGE_TAG="${IMAGE_TAG:-$(git log -1 --format=%H -- \
  src frontend Dockerfile frontend/Dockerfile \
  deploy/wg-hub deploy/edge pyproject.toml alembic.ini \
  migrations .github/workflows/build-images.yml)}"
```

Then verifies the build-images workflow succeeded for that
SHA via the GitHub Actions API:

```bash
gh run list --workflow=build-images.yml --commit="$IMAGE_TAG" \
  --limit 1 --json status,conclusion
```

Bails if the workflow hasn't run or didn't succeed. This
catches "the chart change is committed but CI hasn't built
the image yet" — the most common rollout failure mode.

### 7. Helm upgrade --install

The actual deploy:

```bash
helm upgrade --install "$RELEASE" "$CHART_DIR" \
  --namespace "$NAMESPACE" \
  --set "ingress.host=$HOST" \
  --set "image.backend.tag=$IMAGE_TAG" \
  --set "image.frontend.tag=$IMAGE_TAG" \
  --set "image.pullSecret=$GHCR_SECRET_NAME" \
  --set "existingSecret=$APP_SECRET_NAME" \
  --wait --timeout 5m
```

`--wait` blocks until all rollouts complete (or timeout).

### 8. Rollout status

After helm completes, the script polls rollouts of the
critical Deployments:

```bash
for d in daalu-api daalu-frontend daalu-worker daalu-beat daalu-agents; do
  kubectl -n "$NAMESPACE" rollout status "deploy/$d" --timeout=3m
done
```

### 9. Summary

Prints the live image tags, the host, the next-step commands
for seeding.

---

## Why a script, not pure ArgoCD?

ArgoCD's `daalu-automation` Application is currently
misconfigured (recurse:false + /** glob = no-op; see Chapter
46). Even if we fix it, the install.sh layer adds value:

- The pre-flight checks (cert-manager secret, GHCR scope,
  build-images verification) aren't naturally ArgoCD jobs.
- The image-tag resolution from git history is a step
  that's bespoke to our workflow.
- The .env-to-Secret transform is opinionated and easier
  to read in shell than in a sync hook.

The plan is to keep install.sh and have ArgoCD watch only
secondary applications (monitoring config, GPU workloads,
custom rules).

---

## Common failure modes

- **"No build-images workflow run found"** — image SHA not
  built yet. Push, wait for CI, retry.
- **"GHCR token missing read:packages"** — refresh gh auth
  or set GH_TOKEN explicitly.
- **"helm: chart not found"** — bad CHART_DIR. Check
  `deploy/helm/daalu-automation`.
- **"rollout timed out"** — pod stuck. `kubectl describe
  pod ...` reveals why; often ImagePullBackOff or
  CrashLoopBackOff.

---

## What's next

Chapter 46 — ArgoCD applications, including the ones that
are broken.
