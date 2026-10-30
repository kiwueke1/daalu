#!/usr/bin/env bash
# Wire the workload-cluster GPU into the Daalu ops cluster so the LLM
# router uses it. Implements Part A of docs/go-live-checklist.md and
# Appendix B of docs/nvidia-strategy.md.
#
# Topology recap (see Appendix B):
#   * Ops cluster      — the cluster that runs daalu-api.
#                        kubeconfig: $KUBECONFIG (default).
#   * Workload cluster — CAPI/Tinkerbell, has the GPU on gpu-node-1.
#                        kubeconfig: ~/.kube/workload-cluster (default).
#
# The workload cluster is already onboarded to the ops cluster via the
# existing daalu-edge WireGuard tunnel (slug 'daalu-workload', tunnel IP
# 10.200.0.2). This script does not touch the tunnel keypair or the
# operator-side row — it only adds a serviceProxies socat sidecar to
# expose vLLM on that tunnel.
#
# Stack: fully open source. No NVIDIA AI Enterprise, no NIM, no NGC
# entitlement. vLLM (Apache 2.0) serves an open Llama 3.1 8B Instruct
# AWQ checkpoint from Hugging Face.
#
# What this script does:
#   HOST (gpu-node-1, over SSH):
#     0. Install the NVIDIA driver + nvidia-utils if not present, then
#        reboot, then wait for the node to come back Ready in Kubernetes.
#        Skipped if `nvidia-smi -L` already reports the card.
#   WORKLOAD cluster:
#     1. Label the GPU node (nvidia.com/gpu.present=true, gpu-class=<class>)
#     2. Install the NVIDIA GPU Operator (kubeadm variant). SKIPPED on
#        the CRI-O workload cluster — a standalone device-plugin
#        DaemonSet does the job there. Always pass --skip-operator.
#     3. Apply time-slicing + ClusterPolicy patch + per-node profile
#        label. Skipped together with step 2 on --skip-operator (the
#        standalone device-plugin manages time-slicing via its own
#        ConfigMap, already in place).
#     4. Create HF token secret in the `daalu` ns (vLLM uses it to
#        download the gated Meta Llama weights from Hugging Face).
#     5. Apply vLLM Deployment + Service + PDB + NetworkPolicy
#     6. Wait for the vLLM rollout (first start downloads ~5 GB of AWQ
#        weights from Hugging Face)
#     7. Upgrade daalu-edge with serviceProxies so :8000 (vLLM) and :9400
#        (DCGM) ride the existing tunnel
#   OPS cluster:
#     8. Cross-tunnel smoke test from a daalu-api pod
#     9. Seed the SKU catalog (idempotent)
#    10. Print the exact `kubectl patch secret` command that wires the
#        router (kept manual on purpose — see notes at the end)
#
# Pre-flight requirements (this script will not silently work around
# any of these):
#   * Both kubeconfigs exist and the cluster contexts are reachable
#   * The GPU node (default: gpu-node-1) is Ready in the workload cluster
#   * SSH access to the GPU node with passwordless sudo
#     (default: builder@<node-ip> with key ~/.ssh/openstack-key)
#     — pass --skip-host-prep if the driver is already installed and you
#     don't want the script to touch the host
#   * $HF_TOKEN exported (https://huggingface.co/settings/tokens — free,
#     "Read" type. Click through Meta's licence at
#     https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct first.)
#   * daalu-edge release exists in the workload cluster with status
#     'connected' on the ops side
#
# Safe to re-run — every step is idempotent. The host-prep step is a
# no-op if `nvidia-smi -L` already reports the card.
#
# Flags:
#   --ops-kubeconfig <path>       Default: $KUBECONFIG
#   --workload-kubeconfig <path>  Default: ~/.kube/workload-cluster
#   --gpu-node <name>             Default: gpu-node-1
#   --gpu-class <class>           Default: ada-16
#   --gpu-node-ssh-user <user>    Default: builder
#   --gpu-node-ssh-host <host>    Default: <node-ip>
#   --gpu-node-ssh-key <path>     Default: ~/.ssh/openstack-key
#   --driver-package <pkg>        Default: empty (use `ubuntu-drivers
#                                 install --gpgpu`). Override to pin a
#                                 specific package, e.g.
#                                 nvidia-driver-550-server.
#   --edge-release <name>         Default: daalu-edge
#   --edge-namespace <ns>         Default: default
#   --edge-chart <ref>            Helm chart to upgrade to. Default:
#                                 the local checkout at
#                                 deploy/helm/daalu-edge (no registry).
#   --edge-version <ver>          Required only if --edge-chart points at
#                                 an OCI registry. Default: unset.
#   --tunnel-ip <ip>              Default: 10.200.0.2 (must match the
#                                 daalu-edge release's cluster.tunnelIp)
#   --skip-host-prep              Skip the SSH-based driver install on gpu-node-1
#   --skip-operator               Skip GPU Operator install (use this on
#                                 the CRI-O workload cluster — also gates
#                                 the time-slicing ClusterPolicy patch
#                                 since there is no ClusterPolicy without
#                                 the Operator)
#   --skip-vllm                   Skip vLLM deployment + wait (alias: --skip-nim)
#   --skip-edge-upgrade           Skip the daalu-edge serviceProxies upgrade
#   --skip-ops-smoke              Skip the cross-tunnel smoke test
#   --skip-seed-skus              Skip seeding SKUs in the ops cluster

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

