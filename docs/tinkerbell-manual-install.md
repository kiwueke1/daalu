# Daalu: Manual Tinkerbell Stack Installation Guide

This guide walks through every step that `daalu mgmt` automates for the Tinkerbell bare-metal
provisioning stack. Use it when you want to:

- Understand what the automation is doing at every level
- Debug a failed automated run
- Walk through the install manually after running `daalu mgmt` with `--skip-provisioning-stack`

The guide also covers how to bring up a workload Kubernetes cluster on bare-metal nodes once
the Tinkerbell stack is in place.

---

## Topology Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│  Management Node  (e.g. 192.168.0.171)                               │
│                                                                      │
│  ┌────────────┐  ┌──────────────┐  ┌─────────────┐  ┌────────────┐  │
│  │ tink-server │  │ tink-worker  │  │    hegel     │  │    smee    │  │
│  │  (workflow  │  │  (runs on    │  │  (metadata   │  │ (DHCP/iPXE │  │
│  │   API)      │  │  bare-metal) │  │   server)    │  │  server)   │  │
│  └────────────┘  └──────────────┘  └─────────────┘  └────────────┘  │
│  ┌──────────────┐  ┌──────────────────────────────────────────────┐  │
│  │ image-server  │  │  CAPI / CAPT / Rufio controllers             │  │
│  │  (nginx/80)   │  │  (Cluster API + Tinkerbell provider + BMC)   │  │
│  └──────────────┘  └──────────────────────────────────────────────┘  │
│                                                                      │
│  Provisioning NIC: ens19  (e.g. 10.10.0.9/16)                       │
└──────────────────────────────────────────────────────────────────────┘
         │ PXE / DHCP (provisioning network 10.10.0.0/16)
         ▼
┌───────────────────┐    ┌───────────────────┐
│  cp01 bare-metal  │    │  cp02 bare-metal  │
│  MAC ac:1f:6b:01  │    │  MAC ac:1f:6b:01  │
│  BMC 192.168.0.70 │    │  BMC 192.168.0.69 │
└───────────────────┘    └───────────────────┘
```

**Key networks:**

| Network | Purpose | Example CIDR |
|---------|---------|--------------|
| Management | SSH to mgmt node, BMC access | 192.168.0.0/24 |
| Provisioning | PXE boot, DHCP for bare-metal nodes | 10.10.0.0/16 |
| Pod network (workload cluster) | Internal K8s pods | 10.201.0.0/16 |

---

## Step 0 — Bootstrap Kubernetes (automated via `daalu mgmt`)

The code reference for this section is
`src/daalu/bootstrap/mgmt/manager.py` → `MgmtClusterManager.deploy()` and
`src/daalu/bootstrap/mgmt/k8s_installer.py` → `K8sInstaller.install()`.

Run `daalu mgmt` with `--skip-provisioning-stack` to install only Kubernetes and Cilium,
leaving the Tinkerbell stack for manual installation:

```bash
export WORKSPACE_ROOT=$PWD

daalu mgmt cluster-defs/cluster.yaml --skip-provisioning-stack --skip-harbor
```

What `daalu mgmt --skip-provisioning-stack` does (in order):

1. **SSH connect** (`manager.py:64`) — connects to the fresh Ubuntu node using paramiko
2. **Passwordless sudo** (`k8s_installer.py:145`) — writes `/etc/sudoers.d/daalu-mgmt-nopasswd`
3. **Provisioning interface** (`k8s_installer.py:197`) — assigns static IP to the configured provisioning interface (e.g. `ens19`), writes
   `/etc/netplan/60-provisioning-static.yaml`, runs `netplan apply`
4. **Swap disabled** (`k8s_installer.py:244`) — `swapoff -a`, comments out swap in `/etc/fstab`
5. **Kernel modules** (`k8s_installer.py:253`) — loads `overlay`, `br_netfilter`
6. **sysctl** (`k8s_installer.py:264`) — enables `ip_forward` and bridge netfilter
7. **containerd** (`k8s_installer.py:278`) — installs containerd, enables `SystemdCgroup`
8. **kubeadm/kubelet/kubectl** (`k8s_installer.py:300`) — adds Kubernetes apt repo, installs
9. **kubeadm init** (`k8s_installer.py:336`) — initialises the cluster with `--pod-network-cidr`
   and `--skip-phases=addon/kube-proxy` (Cilium replaces kube-proxy)
10. **kubeconfig saved** (`manager.py:78`) — written to `~/.kube/daalu-mgmt-config`
11. **Cilium installed** (`k8s_installer.py:69`) — Helm installs Cilium CNI with `kubeProxyReplacement=true`

After this completes, verify the cluster is healthy:

```bash
export KUBECONFIG=~/.kube/daalu-mgmt-config

# Shows the management node. Should be Ready after Cilium starts.
kubectl get nodes -o wide

# All kube-system pods should be Running (coredns, cilium, cilium-operator)
kubectl get pods -n kube-system

# Single-node cluster — control-plane taint was already removed by daalu
kubectl describe node | grep Taint
# Expected: <none>
```

---

## Step 1 — Install cert-manager

**Code reference:** `src/daalu/bootstrap/mgmt/tinkerbell_installer.py:82` →
`TinkerbellInstaller._install_cert_manager()`

cert-manager provides TLS certificate automation used by CAPI admission webhooks.
The local chart is at `assets/cert-manager/charts/`.

```bash
# Check if already installed
kubectl get deploy cert-manager -n cert-manager 2>/dev/null

# If not installed, run:
helm upgrade --install cert-manager assets/cert-manager/charts/cert-manager-v1.20.0.tgz \
  --namespace cert-manager \
  --create-namespace \
  --set installCRDs=true

# Wait for all three deployments to roll out
kubectl -n cert-manager rollout status deploy/cert-manager --timeout=5m
kubectl -n cert-manager rollout status deploy/cert-manager-webhook --timeout=5m
kubectl -n cert-manager rollout status deploy/cert-manager-cainjector --timeout=5m
```

The automation (`tinkerbell_installer.py:117`) then polls the cert-manager webhook using a
dry-run Issuer create. This is important because clusterctl will fail if the webhook is not
ready even though the pod shows Running. Replicate that check:

```bash
# Poll until this command succeeds (up to 2 minutes):
kubectl create --dry-run=server -f - <<'EOF'
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: webhook-readiness-probe
  namespace: cert-manager
spec:
  selfSigned: {}
EOF
```

**Verify:**

```bash
# All three deployments Ready
kubectl get deploy -n cert-manager
# NAME                      READY   UP-TO-DATE   AVAILABLE
# cert-manager              1/1     1            1
# cert-manager-cainjector   1/1     1            1
# cert-manager-webhook      1/1     1            1

# CRDs installed
kubectl get crd | grep cert-manager
# certificates.cert-manager.io
# clusterissuers.cert-manager.io
# issuers.cert-manager.io
# (etc.)
```

---

## Step 2 — Install Cluster API (CAPI core)

**Code reference:** `src/daalu/bootstrap/mgmt/tinkerbell_installer.py:149` →
`TinkerbellInstaller._install_capi()`

CAPI provides the declarative API for managing Kubernetes clusters. It installs four
controllers: `capi-controller-manager` (core), kubeadm bootstrap, and kubeadm
control-plane providers.

Version used: `v1.12.0` (from `MgmtClusterConfig.capi_version`, `models.py:65`).

```bash
# Install clusterctl if not present
curl -L https://github.com/kubernetes-sigs/cluster-api/releases/download/v1.12.0/clusterctl-linux-amd64 \
  -o /usr/local/bin/clusterctl
chmod +x /usr/local/bin/clusterctl

# Initialise CAPI core + kubeadm bootstrap + kubeadm control-plane
clusterctl --kubeconfig ~/.kube/daalu-mgmt-config \
  init \
  --core cluster-api:v1.12.0 \
  --bootstrap kubeadm:v1.12.0 \
  --control-plane kubeadm:v1.12.0 \
  -v5
```

**Verify:**

```bash
# Three namespaces created by CAPI
kubectl get ns | grep -E 'capi|kubeadm'
# capi-kubeadm-bootstrap-system
# capi-kubeadm-control-plane-system
# capi-system

# Core controller manager ready
kubectl get deploy capi-controller-manager -n capi-system
# NAME                      READY   UP-TO-DATE   AVAILABLE
# capi-controller-manager   1/1     1            1

# CAPI CRDs installed
kubectl get crd | grep cluster.x-k8s.io | head -10
# clusters.cluster.x-k8s.io
# machinedeploy... etc.

# Check controller logs for errors
kubectl logs -n capi-system deploy/capi-controller-manager --tail=20
```

---

## Step 3 — Install Tinkerbell Stack (Tink + Hegel + SMEE + Rufio)

**Code reference:** `src/daalu/bootstrap/mgmt/tinkerbell_installer.py:225` →
`TinkerbellInstaller._install_tinkerbell_stack()`

> **Why before CAPT?** CAPT's controller (`capt-controller-manager`) starts watching for
> `Workflow.tinkerbell.org` and `Job.bmc.tinkerbell.org` CRDs immediately on startup. If those
> CRDs don't exist yet, CAPT times out after 2 minutes and crashes into CrashLoopBackOff. The
> Tinkerbell Helm chart is what installs those CRDs (along with Rufio), so it **must** be
> installed before `clusterctl init --infrastructure tinkerbell`.

The Tinkerbell Helm chart deploys:

| Component | Purpose |
|-----------|---------|
| **tink-server** | Stores and serves Hardware, Template, and Workflow CRs via gRPC |
| **hegel** | Instance metadata server — bare-metal nodes query it for cloud-init user-data |
| **SMEE** | DHCP server + TFTP + HTTP iPXE server — intercepts PXE boot requests and chains to HookOS |
| **Rufio** | BMC controller — translates Kubernetes Job CRs into Redfish/IPMI power commands |

The chart is at `assets/tinkerbell/charts/stack-0.6.3.tgz`.

The critical Helm value is `global.rbac.type=ClusterRole` (`tinkerbell_installer.py:263`).
Without this, tink-controller only watches the `tinkerbell` namespace. CAPT creates Workflow
and Template CRs in the cluster namespace (e.g., `default`), so tink-controller would never
see them and the tink-worker running in HookOS would never receive any workflow actions.

```bash
# Confirm the chart file exists
ls -lh assets/tinkerbell/charts/stack-0.6.3.tgz

