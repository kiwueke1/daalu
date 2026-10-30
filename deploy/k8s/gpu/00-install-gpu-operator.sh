#!/usr/bin/env bash
# Install the NVIDIA GPU Operator into the current $KUBECONFIG cluster.
#
# Prerequisites (see docs/nvidia-strategy.md Appendix B):
#   * GPU node carries labels: gpu-class=<class>, nvidia.com/gpu.present=true
#   * Host driver already installed via the distro package
#     (driver.enabled=false below).
#
# Two runtime variants are supported:
#   * --variant=kubeadm  Stock containerd paths (CAPI/Tinkerbell, kops,
#                        kubeadm clusters, EKS). DEFAULT.
#   * --variant=k3s      Pass the containerd overrides that point at
#                        /var/lib/rancher/k3s/... — required for the
#                        ops cluster (k3s server).
#
# Tolerations:
#   The Operator's DaemonSets must tolerate whatever taint keeps general
#   workloads off the GPU node. By default this script tolerates BOTH
#   `gpu=*:NoSchedule` (the optional dedicated-GPU taint) and
#   `node-role.kubernetes.io/control-plane:NoSchedule` (because gpu-node-1 in
#   the workload cluster is a control-plane node). Override with
#   --tolerate-control-plane=0 if you don't need the second one.
#
# Flags / env:
#   --variant <kubeadm|k3s>     Runtime variant. Default: kubeadm.
#   --tolerate-control-plane <0|1>  Default: 1.
#   NAMESPACE                   Operator namespace. Default: gpu-operator.
#   OPERATOR_VERSION            Default: v25.10.1.
#                               v24.9.x ships container-toolkit v1.17.3,
#                               which cannot parse containerd 2.x's
#                               /etc/containerd/config.toml (version = 3)
#                               and crash-loops the toolkit DaemonSet.
#                               Containerd-v3-aware toolkit support
#                               landed in chart v25.3.0 (toolkit v1.17.5);
#                               we pin v25.10.1 as the latest patch on
#                               the previous-minor stable line.
#
# Idempotent — re-runs upgrade in place.

set -euo pipefail

NAMESPACE="${NAMESPACE:-gpu-operator}"
OPERATOR_VERSION="${OPERATOR_VERSION:-v25.10.1}"
VARIANT="kubeadm"
TOLERATE_CP=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant)                   VARIANT="$2"; shift 2 ;;
    --variant=*)                 VARIANT="${1#*=}"; shift ;;
    --tolerate-control-plane)    TOLERATE_CP="$2"; shift 2 ;;
    --tolerate-control-plane=*)  TOLERATE_CP="${1#*=}"; shift ;;
    -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done

case "$VARIANT" in
  kubeadm|k3s) ;;
  *) echo "ERROR: --variant must be 'kubeadm' or 'k3s' (got '$VARIANT')" >&2; exit 1 ;;
esac

echo "==> Cluster: $(kubectl config current-context)"
echo "==> Runtime variant: ${VARIANT}"
echo "==> Tolerate control-plane taint: $([ "${TOLERATE_CP}" = "1" ] && echo yes || echo no)"

helm repo add nvidia https://helm.ngc.nvidia.com/nvidia 2>/dev/null || true
helm repo update

# Build the helm flags array so we can switch on variant.
HELM_FLAGS=(
  --namespace "${NAMESPACE}" --create-namespace
  --version "${OPERATOR_VERSION}"
  --set driver.enabled=false
)

if [[ "${VARIANT}" == "k3s" ]]; then
  # k3s ships containerd at non-default locations. Tell the toolkit
  # container where to write the config.
  HELM_FLAGS+=(
    --set toolkit.env[0].name=CONTAINERD_CONFIG
    --set toolkit.env[0].value=/var/lib/rancher/k3s/agent/etc/containerd/config.toml.tmpl
    --set toolkit.env[1].name=CONTAINERD_SOCKET
    --set toolkit.env[1].value=/run/k3s/containerd/containerd.sock
    --set toolkit.env[2].name=CONTAINERD_RUNTIME_CLASS
    --set toolkit.env[2].value=nvidia
    --set toolkit.env[3].name=CONTAINERD_SET_AS_DEFAULT
    # --set-string: chart v25.10.1's ClusterPolicy CRD requires env values to be
    # strings; a bare --set coerces "true" to a boolean and fails validation.
    --set-string toolkit.env[3].value=true
  )
fi

# DaemonSet tolerations. Index 0 is always the dedicated-GPU taint;
# index 1 (optional) is the control-plane taint.
HELM_FLAGS+=(
  --set daemonsets.tolerations[0].key=gpu
  --set daemonsets.tolerations[0].operator=Exists
  --set daemonsets.tolerations[0].effect=NoSchedule
)
if [[ "${TOLERATE_CP}" == "1" ]]; then
  HELM_FLAGS+=(
    --set daemonsets.tolerations[1].key=node-role.kubernetes.io/control-plane
    --set daemonsets.tolerations[1].operator=Exists
    --set daemonsets.tolerations[1].effect=NoSchedule
  )
fi

HELM_FLAGS+=( --wait )

helm upgrade --install gpu-operator nvidia/gpu-operator "${HELM_FLAGS[@]}"

echo
echo "GPU Operator installed. Verifying the node advertises nvidia.com/gpu..."
sleep 5
kubectl describe node -l nvidia.com/gpu.present=true 2>/dev/null \
  | grep -E 'Name:|nvidia.com/gpu' | head -20
echo
echo "Next: apply 01-time-slicing.yaml to expose multiple logical GPUs per card."