OPS_KUBECONFIG="${OPS_KUBECONFIG:-${KUBECONFIG:-${HOME}/.kube/config}}"
WL_KUBECONFIG="${WL_KUBECONFIG:-${HOME}/.kube/workload-cluster}"
GPU_NODE="gpu-node-1"
GPU_CLASS="ada-16"
SSH_USER="builder"
SSH_HOST="<node-ip>"
SSH_KEY="${HOME}/.ssh/openstack-key"
DRIVER_PACKAGE=""
EDGE_RELEASE="daalu-edge"
EDGE_NAMESPACE="default"
EDGE_CHART="${ROOT}/deploy/helm/daalu-edge"
EDGE_VERSION=""
TUNNEL_IP="10.200.0.2"

SKIP_HOST_PREP=0
SKIP_OPERATOR=0
SKIP_VLLM=0
SKIP_EDGE=0
SKIP_OPS_SMOKE=0
SKIP_SEED=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ops-kubeconfig)       OPS_KUBECONFIG="$2"; shift 2 ;;
    --workload-kubeconfig)  WL_KUBECONFIG="$2";  shift 2 ;;
    --gpu-node)             GPU_NODE="$2";       shift 2 ;;
    --gpu-class)            GPU_CLASS="$2";      shift 2 ;;
    --gpu-node-ssh-user)    SSH_USER="$2";       shift 2 ;;
    --gpu-node-ssh-host)    SSH_HOST="$2";       shift 2 ;;
    --gpu-node-ssh-key)     SSH_KEY="$2";        shift 2 ;;
    --driver-package)       DRIVER_PACKAGE="$2"; shift 2 ;;
    --edge-release)         EDGE_RELEASE="$2";   shift 2 ;;
    --edge-namespace)       EDGE_NAMESPACE="$2"; shift 2 ;;
    --edge-chart)           EDGE_CHART="$2";     shift 2 ;;
    --edge-version)         EDGE_VERSION="$2";   shift 2 ;;
    --tunnel-ip)            TUNNEL_IP="$2";      shift 2 ;;
    --skip-host-prep)       SKIP_HOST_PREP=1;    shift ;;
    --skip-operator)        SKIP_OPERATOR=1;     shift ;;
    --skip-vllm|--skip-nim) SKIP_VLLM=1;         shift ;;
    --skip-edge-upgrade)    SKIP_EDGE=1;         shift ;;
    --skip-ops-smoke)       SKIP_OPS_SMOKE=1;    shift ;;
    --skip-seed-skus)       SKIP_SEED=1;         shift ;;
    -h|--help) sed -n '2,92p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done

command -v kubectl >/dev/null || { echo "kubectl not on PATH"; exit 1; }
command -v helm    >/dev/null || { echo "helm not on PATH";    exit 1; }
command -v jq      >/dev/null || { echo "jq not on PATH (used for smoke test parsing)"; exit 1; }
command -v ssh     >/dev/null || { echo "ssh not on PATH";     exit 1; }

kops="kubectl --kubeconfig ${OPS_KUBECONFIG}"
kwl="kubectl --kubeconfig ${WL_KUBECONFIG}"
hwl="helm --kubeconfig ${WL_KUBECONFIG}"

echo "==> Ops cluster:      $(${kops} config current-context 2>/dev/null || echo MISSING)"
echo "==> Workload cluster: $(${kwl} config current-context 2>/dev/null || echo MISSING)"
echo "==> GPU node:         ${GPU_NODE} (class=${GPU_CLASS})"
if [[ "${SKIP_HOST_PREP}" == "0" ]]; then
  echo "==> GPU node SSH:     ${SSH_USER}@${SSH_HOST} (key=${SSH_KEY})"