PROVISIONING_IP=10.10.0.9      # your provisioning interface IP
POD_CIDR=172.16.0.0/16         # mgmt cluster pod CIDR (used by nginx reverse proxy)

helm upgrade --install tinkerbell assets/tinkerbell/charts/stack-0.6.3.tgz \
  --namespace tinkerbell \
  --create-namespace \
  --kubeconfig ~/.kube/daalu-mgmt-config \
  --set global.publicIP=$PROVISIONING_IP \
  --set "global.trustedProxies={$POD_CIDR}" \
  --set global.rbac.type=ClusterRole \
  --wait \
  --timeout 10m
```

**Verify:**

```bash
# All Tinkerbell pods running
kubectl get pods -n tinkerbell
# NAME                            READY   STATUS    RESTARTS   AGE
# smee-xxxx                       1/1     Running   0          2m
# tink-server-xxxx                1/1     Running   0          2m
# hegel-xxxx                      1/1     Running   0          2m
# rufio-controller-manager-xxxx   1/1     Running   0          2m

# ClusterRole RBAC was applied (not namespaced Role)
kubectl get clusterrole | grep tinkerbell

# Tinkerbell CRDs now present (required by CAPT in the next step)
kubectl get crd | grep -E "tinkerbell.org|bmc.tinkerbell"
# hardware.tinkerbell.org
# templates.tinkerbell.org
# workflows.tinkerbell.org
# machines.bmc.tinkerbell.org
# jobs.bmc.tinkerbell.org

# tink-server and hegel services
kubectl get svc -n tinkerbell
# NAME          TYPE        CLUSTER-IP     EXTERNAL-IP   PORT(S)
# tink-server   ClusterIP   10.x.x.x       <none>        42113/TCP
# hegel         ClusterIP   10.x.x.x       <none>        50061/TCP

# Check tink-server logs for any startup errors
kubectl logs -n tinkerbell deploy/tink-server --tail=30
```

---

## Step 4 — Install CAPT (Cluster API Provider Tinkerbell)

**Code reference:** `src/daalu/bootstrap/mgmt/tinkerbell_installer.py:181` →
`TinkerbellInstaller._install_capt()` and
`TinkerbellInstaller._register_capt_in_clusterctl_config()` (line 913)

CAPT is a community provider — not built into clusterctl — so it must be registered in the
clusterctl config file before `clusterctl init` will accept `--infrastructure tinkerbell`.
The code writes `~/.config/cluster-api/clusterctl.yaml` with the CAPT GitHub release URL
(`tinkerbell_installer.py:952`).

`clusterctl init --infrastructure tinkerbell` installs only the three CAPI-side CRDs
(`TinkerbellCluster`, `TinkerbellMachine`, `TinkerbellMachineTemplate`) and the
`capt-controller-manager`. The Tinkerbell stack CRDs and Rufio were installed in Step 3.

Version used: `v0.6.0` (from `MgmtClusterConfig.capt_version`, `models.py:67`).

### 4a. Register CAPT in clusterctl config

```bash
mkdir -p ~/.config/cluster-api

cat >> ~/.config/cluster-api/clusterctl.yaml <<'EOF'

providers:
  - name: tinkerbell
    url: https://github.com/tinkerbell/cluster-api-provider-tinkerbell/releases/v0.6.0/infrastructure-components.yaml
    type: InfrastructureProvider
EOF
```

Verify the entry was written:

```bash
cat ~/.config/cluster-api/clusterctl.yaml
# Should contain a single providers: block with the tinkerbell entry
```

### 4b. Install CAPT

`TINKERBELL_IP` is substituted into the CAPT infrastructure-components.yaml at install time
(`tinkerbell_installer.py:204`). It must be the IP that bare-metal nodes can reach the Tink
server and SMEE on — i.e., your provisioning interface IP.

```bash
TINKERBELL_IP=10.10.0.9   # replace with your provisioning IP

TINKERBELL_IP=$TINKERBELL_IP \
  clusterctl --kubeconfig ~/.kube/daalu-mgmt-config \
  init \
  --infrastructure tinkerbell:v0.6.0 \
  -v5
```

**Verify:**

```bash
# capt-system namespace created
kubectl get ns capt-system

# CAPT controller ready (should come up cleanly now that Tinkerbell CRDs exist)
kubectl get deploy capt-controller-manager -n capt-system
# NAME                       READY   UP-TO-DATE   AVAILABLE
# capt-controller-manager    1/1     1            1

# CAPT infrastructure provider CRDs added by clusterctl
kubectl get crd | grep infrastructure.cluster.x-k8s.io | grep tinkerbell
# tinkerbellclusters.infrastructure.cluster.x-k8s.io
# tinkerbellmachines.infrastructure.cluster.x-k8s.io
# tinkerbellmachinetemplates.infrastructure.cluster.x-k8s.io

# Confirm no CrashLoopBackOff
kubectl get pod -n capt-system
# NAME                                    READY   STATUS    RESTARTS
# capt-controller-manager-xxxx           1/1     Running   0

# Check logs — should show controllers starting cleanly with no CRD errors
kubectl logs -n capt-system deploy/capt-controller-manager --tail=20
```

---

## Step 5 — Configure SMEE DHCP

**Code reference:** `src/daalu/bootstrap/mgmt/tinkerbell_installer.py:271` →
`TinkerbellInstaller._configure_smee()`

SMEE must run in `hostNetwork: true` (`tinkerbell_installer.py:303`) because DHCP broadcasts
are L2 and can only be received by a process listening on the physical interface. In pod
network mode, SMEE cannot see broadcast packets from bare-metal nodes.

The patch adds environment variables telling SMEE its own IP, the DHCP range to serve, and
the default gateway to give clients.

```bash
PROVISIONING_IP=10.10.0.9
DHCP_START=10.10.0.100
DHCP_END=10.10.0.200
GATEWAY=10.10.0.9
DNS=8.8.8.8

kubectl patch deployment smee -n tinkerbell \
  --kubeconfig ~/.kube/daalu-mgmt-config \
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

kubectl rollout status deployment/smee -n tinkerbell --timeout=3m
```

**Verify SMEE is serving:**

```bash
# With hostNetwork: true, kubectl shows the node's PRIMARY IP (e.g. 192.168.0.171),
# not the provisioning IP. This is expected — hostNetwork gives SMEE access to ALL
# interfaces on the host including ens19 at 10.10.0.9. The IP shown here is just
# which node IP Kubernetes uses for pod routing, not what SMEE binds to.
kubectl get pod -n tinkerbell -l app=smee -o wide
# IP column: 192.168.0.171  ← node primary IP, normal for hostNetwork pods

# The real check: log in to the management node and verify SMEE holds the DHCP port
# and confirm all ports it is listening on (TCP + UDP):
sudo ss -ulnp | grep :67
# Expected: smee bound to 0.0.0.0:67

sudo ss -tlnp -ulnp 2>/dev/null | grep smee
# SMEE serves:
#   UDP 67   — DHCP (receives PXE boot requests from bare-metal nodes)
#   UDP 69   — TFTP (serves ipxe.efi for UEFI or undionly.kpxe for BIOS)
#   UDP 514  — Syslog (receives log output from HookOS on bare-metal nodes)
#   TCP 7171 — HTTP (serves iPXE scripts to nodes that loaded iPXE via TFTP)
# NOTE: port 80 is NOT SMEE — that is the image server deployed in Step 6.

# Test: SMEE's iPXE HTTP endpoint (port 7171)
curl http://$PROVISIONING_IP:7171/ipxe
# Should return an iPXE boot script

# Check SMEE logs — shows DHCP leases and iPXE chains as nodes boot
kubectl logs -n tinkerbell deploy/smee --tail=30
```

---

## Step 6 — Deploy the Image Server

**Code reference:** `src/daalu/bootstrap/mgmt/tinkerbell_installer.py:444` →
`TinkerbellInstaller._deploy_image_server()`

The image server is an nginx pod running in `hostNetwork: true` (`tinkerbell_installer.py:487`)
that serves OS images from `/var/www/images` on the management node. Bare-metal nodes
download the Ubuntu image over HTTP during the `image2disk` workflow action.

The Service uses `externalIPs` pinned to the provisioning IP and `NodePort: 30080`, but
because the pod is on hostNetwork, nginx actually binds directly to port 80 on the
provisioning interface — the most reliable way to serve to bare-metal nodes outside the
cluster network.

```bash
kubectl apply --kubeconfig ~/.kube/daalu-mgmt-config -f - <<'EOF'
apiVersion: v1
kind: Namespace
metadata:
  name: tinkerbell
---
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
    - 10.10.0.9     # replace with your provisioning IP
EOF

kubectl rollout status deploy/image-server -n tinkerbell --timeout=3m
```

**Verify:**

```bash
# Pod running on the management node
kubectl get pod -n tinkerbell -l app=image-server -o wide
# IP column: 10.10.0.9 (hostNetwork, binds directly)

