#!/usr/bin/env bash
# Install / upgrade the full monitoring stack (kube-prometheus-stack,
# Loki, Promtail, blackbox-exporter, ServiceMonitors, alert rules).
# Idempotent. Re-running upgrades in place.
#
# Pre-reqs:
#   * KUBECONFIG points at the right cluster.
#   * Helm 3 installed.
#   * For prod: cert-manager + ingress-class `traefik` already up
#     (the bootstrap-prod.sh script ensures both).
#
# Usage:
#   bash deploy/k8s/monitoring/install.sh [dev|prod]
#
# `dev` skips the prod storage class + ingress / TLS bits.

set -euo pipefail

ENV="${1:-prod}"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing monitoring stack in '${ENV}' mode"

kubectl apply -f "${DIR}/00-namespace.yaml"

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
helm repo add grafana https://grafana.github.io/helm-charts 2>/dev/null || true
helm repo update

VALUES="${DIR}/01-kube-prometheus-stack-values.yaml"
EXTRA_VALUES_FLAGS=()
if [[ "${ENV}" == "dev" ]]; then
  # Kind doesn't have hcloud-volumes / traefik ingress / cert-manager.
  # Patch the values inline so the chart installs against vanilla
  # `standard` storage and skips ingress / TLS.
  EXTRA_VALUES_FLAGS=(
    --set prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.storageClassName=standard
    --set alertmanager.alertmanagerSpec.storage.volumeClaimTemplate.spec.storageClassName=standard
    --set grafana.persistence.storageClassName=standard
    --set grafana.ingress.enabled=false
    --set grafana.adminPassword=admin
  )
fi

helm upgrade --install kube-prom-stack \
  prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --version 65.3.1 \
  -f "${VALUES}" \
  "${EXTRA_VALUES_FLAGS[@]}" \
  --wait --timeout 10m

LOKI_VALUES_FLAGS=()
if [[ "${ENV}" == "dev" ]]; then
  LOKI_VALUES_FLAGS=(--set singleBinary.persistence.storageClass=standard)
fi

helm upgrade --install loki grafana/loki \
  --namespace monitoring \
  --version 6.16.0 \
  -f "${DIR}/02-loki-values.yaml" \
  "${LOKI_VALUES_FLAGS[@]}" \
  --wait --timeout 10m

helm upgrade --install promtail grafana/promtail \
  --namespace monitoring \
  --version 6.16.6 \
  -f "${DIR}/02-promtail-values.yaml" \
  --wait --timeout 5m

# Plain manifests after the charts are up.
kubectl apply -f "${DIR}/03-servicemonitors.yaml"
kubectl apply -f "${DIR}/04-alerting-rules.yaml"

if [[ "${ENV}" == "prod" ]]; then
  # In dev we don't page anyone.
  if kubectl -n monitoring get secret alertmanager-secrets >/dev/null 2>&1; then
    echo "    (alertmanager-secrets already present — skipping)"
  else
    echo "!! Missing alertmanager-secrets. Copy 05-alertmanager-secrets.example.yaml"
    echo "   to 05-alertmanager-secrets.yaml, fill in values, kubectl apply -f it,"
    echo "   then bounce the AM pod."
  fi
fi

kubectl apply -f "${DIR}/06-blackbox-exporter.yaml"

# Mount our pre-canned dashboards as ConfigMaps. The grafana sidecar
# (configured in 01-kube-prometheus-stack-values.yaml) auto-loads
# anything labelled grafana_dashboard=1.
for dash in "${DIR}"/dashboards/*.json; do
  [[ -e "$dash" ]] || break
  name="$(basename "$dash" .json)"
  kubectl -n monitoring create configmap "dash-${name}" \
    --from-file="${name}.json=${dash}" \
    --dry-run=client -o yaml \
  | kubectl label --local -o yaml -f - grafana_dashboard=1 \
  | kubectl apply -f -
done

echo
echo "==> Done. Open Grafana:"
if [[ "${ENV}" == "dev" ]]; then
  echo "    kubectl -n monitoring port-forward svc/kube-prom-stack-grafana 3000:80"
  echo "    Username: admin   Password: admin (set by install.sh)"
else
  echo "    https://grafana.daalu.io   (auth: kube-prom-stack-grafana secret)"
fi
