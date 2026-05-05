#!/bin/bash
# tinkerbell-setup.sh — Steps 1–8 of the Tinkerbell manual install guide
# Covers: cert-manager → CAPI core → Tinkerbell stack → CAPT → SMEE → image-server → Hardware CRs
# Run from the daalu repo root.
set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
PROVISIONING_IP=10.10.0.9
POD_CIDR=172.16.0.0/16
DHCP_START=10.10.0.100
DHCP_END=10.10.0.200
GATEWAY=10.10.0.9
DNS=8.8.8.8
KUBECONFIG_PATH=~/.kube/daalu-mgmt-config
# ──────────────────────────────────────────────────────────────────────────────

KC="--kubeconfig $KUBECONFIG_PATH"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { echo ""; echo "==> $*"; }
warn() { echo "    [WARN] $*"; }
ok()   { echo "    [OK] $*"; }

cd "$REPO_ROOT"

# ── Step 1: cert-manager ───────────────────────────────────────────────────────
log "Step 1 — cert-manager"

if kubectl $KC get deploy cert-manager -n cert-manager &>/dev/null; then
  ok "cert-manager already installed, skipping"
else
  helm upgrade --install cert-manager assets/cert-manager/charts/cert-manager-v1.20.0.tgz \
    --namespace cert-manager \
    --create-namespace \
    --kubeconfig $KUBECONFIG_PATH \
    --set installCRDs=true
fi

log "  Waiting for cert-manager rollout..."
kubectl $KC -n cert-manager rollout status deploy/cert-manager          --timeout=5m
kubectl $KC -n cert-manager rollout status deploy/cert-manager-webhook  --timeout=5m
kubectl $KC -n cert-manager rollout status deploy/cert-manager-cainjector --timeout=5m

log "  Polling cert-manager webhook readiness..."
for i in $(seq 1 24); do
  if kubectl $KC create --dry-run=server -f - <<'EOF' 2>/dev/null
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: webhook-readiness-probe
  namespace: cert-manager
spec:
  selfSigned: {}
EOF
  then
    ok "cert-manager webhook ready"
    break
  fi
  echo "    ... attempt $i/24, waiting 5s"
  sleep 5
  if [ "$i" -eq 24 ]; then
    echo "ERROR: cert-manager webhook not ready after 2 minutes" >&2
    exit 1
  fi
done

# ── Step 2: CAPI core ──────────────────────────────────────────────────────────
log "Step 2 — CAPI core (cluster-api v1.12.0)"

if kubectl $KC get deploy capi-controller-manager -n capi-system &>/dev/null; then
  ok "CAPI already installed, skipping"
else
  clusterctl --kubeconfig $KUBECONFIG_PATH \
    init \
    --core cluster-api:v1.12.0 \
    --bootstrap kubeadm:v1.12.0 \
    --control-plane kubeadm:v1.12.0 \
    -v5
fi

log "  Waiting for CAPI controller..."
kubectl $KC -n capi-system rollout status deploy/capi-controller-manager --timeout=5m
ok "CAPI ready"

# ── Step 3: Tinkerbell stack ───────────────────────────────────────────────────
log "Step 3 — Tinkerbell stack (stack-0.6.3)"

if kubectl $KC get deploy tink-server -n tinkerbell &>/dev/null; then
  ok "Tinkerbell stack already installed, skipping"
else
  helm upgrade --install tinkerbell assets/tinkerbell/charts/stack-0.6.3.tgz \
    --namespace tinkerbell \
    --create-namespace \
    --kubeconfig $KUBECONFIG_PATH \
    --set global.publicIP=$PROVISIONING_IP \
    --set "global.trustedProxies={$POD_CIDR}" \
    --set global.rbac.type=ClusterRole \
    --wait \
    --timeout 10m
fi

ok "Tinkerbell stack ready"

# ── Step 4: CAPT ──────────────────────────────────────────────────────────────
log "Step 4a — Register CAPT in clusterctl config"

mkdir -p ~/.config/cluster-api

if grep -q "tinkerbell" ~/.config/cluster-api/clusterctl.yaml 2>/dev/null; then
  ok "CAPT already registered in clusterctl.yaml, skipping"
else
  cat >> ~/.config/cluster-api/clusterctl.yaml <<'EOF'