# Test the image server is reachable (from management node or any provisioning-network host)
curl -I http://10.10.0.9/
# HTTP/1.1 200 OK  (empty directory listing until you add images)
```

---

## Step 7 — Prepare OS Images on the Image Server

The workflow template (`assets/tinkerbell/templates/ubuntu-kubeadm.yaml`) downloads the OS
image directly from the image server during provisioning using `wget`. The image URL format
used in the workflow files is:

```
http://10.10.0.9/UBUNTU_24.04_NODE_IMAGE_K8S_v1.35.0.raw.gz
```

The image must be a raw disk image (not an ISO) compressed with gzip. Images are served from
`/var/www/images` on the management node (mounted into the nginx pod via hostPath,
`tinkerbell_installer.py:504`).

### Getting a Kubernetes-ready Ubuntu image

The simplest approach is to use an image-builder tool or download a pre-built image.
The image name in the workflow files is `UBUNTU_24.04_NODE_IMAGE_K8S_v1.35.0.raw.gz`.

```bash
# Log in to the management node, then:

# Create the images directory
sudo mkdir -p /var/www/images
sudo chmod 755 /var/www/images

# Option A: Build with image-builder (recommended for production)
# https://image-builder.sigs.k8s.io/capi/providers/raw
git clone https://github.com/kubernetes-sigs/image-builder.git
cd image-builder/images/capi
# Edit packer/raw/ubuntu-2404.json for your settings
make build-raw-ubuntu-2404
# Copy the output .gz to /var/www/images/

# Option B: Use a community pre-built image (for testing only)
# Download and rename to match the URL in your workflow files
sudo wget -O /var/www/images/UBUNTU_24.04_NODE_IMAGE_K8S_v1.35.0.raw.gz \
  https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img
# Note: cloud images are qcow2 format — convert to raw first:
# qemu-img convert -f qcow2 -O raw input.img output.raw && gzip output.raw

# Verify the image is present and accessible
ls -lh /var/www/images/
curl -I http://10.10.0.9/UBUNTU_24.04_NODE_IMAGE_K8S_v1.35.0.raw.gz
# HTTP/1.1 200 OK
# Content-Length: <size in bytes>
```

**From the image server pod's perspective** (`tinkerbell_installer.py:492`):
- `/var/www/images` on the host is mounted at `/usr/share/nginx/html` inside nginx
- nginx serves all files at the root URL
- The workflow's `image_url` value must match the filename exactly

---

## Step 8 — Register Hardware CRs

**Code reference:** `src/daalu/bootstrap/mgmt/tinkerbell_installer.py:329` →
`TinkerbellInstaller._register_hardware()`

Each bare-metal node requires three Kubernetes resources applied together
(`tinkerbell_installer.py:428`):

1. **Secret** — BMC credentials (username/password for Redfish/IPMI)
2. **Rufio Machine** — BMC connection spec (endpoint URL + secret reference)
3. **Tinkerbell Hardware** — Network identity (MAC, IP), PXE settings, disk, BMC reference

Hardware CRs must be in the **same namespace as the CAPI cluster objects** (`tinkerbell`).
CAPT creates Workflow CRs in the cluster namespace, and tink-controller resolves `hardwareRef`
within that same namespace. The files in `assets/tinkerbell/hardware/` use
`namespace: tinkerbell`.

The example files are at `assets/tinkerbell/hardware/cp01.yaml` and `cp02.yaml`.

### cp01 example

```yaml
# assets/tinkerbell/hardware/cp01.yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: cp01-bmc-secret
  namespace: tinkerbell       # must match cluster_namespace
type: Opaque
stringData:
  username: ADMIN
  password: ADMIN
---
apiVersion: bmc.tinkerbell.org/v1alpha1
kind: Machine                 # Rufio Machine CR — BMC connection
metadata:
  name: cp01
  namespace: tinkerbell
spec:
  connection:
    host: https://192.168.0.70    # Redfish BMC endpoint
    authSecretRef:
      name: cp01-bmc-secret
      namespace: tinkerbell
    insecureTLS: true             # set false in production with valid TLS
---
apiVersion: tinkerbell.org/v1alpha1
kind: Hardware
metadata:
  name: cp01
  namespace: tinkerbell
spec:
  bmcRef:
    apiGroup: bmc.tinkerbell.org
    kind: Machine
    name: cp01
  disks:
    - device: /dev/sda
  metadata:
    instance:
      id: "ac:1f:6b:01:b7:21"    # MAC address used as instance ID
      hostname: cp01
  interfaces:
    - dhcp:
        arch: x86_64
        hostname: cp01
        ip:
          address: 10.10.0.170   # static IP SMEE assigns to this node
          family: 4
          gateway: 10.10.0.9
          netmask: 255.255.0.0
        mac: "ac:1f:6b:01:b7:21"
        uefi: true
      netboot:
        allowPXE: true            # SMEE serves iPXE to this MAC
        allowWorkflow: true       # tink-worker runs workflows on this node
```

Apply the hardware files:

```bash
kubectl apply -f assets/tinkerbell/hardware/cp01.yaml
kubectl apply -f assets/tinkerbell/hardware/cp02.yaml
```

**Verify:**

```bash
# Hardware CRs in tinkerbell namespace
kubectl get hardware -n tinkerbell
# NAME   AGE
# cp01   30s
# cp02   30s

kubectl describe hardware cp01 -n tinkerbell
# Shows interfaces (dhcp.ip must be present), BMC ref, disk, netboot settings

# Rufio Machine CRs in tinkerbell namespace
kubectl get machines.bmc.tinkerbell.org -n tinkerbell

# Check Rufio can reach the BMC
kubectl describe machine.bmc.tinkerbell.org cp01 -n tinkerbell
# Look for: "Contactable: True" and "Power State: off"

# BMC Secrets present
kubectl get secret -n tinkerbell | grep bmc-secret
```

---

## After Step 8 — Choose Your Provisioning Path

At this point Hardware CRs are registered. There are **two mutually exclusive paths** for
what comes next. **Do not mix them.**

| | Path A — Standalone | Path B — CAPT |
|--|--|--|
| **Goal** | Install Ubuntu on bare metal manually | Deploy a full Kubernetes workload cluster |
| **Who creates Workflows?** | You (Steps 9A–10A) | CAPT (from `templateOverride`) |
| **Who powers on nodes?** | You (Rufio Jobs, Step 11A) | CAPT (creates Rufio Jobs automatically) |
| **Next step** | Step 9A below | [Skip to Step 9B](#step-9b) |

> **If your goal is a Kubernetes workload cluster, skip to [Step 9B](#step-9b) now.**
> Steps 9A–13A are only for standalone Ubuntu provisioning without CAPT.

---

## Path A — Standalone Manual Provisioning

> **Use this path only if you want to install Ubuntu on the nodes without CAPT.**
> If you are going on to deploy a Kubernetes cluster via CAPT, **skip this entire section**
> and go directly to [Step 9B](#step-9b).

---

## Step 9A — Apply the OS Provisioning Template

**Code reference:** `src/daalu/bootstrap/mgmt/tinkerbell_installer.py:566` →
`TinkerbellInstaller._create_os_template()`

The Template CR defines the workflow actions run on each bare-metal node during provisioning.
The file is at `assets/tinkerbell/templates/ubuntu-kubeadm.yaml`.

The template has three actions (`ubuntu-kubeadm.yaml:12`):

1. **`image2disk`** (line 22) — Downloads the gzipped raw image from the image server via
   `wget`, pipes it through `gunzip`, and writes it directly to the disk with `dd bs=16M`.
   Uses `busybox:stable` container with `/dev` and `/proc` bind-mounted from the host.

2. **`configure-node`** (line 36) — Mounts the root partition, then:
   - Sets hostname and `/etc/hosts`
   - Writes a netplan config assigning the static provisioning IP to the correct NIC by MAC
   - Injects the SSH public key for `root` and the image user
   - Writes NoCloud cloud-init user-data (creates the builder user, sets sudo)
   - Enables root SSH login in sshd_config
   - Sets `datasource_list: [NoCloud, None]` so cloud-init uses the seeded data

3. **`reboot`** (line 155) — Schedules a kernel sysrq reboot via `nsenter --target 1 --pid`
   (enters the host PID namespace and writes `b` to `/proc/sysrq-trigger`).

> This template is for **standalone provisioning only**. When CAPT provisions nodes it uses
> `templateOverride` embedded in `TinkerbellMachineTemplate`
> (`assets/tinkerbell/cluster-api/tinkerbell-controlplane.yaml:36`) which fetches kubeadm
> user-data from Hegel instead. You do **not** apply this template for the CAPT path.

```bash
kubectl apply -f assets/tinkerbell/templates/ubuntu-kubeadm.yaml

kubectl get template -n tinkerbell
# NAME               AGE
# ubuntu-kubeadm     10s
```

---

## Step 10A — Create Workflow CRs

**Code reference:** `src/daalu/bootstrap/mgmt/tinkerbell_installer.py:592` →
`TinkerbellInstaller._create_workflows()`

A Workflow CR links a Template to a specific Hardware node and supplies the
per-node values that the template's Go-template variables (`{{.hostname}}`, `{{.disk}}`, etc.)
are replaced with at runtime.

The example files are at `assets/tinkerbell/workflows/cp01-workflow.yaml` and `cp02-workflow.yaml`.

```yaml
# assets/tinkerbell/workflows/cp01-workflow.yaml
apiVersion: tinkerbell.org/v1alpha1
kind: Workflow
metadata:
  name: cp01-provision
  namespace: tinkerbell
