#!/usr/bin/env bash
# From-scratch production install.
#
# Assumes you have:
#   * A fresh Kubernetes cluster (k3s, EKS, GKE, AKS — anything with
#     a working LoadBalancer / IngressClass).
#   * KUBECONFIG pointing at it.
#   * kubectl + helm 3 on $PATH.
#   * DNS records pointing at the cluster ingress IP (for cert-manager).
#
# What it installs, in order:
#   1. cert-manager + ClusterIssuer (Let's Encrypt prod)
#   2. ingress-nginx (or traefik, depending on the flavour of k8s)
#   3. monitoring stack (kube-prometheus-stack + Loki + Promtail)
#   4. NVIDIA GPU Operator (skipped if --no-gpu)
#   5. ArgoCD + the app-of-apps (this is the moment the cluster starts
#      managing itself from git)
#
# Re-running is safe — every step is idempotent.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_GPU=1
INGRESS=ingress-nginx     # or "traefik"
EMAIL="${LETSENCRYPT_EMAIL:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-gpu)  INSTALL_GPU=0; shift ;;
    --ingress) INGRESS="$2"; shift 2 ;;
    --email)   EMAIL="$2"; shift 2 ;;
    *) echo "unknown flag: $1"; exit 1 ;;
  esac
done

[[ -n "$EMAIL" ]] || { echo "set LETSENCRYPT_EMAIL or pass --email <addr>" >&2; exit 1; }

echo "==> Cluster: $(kubectl config current-context)"

# ── 1. cert-manager ────────────────────────────────────────────────────
echo "==> 1/5  cert-manager"
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.16.1/cert-manager.yaml
kubectl -n cert-manager wait --for=condition=available --timeout=300s \
  deploy/cert-manager deploy/cert-manager-webhook deploy/cert-manager-cainjector
kubectl apply -f - <<EOF
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata: { name: letsencrypt-prod }
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: ${EMAIL}
    privateKeySecretRef: { name: letsencrypt-prod }
    solvers:
      - http01:
          ingress:
            class: ${INGRESS}
EOF

# ── 2. Ingress controller ──────────────────────────────────────────────
echo "==> 2/5  ${INGRESS}"
case "${INGRESS}" in
  ingress-nginx)
    helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx 2>/dev/null || true
    helm repo update
    helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
      -n ingress-nginx --create-namespace --wait --timeout 5m \
      --set controller.metrics.enabled=true \
      --set controller.metrics.serviceMonitor.enabled=true \
      --set controller.metrics.serviceMonitor.additionalLabels.release=kube-prom-stack
    ;;
  traefik)
    # k3s ships traefik; only need to add the ServiceMonitor.
    kubectl -n kube-system patch helmchartconfig traefik --type merge -p \
      '{"spec":{"valuesContent":"metrics:\n  prometheus:\n    enabled: true\n"}}' || true
    ;;
esac

# ── 3. Monitoring ──────────────────────────────────────────────────────
echo "==> 3/5  Monitoring stack"
bash "${ROOT}/deploy/k8s/monitoring/install.sh" prod

# ── 4. GPU Operator ────────────────────────────────────────────────────
if [[ "${INSTALL_GPU}" == "1" ]]; then
  echo "==> 4/5  NVIDIA GPU Operator"
  bash "${ROOT}/deploy/k8s/gpu/00-install-gpu-operator.sh"
  kubectl apply -f "${ROOT}/deploy/k8s/gpu/01-time-slicing.yaml"
else
  echo "==> 4/5  GPU Operator — skipped (--no-gpu)"
fi

# ── 5. ArgoCD + app-of-apps ────────────────────────────────────────────
echo "==> 5/5  ArgoCD"
bash "${ROOT}/deploy/argocd/install.sh"

echo
echo "==> Done. Cluster is now self-managing from git."
echo "    ArgoCD UI: kubectl -n argocd port-forward svc/argocd-server 8080:443"
echo "    Grafana:   https://grafana.<your-domain> (Let's Encrypt may take a minute)"
echo
echo "    Next steps:"
echo "    1. Fill in deploy/k8s/monitoring/05-alertmanager-secrets.yaml"
echo "       and apply it for real paging."
echo "    2. Federate the home GPU node — see Appendix B in"
echo "       docs/nvidia-strategy.md."