providers:
  - name: tinkerbell
    url: https://github.com/tinkerbell/cluster-api-provider-tinkerbell/releases/v0.6.0/infrastructure-components.yaml
    type: InfrastructureProvider
EOF
  ok "CAPT registered in clusterctl.yaml"
fi

log "Step 4b — Install CAPT (tinkerbell v0.6.0)"

if kubectl $KC get deploy capt-controller-manager -n capt-system &>/dev/null; then
  ok "CAPT already installed, skipping"
else
  TINKERBELL_IP=$PROVISIONING_IP \
    clusterctl --kubeconfig $KUBECONFIG_PATH \
    init \
    --infrastructure tinkerbell:v0.6.0 \
    -v5
fi

log "  Waiting for CAPT controller..."
kubectl $KC -n capt-system rollout status deploy/capt-controller-manager --timeout=5m
ok "CAPT ready"

# ── Step 5: Configure SMEE DHCP ───────────────────────────────────────────────
log "Step 5 — Configure SMEE DHCP (hostNetwork + env vars)"

kubectl $KC patch deployment smee -n tinkerbell \
  --type=strategic \
  --patch="$(cat <<EOF
spec:
  template:
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      containers:
        - name: smee
          env:
            - name: SMEE_DHCP_IP_FOR_PACKET
              value: "$PROVISIONING_IP"
            - name: SMEE_DHCP_RANGE_START
              value: "$DHCP_START"
            - name: SMEE_DHCP_RANGE_END
              value: "$DHCP_END"
            - name: SMEE_DHCP_GATEWAY
              value: "$GATEWAY"
            - name: SMEE_DHCP_DNS
              value: "$DNS"
EOF
)"

kubectl $KC rollout status deployment/smee -n tinkerbell --timeout=3m
ok "SMEE ready"

# ── Step 6: Image server ───────────────────────────────────────────────────────
log "Step 6 — Deploy image server"

if kubectl $KC get deploy image-server -n tinkerbell &>/dev/null; then
  ok "image-server already deployed, skipping"
else
  kubectl $KC apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: image-server
  namespace: tinkerbell
spec:
  replicas: 1
  selector:
    matchLabels:
      app: image-server
  template:
    metadata:
      labels:
        app: image-server
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      containers:
        - name: nginx
          image: nginx:stable-alpine
          ports:
            - containerPort: 80
          volumeMounts:
            - name: images
              mountPath: /usr/share/nginx/html
      volumes:
        - name: images
          hostPath:
            path: /var/www/images
            type: DirectoryOrCreate
      nodeSelector:
        node-role.kubernetes.io/control-plane: ""
      tolerations:
        - key: node-role.kubernetes.io/control-plane
          operator: Exists
          effect: NoSchedule
---
apiVersion: v1
kind: Service
metadata:
  name: image-server
  namespace: tinkerbell
spec:
  type: NodePort
  selector:
    app: image-server
  ports:
    - port: 80
      targetPort: 80
      nodePort: 30080
      protocol: TCP
  externalIPs:
    - $PROVISIONING_IP
EOF
fi

kubectl $KC rollout status deploy/image-server -n tinkerbell --timeout=3m
ok "image-server ready"

# ── Step 7: OS image (manual) ──────────────────────────────────────────────────
log "Step 7 — OS image (manual step)"
warn "Ensure /var/www/images/UBUNTU_24.04_NODE_IMAGE_K8S_v1.35.0.raw.gz exists on the mgmt node."
warn "Test with: curl -I http://$PROVISIONING_IP/UBUNTU_24.04_NODE_IMAGE_K8S_v1.35.0.raw.gz"

# ── Step 8: Hardware CRs ──────────────────────────────────────────────────────
log "Step 8 — Apply Hardware CRs"

kubectl $KC apply -f assets/tinkerbell/hardware/cp01.yaml
kubectl $KC apply -f assets/tinkerbell/hardware/cp02.yaml

log "  Verifying hardware CRs..."
kubectl $KC get hardware -n tinkerbell
kubectl $KC get machines.bmc.tinkerbell.org -n tinkerbell

ok "Hardware CRs applied"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "=========================================="
echo " Steps 1–8 complete. Choose your path:"
echo "   Path A (standalone): apply Template + Workflow CRs, power on nodes"
echo "   Path B (CAPT):       daalu deploy --install cluster-api"
echo "=========================================="