spec:
  templateRef: ubuntu-kubeadm      # references the Template CR by name
  hardwareRef: cp01                # references the Hardware CR by name
  hardwareMap:
    device_1: "ac:1f:6b:01:b7:21" # MAC address — maps to {{.device_1}} in template
    disk: "/dev/sda"               # maps to {{.disk}}
    hostname: "cp01"               # maps to {{.hostname}}
    image_url: "http://10.10.0.9/UBUNTU_24.04_NODE_IMAGE_K8S_v1.35.0.raw.gz"
    image_username: "builder"
    ssh_pub_key: "ssh-ed25519 AAAA..."   # your SSH public key
    prov_mac: "ac:1f:6b:01:b7:21"
    prov_ip: "10.10.0.170"
    prov_prefix: "16"
```

The automation deletes and recreates workflows (`tinkerbell_installer.py:613`) to ensure
stale STATE_PENDING or STATE_FAILED workflows from previous runs don't block new ones.
Do the same manually:

```bash
# Delete any existing (stale) workflows first
kubectl delete -f assets/tinkerbell/workflows/cp01-workflow.yaml --ignore-not-found
kubectl delete -f assets/tinkerbell/workflows/cp02-workflow.yaml --ignore-not-found

# Apply fresh workflows
kubectl apply -f assets/tinkerbell/workflows/cp01-workflow.yaml
kubectl apply -f assets/tinkerbell/workflows/cp02-workflow.yaml
```

**Verify:**

```bash
# Workflows created in STATE_PENDING (waiting for the node to PXE boot)
kubectl get workflow -n tinkerbell
# NAME              TEMPLATE         HARDWARE   STATE
# cp01-provision    ubuntu-kubeadm   cp01       STATE_PENDING
# cp02-provision    ubuntu-kubeadm   cp02       STATE_PENDING

kubectl get workflow -n tinkerbell -w
```

---

## Step 11A — Power On Bare-Metal Nodes via Rufio

**Code reference:** `src/daalu/bootstrap/mgmt/tinkerbell_installer.py:626` →
`TinkerbellInstaller._power_on_nodes()`

A Rufio Job CR triggers BMC operations. The power-on sequence is (`tinkerbell_installer.py:678`):

1. `powerAction: off` — ensures the node is in a known state
2. `oneTimeBootDeviceAction: pxe` — sets a one-time PXE boot override via Redfish/IPMI
3. `powerAction: on` — powers the node on

The "one-time" PXE override is critical: after the first boot (which runs HookOS and the
Tinkerbell workflow), the node's BIOS reverts to its default boot order (disk). This means
after provisioning, the node boots Ubuntu from disk automatically.

```bash
# Create the Rufio Job for cp01
kubectl apply --kubeconfig ~/.kube/daalu-mgmt-config -f - <<'EOF'
apiVersion: bmc.tinkerbell.org/v1alpha1
kind: Job
metadata:
  name: cp01-pxe-poweron
  namespace: tinkerbell
spec:
  machineRef:
    name: cp01
    namespace: tinkerbell
  tasks:
    - powerAction: "off"
    - oneTimeBootDeviceAction:
        device:
          - pxe
        efiBoot: true       # set false for legacy BIOS
    - powerAction: "on"
EOF

# Create the Rufio Job for cp02
kubectl apply --kubeconfig ~/.kube/daalu-mgmt-config -f - <<'EOF'
apiVersion: bmc.tinkerbell.org/v1alpha1
kind: Job
metadata:
  name: cp02-pxe-poweron
  namespace: tinkerbell
spec:
  machineRef:
    name: cp02
    namespace: tinkerbell
  tasks:
    - powerAction: "off"
    - oneTimeBootDeviceAction:
        device:
          - pxe
        efiBoot: false      # cp02 has uefi: false in hardware CR
    - powerAction: "on"
EOF
```

**Monitor Rufio Job progress:**

```bash
# Watch job status — look for Completed=True
kubectl get jobs.bmc.tinkerbell.org -n tinkerbell -w

# Inspect a job's conditions
kubectl get job.bmc.tinkerbell.org cp01-pxe-poweron -n tinkerbell \
  -o jsonpath='{.status.conditions}' | python3 -m json.tool

# Rufio controller logs — shows Redfish/IPMI API calls
kubectl logs -n capt-system deploy/capt-controller-manager --tail=50 | grep -i "rufio\|bmc\|job"

# If the Rufio controller is in its own namespace:
kubectl get pods --all-namespaces | grep rufio
kubectl logs -n rufio-system deploy/rufio-controller-manager --tail=50
```

**What happens on the bare-metal node:**

1. BMC receives the Redfish `set boot device = PXE (once)` command
2. BMC powers the node on
3. Node's NIC broadcasts a DHCP request on the provisioning network
4. SMEE responds with an IP lease and points the node at its iPXE script
5. Node downloads and boots HookOS (a minimal Linux OS from Tinkerbell)
6. HookOS starts the tink-worker binary
7. tink-worker connects to tink-server and receives the workflow actions
8. Actions execute in order: image2disk → configure-node → reboot

---

## Step 12A — Monitor Provisioning

This is the most important phase to monitor. Each workflow action runs as a container on the
bare-metal node inside HookOS.

```bash
# Watch workflow state transitions (runs on management node)
kubectl get workflow -n tinkerbell -w
# cp01-provision   ...   STATE_RUNNING   (node booted HookOS, tink-worker connected)
# cp01-provision   ...   STATE_SUCCESS   (all actions completed)

# Get detailed action-level progress
kubectl get workflow cp01-provision -n tinkerbell \
  -o jsonpath='{.status.tasks}' | python3 -m json.tool

# Shorter: just see current state and which action is running
kubectl get workflow cp01-provision -n tinkerbell \
  -o jsonpath='{.status.state} - {.status.currentWorker} - {.status.currentAction}{"\n"}'

# Watch tink-server logs — shows workflow events as they happen
kubectl logs -n tinkerbell deploy/tink-server -f

# Watch SMEE logs — shows DHCP leases issued, iPXE chains
kubectl logs -n tinkerbell deploy/smee -f

# Watch image server logs — shows when the node fetches the OS image
kubectl logs -n tinkerbell deploy/image-server -f
```

**Timeline for a typical provisioning run:**

| Time | Event |
|------|-------|
| 0:00 | Node powers on, broadcasts DHCP |
| 0:05 | SMEE assigns IP, iPXE boots HookOS |
| 1:00 | HookOS fully booted, tink-worker connects |
| 1:05 | image2disk starts — downloads raw image from http://10.10.0.9/ |
| 10:00 | image2disk completes (time varies by image size and disk speed) |
| 10:05 | configure-node starts — mounts partition, writes netplan/SSH/cloud-init |
| 10:20 | configure-node completes |
| 10:25 | reboot action runs — sysrq reboot via nsenter |
| 10:30 | Workflow reaches STATE_SUCCESS |

**Troubleshooting STATE_RUNNING stuck:**

```bash
# Check tink-server can reach the tink-worker on the node
# tink-worker calls back to tink-server on port 42113
kubectl get svc tink-server -n tinkerbell

# Check SMEE gave the node an IP
kubectl logs -n tinkerbell deploy/smee | grep -i "DHCP\|lease\|ack"

# Check the node actually booted HookOS — look for tink-worker connecting
kubectl logs -n tinkerbell deploy/tink-server | grep -i "worker\|connected"

# Check image server — did the node download the image?
kubectl logs -n tinkerbell deploy/image-server | grep "GET /"
```

**Troubleshooting STATE_FAILED:**

```bash
# See which action failed and its error message
kubectl describe workflow cp01-provision -n tinkerbell
# Look at Events section at the bottom

# Get the full status JSON
kubectl get workflow cp01-provision -n tinkerbell -o json | \
  python3 -c "import sys,json; w=json.load(sys.stdin); print(json.dumps(w['status'], indent=2))"
```

---

## Step 13A — Post-Provision: Disable PXE and Reboot Into Ubuntu

**Code reference:** `src/daalu/bootstrap/mgmt/tinkerbell_installer.py:709` →
`TinkerbellInstaller._reboot_provisioned_nodes()`

After STATE_SUCCESS, the node's sysrq reboot fires. But because `allowPXE: true` is still
set in the Hardware CR, SMEE will answer the next DHCP/PXE request and boot HookOS again —
the node loops back into the provisioner.

The fix (`tinkerbell_installer.py:792`) is to:
1. Patch the Hardware CR to set `allowPXE: false`
2. Issue a power-cycle via a new Rufio Job

With `allowPXE: false`, SMEE ignores the node's PXE boot request and the node falls through
to its disk boot order, booting Ubuntu.

> **Patch warning:** Do NOT use `--type=merge` to patch `spec.interfaces` — for arrays,
> merge-patch replaces the entire array element, wiping the `dhcp` section. Use
> `--type=json` with targeted path operations instead.

```bash
# After workflow reaches STATE_SUCCESS for each node:

# Step 1: Disable PXE on cp01 — use --type=json to preserve the dhcp section
kubectl patch hardware cp01 -n tinkerbell \
  --type=json \
  --patch='[
    {"op":"replace","path":"/spec/interfaces/0/netboot/allowPXE","value":false},
    {"op":"replace","path":"/spec/interfaces/0/netboot/allowWorkflow","value":false}
  ]'

# Verify the patch took effect — dhcp section must still be present
kubectl get hardware cp01 -n tinkerbell \
  -o jsonpath='{.spec.interfaces[0]}{"\n"}' | python3 -m json.tool
# Must show both "dhcp": {...} and "netboot": {"allowPXE": false, "allowWorkflow": false}