fi
echo "==> Tunnel IP:        ${TUNNEL_IP}"

# ─── SSH helpers (host prep only) ──────────────────────────────────────

# ssh_run "<cmd>"          — run on the GPU host, stdout to ours.
# ssh_run_capture "<cmd>"  — same but echoes output for caller to parse.
# Common opts: accept-new keeps the first run from hanging on a
# host-key prompt; BatchMode=yes prevents password fallback if the key
# auth fails so we exit cleanly rather than blocking on a TTY.
SSH_OPTS=(
  -i "${SSH_KEY}"
  -o StrictHostKeyChecking=accept-new
  -o BatchMode=yes
  -o ConnectTimeout=10
  -o ServerAliveInterval=15
)

ssh_run()         { ssh "${SSH_OPTS[@]}" "${SSH_USER}@${SSH_HOST}" "$@"; }
ssh_run_quiet()   { ssh "${SSH_OPTS[@]}" "${SSH_USER}@${SSH_HOST}" "$@" >/dev/null 2>&1; }

# ─── Pre-flight ────────────────────────────────────────────────────────

if ! ${kwl} get node "${GPU_NODE}" >/dev/null 2>&1; then
  cat >&2 <<EOF
==> ERROR: node '${GPU_NODE}' not found in the workload cluster.

    Pass --gpu-node <name> if it is called something else, or fix the
    cluster first. The workload cluster is the CAPI/Tinkerbell one
    (kubeconfig: ${WL_KUBECONFIG}).
EOF
  exit 1
fi

if ! ${kwl} get node "${GPU_NODE}" -o jsonpath='{.metadata.labels}' \
    | grep -q 'pci-0300_10de.present'; then
  echo "==> WARN: node '${GPU_NODE}' has no NFD label pci-0300_10de.present=true."
  echo "    Is the NVIDIA card actually on this node? Continuing anyway."
fi

if [[ "${SKIP_VLLM}" == "0" && -z "${HF_TOKEN:-}" ]]; then
  cat >&2 <<EOF
==> ERROR: \$HF_TOKEN not set.

    vLLM needs a Hugging Face access token to download the Meta Llama
    3.1 8B weights (gated, free click-through). Steps:

      1. Sign in / create an account at https://huggingface.co
      2. Click through Meta's licence at
         https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct
         (one-time; covers the AWQ derivative too)
      3. Create a "Read" token at
         https://huggingface.co/settings/tokens
      4. export HF_TOKEN=<your-token>

    Then re-run this script. Or pass --skip-vllm to apply everything
    else and bring vLLM up by hand later.
EOF
  exit 1
fi

if [[ "${SKIP_HOST_PREP}" == "0" ]]; then
  if [[ ! -r "${SSH_KEY}" ]]; then
    echo "==> ERROR: SSH key '${SSH_KEY}' is not readable. Pass --gpu-node-ssh-key or --skip-host-prep." >&2
    exit 1
  fi
  if ! ssh_run_quiet "echo ok"; then
    cat >&2 <<EOF
==> ERROR: cannot SSH to ${SSH_USER}@${SSH_HOST} with key ${SSH_KEY}.

    Smoke-test by hand:
      ssh -i ${SSH_KEY} ${SSH_USER}@${SSH_HOST} echo ok

    Pass --gpu-node-ssh-host / --gpu-node-ssh-user / --gpu-node-ssh-key
    to override, or --skip-host-prep if the driver is already installed.
EOF
    exit 1
  fi
fi

if [[ "${SKIP_EDGE}" == "0" ]]; then
  if ! ${hwl} -n "${EDGE_NAMESPACE}" status "${EDGE_RELEASE}" >/dev/null 2>&1; then
    cat >&2 <<EOF
==> ERROR: helm release '${EDGE_RELEASE}' not found in workload cluster
    namespace '${EDGE_NAMESPACE}'.

    The serviceProxies upgrade builds on an existing daalu-edge release.
    Onboard the workload cluster first via the operator UI's /clusters
    wizard (see docs/cluster-tunnel-feature.md §1), then re-run this
    script. Or pass --skip-edge-upgrade to do everything else.
EOF
    exit 1
  fi
fi

# ─── 0/10 — Host prep on the GPU node (driver install over SSH) ────────

