#!/usr/bin/env bash
# Bring up a complete local Daalu environment on Kind.
#
# This is the "from scratch" path — on a fresh laptop with docker +
# kind + kubectl + helm installed, ~10 minutes from `bash bootstrap.sh`
# to a working stack at:
#
#   * App:      http://localhost:8080
#   * Grafana / Prometheus: port-forward commands printed at the end
#   * Prometheus port-forwarded to :9090 on demand
#
# What it does:
#   1. Creates a Kind cluster from dev/kind-cluster.yaml
#   2. Installs ingress-nginx (Traefik would also work)
#   3. Installs the monitoring stack via deploy/k8s/monitoring/install.sh dev
#   4. Builds the daalu-api + frontend images locally and loads them
#      into the Kind nodes (no registry push needed)
#   5. Applies app manifests with image tags pointed at the loaded
#      images
#   6. Runs alembic + seeds the default tenant + SKUs

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLUSTER_NAME="daalu-dev"

require() { command -v "$1" >/dev/null || { echo "Missing $1"; exit 1; }; }
for t in docker kind kubectl helm; do require "$t"; done

echo "==> 1/6  Creating Kind cluster '${CLUSTER_NAME}'"
if kind get clusters | grep -qx "${CLUSTER_NAME}"; then
  echo "    (cluster already exists — reusing)"
else
  kind create cluster --config "${ROOT}/dev/kind-cluster.yaml" --wait 5m
fi

echo "==> 2/6  Installing ingress-nginx"
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.11.2/deploy/static/provider/kind/deploy.yaml
kubectl -n ingress-nginx wait --for=condition=ready pod \
  -l app.kubernetes.io/component=controller --timeout=180s

echo "==> 3/6  Installing monitoring stack (dev mode)"
bash "${ROOT}/deploy/k8s/monitoring/install.sh" dev

echo "==> 4/6  Building app images"
docker build -t daalu-automation:dev "${ROOT}"
docker build -t daalu-automation-frontend:dev "${ROOT}/frontend"
kind load docker-image daalu-automation:dev          --name "${CLUSTER_NAME}"
kind load docker-image daalu-automation-frontend:dev --name "${CLUSTER_NAME}"

echo "==> 5/6  Applying app manifests"
kubectl apply -f "${ROOT}/deploy/k8s/namespace.yaml"

# Postgres + Redis — single-replica dev shapes (no PVC for speed).
kubectl apply -f "${ROOT}/deploy/k8s/postgres/" || true
kubectl apply -f "${ROOT}/deploy/k8s/redis/"    || true

# Generate a dev secret with random values if missing.
if ! kubectl -n daalu-automation get secret daalu-automation-secrets >/dev/null 2>&1; then
  kubectl -n daalu-automation create secret generic daalu-automation-secrets \
    --from-literal=DATABASE_URL='postgresql+asyncpg://daalu:daalu_password@postgres:5432/daalu_automation' \
    --from-literal=DATABASE_SYNC_URL='postgresql://daalu:daalu_password@postgres:5432/daalu_automation' \
    --from-literal=REDIS_URL='redis://redis:6379/0' \
    --from-literal=CELERY_BROKER_URL='redis://redis:6379/0' \
    --from-literal=CELERY_RESULT_BACKEND='redis://redis:6379/1' \
    --from-literal=SECRET_KEY="$(openssl rand -hex 32)" \
    --from-literal=INGEST_API_KEY="$(openssl rand -hex 24)" \
    --from-literal=ANTHROPIC_API_KEY='' \
    --from-literal=LLM_API_KEY='' \
    --from-literal=ENVIRONMENT=development
fi

# Apply api/workers/frontend with the :dev tag (sed in-place on a temp copy
# so we don't dirty the repo).
TMPDIR_="$(mktemp -d)"; trap 'rm -rf "$TMPDIR_"' EXIT
cp -r "${ROOT}/deploy/k8s/api" "${ROOT}/deploy/k8s/workers" "${ROOT}/deploy/k8s/frontend" "${TMPDIR_}/"
find "${TMPDIR_}" -name "*.yaml" -exec sed -i -E \
  's|(ghcr\.io/[^:[:space:]]+/(daalu-automation|daalu-automation-frontend))[:[:space:]][^[:space:]]+|\1:dev|g; s|imagePullPolicy: IfNotPresent|imagePullPolicy: Never|g' {} +

# The base Ingress targets traefik + a real hostname + cert-manager TLS.
# On kind we run ingress-nginx and serve plain HTTP on any host.
python3 - "${TMPDIR_}/frontend/ingress.yaml" <<'PY'
import re, sys
p = sys.argv[1]; s = open(p).read()
s = re.sub(r"  annotations:\n    cert-manager.io/cluster-issuer: [^\n]+\n", "", s)
s = s.replace("ingressClassName: traefik", "ingressClassName: nginx")
s = re.sub(r"  tls:\n(    .*\n)+?(?=  rules:)", "", s)
s = re.sub(r"    - host: [^\n]+\n      http:", "    - http:", s)
open(p, "w").write(s)
PY

sed -i 's|https://daalu.example.com|http://localhost:8080|g' "${TMPDIR_}"/api/*.yaml

kubectl apply -f "${TMPDIR_}/api"
kubectl apply -f "${TMPDIR_}/workers" || true
kubectl apply -f "${TMPDIR_}/frontend" || true

echo "==> 6/6  Running migrations + seeding"
kubectl -n daalu-automation wait --for=condition=available --timeout=300s \
  deploy/daalu-api
kubectl -n daalu-automation exec deploy/daalu-api -- daalu seed
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@example.com}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(openssl rand -hex 12)}"
kubectl -n daalu-automation exec deploy/daalu-api -- \
  daalu create-admin --email "${ADMIN_EMAIL}" --password "${ADMIN_PASSWORD}" || true

echo
echo "==> Done."
echo "    App:       http://localhost:8080"
echo "    Login:     ${ADMIN_EMAIL} / ${ADMIN_PASSWORD}"
echo "    Grafana:   kubectl -n monitoring port-forward svc/kube-prom-stack-grafana 3000:80"
echo "    Prometheus: kubectl -n monitoring port-forward svc/kube-prom-stack-prometheus 9090:9090"
echo
echo "    Tear down: kind delete cluster --name ${CLUSTER_NAME}"
