#!/usr/bin/env bash
# Hand observability off to ArgoCD on an existing cluster.
#
# Idempotent flow:
#   1. Verify ArgoCD is already installed (this script does NOT install
#      ArgoCD — use `scripts/deploy-from-scratch.sh --prod` for a fresh
#      cluster that needs it).
#   2. Ensure the GitHub PAT-based repo credential exists in `argocd`.
#      If missing, prompt for it (token is read with `read -s` so it
#      never appears in shell history or echo).
#   3. Apply the `daalu` AppProject + the `daalu-root` app-of-apps,
#      which discovers every Application under
#      `deploy/argocd/applications/` (monitoring, automation, gpu).
#   4. Trigger a hard refresh and wait for daalu-root to leave the
#      `Unknown` sync state.
#
# After this script, monitoring (and everything else in
# applications/) is *ArgoCD-managed* — no more direct `helm install`
# from a laptop. ArgoCD selfHeals from master.
#
# Pre-flight:
#   * KUBECONFIG points at the target cluster (verify with
#     `kubectl config current-context` first).
#   * ArgoCD is running in the `argocd` namespace (this is the case
#     after `deploy-from-scratch.sh --prod`).
#   * Either: `daalu-automation-repo` secret already exists, OR a
#     GitHub PAT with read-only access to kiwueke1/daalu-automation
#     is ready to paste when prompted.
#
# Flags:
#   --repo-secret-name <name>  Override the ArgoCD repo secret name
#                              (default: daalu-automation-repo).
#   --username <user>          GitHub username (default: kiwueke1).
#   --repo-url <url>           Override the git URL ArgoCD reads
#                              (default: https://github.com/kiwueke1/daalu).
#   --skip-repo-secret         Don't create / verify the secret
#                              (assumes it's already there).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SECRET_NAME="daalu-automation-repo"
GH_USERNAME="kiwueke1"
REPO_URL="https://github.com/kiwueke1/daalu"
SKIP_SECRET=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-secret-name) SECRET_NAME="$2"; shift 2 ;;
    --username)         GH_USERNAME="$2"; shift 2 ;;
    --repo-url)         REPO_URL="$2";    shift 2 ;;
    --skip-repo-secret) SKIP_SECRET=1;    shift ;;
    -h|--help) sed -n '2,38p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done

command -v kubectl >/dev/null || { echo "kubectl not on PATH"; exit 1; }

echo "==> Cluster: $(kubectl config current-context)"

# 1/4 — Verify ArgoCD is present
echo
echo "==> 1/4  Checking ArgoCD"
if ! kubectl -n argocd get deploy argocd-server >/dev/null 2>&1; then
  cat >&2 <<EOF
==> ERROR: ArgoCD is not installed in this cluster.

    This script hands monitoring off to an existing ArgoCD. For a
    fresh cluster, run:
      ./scripts/deploy-from-scratch.sh --prod

    That installs cert-manager + ingress + monitoring + GPU operator
    + ArgoCD, then you can re-run this script (or skip it — bootstrap
    already registers the app-of-apps).
EOF
  exit 1
fi
ARGOCD_VERSION="$(kubectl -n argocd get deploy argocd-server \
  -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null | awk -F: '{print $NF}')"
echo "    ArgoCD running (image tag: ${ARGOCD_VERSION:-unknown})"

# 2/4 — Ensure the repo credential secret exists
echo
echo "==> 2/4  GitHub repo credential"
if [[ "${SKIP_SECRET}" == "1" ]]; then
  echo "    Skipped (--skip-repo-secret)"
elif kubectl -n argocd get secret "${SECRET_NAME}" >/dev/null 2>&1; then
  echo "    Secret '${SECRET_NAME}' already exists — reusing"
else
  echo "    Secret '${SECRET_NAME}' missing — need a GitHub PAT to create it."
  echo
  echo "    Generate one at https://github.com/settings/personal-access-tokens"
  echo "    Fine-grained, scoped to repository '${GH_USERNAME}/$(basename "${REPO_URL}")',"
  echo "    permission 'Contents: Read-only'. (Classic PAT with 'repo' scope also works.)"
  echo
  read -r -s -p "    GitHub PAT for ${GH_USERNAME}: " GH_PAT; echo
  if [[ -z "${GH_PAT}" ]]; then
    echo "    No PAT entered — aborting." >&2
    exit 1
  fi

  kubectl -n argocd apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: ${SECRET_NAME}
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
type: Opaque
stringData:
  type: git
  url: ${REPO_URL}
  username: ${GH_USERNAME}
  password: ${GH_PAT}
EOF
  unset GH_PAT
  echo "    Secret created"
fi

# 3/4 — Apply project + root app-of-apps
echo
echo "==> 3/4  Applying AppProject + daalu-root app-of-apps"
kubectl apply -f "${ROOT}/deploy/argocd/project.yaml"
kubectl apply -f "${ROOT}/deploy/argocd/root-application.yaml"

# 4/4 — Force refresh + wait for sync to leave Unknown state
echo
echo "==> 4/4  Refreshing daalu-root and waiting for sync"
kubectl -n argocd annotate application daalu-root \
  argocd.argoproj.io/refresh=hard --overwrite >/dev/null
echo "    Polling (will time out after 5 min)..."
deadline=$(( $(date +%s) + 300 ))
while (( $(date +%s) < deadline )); do
  status=$(kubectl -n argocd get application daalu-root \
    -o jsonpath='{.status.sync.status}' 2>/dev/null || echo "")
  if [[ "${status}" == "Synced" || "${status}" == "OutOfSync" ]]; then
    echo "    daalu-root sync status: ${status}"
    break
  fi
  sleep 5
done

cat <<EOF

==> Observability handed off to ArgoCD.

    Inspect from the UI:
      kubectl -n argocd port-forward svc/argocd-server 8080:443
      admin password:
        kubectl -n argocd get secret argocd-initial-admin-secret \\
          -o jsonpath='{.data.password}' | base64 -d ; echo

    Expected applications (may take a few minutes to all appear):
      daalu-root, kube-prometheus-stack, loki, promtail,
      monitoring-config, daalu-automation, daalu-gpu

    Listing now:
EOF
kubectl -n argocd get applications

cat <<'EOF'

    Next:
      * Wire paging — copy deploy/k8s/monitoring/05-alertmanager-secrets.example.yaml,
        fill in slack_webhook_url + pagerduty_routing_key, apply,
        and add the real file to .gitignore.
      * Grafana:
          kubectl -n monitoring port-forward svc/kube-prom-stack-grafana 3000:80
        (TLS at https://grafana.daalu.io once cert-manager finishes
        the Let's Encrypt challenge — requires cloudflare-api-token
        secret in the monitoring namespace, copied automatically
        from cert-manager namespace by deploy-from-scratch.sh).

    Verification steps live in docs/go-live-checklist.md §B.5.
EOF