if [[ "${SKIP_HOST_PREP}" == "0" ]]; then
  echo
  echo "==> 0/10  Host prep on ${GPU_NODE} via SSH"

  # Idempotency check first — if the driver is loaded and nvidia-smi
  # enumerates the card, do nothing.
  if ssh_run_quiet "command -v nvidia-smi && nvidia-smi -L | grep -qi 'NVIDIA'"; then
    GPU_LINE=$(ssh_run "nvidia-smi -L" | head -1)
    echo "    Driver already loaded on ${SSH_HOST}: ${GPU_LINE}"
  else
    echo "    No working driver on ${SSH_HOST} yet — installing."

    # Detect Secure Boot. The Ubuntu DKMS path needs a MOK enrolment
    # which is interactive at first boot post-install. If Secure Boot
    # is enforcing, warn loudly — the install will *appear* to succeed
    # but the module won't load until MOK is enrolled.
    if ssh_run_quiet "command -v mokutil && mokutil --sb-state | grep -qi 'SecureBoot enabled'"; then
      cat >&2 <<EOF

==> ============================================================
==> WARN: SecureBoot is ENABLED on ${SSH_HOST}.

    Ubuntu's signed driver packages ship a DKMS module signed with a
    MOK that has to be enrolled on first boot via the console (you'll
    see a blue "Perform MOK management" screen during POST). If you
    can't reach that screen on a headless server, the kernel will boot
    fine but nvidia.ko will refuse to load and nvidia-smi will report
    "No devices were found".

    Options before continuing:
      (a) Disable Secure Boot in BIOS/UEFI for this host. Reboot.
      (b) Reach the iDRAC/IPMI console and enrol the MOK at first boot
          after the apt install below.
      (c) Cancel (Ctrl+C now) and install the driver out-of-band.

    Continuing in 10 s. Press Ctrl+C to abort.
==> ============================================================

EOF
      sleep 10
    fi

    # Install. Two paths:
    #   - `ubuntu-drivers install --gpgpu` picks the recommended server
    #     driver for the running kernel. Best for autopilot.
    #   - --driver-package <pkg> pins a specific package if the user
    #     wants determinism (e.g. nvidia-driver-550-server).
    #
    # `apt-get update` is `|| true` because a single broken third-party
    # PPA (e.g. vexxhost/backports without a Release file on this
    # Ubuntu release) makes apt-update exit non-zero even though the
    # main archive update succeeded. The subsequent `install` is the
    # real gate — it fails loud if the packages can't actually be
    # found.
    ssh_run "sudo DEBIAN_FRONTEND=noninteractive apt-get update -y" \
      || echo "    WARN: 'apt-get update' returned non-zero (broken third-party repo?) — continuing; install step will be the real gate."
    ssh_run "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ubuntu-drivers-common pciutils"

    if [[ -n "${DRIVER_PACKAGE}" ]]; then
      echo "    Installing pinned driver package: ${DRIVER_PACKAGE}"
      ssh_run "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ${DRIVER_PACKAGE}"
    else
      echo "    Running 'ubuntu-drivers install --gpgpu' (picks recommended server driver)"
      # `ubuntu-drivers install --gpgpu` exits non-zero if no devices
      # need a driver (already installed), so OR with `|| true`. The
      # subsequent reboot+verify is the real check.
      ssh_run "sudo DEBIAN_FRONTEND=noninteractive ubuntu-drivers install --gpgpu" || true
    fi

    # `ubuntu-drivers install --gpgpu` runs apt-get with
    # `--no-install-recommends` (hard-coded inside ubuntu-drivers), and
    # the `-headless-no-dkms-XXX-server-open` package only *recommends*
    # `nvidia-utils-XXX-server` rather than depending on it. The result
    # is a working kernel module but no `nvidia-smi` binary — which our
    # idempotency check needs to verify the install worked. Backfill
    # nvidia-utils for whichever driver version ubuntu-drivers picked.
    INSTALLED_VER=$(ssh_run "dpkg -l | awk '/^ii.*nvidia-headless-no-dkms-[0-9]+-server/{print \$2}' | head -1" 2>/dev/null \
      | grep -oP 'nvidia-headless-no-dkms-\K[0-9]+(?=-server)' || true)
    if [[ -n "${INSTALLED_VER}" ]]; then
      echo "    Backfilling nvidia-utils-${INSTALLED_VER}-server (nvidia-smi CLI)"
      ssh_run "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nvidia-utils-${INSTALLED_VER}-server" || true
    else
      echo "    WARN: couldn't determine installed nvidia driver version — skipping nvidia-utils backfill."
    fi

    # Reboot. The SSH connection will drop mid-command — that's
    # expected. systemd-reboot returns immediately so the OR isn't
    # strictly needed, but keep it for the `reboot` fallback.
    echo "    Rebooting ${SSH_HOST}..."
    ssh_run "sudo systemctl reboot" || ssh_run "sudo reboot" || true

    # Wait for the host to actually go down before probing for it to
    # come back. Otherwise we'll see the still-alive pre-reboot SSH
    # answer "yes I'm up" and skip the wait.
    sleep 20

    echo "    Waiting for ${SSH_HOST} to come back over SSH..."
    BACK=0
    for i in {1..60}; do
      if ssh_run_quiet "echo up"; then
        echo "    ${SSH_HOST} SSH is back (after ~$((i*10))s)"
        BACK=1
        break
      fi
      sleep 10
    done
    if [[ "${BACK}" != "1" ]]; then
      echo "==> ERROR: ${SSH_HOST} did not come back within 10 min after reboot." >&2
      echo "    Check the BMC/IPMI console. The script cannot help past this." >&2
      exit 1
    fi

    # Verify the driver actually loaded. This is the real "did it work"
    # gate — `apt install` succeeding doesn't mean the module loaded
    # (Secure Boot, DKMS build failure, kernel mismatch all silently
    # produce "installed but not loaded").
    echo "    Verifying nvidia-smi on ${SSH_HOST}..."
    if ! ssh_run "command -v nvidia-smi && nvidia-smi -L" 2>&1 | grep -qi 'NVIDIA'; then
      cat >&2 <<EOF
