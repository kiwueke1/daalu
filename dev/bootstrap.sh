#!/usr/bin/env bash
# Install Daalu on a local Kubernetes (kind) cluster with the Helm chart.
#
# This is Method 2 in docs/getting-started.md: the same chart and the same
# topology as a production install (API, workers, agent host, executor,
# frontend, Postgres, Redis and the WireGuard hub as Kubernetes workloads),
# on one machine. With docker, kind, kubectl and helm installed it takes
# about 10 minutes and ends with:
#
#   * Daalu:  http://localhost:8080  (and http://<host-ip>:8080; DAALU_HTTP_PORT)
#   * Login:  ADMIN_EMAIL / ADMIN_PASSWORD (printed at the end)
#   * Hub:    UDP 51820 on this machine, for clusters you add to Daalu
#
# Steps:
#   1. Create the kind cluster from dev/kind-cluster.yaml
#   2. Install ingress-nginx
#   3. Build the backend, frontend and WireGuard-hub images; load them into kind
#   4. Create the app Secret from .env (DB/Redis URLs rewritten in-cluster)
#   5. helm upgrade --install with dev/values-kind.yaml
#   6. Create the admin login
#
# Re-running is safe: the cluster is reused and the release upgraded.
#
# Env:
#   ADMIN_EMAIL      default admin@example.com
#   ADMIN_PASSWORD   default: random, printed at the end
#   DAALU_HOST_IP    address other clusters reach this machine on
#                    (default: first address from `hostname -I`)
#   DAALU_HTTP_PORT  host port for the UI + API (default 8080)
#   WITH_MONITORING  1 = also install kube-prometheus-stack into this cluster

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLUSTER_NAME="${CLUSTER_NAME:-daalu-dev}"
NAMESPACE="daalu-automation"
RELEASE="daalu-automation"
ENV_FILE="${ENV_FILE:-${ROOT}/.env}"
DAALU_HOST_IP="${DAALU_HOST_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
HTTP_PORT="${DAALU_HTTP_PORT:-8080}"

require() { command -v "$1" >/dev/null || { echo "Missing $1"; exit 1; }; }
for t in docker kind kubectl helm openssl python3; do require "$t"; done
[[ -n "$DAALU_HOST_IP" ]] || { echo "Set DAALU_HOST_IP to this machine's IP"; exit 1; }

echo "==> 1/6  Kind cluster '${CLUSTER_NAME}'"
if kind get clusters | grep -qx "${CLUSTER_NAME}"; then
  echo "    (cluster already exists, reusing it)"
else
  KIND_CFG="$(mktemp)"
  sed -e "s/listenAddress: \"0.0.0.0\"/listenAddress: \"${DAALU_HOST_IP}\"/" \
      -e "s/hostPort: 8080/hostPort: ${HTTP_PORT}/" \
    "${ROOT}/dev/kind-cluster.yaml" > "$KIND_CFG"
  kind create cluster --name "${CLUSTER_NAME}" --config "$KIND_CFG" --wait 5m
  rm -f "$KIND_CFG"
fi
kubectl config use-context "kind-${CLUSTER_NAME}" >/dev/null

echo "==> 2/6  ingress-nginx"
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.11.2/deploy/static/provider/kind/deploy.yaml
kubectl -n ingress-nginx wait --for=condition=ready pod \
  -l app.kubernetes.io/component=controller --timeout=300s

if [[ "${WITH_MONITORING:-0}" == "1" ]]; then
  echo "==> 2b    monitoring stack"
  bash "${ROOT}/deploy/k8s/monitoring/install.sh" dev
fi

echo "==> 3/6  Building images and loading them into kind"
docker build -t daalu-automation:local "${ROOT}"
docker build -t daalu-automation-frontend:local "${ROOT}/frontend"
docker build -t daalu-wg-hub:local "${ROOT}/deploy/wg-hub"
for img in daalu-automation:local daalu-automation-frontend:local daalu-wg-hub:local; do
  kind load docker-image "$img" --name "${CLUSTER_NAME}"
done

