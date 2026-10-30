#!/usr/bin/env bash
# Install ArgoCD into the `argocd` namespace and bootstrap the
# "app-of-apps" — a single Application that points at this repo and
# tells ArgoCD to manage every other Application from
# `deploy/argocd/applications/`.
#
# After this script:
#   * ArgoCD UI: kubectl -n argocd port-forward svc/argocd-server 8080:443
#   * Admin password: kubectl -n argocd get secret argocd-initial-admin-secret \
#                       -o jsonpath='{.data.password}' | base64 -d

set -euo pipefail

ENV="${1:-prod}"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing ArgoCD"
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -

# Pinned ArgoCD release — bump deliberately, not automatically.
kubectl apply -n argocd -f \
  https://raw.githubusercontent.com/argoproj/argo-cd/v2.13.1/manifests/install.yaml

echo "==> Waiting for ArgoCD pods…"
kubectl -n argocd wait --for=condition=available --timeout=300s \
  deploy/argocd-server deploy/argocd-repo-server deploy/argocd-redis \
  deploy/argocd-applicationset-controller

echo "==> Applying AppProject + root Application"
kubectl apply -f "${DIR}/project.yaml"
kubectl apply -f "${DIR}/root-application.yaml"

echo
echo "==> Done. UI access:"
echo "    kubectl -n argocd port-forward svc/argocd-server 8080:443"
echo "    User: admin"
echo "    Pass: kubectl -n argocd get secret argocd-initial-admin-secret \\"
echo "             -o jsonpath='{.data.password}' | base64 -d ; echo"
