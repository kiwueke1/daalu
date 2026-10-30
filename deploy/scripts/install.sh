#!/usr/bin/env bash
#
# Bootstrap a fresh daalu-automation install on a Kubernetes cluster.
#
# Idempotent — re-running this script just upgrades the Helm release.
# Assumes:
#   * KUBECONFIG points at the target cluster
#   * gh CLI is authenticated with a token that can pull from GHCR
#   * .env exists at the repo root with production-grade values
#
# Usage:
#   REGISTRY=registry.example.com/daalu IMAGE_TAG=v0.1.0 HOST=daalu.example.com \
#     ./deploy/scripts/install.sh
#
# Override the image tag (defaults to the current git SHA):
#   IMAGE_TAG=ca5ae81... ./deploy/scripts/install.sh
#
# Use a different namespace / host:
#   NAMESPACE=daalu-staging HOST=ops-staging.daalu.io ./deploy/scripts/install.sh

set -euo pipefail

NAMESPACE="${NAMESPACE:-daalu-automation}"
RELEASE="${RELEASE:-daalu-automation}"
HOST="${HOST:-daalu.example.com}"
# Match the path filter in .github/workflows/build-images.yml — only
# those paths trigger an image build, so HEAD may not have a pushed
# tag (e.g. docs-only or helm-only commits). Walk back to the last
# commit that would have produced an image.
IMAGE_TAG="${IMAGE_TAG:-$(git log -1 --format=%H -- \
  src frontend Dockerfile frontend/Dockerfile \
  deploy/wg-hub deploy/edge pyproject.toml alembic.ini migrations \
  .github/workflows/build-images.yml)}"
ENV_FILE="${ENV_FILE:-.env}"
CHART_DIR="${CHART_DIR:-$(dirname "$0")/../helm/daalu-automation}"
GHCR_SECRET_NAME="${GHCR_SECRET_NAME:-ghcr}"
# Self-hosted images: set REGISTRY (e.g. registry.example.com/daalu) to skip the
# GHCR pull-secret + GitHub Actions checks and pull from your own registry.
# PULL_SECRET names an existing docker-registry Secret if the registry is private.
REGISTRY="${REGISTRY:-}"
APP_SECRET_NAME="${APP_SECRET_NAME:-daalu-automation-secrets}"

bold() { printf "\033[1;36m== %s ==\033[0m\n" "$*"; }
warn() { printf "\033[1;33m!! %s\033[0m\n" "$*" >&2; }

if [[ ! -f "$ENV_FILE" ]]; then
  warn "$ENV_FILE not found — copy .env.example and fill in values first."
  exit 1
fi

bold "Target cluster"
kubectl config current-context
kubectl cluster-info | head -1

bold "Namespace: $NAMESPACE"
kubectl get ns "$NAMESPACE" >/dev/null 2>&1 || kubectl create ns "$NAMESPACE"

bold "Cloudflare DNS-01 token (copy into namespace for cert-manager)"
# cert-manager v1.16's Cloudflare DNS-01 solver reads the API-token secret
# from the *challenge's* namespace, not from --cluster-resource-namespace.
# If we don't copy it here, the TLS challenge sits in pending forever
# with `secrets "cloudflare-api-token" not found` (we lost ~4h to this on
# 2026-05-13). Idempotent — kubectl apply just no-ops if it's already there.
if kubectl -n cert-manager get secret cloudflare-api-token >/dev/null 2>&1; then
  kubectl -n cert-manager get secret cloudflare-api-token -o yaml \
    | grep -v -E '^\s+(namespace|resourceVersion|uid|creationTimestamp):' \
    | sed "s/namespace: cert-manager/namespace: ${NAMESPACE}/" \
    | kubectl apply -n "$NAMESPACE" -f -
else
  warn "cert-manager/cloudflare-api-token not found — TLS issuance will fail until you create it."
fi

if [[ -z "$REGISTRY" ]]; then
bold "GHCR pull secret: $GHCR_SECRET_NAME"
GH_USER="${GH_USER:-$(gh api user --jq .login)}"
# The token MUST carry read:packages — otherwise the pull secret looks
# valid but silently fails for any image not already cached on the node
# (manifests as ImagePullBackOff on every newly published package, while
# old cached images still "work"). Lost ~3h to this on 2026-05-17.
#
# Resolution order:
#   1. Explicit GH_TOKEN env var (operator's choice — trust them).
#   2. `gh auth token` with GITHUB_TOKEN unset, because env-var tokens
#      injected by CI runners or shell rc files (often `repo`-scoped
#      only) take precedence over the OAuth login that has the right
#      scopes. Stripping the env var picks the gh-managed token.
#   3. Last resort: whatever `gh auth token` returns with env vars
#      intact — usually wrong but lets us produce a clear error below.
GH_TOKEN="${GH_TOKEN:-$(GITHUB_TOKEN= gh auth token 2>/dev/null || gh auth token)}"
if [[ -z "$GH_USER" || -z "$GH_TOKEN" ]]; then
  warn "Cannot resolve GitHub user/token — set GH_USER + GH_TOKEN env vars manually."
  exit 1