==> ERROR: nvidia-smi still doesn't enumerate the GPU on ${SSH_HOST}
    after reboot.

    Common causes:
      * Secure Boot enabled, MOK not enrolled — see the warning above.
      * DKMS module didn't build for the running kernel. SSH in and
        check: dkms status; ls /lib/modules/\$(uname -r)/updates/dkms/
      * Card not seated / not powered. Check: lspci | grep -i nvidia
        (should list the RTX 2000 Ada).

    The validator container in the GPU Operator will retry every 5s
    once the host driver is sorted; you can re-run this script then
    with --skip-host-prep.
EOF
      exit 1
    fi
    GPU_LINE=$(ssh_run "nvidia-smi -L" | head -1)
    echo "    Driver loaded: ${GPU_LINE}"
  fi

  # Wait for kubelet to report the node Ready again. After a reboot the
  # node briefly flips NotReady; we don't want to race the operator
  # install against that.
  echo "    Waiting for ${GPU_NODE} to be Ready in Kubernetes..."
  if ! ${kwl} wait --for=condition=Ready "node/${GPU_NODE}" --timeout=300s; then
    echo "==> ERROR: ${GPU_NODE} did not return to Ready within 5 min." >&2
    exit 1
  fi
else
  echo
  echo "==> 0/10  Host prep — skipped (--skip-host-prep)"
fi

# ─── 1/10 — Label the GPU node ─────────────────────────────────────────

echo
echo "==> 1/10  Labelling ${GPU_NODE} in workload cluster"
${kwl} label node "${GPU_NODE}" \
  nvidia.com/gpu.present=true \
  "gpu-class=${GPU_CLASS}" \
  --overwrite

# ─── 2/10 — GPU Operator ───────────────────────────────────────────────

if [[ "${SKIP_OPERATOR}" == "0" ]]; then
  echo
  echo "==> 2/10  Installing NVIDIA GPU Operator on workload cluster (kubeadm variant)"
  KUBECONFIG="${WL_KUBECONFIG}" \
    bash "${ROOT}/deploy/k8s/gpu/00-install-gpu-operator.sh" \
      --variant kubeadm \
      --tolerate-control-plane 1
else
  echo
  echo "==> 2/10  GPU Operator — skipped (--skip-operator)"
fi

# ─── 3/10 — Time-slicing + ClusterPolicy patch + per-node profile ──────
#
# Operator path: applies the time-slicing ConfigMap in the gpu-operator
# namespace and patches the ClusterPolicy to point at it.
#
# Standalone device-plugin path (--skip-operator): the time-slicing
# config lives in the nvidia-device-plugin namespace and is consumed by
# the device-plugin DaemonSet directly — no ClusterPolicy to patch. We
# just verify that the node is already advertising nvidia.com/gpu
# capacity so downstream steps (NIM scheduling) can proceed.

