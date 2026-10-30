#!/usr/bin/env bash
# Bring the entire Daalu stack up on a fresh cluster — dev or prod.
#
# DEV  (default): wraps `make bootstrap-dev` →
#                 Kind cluster + ingress-nginx + monitoring + app
#                 images built and side-loaded + migrations + seed.
#                 ~10 minutes on a fresh laptop.
#
# PROD (--prod):  wraps `make bootstrap-prod` →
#                 cert-manager + Let's Encrypt + ingress-nginx +
#                 kube-prometheus-stack + Loki + Promtail + blackbox +
#                 NVIDIA GPU Operator + ArgoCD + app-of-apps.
#                 ~15 minutes on a fresh cluster.
#
# Pre-flight (dev):
#   * docker, kind, kubectl, helm on $PATH
#
# Pre-flight (prod):
#   * KUBECONFIG points at the new cluster
#   * DNS records point at the cluster ingress IP
#   * $LETSENCRYPT_EMAIL exported (or pass --email)
#   * kubectl, helm on $PATH
#
# Flags:
#   --prod              Use prod path (bootstrap-prod) instead of dev
#   --email <addr>      Let's Encrypt contact email (prod only;
#                       defaults to $LETSENCRYPT_EMAIL)
#   --no-gpu            Skip GPU Operator install (prod only)
#   --ingress <name>    ingress-nginx (default) or traefik (prod only)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="dev"
PROD_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prod)    MODE="prod"; shift ;;
    --email)   PROD_ARGS+=(--email "$2"); shift 2 ;;
    --no-gpu)  PROD_ARGS+=(--no-gpu); shift ;;
    --ingress) PROD_ARGS+=(--ingress "$2"); shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done

case "${MODE}" in
  dev)
    echo "==> Fresh DEV environment on Kind"
    make -C "${ROOT}" bootstrap-dev
    cat <<'EOF'

==> Dev cluster up.

    App:        http://localhost:8080
    Grafana:    kubectl -n monitoring port-forward svc/kube-prom-stack-grafana 3000:80
    Prometheus: kubectl -n monitoring port-forward svc/kube-prom-stack-prometheus 9090:9090

    Inner-loop dev with live reload:  make tilt
    Tear down:                        make down
EOF
    ;;

  prod)
    : "${LETSENCRYPT_EMAIL:?LETSENCRYPT_EMAIL not set — export it or pass --email}"
    echo "==> Fresh PROD bootstrap on $(kubectl config current-context)"
    # bootstrap-prod.sh reads $LETSENCRYPT_EMAIL by default, and also
    # accepts --email/--no-gpu/--ingress flags.
    bash "${ROOT}/deploy/bootstrap-prod.sh" "${PROD_ARGS[@]}"
    cat <<'EOF'

==> Prod cluster bootstrapped.

    Next:
      * Wire paging — scripts/deploy-observability.sh prints the steps,
        or see docs/go-live-checklist.md §B.2.
      * Federate the home GPU node (checklist §A.1 — one-time manual
        work on the home box), then run scripts/deploy-gpu.sh.
      * Set branch protection on master per checklist §B.4.
EOF
    ;;
esac
