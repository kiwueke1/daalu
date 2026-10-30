#!/usr/bin/env bash
#
# tier-a-check.sh — verify the Tier-A cluster-scoped singletons exist.
#
# Engineer chapter 64 §64.2 step 1. Read-only. This is the operator-side
# mirror of the controller's host_cluster_ready precheck
# (src/daalu_automation/config_manager_controller/prechecks.py) — run it
# against the *target* host cluster before provisioning a tenant so the
# wizard's first install doesn't bounce off a missing operator.
#
#   Usage:
#     KUBECONFIG=~/.kube/host-cluster ./tier-a-check.sh
#
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
GATEWAYCLASS="${GATEWAYCLASS:-envoy-gateway}"

# CRD name | human label  (same set as prechecks.REQUIRED_CRDS)
CRDS=(
  "gatewayclasses.gateway.networking.k8s.io|Envoy Gateway (Gateway API CRDs)"
  "certificates.cert-manager.io|cert-manager"
  "clusterissuers.cert-manager.io|cert-manager ClusterIssuer support"
  "clusters.postgresql.cnpg.io|CloudNativePG operator"
)

miss=0
echo "checking Tier-A on $($KUBECTL config current-context 2>/dev/null || echo '?') ..."
for entry in "${CRDS[@]}"; do
  IFS='|' read -r crd label <<<"$entry"
  if $KUBECTL get crd "$crd" >/dev/null 2>&1; then
    echo "  OK   $label  ($crd)"
  else
    echo "  MISS $label  ($crd)"; miss=1
  fi
done

# The shared GatewayClass object (not just its CRD) must exist — the chart
# references it by name with createGatewayClass=false.
if $KUBECTL get gatewayclass "$GATEWAYCLASS" >/dev/null 2>&1; then
  echo "  OK   shared GatewayClass '$GATEWAYCLASS'"
else
  echo "  MISS shared GatewayClass '$GATEWAYCLASS'"; miss=1
fi

echo
if [[ "$miss" == "0" ]]; then
  echo "Tier-A ready — safe to provision a tenant into this cluster."
else
  echo "Tier-A INCOMPLETE — install the MISS items before provisioning." >&2
  echo "(The controller will refuse with the same 'missing …' error unless" >&2
  echo " CONFIG_MANAGER_SKIP_HOST_PRECHECK=true.)" >&2
  exit 1
fi