if [[ "${SKIP_OPERATOR}" == "0" ]]; then
  echo
  echo "==> 3/10  Time-slicing config + ClusterPolicy patch + pinning ${GPU_NODE} to ${GPU_CLASS}"
  ${kwl} apply -f "${ROOT}/deploy/k8s/gpu/01-time-slicing.yaml"

  for i in {1..30}; do
    if ${kwl} get clusterpolicy/cluster-policy -n gpu-operator >/dev/null 2>&1; then
      break
    fi
    echo "    waiting for clusterpolicy/cluster-policy... (${i}/30)"
    sleep 5
  done

  ${kwl} patch clusterpolicy/cluster-policy -n gpu-operator --type merge -p \
    "{\"spec\":{\"devicePlugin\":{\"config\":{\"name\":\"time-slicing-config\",\"default\":\"${GPU_CLASS}\"}}}}"

  ${kwl} label node "${GPU_NODE}" "nvidia.com/device-plugin.config=${GPU_CLASS}" --overwrite
else
  echo
  echo "==> 3/10  Time-slicing + ClusterPolicy patch — skipped (--skip-operator)"
  echo "    Standalone device-plugin manages time-slicing via its own ConfigMap."
fi

# Wait for the device plugin (Operator-managed or standalone) to
# actually advertise nvidia.com/gpu on the node. Without this, NIM will
# FailedScheduling with "Insufficient nvidia.com/gpu".
echo "    Waiting for ${GPU_NODE} to advertise nvidia.com/gpu capacity..."
GPU_READY=0
for i in {1..60}; do
  GPU_CAP=$(${kwl} get node "${GPU_NODE}" -o jsonpath='{.status.capacity.nvidia\.com/gpu}' 2>/dev/null || echo "")
  if [[ -n "${GPU_CAP}" && "${GPU_CAP}" != "0" ]]; then
    echo "    ${GPU_NODE} now advertises nvidia.com/gpu=${GPU_CAP}"
    GPU_READY=1
    break
  fi
  sleep 5
done
if [[ "${GPU_READY}" != "1" ]]; then
  echo "==> ERROR: ${GPU_NODE} did not advertise nvidia.com/gpu within 5 min." >&2
  if [[ "${SKIP_OPERATOR}" == "0" ]]; then
    echo "    Check the Operator validator:" >&2
    echo "      ${kwl} -n gpu-operator logs -l app=nvidia-operator-validator -c driver-validation --tail=20" >&2
  else
    echo "    Check the standalone device-plugin:" >&2
    echo "      ${kwl} -n nvidia-device-plugin get pods" >&2
    echo "      ${kwl} -n nvidia-device-plugin logs -l app=nvidia-device-plugin --tail=50" >&2
  fi
  exit 1
fi

# ─── 4/10 — HF token secret in the workload cluster's `daalu` ns ───────

if [[ "${SKIP_VLLM}" == "0" ]]; then
  echo
  echo "==> 4/10  HF token secret in ns 'daalu' (workload cluster)"
  ${kwl} create namespace daalu --dry-run=client -o yaml | ${kwl} apply -f -

  ${kwl} create secret generic hf-token \
    --from-literal=token="${HF_TOKEN}" \
    -n daalu \
    --dry-run=client -o yaml | ${kwl} apply -f -
else
  echo
  echo "==> 4/10  HF token secret — skipped (--skip-vllm)"
fi

# ─── 5/10 — vLLM Deployment + Service + PDB + NetworkPolicy ────────────

if [[ "${SKIP_VLLM}" == "0" ]]; then
  echo
  echo "==> 5/10  Applying vLLM + NetworkPolicy"
  ${kwl} apply -f "${ROOT}/deploy/k8s/gpu/03-vllm-llama-classifier.yaml"
  ${kwl} apply -f "${ROOT}/deploy/k8s/gpu/05-network-policy.yaml"

  echo
  echo "==> 6/10  Waiting for vLLM rollout (first start downloads ~5 GB of AWQ weights — ~5–10 min on a normal pipe)"
  if ! ${kwl} -n daalu rollout status deploy/llama-classifier --timeout=15m; then
    echo "==> ERROR: vLLM rollout did not become Ready in 15 min." >&2
    echo "    Inspect: ${kwl} -n daalu describe pod -l app=llama-classifier" >&2
    echo "             ${kwl} -n daalu logs -l app=llama-classifier --tail=200" >&2
    exit 1
  fi
else
  echo
  echo "==> 5-6/10  vLLM — skipped (--skip-vllm)"
fi

# ─── 7/10 — daalu-edge upgrade with serviceProxies ─────────────────────