# Step 2: Power-cycle cp01 into Ubuntu (BMC hard reboot)
kubectl apply --kubeconfig ~/.kube/daalu-mgmt-config -f - <<'EOF'
apiVersion: bmc.tinkerbell.org/v1alpha1
kind: Job
metadata:
  name: cp01-post-provision-reboot
  namespace: tinkerbell
spec:
  machineRef:
    name: cp01
    namespace: tinkerbell
  tasks:
    - powerAction: "cycle"
EOF

# Monitor the reboot job
kubectl get job.bmc.tinkerbell.org cp01-post-provision-reboot -n tinkerbell \
  -o jsonpath='{.status.conditions}' | python3 -m json.tool

# Repeat for cp02
kubectl patch hardware cp02 -n tinkerbell \
  --type=json \
  --patch='[
    {"op":"replace","path":"/spec/interfaces/0/netboot/allowPXE","value":false},
    {"op":"replace","path":"/spec/interfaces/0/netboot/allowWorkflow","value":false}
  ]'

kubectl apply --kubeconfig ~/.kube/daalu-mgmt-config -f - <<'EOF'
apiVersion: bmc.tinkerbell.org/v1alpha1
kind: Job
metadata:
  name: cp02-post-provision-reboot
  namespace: tinkerbell
spec:
  machineRef:
    name: cp02
    namespace: tinkerbell
  tasks:
    - powerAction: "cycle"
EOF
```

**Verify nodes booted Ubuntu:**

```bash
# After ~2 minutes, SSH to the provisioned node (key was injected during configure-node)
ssh root@10.10.0.170    # cp01
ssh root@10.10.0.171    # cp02

# Verify it is Ubuntu (not HookOS)
cat /etc/os-release | grep PRETTY_NAME
# PRETTY_NAME="Ubuntu 24.04.x LTS"

# Verify hostname
hostname
# cp01

# Verify networking
ip addr
# Should show the provisioning NIC with the assigned static IP (10.10.0.170)
```

---

---

## Path B — CAPT Provisioning (Kubernetes Workload Cluster)

> **Start here if you skipped Steps 9A–13A.** CAPT creates Workflow and Template CRs from
> `templateOverride` and bootstraps Kubernetes via Hegel-delivered kubeadm tokens. The nodes
> **do not need to be pre-installed with Ubuntu** — CAPT will image them from scratch.
>
> **Important:** CAPT v0.6.0 with `bootOptions.toggleAllowNetboot` manages `allowPXE` on the
> Hardware CR automatically, but does **not** create a Rufio Job to physically power on the
> nodes. You must create the Rufio power-on Job manually after CAPT creates the Workflow
> (Step 10B-c below).

---

## Step 9B — Verify Hardware CRs Are Clean Before CAPT

Before applying CAPI manifests, ensure the Hardware CRs are in the correct state for CAPT.

> **Namespace is critical:** CAPT creates Workflow CRs in the same namespace as the Cluster
> CR (`tinkerbell`). When tink-controller renders a Workflow it resolves `hardwareRef` in the
> **same namespace as the Workflow** — so Hardware CRs (and their Secrets and Rufio Machines)
> **must be in `tinkerbell` namespace**. The files in `assets/tinkerbell/hardware/` use
> `namespace: tinkerbell` for this reason.

```bash
# Apply hardware CRs to tinkerbell namespace
kubectl apply -f assets/tinkerbell/hardware/cp01.yaml
kubectl apply -f assets/tinkerbell/hardware/cp02.yaml

# Confirm they are in tinkerbell namespace with dhcp.ip set AND allowPXE=true
kubectl get hardware -n tinkerbell -o custom-columns=\
'NAME:.metadata.name,IP:.spec.interfaces[0].dhcp.ip.address,PXE:.spec.interfaces[0].netboot.allowPXE'
# NAME   IP            PXE
# cp01   10.10.0.170   true
# cp02   10.10.0.171   true

# If IP column is blank, re-apply (the yaml files have the full dhcp spec)
kubectl apply -f assets/tinkerbell/hardware/

# IMPORTANT: if re-running after a previous attempt, allowPXE may be false from the last run.
# CAPT v0.6.0 toggleAllowNetboot sets allowPXE=false after workflow success. Reset it:
kubectl patch hardware cp01 -n tinkerbell --type=json \
  --patch='[{"op":"replace","path":"/spec/interfaces/0/netboot/allowPXE","value":true},
            {"op":"replace","path":"/spec/interfaces/0/netboot/allowWorkflow","value":true}]'
kubectl patch hardware cp02 -n tinkerbell --type=json \
  --patch='[{"op":"replace","path":"/spec/interfaces/0/netboot/allowPXE","value":true},
            {"op":"replace","path":"/spec/interfaces/0/netboot/allowWorkflow","value":true}]'

# Confirm no stale Workflow or Template CRs exist (CAPT creates its own — old ones will block it)
kubectl get workflow -A
kubectl get template -n tinkerbell
# Both should output: No resources found
# If any exist, delete them:
kubectl delete workflow --all -A
kubectl delete template --all -n tinkerbell
```

---

## Step 10B — Deploy the Workload Kubernetes Cluster via CAPT

CAPT provisions nodes declaratively via Cluster API. The CAPI manifests are in
`assets/tinkerbell/cluster-api/`.

### What CAPT does when you apply these manifests

1. CAPT reconciles the `TinkerbellMachine` CRs and selects available Hardware CRs
2. CAPT generates a Template CR and `Workflow` CR per node (from `templateOverride`)
3. CAPT sets `allowPXE: true` on the Hardware CR via `bootOptions.toggleAllowNetboot`
4. **You** create a Rufio Job to PXE-boot each node (Step 10B-c) — CAPT v0.6.0 does not do this automatically
5. SMEE serves DHCP + HookOS to the node; tink-worker runs the workflow
6. The workflow's `configure-node` action fetches kubeadm bootstrap data from Hegel
7. Node reboots into Ubuntu and kubeadm runs, joining the cluster

### Understanding the CAPI resources

**`tinkerbell-cluster.yaml`** — Defines the Cluster and TinkerbellCluster CRs.
The `TinkerbellCluster.spec.imageLookupFormat` field (`cluster-api/tinkerbell-cluster.yaml:31`)
is used by CAPT to determine which image URL to inject into the workflow when it auto-creates
Workflows for nodes.

**`tinkerbell-controlplane.yaml`** — Defines the KubeadmControlPlane and
TinkerbellMachineTemplate. The `templateOverride` field (`tinkerbell-controlplane.yaml:36`)
contains the full workflow inline — this overrides the Template CR and is what CAPT actually
uses to provision control plane nodes.

The critical difference from the standalone Template is the `configure-node` action
(`tinkerbell-controlplane.yaml:129`):
- Instead of writing static NoCloud user-data with hardcoded kubeadm commands,
  it fetches user-data from Hegel (`${HEGEL_URL}/2009-04-04/user-data`)
- Hegel provides the CAPT-generated kubeadm bootstrap token for each node
- This is how CAPT gets nodes to join the right cluster automatically

**`tinkerbell-workers.yaml`** — Same structure but for worker nodes, using
`KubeadmConfigTemplate` instead of `KubeadmControlPlane`.

### Pre-requisite: Expose Hegel on the provisioning IP

Hegel's service is `ClusterIP` only by default. Bare-metal nodes running HookOS are outside
the Kubernetes cluster and **cannot reach ClusterIPs**. The `configure-node` workflow action
fetches kubeadm user-data from Hegel via HTTP — if Hegel is not reachable, the wget will hang
until timeout and the workflow will fail.

Patch the Hegel service to add the provisioning IP as an ExternalIP before deploying the cluster.
This patch must be reapplied after every Tinkerbell stack reinstall (`daalu clean` + redeploy),
as Helm recreates the service without the ExternalIP.

```bash
kubectl patch svc hegel -n tinkerbell \
  --type=strategic \
  --patch='{"spec":{"externalIPs":["10.10.0.9"]}}'

# Verify it is reachable from the management node
curl -s http://10.10.0.9:50061/2009-04-04/meta-data
# Should return quickly (even if empty) — a hang means ExternalIP routing is not working
```

### Substituting variables

The YAML files use `${ VAR }` placeholders (with spaces inside the braces). This format is
**not compatible with `envsubst`** — `envsubst` only handles `$VAR` or `${VAR}` and will
leave the placeholders unsubstituted, producing invalid YAML. Use `sed` instead.

First set your values — run each line individually to avoid shell mangling:

```bash
CLUSTER_NAME=auto-openstack-infra
NAMESPACE=tinkerbell
KUBERNETES_VERSION=v1.35.0
CONTROL_PLANE_MACHINE_COUNT=1
WORKER_MACHINE_COUNT=1
CLUSTER_APIENDPOINT_HOST=10.10.0.249
SERVICE_CIDR=10.96.0.0/12
POD_CIDR=10.201.0.0/16
IMAGE_URL=http://10.10.0.9/UBUNTU_24.04_NODE_IMAGE_K8S_v1.35.0.raw.gz
IMAGE_USERNAME=builder
SSH_PUB_KEY_CONTENT="$(cat ~/.ssh/id_ed25519.pub)"
HEGEL_URL=http://10.10.0.9:50061          # provisioning IP — bare-metal nodes cannot reach ClusterIPs
```

Verify the key variables resolved correctly before applying:

```bash
echo $HEGEL_URL
# Expected: http://10.10.0.9:50061  (provisioning IP, NOT a ClusterIP)