fi
# Verify the token actually has read:packages by reading its scopes
# from the X-OAuth-Scopes response header. write:packages implies read,
# so either is acceptable.
scope_header=$(curl -sSI -H "Authorization: Bearer $GH_TOKEN" \
  https://api.github.com/user | tr -d '\r' | grep -i '^x-oauth-scopes:' || true)
if ! printf '%s' "$scope_header" | grep -qE '(read:packages|write:packages)'; then
  warn "The GH token in use does not have read:packages scope."
  warn "Scopes seen: ${scope_header:-<none>}"
  warn "Fix one of:"
  warn "  - gh auth refresh -h github.com -s read:packages   (then re-run)"
  warn "  - export GH_TOKEN=<a PAT with read:packages>       (then re-run)"
  warn "  - unset GITHUB_TOKEN                               (if your shell injects a narrow one)"
  exit 1
fi
# `kubectl create secret docker-registry` with --dry-run lets us apply
# (re-)create idempotently.
kubectl -n "$NAMESPACE" create secret docker-registry "$GHCR_SECRET_NAME" \
  --docker-server=ghcr.io \
  --docker-username="$GH_USER" \
  --docker-password="$GH_TOKEN" \
  --docker-email="$GH_USER@users.noreply.github.com" \
  --dry-run=client -o yaml | kubectl apply -f -
fi

bold "App secret from $ENV_FILE: $APP_SECRET_NAME"
# Strip empty/commented lines AND rewrite local-dev DB/Redis URLs to
# the in-cluster service names. Without this, the API alembic init
# container would dial localhost inside the pod and crash.
TMP_ENV=$(mktemp)
trap 'rm -f "$TMP_ENV"' EXIT
grep -Ev '^\s*(#|$)' "$ENV_FILE" \
  | sed -E "s|@localhost:543[23]/|@daalu-postgres.${NAMESPACE}.svc.cluster.local:5432/|g" \
  | sed -E "s|redis://localhost:638[01]/|redis://daalu-redis.${NAMESPACE}.svc.cluster.local:6379/|g" \
  | sed -E "s|redis://localhost:6379/|redis://daalu-redis.${NAMESPACE}.svc.cluster.local:6379/|g" \
  > "$TMP_ENV"
kubectl -n "$NAMESPACE" create secret generic "$APP_SECRET_NAME" \
  --from-env-file="$TMP_ENV" \
  --dry-run=client -o yaml | kubectl apply -f -

if [[ -z "$REGISTRY" ]]; then
bold "Verify build-images workflow succeeded for $IMAGE_TAG"
# Catches the second ImagePullBackOff failure mode: a buildable commit
# was pushed but the build-images workflow hasn't finished yet (or
# failed), so the SHA tag isn't on GHCR. We ask the Actions API
# directly instead of probing GHCR — GHCR returns 403 for both
# "missing tag" and "insufficient scope", so it can't tell us apart.
# The Actions check works with the standard `repo` scope every gh
# install has.
run_info=$(gh run list --workflow=build-images.yml --commit="$IMAGE_TAG" \
  --limit 1 --json status,conclusion 2>/dev/null || echo "[]")
run_status=$(printf '%s' "$run_info" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p')
run_conclusion=$(printf '%s' "$run_info" | sed -n 's/.*"conclusion":"\([^"]*\)".*/\1/p')
if [[ "$run_info" == "[]" || -z "$run_status" ]]; then
  warn "No build-images workflow run found for commit $IMAGE_TAG."
  warn "Has it been pushed to origin? (git push) The image won't exist on GHCR until CI builds it."
  exit 1
elif [[ "$run_status" != "completed" ]]; then
  warn "build-images workflow for $IMAGE_TAG is still '$run_status' — wait for it to finish."
  warn "Watch with: gh run watch --workflow=build-images.yml"
  exit 1
elif [[ "$run_conclusion" != "success" ]]; then
  warn "build-images workflow for $IMAGE_TAG ended with conclusion '$run_conclusion' — no image was pushed."
  warn "Re-run the workflow or commit a fix: gh run rerun --failed"
  exit 1
fi
echo "  workflow run: completed/success"
fi

bold "Helm release: $RELEASE  (chart: $CHART_DIR)"
if [[ -n "$REGISTRY" ]]; then
  IMAGE_ARGS=(--set "image.registry=$REGISTRY" --set "image.pullSecret=${PULL_SECRET:-}"
              --set "wireguard.image=$REGISTRY/daalu-wg-hub:$IMAGE_TAG")
else
  IMAGE_ARGS=(--set "image.pullSecret=$GHCR_SECRET_NAME")
fi
helm upgrade --install "$RELEASE" "$CHART_DIR" \
  --namespace "$NAMESPACE" \
  --set "ingress.host=$HOST" \
  --set "image.backend.tag=$IMAGE_TAG" \
  --set "image.frontend.tag=$IMAGE_TAG" \
  "${IMAGE_ARGS[@]}" \
  --set "existingSecret=$APP_SECRET_NAME" \
  --wait --timeout 5m

bold "Rollout status"
for d in daalu-api daalu-frontend daalu-worker daalu-beat daalu-agents; do
  kubectl -n "$NAMESPACE" rollout status "deploy/$d" --timeout=3m || true
done

bold "Pods"
kubectl -n "$NAMESPACE" get pods -o wide

bold "Ingress"
kubectl -n "$NAMESPACE" get ingress

cat <<EOF

Daalu Automation is up.

  URL        : https://$HOST  (TLS provisioned by cert-manager — first
                                 issuance takes ~60s)
  Namespace  : $NAMESPACE
  Image tag  : $IMAGE_TAG

Next:
  1. Point an A/AAAA record for $HOST at the cluster's ingress IP.
     (Cloudflare → proxied is fine; cert-manager uses HTTP-01.)
  2. Seed the default tenant once:
     kubectl -n $NAMESPACE exec deploy/daalu-api -- daalu seed
EOF
