#!/usr/bin/env bash
# Quick view of the demo lab's state — handy while running the demo.
#   ./demo/status.sh
set -euo pipefail
# Find kubectl whether system-installed or fetched by up.sh into BINDIR.
export PATH="$PATH:${DAALU_DEMO_BINDIR:-$HOME/.daalu/bin}"
NS="daalu-demo"
# Match up.sh's DEMO_BIND_ADDR (where the monitoring NodePorts are published).
BROWSE_HOST="${DEMO_BIND_ADDR:-localhost}"; [ "$BROWSE_HOST" = "0.0.0.0" ] && BROWSE_HOST="localhost"
BOLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'
kubectl config use-context kind-daalu-demo >/dev/null 2>&1 || true

printf "%s\n" "${BOLD}Nodes${RST}"
kubectl get nodes 2>/dev/null || { echo "  (cluster not reachable — run ./demo/up.sh)"; exit 0; }

printf "\n%s\n" "${BOLD}demo apps (namespace ${NS})${RST}"
kubectl -n "$NS" get deploy,pods -o wide 2>/dev/null || echo "  (not deployed)"
printf "%s" "  dummy-app available replicas: "
kubectl -n "$NS" get deploy dummy-app -o jsonpath='{.status.availableReplicas}' 2>/dev/null || true
echo
printf "%s" "  metrics-app ERROR_MODE: "
kubectl -n "$NS" get deploy metrics-app -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="ERROR_MODE")].value}' 2>/dev/null || true
echo

printf "\n%s\n" "${BOLD}Firing alerts (Alertmanager)${RST}"
# Reachable on the host via the kind extraPortMapping (see DEMO_BIND_ADDR).
if command -v python3 >/dev/null 2>&1; then
  curl -fsS "http://${BROWSE_HOST}:9093/api/v2/alerts?active=true" 2>/dev/null \
    | python3 -c 'import json,sys;
a=json.load(sys.stdin)
fired=[x["labels"].get("alertname") for x in a if x.get("status",{}).get("state")=="active"]
print("  " + (", ".join(fired) if fired else "(none firing)"))' 2>/dev/null \
    || echo "  (Alertmanager not reachable on ${BROWSE_HOST}:9093 yet)"
else
  echo "  ${DIM}(install python3 to summarize alerts; or open http://${BROWSE_HOST}:9093)${RST}"
fi