if [[ "${SKIP_EDGE}" == "0" ]]; then
  echo
  echo "==> 7/10  Upgrading daalu-edge with serviceProxies (vLLM :8000, DCGM :9400+:9401, RGW :8080)"
  # NOTE: DCGM is scraped PER GPU NODE. The exporter's ClusterIP Service
  # load-balances to one backend pod per scrape, so a single dcgm proxy only
  # ever surfaces ONE node — with >1 GPU node (gpu-node-1 + gpu-node-3) the AI Factory page
  # showed a single card. Each node's exporter is reached via the
  # nvidia-dcgm-exporter-nodes NodePort (externalTrafficPolicy: Local — see
  # deploy/k8s/gpu/14-dcgm-nodeport.yaml) at <nodeInternalIP>:30940, fronted by
  # one socat proxy per node (9400→gpu-node-1, 9401→gpu-node-3). The hub scrapes both ports
  # (deploy/k8s/monitoring/07-gpu-tunnel-dcgm.yaml).
  # TODO(gpu-controller): generate the per-node proxies + node IPs from the
  # gpu_tenant rows so this scales without hand-maintained node IPs.
  # --reset-then-reuse-values: discard the old release's merged values,
  # take the NEW chart's defaults (this is how apiserverProxy.enabled
  # etc. introduced after chart 0.2.0 actually get applied), then merge
  # in only the user-supplied values from the previous install (cluster
  # slug, hub endpoint, etc.). Requires Helm >= 3.14.
  # `persistence.enabled=false` falls back to emptyDir for the WG state
  # volume. Used here because the workload cluster's only StorageClass
  # (`general`, Ceph RBD) is currently wedged — the rook-ceph operator
  # is stuck and no csi-rbdplugin DaemonSet pods are registered, so any
  # PVC against `general` will block forever on FailedAttachVolume.
  # Trade-off: the WG keypair regenerates on pod restart, which forces a
  # re-onboard via the operator UI to mint a fresh bootstrap token. Flip
  # this back to default once Ceph is repaired.
  HELM_ARGS=(
    upgrade "${EDGE_RELEASE}" "${EDGE_CHART}"
    -n "${EDGE_NAMESPACE}"
    --reset-then-reuse-values
    # persistence ON: the workload cluster's Ceph (`general`, RBD) is healthy
    # again, so the WG state PVC binds. Keeping it means the WG keypair SURVIVES
    # pod restarts — no re-onboard needed when this chart is upgraded (e.g. to
    # add a serviceProxy). Do NOT set this false unless Ceph is wedged again.
    --set 'persistence.enabled=true'
    --set 'serviceProxies[0].name=vllm'
    --set 'serviceProxies[0].listenPort=8000'
    --set 'serviceProxies[0].upstream=llm-classifier.daalu.svc.cluster.local:80'
    # Per-node DCGM scrape: one socat proxy per GPU node, each pinned to that
    # node's exporter via the NodePort (node IPs are the workload cluster's
    # stable kubeadm InternalIPs). Add one entry per additional GPU node.
    --set 'serviceProxies[1].name=dcgm-gpu-node-1'
    --set 'serviceProxies[1].listenPort=9400'
    --set 'serviceProxies[1].upstream=<node-ip>:30940'
    --set 'serviceProxies[2].name=dcgm-gpu-node-3'
    --set 'serviceProxies[2].listenPort=9401'
    --set 'serviceProxies[2].upstream=<node-ip>:30940'
    # AIPerf artifacts: expose the in-cluster Ceph RGW on the tunnel so the hub
    # (gpu-controller + daalu-api) can read the uploaded artifact tree back for
    # the per-concurrency curve + downloads. Reached at http://<tunnelIp>:8080.
    --set 'serviceProxies[3].name=rgw'
    --set 'serviceProxies[3].listenPort=8080'
    --set 'serviceProxies[3].upstream=rook-ceph-rgw-ceph.openstack.svc.cluster.local:80'
  )
  if [[ -n "${EDGE_VERSION}" ]]; then
    HELM_ARGS+=( --version "${EDGE_VERSION}" )
  fi

  KUBECONFIG="${WL_KUBECONFIG}" helm "${HELM_ARGS[@]}"

  echo
  echo "==> Waiting for daalu-edge pod to come back up (strategy: Recreate)"
  ${kwl} -n "${EDGE_NAMESPACE}" rollout status deploy/daalu-edge --timeout=3m
else
  echo
  echo "==> 7/10  daalu-edge upgrade — skipped (--skip-edge-upgrade)"