echo $SSH_PUB_KEY_CONTENT
# Expected: your full public key string
```

Apply cluster infrastructure:

```bash
sed \
  -e "s|\${ CLUSTER_NAME }|$CLUSTER_NAME|g" \
  -e "s|\${ NAMESPACE }|$NAMESPACE|g" \
  -e "s|\${ SERVICE_CIDR }|$SERVICE_CIDR|g" \
  -e "s|\${ POD_CIDR }|$POD_CIDR|g" \
  -e "s|\${ CLUSTER_APIENDPOINT_HOST }|$CLUSTER_APIENDPOINT_HOST|g" \
  -e "s|\${ IMAGE_URL }|$IMAGE_URL|g" \
  assets/tinkerbell/cluster-api/tinkerbell-cluster.yaml | \
  kubectl apply --kubeconfig ~/.kube/daalu-mgmt-config -f -
```

Apply control plane:

```bash
sed \
  -e "s|\${ CLUSTER_NAME }|$CLUSTER_NAME|g" \
  -e "s|\${ NAMESPACE }|$NAMESPACE|g" \
  -e "s|\${ KUBERNETES_VERSION }|$KUBERNETES_VERSION|g" \
  -e "s|\${ CONTROL_PLANE_MACHINE_COUNT }|$CONTROL_PLANE_MACHINE_COUNT|g" \
  -e "s|\${ IMAGE_URL }|$IMAGE_URL|g" \
  -e "s|\${ IMAGE_USERNAME }|$IMAGE_USERNAME|g" \
  -e "s|\${ SSH_PUB_KEY_CONTENT }|$SSH_PUB_KEY_CONTENT|g" \
  -e "s|\${ HEGEL_URL }|$HEGEL_URL|g" \
  assets/tinkerbell/cluster-api/tinkerbell-controlplane.yaml | \
  kubectl apply --kubeconfig ~/.kube/daalu-mgmt-config -f -
```

Apply workers:

```bash
sed \
  -e "s|\${ CLUSTER_NAME }|$CLUSTER_NAME|g" \
  -e "s|\${ NAMESPACE }|$NAMESPACE|g" \
  -e "s|\${ KUBERNETES_VERSION }|$KUBERNETES_VERSION|g" \
  -e "s|\${ WORKER_MACHINE_COUNT }|$WORKER_MACHINE_COUNT|g" \
  -e "s|\${ IMAGE_URL }|$IMAGE_URL|g" \
  -e "s|\${ IMAGE_USERNAME }|$IMAGE_USERNAME|g" \
  -e "s|\${ SSH_PUB_KEY_CONTENT }|$SSH_PUB_KEY_CONTENT|g" \
  -e "s|\${ HEGEL_URL }|$HEGEL_URL|g" \
  assets/tinkerbell/cluster-api/tinkerbell-workers.yaml | \
  kubectl apply --kubeconfig ~/.kube/daalu-mgmt-config -f -
```

## Step 10B-c — Power On Nodes via Rufio (manual — CAPT does not do this)

After applying the cluster manifests, wait for CAPT to create the Workflow and Template CRs
(usually within 30–60 seconds), then create Rufio Jobs to physically PXE-boot each node.

```bash
# Wait for CAPT to create Workflows — must show STATE_PENDING with TEMPLATE-RENDERING=successful
# before you create the Rufio Jobs. If TEMPLATE-RENDERING=failed see the troubleshooting note below.
kubectl get workflow -n $NAMESPACE -w
```

> **Troubleshooting — `TEMPLATE-RENDERING=failed` / "template not found":**
> CAPT v0.6.0 sometimes creates the Workflow before the Template CR is ready. If this happens:
> ```bash
> # Delete the broken workflow — CAPT will recreate both Template and Workflow
> kubectl delete workflow -n $NAMESPACE --all
> kubectl annotate tinkerbellmachine -n $NAMESPACE --all \
>   reconcile.capt.io/trigger=$(date +%s) --overwrite
> # Wait 30s and check again
> kubectl get workflow -n $NAMESPACE
> ```

Once Workflows are in `STATE_PENDING` with `TEMPLATE-RENDERING=successful`, power on cp01:

```bash
# Power on cp01 (control plane) — PXE boot into HookOS to run the workflow
kubectl apply -f - <<'EOF'
apiVersion: bmc.tinkerbell.org/v1alpha1
kind: Job
metadata:
  name: cp01-capt-poweron
  namespace: tinkerbell
spec:
  machineRef:
    name: cp01
    namespace: tinkerbell
  tasks:
    - powerAction: "off"
    - oneTimeBootDeviceAction:
        device:
          - pxe
        efiBoot: true       # set false for legacy BIOS nodes
    - powerAction: "on"
EOF

kubectl get jobs.bmc.tinkerbell.org -n tinkerbell -w
# NAME                  COMPLETE   FAILED
# cp01-capt-poweron     True
```

## Step 11B — Monitor cp01 Workflow and Boot to Disk

Watch the cp01 Workflow run to completion:

```bash
kubectl get workflow -n $NAMESPACE -w
# STATE_PENDING → STATE_RUNNING → STATE_SUCCESS
```

Useful logs while waiting:

```bash
# clusterctl describe gives a tree view of all CAPI objects
clusterctl --kubeconfig ~/.kube/daalu-mgmt-config \
  describe cluster $CLUSTER_NAME -n $NAMESPACE

# CAPT controller logs — most useful for debugging provisioning decisions
kubectl logs -n capt-system deploy/capt-controller-manager -f

# Watch SMEE for DHCP/iPXE activity from cp01
kubectl logs -n tinkerbell deploy/smee -f
```

**After cp01 Workflow reaches STATE_SUCCESS — power-cycle to disk (mandatory):**

The workflow's `reboot` action uses a backgrounded sysrq trigger that is often killed when
the HookOS container exits, so the node does not always reboot automatically. Always do this
step explicitly — it is required for KubeadmControlPlane to reach INITIALIZED.

```bash
# 1. Verify CAPT toggled allowPXE to false after STATE_SUCCESS
kubectl get hardware cp01 -n tinkerbell \
  -o jsonpath='{.spec.interfaces[0].netboot.allowPXE}{"\n"}'
# Expected: false
# If still true, patch manually so the node boots from disk not PXE:
kubectl patch hardware cp01 -n tinkerbell --type=json \
  --patch='[{"op":"replace","path":"/spec/interfaces/0/netboot/allowPXE","value":false},
            {"op":"replace","path":"/spec/interfaces/0/netboot/allowWorkflow","value":false}]'

# 2. Power-cycle cp01 to boot from disk
kubectl apply -f - <<'EOF'
apiVersion: bmc.tinkerbell.org/v1alpha1
kind: Job
metadata:
  name: cp01-reboot-to-disk
  namespace: tinkerbell
spec:
  machineRef:
    name: cp01
    namespace: tinkerbell
  tasks:
    - powerAction: "off"
    - powerAction: "on"
EOF

kubectl get job.bmc.tinkerbell.org cp01-reboot-to-disk -n tinkerbell -w

# 3. Wait for cp01 to boot Ubuntu and kubelet to start
# SSH should work within ~2 minutes of the power-cycle completing
ssh root@10.10.0.170
cloud-init status          # should be: done
tail -50 /var/log/cloud-init-output.log
systemctl status kubelet
```

Wait for KubeadmControlPlane to become initialized (cp01's kubelet must have joined):

```bash
kubectl get kubeadmcontrolplane -n $NAMESPACE -w
# NAME                    INITIALIZED   API SERVER AVAILABLE   REPLICAS   ...
# Wait until INITIALIZED=true before proceeding to cp02
```

**After KCP INITIALIZED=true — power on cp02:**

CAPT does not create cp02's Workflow until KCP is initialized, so there is nothing to boot
into before that point.

```bash
# Power on cp02 — PXE boot into HookOS to run its workflow
kubectl apply -f - <<'EOF'
apiVersion: bmc.tinkerbell.org/v1alpha1
kind: Job
metadata:
  name: cp02-capt-poweron
  namespace: tinkerbell
spec:
  machineRef:
    name: cp02
    namespace: tinkerbell
  tasks:
    - powerAction: "off"
    - oneTimeBootDeviceAction:
        device:
          - pxe
        efiBoot: true       # set false for legacy BIOS nodes
    - powerAction: "on"
EOF

kubectl get jobs.bmc.tinkerbell.org -n tinkerbell -w
```

Watch cp02's Workflow, then power-cycle it to disk exactly as done for cp01:

```bash
kubectl get workflow -n $NAMESPACE -w
# Wait for cp02's workflow to reach STATE_SUCCESS

# Verify allowPXE=false, then:
kubectl apply -f - <<'EOF'
apiVersion: bmc.tinkerbell.org/v1alpha1
kind: Job
metadata:
  name: cp02-reboot-to-disk
  namespace: tinkerbell
spec:
  machineRef:
    name: cp02
    namespace: tinkerbell
  tasks:
    - powerAction: "off"
    - powerAction: "on"
EOF

