#!/usr/bin/env bash
# =============================================================================
#  Daalu demo lab (hosted) — a monitored test cluster wired to daalu.example.com.
# -----------------------------------------------------------------------------
#  Stands up a throwaway kind cluster on THIS machine (e.g. a lab box), installs a
#  monitoring stack + a breakable dummy app, then connects it to your hosted
#  Daalu at daalu.example.com over the WireGuard edge tunnel and registers the
#  Prometheus / Loki / Kubernetes integrations — all in one command.
#
#  Unlike the self-hosted demo (which co-locates Daalu on the kind network),
#  the hosted Daalu reaches this cluster through the daalu-edge tunnel:
#    • kubectl  → the edge's apiserver-proxy on  <tunnelIp>:6443
#    • Prom/Loki → the edge's HTTP proxy on       <tunnelIp>:8888  (in-cluster URLs)
#
#  Prereqs on this machine: docker, kind, kubectl, helm, curl, python3, and
#  outbound UDP to wg.example.com:51820 (the tunnel). The kind cluster must be
#  able to pull ghcr.io/kiwueke1/daalu-edge (public, or add a pull secret).
#
#  Required env:
#    DAALU_TOKEN   a `dpat_` personal access token from
#                  daalu.example.com → Settings → API tokens (tenant-admin).
#  Optional env:
#    DAALU_API     default https://daalu.example.com
#    CLUSTER_SLUG  default daalu-demo  (the cluster's tunnel slug on the hub)
#
#  Usage:    DAALU_TOKEN=dpat_... ./demo/up.sh
#  Teardown: DAALU_TOKEN=dpat_... ./demo/down.sh
# =============================================================================
set -euo pipefail

DAALU_API="${DAALU_API:-https://daalu.example.com}"; DAALU_API="${DAALU_API%/}"
CLUSTER_SLUG="${CLUSTER_SLUG:-daalu-demo}"
CLUSTER="daalu-demo"                       # local kind cluster name
BIND_ADDR="${DEMO_BIND_ADDR:-0.0.0.0}"     # where the monitoring NodePorts publish (browser only)
BROWSE_HOST="$BIND_ADDR"; [ "$BIND_ADDR" = "0.0.0.0" ] && BROWSE_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
HERE="$(cd "$(dirname "$0")" && pwd)"