fi

# ─── 8/10 — Cross-tunnel smoke test from ops side ──────────────────────

if [[ "${SKIP_OPS_SMOKE}" == "0" ]]; then
  echo
  echo "==> 8/10  Cross-tunnel smoke test from ops cluster"
  if ! ${kops} -n daalu-automation get deploy/daalu-api >/dev/null 2>&1; then
    echo "    WARN: deploy/daalu-api not found in ops cluster — skipping smoke test."
  else
    OK=0
    for i in {1..12}; do
      OUT=$(${kops} -n daalu-automation exec deploy/daalu-api -- \
              curl -fsS --max-time 5 "http://${TUNNEL_IP}:8000/v1/models" 2>/dev/null || true)
      if [[ -n "${OUT}" ]] && echo "${OUT}" | jq -e '.data[0].id' >/dev/null 2>&1; then
        echo "    OK — vLLM reachable on ${TUNNEL_IP}:8000 (model: $(echo "${OUT}" | jq -r '.data[0].id'))"
        OK=1
        break
      fi
      echo "    waiting for tunnel + vLLM... (${i}/12)"
      sleep 5
    done
    if [[ "${OK}" != "1" ]]; then
      echo "==> ERROR: cross-tunnel smoke test failed after 60s." >&2
      echo "    Check the daalu-edge pod's svcproxy-vllm container:" >&2
      echo "      ${kwl} -n ${EDGE_NAMESPACE} logs deploy/daalu-edge -c svcproxy-vllm" >&2
      echo "    And the tunnel row on the ops side via /clusters." >&2
      exit 1
    fi
  fi
else
  echo
  echo "==> 8/10  Cross-tunnel smoke test — skipped (--skip-ops-smoke)"
fi

# ─── 9/10 — Seed SKUs on ops side ──────────────────────────────────────

if [[ "${SKIP_SEED}" == "0" ]]; then
  echo
  echo "==> 9/10  Seeding SKU catalog on ops cluster"
  if ${kops} -n daalu-automation get deploy/daalu-api >/dev/null 2>&1; then
    # The CLI was reorganised: `seed-skus` is a separate subcommand in
    # current source but older deployed images don't have it. `daalu
    # seed` calls _seed_skus() internally in the new code and is
    # idempotent in either case (creates tenant + admin + SKUs). On
    # very old images that pre-date the billing tables, this is a no-op
    # for SKUs — fall back to a warning.
    if ${kops} -n daalu-automation exec deploy/daalu-api -c api -- daalu seed-skus 2>/dev/null; then
      :
    else
      echo "    'daalu seed-skus' not in deployed image; falling back to 'daalu seed'"
      ${kops} -n daalu-automation exec deploy/daalu-api -c api -- daalu seed || \
        echo "    WARN: seed failed — deployed image may pre-date the billing tables; redeploy daalu-api to land migration 0006_billing.py then re-run."
    fi
  else
    echo "    WARN: deploy/daalu-api not found in ops cluster — skipping seed."
  fi
else
  echo
  echo "==> 9/10  Seed SKUs — skipped (--skip-seed-skus)"
fi

# ─── 10/10 — Print the exact ops-side router-flip commands ─────────────

cat <<EOF

==> 10/10  Manual final step — flip the LLM router on the ops cluster.

    This step is kept manual because daalu-automation-secrets carries
    multiple keys this script doesn't know the live values of, and an
    incorrect merge would knock out other settings.

    Run these two commands against the ops cluster (kubeconfig:
    ${OPS_KUBECONFIG}):

      kubectl --kubeconfig ${OPS_KUBECONFIG} -n daalu-automation patch \\
        secret daalu-automation-secrets --type=merge -p '{
          "stringData": {
            "LLM_LOCAL_BASE_URL": "http://${TUNNEL_IP}:8000/v1",
            "LLM_LOCAL_MODEL_CLASSIFIER": "meta/llama-3.1-8b-instruct"
          }
        }'

      kubectl --kubeconfig ${OPS_KUBECONFIG} -n daalu-automation \\
        rollout restart deploy/daalu-api

    Verify (per docs/go-live-checklist.md §A.5):

      kubectl --kubeconfig ${OPS_KUBECONFIG} -n daalu-automation exec \\
        deploy/daalu-api -- curl -s http://${TUNNEL_IP}:8000/v1/models | jq

      # Open https://app.daalu.io/billing — 'Local GPU' pill should be online.
      # Trigger a classifier call and check usage_events for tier='local'.
EOF