kubectl get job.bmc.tinkerbell.org cp02-reboot-to-disk -n tinkerbell -w
```

**Troubleshooting — node booted Ubuntu but SSH / ping not working:**

The node is visible at the Ubuntu login screen via IPMI console but `ping` returns
"No route to host". This means Ubuntu booted but the network interface did not come up.

**Root cause (fixed in current templates):** Two separate issues were found and fixed:

1. **Pre-existing base-image netplan files**: Ubuntu base images ship with netplan files (e.g.
   `/etc/netplan/50-cloud-init.yaml`) with hardcoded NIC names from the Packer build environment.
   On real hardware the NIC name doesn't exist, networkd fails. Fixed by `rm -f /mnt/target/etc/netplan/*.yaml`
   in configure-node before writing `99-daalu-static.yaml`.

2. **SMEE siaddr/DHCP incompatibility**: SMEE includes `siaddr` (next-server) in every DHCP OFFER
   regardless of whether PXE is active. Ubuntu's `systemd-networkd` interprets `siaddr` as a
   PXE-only field and refuses to send a DHCP REQUEST — the node loops forever in
   DISCOVER→OFFER with no lease. Fixed by writing a **static** netplan (`99-daalu-static.yaml`)
   using IPs fetched from Hegel at provision time rather than relying on DHCP at all.

The `configure-node` action now uses `nsenter --target 1 --net` to read the HookOS host
network interfaces and writes a static `99-daalu-static.yaml` covering every non-loopback NIC.
This should not occur on fresh provisioning runs. If you see it, re-provision from Step 9B.

Diagnose via IPMI/iDRAC console — boot into recovery mode to get a root shell without a password:

1. In the IPMI console, reboot the node (Rufio power cycle or BMC chassis reset)
2. Hold `Shift` during boot (or press `Esc` repeatedly) to get the GRUB menu
3. Select **Advanced options for Ubuntu** → **recovery mode**
4. At the recovery menu, select **root — Drop to root shell prompt**

From the root shell:

```bash
# What interfaces exist and what IPs do they have?
ip addr show

# What netplan files are present? (check for conflicts from base image)
ls /etc/netplan/
cat /etc/netplan/*.yaml

# If there are legacy base-image files alongside 99-daalu-static.yaml, remove them:
rm /etc/netplan/50-cloud-init.yaml /etc/netplan/00-installer-config.yaml 2>/dev/null || true

# Force netplan to apply
netplan apply

# If no IP after netplan apply, assign manually to get SSH working
# Replace <iface> with the actual interface name from ip addr show
ip addr add 10.10.0.170/24 dev <iface>
ip route add default via 10.10.0.9
systemctl start ssh

# Check cloud-init status
cloud-init status
tail -80 /var/log/cloud-init-output.log
systemctl status kubelet
```

If netplan matched the wrong interface or the MAC in the Hardware CR doesn't match the
actual NIC, update `assets/tinkerbell/hardware/cp01.yaml` with the correct MAC and re-run
from Step 9B.

## Step 12B — Retrieve the Workload Cluster Kubeconfig

```bash
# Once KubeadmControlPlane shows INITIALIZED=true:
clusterctl --kubeconfig ~/.kube/daalu-mgmt-config \
  get kubeconfig $CLUSTER_NAME -n $NAMESPACE \
  > ~/.kube/$CLUSTER_NAME.kubeconfig

# Access the workload cluster
export KUBECONFIG=~/.kube/$CLUSTER_NAME.kubeconfig

kubectl get nodes
# NAME   STATUS   ROLES           AGE   VERSION
# cp01   Ready    control-plane   5m    v1.35.0

kubectl get pods -A
```

## Step 13B — Install CNI on the Workload Cluster

Kubeadm does not install a CNI by default. Nodes will stay NotReady until one is installed.

```bash
export KUBECONFIG=~/.kube/$CLUSTER_NAME.kubeconfig

# Install Cilium 1.16.0 using the cilium CLI (already installed on dev VM)
cilium install --version 1.16.0

# Watch rollout
cilium status --wait

kubectl get nodes
# STATUS should change to Ready
```

## Step 14B — Verify providerID Is Set on All Nodes

CAPI uses `spec.providerID` on each Node to match Kubernetes nodes to CAPI Machine objects.
The kubeadm templates set `KUBELET_EXTRA_ARGS=--provider-id=tinkerbell://tinkerbell/$(hostname)`
in `preKubeadmCommands`, so kubelet sets providerID automatically from the node hostname.
Since the hostname is set to the Hardware CR name during configure-node, no manual step is needed.

Verify:

```bash
export KUBECONFIG=~/.kube/$CLUSTER_NAME.kubeconfig

kubectl get nodes -o custom-columns=NAME:.metadata.name,PROVIDERID:.spec.providerID
# NAME   PROVIDERID
# cp01   tinkerbell://tinkerbell/cp01
# cp02   tinkerbell://tinkerbell/cp02
```

If providerID is missing (e.g. cluster was deployed from older templates), patch manually:

```bash
kubectl patch node cp01 --type merge -p '{"spec":{"providerID":"tinkerbell://tinkerbell/cp01"}}'
kubectl patch node cp02 --type merge -p '{"spec":{"providerID":"tinkerbell://tinkerbell/cp02"}}'
```

Verify CAPI Machines are in Running state:

```bash
kubectl --kubeconfig ~/.kube/daalu-mgmt-config get machines -n $NAMESPACE
# PHASE should be: Running
```

---

## Appendix A — Tinkerbell Component Architecture Detail

### SMEE boot sequence for a PXE-booting node

1. Node NIC broadcasts DHCP Discover on the provisioning network
2. SMEE (running in hostNetwork on the mgmt node) receives the broadcast
3. SMEE queries tink-server: "is there a Hardware CR with MAC=`XX:XX:XX:XX:XX:XX`?"
4. If found and `allowPXE: true`: SMEE sends DHCP Offer with the assigned IP and `next-server`
   pointing at SMEE itself for TFTP
5. Node fetches `ipxe.efi` (or `undionly.kpxe` for BIOS) via TFTP
6. iPXE script loaded — it chains to SMEE's HTTP iPXE script endpoint
7. iPXE script tells the node to download HookOS kernel + initrd from SMEE
8. Node boots HookOS
9. HookOS starts tink-worker, which connects to tink-server on port 42113
10. tink-server delivers the workflow actions to tink-worker
11. tink-worker executes each action container sequentially

### Hegel metadata for CAPT-provisioned nodes

When CAPT provisions nodes (rather than standalone workflows), the `configure-node` action
fetches user-data from Hegel instead of writing static data:

```bash
# The configure-node action runs this inside the bare-metal node:
wget -O /mnt/target/var/lib/cloud/seed/nocloud/user-data \
  http://10.10.0.9:50061/2009-04-04/user-data
```

> **Important:** Use the provisioning IP (`10.10.0.9`), not the Hegel ClusterIP. Bare-metal
> nodes in HookOS cannot reach Kubernetes ClusterIPs. Hegel's Service must be patched with
> `externalIPs: ["10.10.0.9"]` (done in the pre-requisites) to make it reachable from HookOS.

Hegel responds with CAPT-generated kubeadm bootstrap data (certificates, tokens) that
cloud-init then executes on first boot, joining the node to the workload cluster.
The instance is identified by the node's MAC address, which maps to the Hardware CR.

### Namespace layout

```
tinkerbell namespace:
  - Deployment: tink-server, hegel, smee, image-server
  - Template CRs (ubuntu-kubeadm)          [Path A standalone only]
  - Workflow CRs (cp01-provision, ...)     [Path A standalone only]
  - Hardware CRs (cp01, cp02, ...)         [Path B CAPT]
  - Rufio Machine CRs (cp01, cp02, ...)    [Path B CAPT]
  - Secrets (cp01-bmc-secret, ...)         [Path B CAPT]
  - Cluster CR (auto-openstack-infra)      [Path B CAPT]
  - TinkerbellCluster CR                   [Path B CAPT]
  - KubeadmControlPlane CR                 [Path B CAPT]
  - TinkerbellMachineTemplate CR (controlplane, workers)
  - MachineDeployment CR
  - KubeadmConfigTemplate CR
  - TinkerbellMachine CRs  (created by CAPT per node)
  - Workflow CRs           (created by CAPT per node)
  - Template CRs           (created by CAPT, uses templateOverride)

capi-system:            capi-controller-manager
capi-kubeadm-*:         kubeadm bootstrap + control-plane providers
capt-system:            capt-controller-manager (includes Rufio)
cert-manager:           cert-manager, webhook, cainjector
```

---

## Appendix B — Quick Reference Commands

```bash
# Environment (run once per session)
export KUBECONFIG=~/.kube/daalu-mgmt-config
PROV_IP=10.10.0.9
NS=tinkerbell

# All Tinkerbell stack pods
kubectl get pods -n $NS

# All CAPI objects tree view
clusterctl describe cluster auto-openstack-infra -n tinkerbell

# Workflow states
kubectl get workflow -A

# Hardware PXE status
kubectl get hardware -n $NS -o custom-columns=\
'NAME:.metadata.name,MAC:.spec.interfaces[0].dhcp.mac,IP:.spec.interfaces[0].dhcp.ip.address,PXE:.spec.interfaces[0].netboot.allowPXE'

# Rufio Machine BMC connectivity
kubectl get machine.bmc.tinkerbell.org -n $NS

# Rufio Jobs (power operations)
kubectl get job.bmc.tinkerbell.org -n $NS

# SMEE logs (DHCP/iPXE)
kubectl logs -n $NS deploy/smee --tail=50 -f

# tink-server logs (workflow delivery)
kubectl logs -n $NS deploy/tink-server --tail=50 -f

# CAPT controller logs (CAPI reconciliation + Rufio)
kubectl logs -n capt-system deploy/capt-controller-manager --tail=50 -f

# Image server access log (image downloads)
kubectl logs -n $NS deploy/image-server -f

# Hegel logs (metadata requests)
kubectl logs -n $NS deploy/hegel --tail=50 -f

# Check image is available on image server
curl -sI http://$PROV_IP/UBUNTU_24.04_NODE_IMAGE_K8S_v1.35.0.raw.gz | head -5

# Check clusterctl config includes Tinkerbell provider
cat ~/.config/cluster-api/clusterctl.yaml

# List all CAPT-managed machines
kubectl get tinkerbellmachine -A

# Force delete a stuck workflow (if retrying provisioning)
kubectl delete workflow cp01-provision -n $NS
kubectl apply -f assets/tinkerbell/workflows/cp01-workflow.yaml
```

---

## Appendix C — Adapting to Your Environment

When customising the files for your environment, change these values consistently across all
relevant files:

| Variable | Example value | Files to update |
|----------|--------------|-----------------|
| Provisioning IP | `10.10.0.9` | `cluster.yaml`, workflow files, Helm values |
| BMC IP (per node) | `192.168.0.70` | `hardware/cp01.yaml` |
| MAC address | `ac:1f:6b:01:b7:21` | `hardware/cp01.yaml`, `workflows/cp01-workflow.yaml` |
| Node IP | `10.10.0.170` | `hardware/cp01.yaml`, `workflows/cp01-workflow.yaml` |
| SSH public key | `ssh-ed25519 AAAA...` | `workflows/cp01-workflow.yaml`, `cluster-api/*.yaml` |
| Image URL | `http://10.10.0.9/UBUNTU_...raw.gz` | `workflows/*.yaml`, `cluster-api/*.yaml` |
| Control plane VIP | `10.10.0.249` | `cluster.yaml`, `cluster-api/tinkerbell-cluster.yaml` |
| Kubernetes version | `v1.35.0` | `cluster.yaml`, `cluster-api/*.yaml` |
| CAPT version | `v0.6.0` | `cluster.yaml` (`mgmt_cluster.capt_version`) |
| CAPI version | `v1.12.0` | `cluster.yaml` (`mgmt_cluster.capi_version`) |

---

## Appendix D — Known Gotchas and Root Causes

This appendix documents every real-world failure mode encountered across multiple provisioning
runs. Each entry lists the symptom, root cause, and fix.

---

### D1 — CAPT v0.6.0 does NOT power on nodes automatically

**Symptom:** Workflow stays `STATE_PENDING` indefinitely. No Rufio Jobs are created.

**Root cause:** CAPT v0.6.0 with `bootOptions.toggleAllowNetboot: true` sets `allowPXE=true`
on the Hardware CR but does **not** create a Rufio Job to physically cycle power. The node
never PXE-boots so the Workflow never starts executing.

**Fix:** After CAPT creates the Workflow CR, manually create a Rufio power-on Job as shown
in Step 10B-c. Do this for each node (cp01, cp02, …).

---

### D2 — configure-node times out: HEGEL_URL is unreachable

**Symptom:** Workflow STATE_FAILED after ~135 s on `configure-node`. Tink-worker log shows
`wget` hanging then exiting non-zero. The Hegel URL resolves to a ClusterIP.

**Root cause:** Bare-metal nodes in HookOS are on the provisioning network and cannot reach
Kubernetes ClusterIPs. The default Hegel service has no external endpoint.

**Fix:**
1. Patch the Hegel service to add `externalIPs` pointing at the provisioning NIC IP:
   ```bash
   kubectl patch svc hegel -n tinkerbell --type=json \
     --patch='[{"op":"add","path":"/spec/externalIPs","value":["10.10.0.9"]}]'
   ```
2. Set `HEGEL_URL=http://10.10.0.9:50061` — hardcode the provisioning IP, never use the
   ClusterIP. This is already the default in the daalu env-var substitution step.
3. The ExternalIP is lost if Tinkerbell is reinstalled — reapply after every `daalu mgmt`
   or Tinkerbell Helm upgrade.

---

### D3 — Node not rebooting after STATE_SUCCESS (sysrq killed too early)

**Symptom:** Workflow shows STATE_SUCCESS but the node stays in HookOS. KubeadmControlPlane
never reaches INITIALIZED=true. cp02 never gets a Workflow from CAPT (it waits for KCP).

**Root cause:** The `reboot` action backgrounds a `sleep 5 && echo b > /proc/sysrq-trigger`
process then exits. When the tink-worker container is stopped immediately after the action
exits, the backgrounded `sleep` is killed before it fires.

**Fix:** The post-STATE_SUCCESS power-cycle is a **mandatory step in the normal flow** (Step 11B),
not just a troubleshooting step. After every node's Workflow reaches STATE_SUCCESS:
1. Verify `allowPXE=false` (CAPT should have set it)
2. Create a Rufio off→on Job with no `oneTimeBootDeviceAction` so the node boots from disk

---

### D4 — TinkerbellMachineTemplate is immutable

**Symptom:** After updating `assets/tinkerbell/cluster-api/tinkerbell-controlplane.yaml` and
running `kubectl apply`, the admission webhook rejects with:
```
admission webhook "validation.tinkerbellmachinetemplate..." denied:
TinkerbellMachineTemplate.Spec is immutable
```

**Root cause:** The CAPT admission webhook enforces immutability of `TinkerbellMachineTemplate`
spec (same design pattern as Kubernetes Deployment pod template immutability). Even a single
character change in `templateOverride` is rejected.

**Fix:** Delete the existing CR then apply the new one:
```bash
kubectl get tinkerbellmachinetemplate auto-openstack-infra-controlplane \
  -n tinkerbell -o json \
  | sed 's/old-value/new-value/g' \
  > /tmp/fixed.json
kubectl delete tinkerbellmachinetemplate auto-openstack-infra-controlplane -n tinkerbell
kubectl apply -f /tmp/fixed.json
```

---

### D5 — Template/Workflow race (TEMPLATE-RENDERING=failed, "template not found")

**Symptom:** After deleting and recreating CAPT CRs, the new Workflow shows
`TEMPLATE-RENDERING=failed` with message `template "auto-openstack-infra-xxxxx" not found`.

**Root cause:** CAPT creates the Workflow CR before the Template CR is fully reconciled and
written to etcd. This is an intermittent CAPT v0.6.0 bug.

**Fix:** Manually create the Template CR from the TinkerbellMachine's `templateOverride`:
```bash
kubectl get tinkerbellmachine auto-openstack-infra-glldw -n tinkerbell -o json | \
  python3 -c "
import sys, json
m = json.load(sys.stdin)
override = m['spec']['templateOverride']
uid = m['metadata']['uid']
name = m['metadata']['name']
indented = '\n'.join('    ' + l for l in override.splitlines())
print(f'''apiVersion: tinkerbell.org/v1alpha1
kind: Template
metadata:
  name: {name}
  namespace: tinkerbell
  ownerReferences:
  - apiVersion: infrastructure.cluster.x-k8s.io/v1beta1
    blockOwnerDeletion: true
    controller: true
    kind: TinkerbellMachine
    name: {name}
    uid: {uid}
spec:
  data: |
{indented}''')
" | kubectl apply -f -
```

---

### D6 — Node boots Ubuntu but network is down (DHCP DISCOVER loop / SMEE incompatibility)

**Symptom:** IPMI console shows the Ubuntu login prompt. `ping 10.10.0.170` returns
"No route to host". SMEE logs show the node's MAC sending `DISCOVER` → receiving `OFFER`
repeatedly but **never sending `REQUEST`** — the DHCP handshake never completes. The same
node sends `REQUEST` → `ACK` successfully in HookOS (BusyBox `udhcpc`).

**Root cause:** SMEE includes the `siaddr` (next-server) field in every DHCP OFFER
regardless of `allowPXE`. BusyBox's `udhcpc` (used by HookOS) ignores this field and
completes the handshake. **systemd-networkd** (used by Ubuntu 24.04) treats an OFFER with
`siaddr` set as a PXE-only lease and refuses to send `REQUEST`, causing the permanent
DISCOVER→OFFER loop.

**Diagnosis:**
```bash
kubectl logs -n tinkerbell deploy/smee --tail=200 | grep "ac:1f:6b:01:b7:21"
# During HookOS: type=REQUEST / type=ACK  ← completes
# During Ubuntu: type=DISCOVER / type=OFFER (repeating, never REQUEST)  ← broken
```

**Fix (applied):** `configure-node` now writes a **static IP** netplan config instead of
DHCP. The node's IP is fetched from Hegel at provisioning time (Hegel already knows the IP
from the Hardware CR). `printf` is used to write clean YAML without heredoc indentation:
```bash
NODE_IP=$(wget -q -O - "${HEGEL_URL}/2009-04-04/meta-data/local-ipv4")
PROV_GW=$(echo "${HEGEL_URL}" | sed 's|http://||' | cut -d: -f1)
printf 'network:\n  version: 2\n ...' > /mnt/target/etc/netplan/99-daalu-static.yaml
```
Applied in both:
- `assets/tinkerbell/templates/ubuntu-kubeadm.yaml` (standalone Path A)
- `assets/tinkerbell/cluster-api/tinkerbell-controlplane.yaml` (CAPT Path B)

---

### D7 — allowPXE must be reset to true before re-provisioning

**Symptom:** On a second provisioning attempt (after `daalu clean` or manual cleanup),
the Workflow moves to STATE_RUNNING but the node never PXE-boots — it boots from disk
instead (boots into whatever is on disk, or fails to boot).

**Root cause:** CAPT sets `allowPXE=false` on the Hardware CR after a successful workflow.
On re-runs, the Hardware CR still has `allowPXE=false` so SMEE will not serve the iPXE
script to the node.

**Fix:** Reset `allowPXE=true` and `allowWorkflow=true` before starting a new run:
```bash
kubectl patch hardware cp01 -n tinkerbell --type=json \
  --patch='[{"op":"replace","path":"/spec/interfaces/0/netboot/allowPXE","value":true},
            {"op":"replace","path":"/spec/interfaces/0/netboot/allowWorkflow","value":true}]'
kubectl patch hardware cp02 -n tinkerbell --type=json \
  --patch='[{"op":"replace","path":"/spec/interfaces/0/netboot/allowPXE","value":true},
            {"op":"replace","path":"/spec/interfaces/0/netboot/allowWorkflow","value":true}]'
```
This is already called out in Step 9B under "Re-run checklist" but is worth knowing as a
standalone root cause.