echo "==> 4/6  App secret"
kubectl get ns "$NAMESPACE" >/dev/null 2>&1 || kubectl create ns "$NAMESPACE"
TMP_ENV="$(mktemp)"; trap 'rm -f "$TMP_ENV"' EXIT
if [[ -f "$ENV_FILE" ]]; then
  # Same rewrite as deploy/scripts/install.sh: localhost DB/Redis → in-cluster.
  grep -Ev '^\s*(#|$)' "$ENV_FILE" \
    | grep -Ev '^(ADMIN_EMAIL|ADMIN_PASSWORD|DAALU_HOST_IP|AUTH_COOKIE_SECURE|CORS_ORIGINS|WIREGUARD_PUBLIC_ENDPOINT|OPERATOR_API_PUBLIC_URL)=' \
    | sed -E "s|@localhost:543[0-9]/|@daalu-postgres:5432/|g; s|redis://localhost:63[0-9]{2}/|redis://daalu-redis:6379/|g" \
    > "$TMP_ENV"
fi
# Keep an existing SECRET_KEY across re-runs: it encrypts stored credentials.
EXISTING_SECRET_KEY="$(kubectl -n "$NAMESPACE" get secret daalu-automation-secrets \
  -o jsonpath='{.data.SECRET_KEY}' 2>/dev/null | base64 -d 2>/dev/null || true)"
grep -q '^DATABASE_URL=' "$TMP_ENV" || cat >> "$TMP_ENV" <<'EOF'
DATABASE_URL=postgresql+asyncpg://daalu:daalu_password@daalu-postgres:5432/daalu_automation
DATABASE_SYNC_URL=postgresql://daalu:daalu_password@daalu-postgres:5432/daalu_automation
REDIS_URL=redis://daalu-redis:6379/0
CELERY_BROKER_URL=redis://daalu-redis:6379/0
CELERY_RESULT_BACKEND=redis://daalu-redis:6379/1
EOF
sed -i '/^SECRET_KEY=/d' "$TMP_ENV"
cat >> "$TMP_ENV" <<EOF
SECRET_KEY=${EXISTING_SECRET_KEY:-$(openssl rand -hex 32)}
AUTH_COOKIE_SECURE=false
WIREGUARD_PUBLIC_ENDPOINT=${DAALU_HOST_IP}
OPERATOR_API_PUBLIC_URL=http://${DAALU_HOST_IP}:${HTTP_PORT}
EOF
kubectl -n "$NAMESPACE" create secret generic daalu-automation-secrets \
  --from-env-file="$TMP_ENV" --dry-run=client -o yaml | kubectl apply -f -

echo "==> 5/6  helm upgrade --install ${RELEASE}"
HUB_ARGS=()
if ! kubectl -n "$NAMESPACE" get secret daalu-wireguard-peers -o jsonpath='{.data.wg0\.conf}' 2>/dev/null | grep -q .; then
  # First install: seed the hub keypair (the coordinator owns it afterwards).
  HUB_KEY="$(python3 -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode())')"
  HUB_ARGS=(--set "wireguard.hubPrivateKey=${HUB_KEY}")
fi
helm upgrade --install "$RELEASE" "${ROOT}/deploy/helm/daalu-automation" \
  -n "$NAMESPACE" -f "${ROOT}/dev/values-kind.yaml" \
  --set "config.corsOrigins=[\"http://localhost:${HTTP_PORT}\"\,\"http://${DAALU_HOST_IP}:${HTTP_PORT}\"]" \
  "${HUB_ARGS[@]}"
# Images are re-tagged :local on every build; restart so pods pick them up.
kubectl -n "$NAMESPACE" rollout restart deploy >/dev/null
kubectl -n "$NAMESPACE" rollout status deploy/daalu-api --timeout=600s
kubectl -n "$NAMESPACE" rollout status deploy/daalu-frontend --timeout=300s

echo "==> 6/6  Seed + admin login"
kubectl -n "$NAMESPACE" exec deploy/daalu-api -- daalu seed >/dev/null
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@example.com}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(openssl rand -hex 12)}"
kubectl -n "$NAMESPACE" exec deploy/daalu-api -- \
  daalu create-admin --email "${ADMIN_EMAIL}" --password "${ADMIN_PASSWORD}"

cat <<EOF

==> Done.
    Daalu:   http://localhost:${HTTP_PORT}   (from other machines: http://${DAALU_HOST_IP}:${HTTP_PORT})
    Login:   ${ADMIN_EMAIL} / ${ADMIN_PASSWORD}
    Pods:    kubectl -n ${NAMESPACE} get pods
    Hub:     UDP ${DAALU_HOST_IP}:51820 (clusters added in the UI dial this)

    Tear down: kind delete cluster --name ${CLUSTER_NAME}
EOF