BOLD=$'\033[1m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RED=$'\033[31m'; BLU=$'\033[36m'; RST=$'\033[0m'
say(){ printf "%s\n" "${BLU}▶${RST} $*"; }
ok(){ printf "%s\n" "${GRN}✔${RST} $*"; }
warn(){ printf "%s\n" "${YLW}!${RST} $*"; }
die(){ printf "%s\n" "${RED}✘ $*${RST}" >&2; exit 1; }

: "${DAALU_TOKEN:?set DAALU_TOKEN to a dpat_ PAT — daalu.example.com → Settings → API tokens}"

# Find kind/kubectl/helm in PATH or the demo's bin dir.
export PATH="$PATH:${DAALU_DEMO_BINDIR:-$HOME/.daalu/bin}"
for bin in docker kind kubectl helm curl python3; do
  command -v "$bin" >/dev/null 2>&1 || die "missing '$bin' — install it and retry."
done
docker info >/dev/null 2>&1 || die "the docker daemon isn't running"

# ── Authenticated hub API helper ─────────────────────────────────────────────
api(){ # api METHOD PATH [JSON-body]
  local m="$1" p="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -fsS -X "$m" "${DAALU_API}${p}" \
      -H "Authorization: Bearer ${DAALU_TOKEN}" -H 'content-type: application/json' -d "$body"
  else
    curl -fsS -X "$m" "${DAALU_API}${p}" -H "Authorization: Bearer ${DAALU_TOKEN}"
  fi
}
jget(){ python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get(sys.argv[1],""))' "$1"; }

say "Checking the hosted API at ${DAALU_API}"
api GET /api/v1/clusters >/dev/null 2>&1 \
  || die "couldn't reach ${DAALU_API} with this token. Check the URL and that DAALU_TOKEN is a valid tenant-admin dpat_ token."
ok "authenticated to ${DAALU_API}"

# ── 1. kind cluster ──────────────────────────────────────────────────────────
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  ok "kind cluster '${CLUSTER}' already exists"
else
  say "Creating kind cluster '${CLUSTER}'"
  RENDERED_CFG="$(mktemp)"; trap 'rm -f "$RENDERED_CFG"' EXIT
  sed "s/listenAddress: \"0.0.0.0\"/listenAddress: \"${BIND_ADDR}\"/" "${HERE}/kind-config.yaml" > "$RENDERED_CFG"
  kind create cluster --name "$CLUSTER" --config "$RENDERED_CFG"
  ok "cluster created"
fi
kubectl config use-context "kind-${CLUSTER}" >/dev/null

# ── 2. Monitoring + logging + demo apps ──────────────────────────────────────
say "Installing kube-prometheus-stack (a few minutes — pulls several images)"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --set prometheus.service.type=NodePort --set prometheus.service.nodePort=30909 \
  --set alertmanager.service.type=NodePort --set alertmanager.service.nodePort=30903 \
  --set grafana.service.type=NodePort --set grafana.service.nodePort=30300 \
  --set grafana.adminPassword=admin \
  --set defaultRules.create=false \
  --set prometheus.prometheusSpec.retention=3h \
  --set prometheus.prometheusSpec.resources.requests.memory=256Mi \
  --wait --timeout 12m
ok "monitoring stack up"

say "Installing Loki + Promtail"
helm upgrade --install loki grafana/loki-stack --namespace monitoring --set promtail.enabled=true --wait --timeout 8m
kubectl -n monitoring patch svc loki -p \
  '{"spec":{"type":"NodePort","ports":[{"name":"http-metrics","port":3100,"targetPort":3100,"nodePort":30100}]}}' >/dev/null 2>&1 || true
ok "logging stack up"

say "Deploying the demo apps + alert rules"
kubectl apply -f "${HERE}/manifests/dummy-app.yaml" >/dev/null
kubectl apply -f "${HERE}/manifests/dummy-app-alerts.yaml" >/dev/null
kubectl apply -f "${HERE}/manifests/metrics-app.yaml" >/dev/null
kubectl -n daalu-demo rollout status deploy/dummy-app  --timeout=3m
kubectl -n daalu-demo rollout status deploy/metrics-app --timeout=3m
ok "dummy-app + metrics-app running"

# ── 3. Onboard the cluster onto the hub (WireGuard edge tunnel) ───────────────
say "Onboarding the cluster '${CLUSTER_SLUG}' to ${DAALU_API}"
ONBOARD="$(api POST /api/v1/clusters "{\"slug\":\"${CLUSTER_SLUG}\",\"name\":\"Daalu demo lab\"}" 2>/dev/null || true)"
if [ -z "$ONBOARD" ]; then
  # Likely already exists. Reuse it if it's already connected; otherwise stop.
  EXIST="$(api GET "/api/v1/clusters/${CLUSTER_SLUG}" 2>/dev/null || true)"
  [ -n "$EXIST" ] || die "onboarding failed and no existing '${CLUSTER_SLUG}' cluster found. Check the token/permissions."
  TUNNEL_ID="$(printf '%s' "$EXIST" | jget id)"
  TUNNEL_IP="$(printf '%s' "$EXIST" | jget tunnel_ip)"
  STATUS="$(printf '%s' "$EXIST" | jget status)"
  warn "cluster '${CLUSTER_SLUG}' already exists (status=${STATUS}, ip=${TUNNEL_IP})."
  if [ "$STATUS" != "connected" ]; then
    die "it isn't connected. Tear down first (./demo/down.sh) to re-mint a one-shot token, then re-run."
  fi
  ok "reusing the connected tunnel"
else
  TUNNEL_ID="$(printf '%s' "$ONBOARD" | jget id)"
  TUNNEL_IP="$(printf '%s' "$ONBOARD" | jget tunnel_ip)"
  printf '%s' "$ONBOARD" | python3 -c 'import sys,json; print(json.load(sys.stdin)["install_snippet"])' > "${HERE}/.edge-install.sh"
  ok "cluster row created — tunnel IP ${TUNNEL_IP}, id ${TUNNEL_ID}"

  say "Installing daalu-edge into the kind cluster (dials wg.example.com)"
  # The snippet targets the current kubeconfig context (kind-${CLUSTER}).
  bash "${HERE}/.edge-install.sh" || die "helm install daalu-edge failed (image pullable? see README on a ghcr pull secret)."
  rm -f "${HERE}/.edge-install.sh"
  kubectl rollout status deploy/daalu-edge --timeout=3m 2>/dev/null \
    || kubectl get pods -l app=daalu-edge -A 2>/dev/null || true

  say "Waiting for the tunnel to connect (handshake with the hub)…"
  STATUS=""
  for _ in $(seq 1 48); do
    STATUS="$(api GET "/api/v1/clusters/${CLUSTER_SLUG}" 2>/dev/null | jget status)"
    [ "$STATUS" = "connected" ] && break
    printf '.'; sleep 5
  done
  printf '\n'
  [ "$STATUS" = "connected" ] && ok "tunnel connected" \
    || warn "tunnel still '${STATUS}' after ~4 min — integrations are registered below but will only work once it connects (check the daalu-edge pod logs)."
fi

# ── 4. Register the integrations (bound to this tunnel) ───────────────────────
# Prometheus/Loki use in-cluster Service DNS routed through the edge's HTTP
# proxy (cluster_tunnel_id). `prometheus-operated` + `loki` are stable
# in-cluster names regardless of the helm release name. Kubernetes uses a
# kubeconfig whose server is the edge's apiserver-proxy at <tunnelIp>:6443.
#
# The prometheus integration points at the Prometheus *server* (:9090), not
# Alertmanager (:9093): it backs both the metric console (/api/v1/query) and
# alert ingestion (Alertmanager /api/v2/alerts when present, else Prometheus'
# own /api/v1/alerts). An Alertmanager URL has no /api/v1/query and 404s the
# console.
say "Registering integrations (prometheus, loki, kubernetes)"
reg(){ api PUT "/api/v1/integrations/config/$1" "$2" >/dev/null 2>&1 && ok "  registered '$1'" || warn "  failed to register '$1' (add it by hand in the UI → Integrations)"; }

reg prometheus "{\"config\":{\"url\":\"http://prometheus-operated.monitoring.svc.cluster.local:9090\"},\"cluster_tunnel_id\":\"${TUNNEL_ID}\"}"
reg loki       "{\"config\":{\"url\":\"http://loki.monitoring.svc.cluster.local:3100\"},\"cluster_tunnel_id\":\"${TUNNEL_ID}\"}"

# Kubeconfig pointed at the tunnel: server → <tunnelIp>:6443, skip TLS verify
# (the apiserver cert has no SAN for the tunnel IP), drop the CA, keep client auth.
K8S_BODY="$(kind get kubeconfig --name "$CLUSTER" | python3 -c '
import sys, json, yaml
ip = sys.argv[1]
k = yaml.safe_load(sys.stdin.read())
for c in k.get("clusters", []):
    cl = c.setdefault("cluster", {})
    cl["server"] = f"https://{ip}:6443"
    cl["insecure-skip-tls-verify"] = True
    cl.pop("certificate-authority-data", None)
print(json.dumps({"config": {"kubeconfig": yaml.safe_dump(k)}}))
' "$TUNNEL_IP")"
reg_kube(){ api PUT /api/v1/integrations/config/kubernetes "$K8S_BODY" >/dev/null 2>&1 && ok "  registered 'kubernetes'" || warn "  failed to register 'kubernetes'"; }
reg_kube

# ── Done ─────────────────────────────────────────────────────────────────────
cat <<EOF

${GRN}✔ Demo lab is up and connected to ${DAALU_API}.${RST}

  Browse the stack locally on this host (optional):
     Grafana:       http://${BROWSE_HOST}:3001   (admin / admin)
     Prometheus:    http://${BROWSE_HOST}:9090
     Alertmanager:  http://${BROWSE_HOST}:9093

  In Daalu (${DAALU_API}):
     • Managed infra → Kubernetes/Observability show this cluster connected.
     • Run the demo:
         ${BOLD}./demo/break.sh${RST}      # break the app (default: a bad image)
       …then open Daalu → Alerts. Within ~2–3 min the DummyAppDown alert appears;
       open it and let the agent investigate + propose a rollback to approve.
     • ${BOLD}./demo/status.sh${RST}     # app + alert state at any time
     • ${BOLD}./demo/down.sh${RST}       # tear the lab down + detach from Daalu

  Manual fix (instead of via Daalu):
     kubectl -n daalu-demo rollout undo deploy/dummy-app
EOF
