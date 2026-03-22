# Tinkerbell Implementation — Low-Level Design Document

**Project:** Daalu
**Author:** Reverse-engineered from production codebase
**Date:** 2026-03-22
**Status:** Active — describes deployed infrastructure (cp01, cp02 both provisioned and SSH-accessible)

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Codebase Entry Points](#2-codebase-entry-points)
3. [Module-by-Module Breakdown](#3-module-by-module-breakdown)
4. [Tinkerbell Installation Flow](#4-tinkerbell-installation-flow)
5. [Hardware Object Implementation](#5-hardware-object-implementation)
6. [Template Implementation](#6-template-implementation)
7. [Workflow Implementation](#7-workflow-implementation)
8. [Runtime Interaction](#8-runtime-interaction)
9. [Cluster API Integration](#9-cluster-api-integration)
10. [End-to-End Execution Trace](#10-end-to-end-execution-trace)
11. [Error Handling and State Management](#11-error-handling-and-state-management)
12. [Observations and Gaps](#12-observations-and-gaps)

---

## 1. System Overview

### What Was Built

Daalu is a Python-based CLI orchestrator that automates the full lifecycle of bare-metal Kubernetes cluster provisioning using Tinkerbell as the provisioning backend. The system is not a wrapper around `clusterctl generate` or any template rendering tool — it directly builds and applies Kubernetes Custom Resources (CRDs) by constructing Python dictionaries, serialising them to YAML, and piping them to `kubectl apply -f -` via subprocess.

### Deployed Infrastructure (As of 2026-03-22)

| Component | Details |
|---|---|
| Management host | `192.168.0.171` (Ubuntu 24.04) |
| Management cluster | Single-node kubeadm, Kubernetes v1.30, Cilium CNI |
| Kubeconfig | `~/.kube/daalu-mgmt-config` |
| Provisioning NIC | `ens19` → static IP `10.10.0.9/16` |
| Management NIC | `ens18` → DHCP from home router |
| Tinkerbell stack | Helm release `tinkerbell`, namespace `tinkerbell`, chart `stack-0.6.3.tgz` |
| SMEE VIP | `10.10.0.9` (hostNetwork pod, binds directly to `ens19`) |
| Image server | nginx pod (hostNetwork), serves `/var/www/images` at `http://10.10.0.9/` |
| DHCP relay | `dhcrelay` forwarding `ens18 → 10.10.0.9` (management LAN to SMEE) |
| Bare-metal node cp01 | MAC `ac:1f:6b:01:b7:21`, IP `10.10.0.170`, BMC `https://192.168.0.70` |
| Bare-metal node cp02 | MAC `ac:1f:6b:01:b5:eb`, IP `10.10.0.171`, BMC `https://192.168.0.69` |
| OS image | `UBUNTU_24.04_NODE_IMAGE_K8S_v1.35.0.raw.gz` at `/var/www/images/` |
| Workflow status | Both cp01-provision and cp02-provision: **STATE_SUCCESS** |

### High-Level Architecture

```
Operator's laptop
    └─► daalu mgmt cluster-defs/cluster.yaml --provider tinkerbell
              │
              ▼
    MgmtClusterManager.deploy()   [manager.py]
         │
         ├─ SSH → 192.168.0.171   (paramiko)
         │       K8sInstaller.install()           → kubeadm init, Cilium
         │       kubeconfig → ~/.kube/daalu-mgmt-config
         │
         └─ TinkerbellInstaller.install()         [tinkerbell_installer.py]
                  │
                  ├─ 1. cert-manager       (Helm, local chart)
                  ├─ 2. CAPI core          (clusterctl init)
                  ├─ 3. CAPT               (clusterctl init --infrastructure tinkerbell)
                  ├─ 4. Tinkerbell stack   (Helm, local chart stack-0.6.3.tgz)
                  ├─ 5. SMEE patch         (kubectl patch deployment/smee)
                  ├─ 6. Image server       (kubectl apply nginx deployment + service)
                  ├─ 7. Hardware CRs       (kubectl apply -f - from Python dict)
                  ├─ 8. Template CR        (kubectl apply -f assets/tinkerbell/templates/)
                  └─ 9. Workflow CRs       (kubectl apply -f assets/tinkerbell/workflows/)

Bare-metal nodes (cp01, cp02)
    └─► PXE → SMEE → HookOS → tink-worker → tink-server
         → image2disk → configure-node → reboot → Ubuntu running
```

### Network Architecture

```
Management LAN (192.168.0.0/24)
  192.168.0.171  — management node (ens18, DHCP from home router)
  192.168.0.70   — cp01 BMC (Redfish/IPMI)
  192.168.0.69   — cp02 BMC (Redfish/IPMI)
  192.168.0.173  — cp01 post-install (eno1, DHCP)
  192.168.0.172  — cp02 post-install (eno1, DHCP)
  dhcrelay: forwards DHCP DISCOVERs from ens18 → SMEE at 10.10.0.9

Provisioning LAN (10.10.0.0/16)
  10.10.0.9    — SMEE / Tink stack VIP (ens19 on mgmt node, hostNetwork)
  10.10.0.170  — cp01 provisioning IP (eno2, static via SMEE DHCP offer)
  10.10.0.171  — cp02 provisioning IP (eno2, static via SMEE DHCP offer)
```

---

## 2. Codebase Entry Points

### CLI Definition

**File:** `src/daalu/cli/app.py`

The CLI is built with [Typer](https://typer.tiangolo.com/). Two commands are relevant to Tinkerbell:

```
daalu mgmt  <config>  [--provider tinkerbell]  [--ssh-host ...]   [--ssh-password ...]
daalu deploy <config> [--install cluster-api]  [--mgmt-kubeconfig ...]
```

#### `daalu mgmt` — Primary Entry Point for Tinkerbell

```python
# src/daalu/cli/app.py:1243
@app.command()
def mgmt(
    config: str,
    ssh_host: Optional[str],
    ssh_password: Optional[str],
    ssh_key: Optional[Path],
    provider: Optional[str],          # "tinkerbell" | "metal3" | "proxmox"
    provisioning_interface: Optional[str],
    ...
):
```

**Execution path:**

```
daalu mgmt cluster-defs/cluster.yaml --provider tinkerbell
    │
    ├─ load_config(config)                     → DaaluConfig (Pydantic)
    ├─ resolve provider (CLI flag > config > default=tinkerbell)
    ├─ apply CLI overrides onto mgmt_cluster config
    └─ MgmtClusterManager(cfg, WORKSPACE_ROOT).deploy()
```

#### `daalu deploy` — CAPI Cluster Provisioning

```python
# src/daalu/cli/app.py:877
@app.command()
def deploy(config: str, install: Optional[str], ...):
```

When `provider == "tinkerbell"` and `"cluster-api" in install_plan`:

```python
# src/daalu/cli/app.py:1014-1020
elif provider == "tinkerbell":
    deploy_cluster_api_tinkerbell(
        cfg=cfg,
        workspace_root=WORKSPACE_ROOT,
        mgmt_context=mgmt_context,
    )
```

#### `WORKSPACE_ROOT` Resolution

```python
# src/daalu/cli/app.py:110-112
_ws_env = os.environ.get("WORKSPACE_ROOT")
WORKSPACE_ROOT = Path(_ws_env).resolve() if _ws_env else Path(__file__).resolve().parents[3]
os.environ["WORKSPACE_ROOT"] = str(WORKSPACE_ROOT)
```

`WORKSPACE_ROOT` resolves to the project root (where `cluster-defs/`, `assets/`, `src/` live). All file paths throughout the codebase are relative to this. It must be set correctly or the file-based asset lookups will fail.

---

## 3. Module-by-Module Breakdown

### 3.1 `src/daalu/cli/app.py`

The top-level CLI. Owns command definitions, argument parsing, config loading, and top-level orchestration dispatch. Contains two helper functions that are relevant to Tinkerbell:

**`deploy_cluster_api_tinkerbell()`** (`app.py:222`):

```python
def deploy_cluster_api_tinkerbell(*, cfg, workspace_root, mgmt_context):
    manifests_dir = workspace_root / "assets" / "tinkerbell" / "cluster-api"
    kubeconfig = str(Path(cfg.mgmt_cluster.kubeconfig_output_path).expanduser())
    for manifest in sorted(manifests_dir.glob("*.yaml")):
        subprocess.run(["kubectl", "--kubeconfig", kubeconfig, "apply", "-f", str(manifest)], check=True)
```

This applies `tinkerbell-cluster.yaml`, `tinkerbell-controlplane.yaml`, `tinkerbell-workers.yaml` **as-is** — no variable substitution is performed. The `${ VAR }` placeholders in those files are not rendered. This is a known gap (see Section 12).

---

### 3.2 `src/daalu/bootstrap/mgmt/manager.py` — `MgmtClusterManager`

**Orchestrates the entire management cluster bootstrap.** This is the top-level state machine for the `daalu mgmt` command.

```python
class MgmtClusterManager:
    def deploy(self) -> tuple[str, str | None]:
        # 1. SSH connect (paramiko)
        client = self._ssh_connect(mgmt_cfg)
        ssh = SSHRunner(client)

        # 2. Install Kubernetes + Cilium
        k8s = K8sInstaller(ssh, mgmt_cfg)
        kubeconfig_text = k8s.install()

        # 3. Write kubeconfig locally (chmod 600)
        kc_path.write_text(kubeconfig_text)
        shutil.copy2(kc_path, ~/.kube/config)

        # 4. Install Cilium (local helm → remote cluster)
        k8s.install_cilium(kubeconfig_path)

        # 5. Dispatch to provider-specific installer
        if provider == BaremetalProvider.tinkerbell:
            TinkerbellInstaller(kubeconfig_path, mgmt_cfg, workspace_root).install()

        # 6. Deploy Harbor (optional)
        if mgmt_cfg.install_harbor and cfg.registry:
            RegistryManager(...).deploy_harbor(...)
            RegistryManager(...).mirror_images()
```

SSH connection strategy (`_ssh_connect`): tries `ssh_key` (Ed25519 → RSA → ECDSA), then `ssh_password`, then ssh-agent. Uses `paramiko.AutoAddPolicy` (no host key verification).

---

### 3.3 `src/daalu/bootstrap/mgmt/k8s_installer.py` — `K8sInstaller`

Runs over SSH (via `SSHRunner`) to bootstrap Kubernetes on the fresh Ubuntu node. All commands run as `sudo -H -E bash -c '<cmd>'` (via `SSHRunner.run(sudo=True)`).

**Install sequence:**

| Step | Method | What it does |
|---|---|---|
| 0 | `_setup_passwordless_sudo()` | Writes `/etc/sudoers.d/daalu-mgmt-nopasswd` via `sudo -S` |
| 0b | `_setup_provisioning_interface()` | `ip addr add 10.10.0.9/16 dev ens19`, writes `/etc/netplan/60-provisioning-static.yaml` |
| 1 | `_disable_swap()` | `swapoff -a`, comments out swap in `/etc/fstab` |
| 2 | `_load_kernel_modules()` | Writes `/etc/modules-load.d/k8s.conf`, `modprobe overlay br_netfilter` |
| 3 | `_set_sysctl()` | Writes `/etc/sysctl.d/k8s.conf`, `sysctl --system` |
| 4 | `_install_containerd()` | `apt-get install containerd`, generates `config.toml` with `SystemdCgroup=true` |
| 5 | `_install_kube_tools()` | Adds pkgs.k8s.io apt repo, installs kubeadm/kubelet/kubectl, `apt-mark hold` |
| 6 | `_kubeadm_init()` | `kubeadm init --pod-network-cidr=172.16.0.0/16 --skip-phases=addon/kube-proxy` |
| 7 | `_fetch_kubeconfig()` | `cat /etc/kubernetes/admin.conf` → returned as string |

**Idempotency:** `_cluster_is_running()` checks if `kubectl get nodes` succeeds on the remote. If yes, it skips steps 1–6 and only fetches the kubeconfig.

**Cilium install** runs locally via subprocess (not SSH):

```python
subprocess.run(["helm", "--kubeconfig", kubeconfig_path, "upgrade", "--install", "cilium",
    "cilium/cilium", "--version", "1.16.0",
    "--set", "kubeProxyReplacement=true",
    "--set", f"k8sServiceHost={cfg.host}",
    "--set", "socketLB.enabled=true",
    "--set", "operator.replicas=1",  # single-node mgmt cluster
    "--wait", "--timeout", "5m"])
```

After Cilium installs, containerd is restarted remotely so it picks up the CNI plugin (otherwise kubelet stays `NotReady` with "cni plugin not initialized").

---

### 3.4 `src/daalu/bootstrap/mgmt/tinkerbell_installer.py` — `TinkerbellInstaller`

This is the central module for everything Tinkerbell. It takes the kubeconfig path, `MgmtClusterConfig`, and `workspace_root`, and runs a 9-step sequence entirely via `subprocess` (helm + kubectl).

All kubectl calls go through:
```python
def _kubectl(self, *args, check=True):
    return subprocess.run(["kubectl", "--kubeconfig", self._kc, *args], check=check)

def _helm(self, *args, check=True):
    return subprocess.run(["helm", "--kubeconfig", self._kc, *args], check=check)
```

---

### 3.5 `src/daalu/bootstrap/mgmt/models.py` — Data Models

**`TinkerbellHardware`** (Pydantic `BaseModel`):

```python
class TinkerbellHardware(BaseModel):
    name: str          # e.g. "cp01"
    mac: str           # PXE-boot NIC MAC: "ac:1f:6b:01:b7:21"
    ip: str            # provisioning IP: "10.10.0.170"
    bmc_endpoint: str  # "https://192.168.0.70"
    bmc_username: str  # "ADMIN"
    bmc_password: str  # "ADMIN"
    disk: str = "/dev/sda"
    uefi: bool = True
```

**`MgmtClusterConfig`** (Pydantic `BaseModel`):

```python
class MgmtClusterConfig(BaseModel):
    provider: BaremetalProvider = BaremetalProvider.tinkerbell
    host: str                          # "192.168.0.171"
    ssh_username: str = "ubuntu"
    ssh_password: Optional[str]
    ssh_key: Optional[str]
    kubernetes_version: str = "1.30"
    pod_cidr: str = "172.16.0.0/16"
    service_cidr: str = "10.96.0.0/12"
    cilium_version: str = "1.16.0"
    capi_version: str = "v1.12.0"
    capt_version: str = "v0.6.0"
    provisioning_interface: str = "ens18"
    provisioning_ip: Optional[str]       # "10.10.0.9"
    provisioning_prefix: str = "16"
    dhcp_range_begin: Optional[str]      # "10.10.0.200"
    dhcp_range_end: Optional[str]        # "10.10.0.250"
    dhcp_gateway: Optional[str]          # "10.10.0.9"
    dhcp_dns: str = "8.8.8.8"
    hardware: list[TinkerbellHardware] = []
    kubeconfig_output_path: str = "~/.kube/daalu-mgmt-config"
    install_harbor: bool = True
```

These values come directly from `cluster-defs/cluster.yaml` under the `mgmt_cluster:` key, deep-merged with `cloud-config/secrets.yaml`.

---

### 3.6 `src/daalu/utils/ssh_runner.py` — `SSHRunner`

A thin Paramiko wrapper used by `K8sInstaller` for all remote commands.

```python
class SSHRunner:
    def run(self, cmd, *, sudo=False, timeout=None) -> tuple[int, str, str]:
        if sudo:
            cmd = f"sudo -H -E bash -c '{cmd}'"
        stdin, stdout, stderr = self.client.exec_command(cmd, timeout=timeout)
        return rc, stdout_str, stderr_str

    def put_text(self, content, remote_path, *, sudo=False):
        # If sudo: writes to /tmp first, then mv
        sftp.open(remote_path, "w").write(content)

    def put_file(self, local_path, remote_path, *, sudo=False):
        sftp.put(local_path, remote_path)
```

**Important:** `sudo=True` wraps the entire command in `sudo -H -E bash -c '...'`. This means the command is a single shell string and shell metacharacters in arguments can cause issues. In practice all commands are constructed as static strings so this is not a problem.

---

### 3.7 `src/daalu/bootstrap/engine/helm_engine.py` — `HelmInfraEngine`

This engine is **not used** by `TinkerbellInstaller`. It is the more sophisticated engine used by the OpenStack, monitoring, and infrastructure components. `TinkerbellInstaller` drives Helm directly via `subprocess.run(["helm", ...])`.

`HelmInfraEngine` provides a lifecycle: `pre_install → helm install/upgrade → post_install`, with image rewriting for Harbor. It is documented here for completeness but is not in the Tinkerbell code path.

---

## 4. Tinkerbell Installation Flow

**Entry:** `TinkerbellInstaller.install()` at `tinkerbell_installer.py:45`

### Step 1 — cert-manager (`_install_cert_manager`, line 62)

```python
if self._deployment_ready("cert-manager", "cert-manager"):
    return  # idempotent skip

chart = self._local_chart("cert-manager")
# → assets/cert-manager/charts/<first dir or .tgz>

self._helm("upgrade", "--install", "cert-manager", chart,
    "--namespace", "cert-manager", "--create-namespace",
    "--set", "installCRDs=true")

# Wait for all 3 deployments
for deploy in ["cert-manager", "cert-manager-webhook", "cert-manager-cainjector"]:
    self._kubectl("-n", "cert-manager", "rollout", "status",
                  f"deploy/{deploy}", "--timeout=5m")

# Poll webhook readiness: dry-run create Issuer CR for up to 120s
# (clusterctl init hangs forever if webhook is not ready)
```

**Why this matters:** CAPT's CRDs include webhook configurations. If cert-manager's webhook is not fully ready when `clusterctl init --infrastructure tinkerbell` runs, clusterctl will hang indefinitely trying to validate CRs against the webhook.

### Step 2 — CAPI core providers (`_install_capi`, line 129)

```python
if self._deployment_ready("capi-system", "capi-controller-manager"):
    return  # idempotent

subprocess.run([
    "clusterctl", "--kubeconfig", self._kc,
    "init",
    "--core", f"cluster-api:{ver}",        # ver = "v1.12.0"
    "--bootstrap", f"kubeadm:{ver}",
    "--control-plane", f"kubeadm:{ver}",
    "-v5",
], check=True)
```

This installs CAPI's core controller, CAPI kubeadm bootstrap provider, and CAPI kubeadm control-plane provider. All from the clusterctl default provider registry (fetched from GitHub releases).

### Step 3 — CAPT (`_install_capt`, line 161)

CAPT is a community provider not built into clusterctl. Two sub-steps:

**3a. Register in clusterctl config** (`_register_capt_in_clusterctl_config`, line 580):

```python
config_path = Path.home() / ".config" / "cluster-api" / "clusterctl.yaml"
# Reads existing file, parses YAML, checks if tinkerbell already registered
# If not:
providers.append({
    "name": "tinkerbell",
    "url": f"https://github.com/tinkerbell/cluster-api-provider-tinkerbell/releases/{ver}/infrastructure-components.yaml",
    "type": "InfrastructureProvider",
})
# Removes old providers: block, appends fresh YAML block
```

**3b. clusterctl init with TINKERBELL_IP env var:**

```python
ip = self._cfg.provisioning_ip or self._cfg.host  # "10.10.0.9"
env = {**os.environ, "TINKERBELL_IP": ip}

subprocess.run([
    "clusterctl", "--kubeconfig", self._kc,
    "init", "--infrastructure", f"tinkerbell:{ver}",  # ver = "v0.6.0"
    "-v5",
], env=env, check=True)
```

`TINKERBELL_IP` is substituted into CAPT's `infrastructure-components.yaml` at install time so CAPT knows where to reach the Tinkerbell gRPC server. CAPT also bundles Rufio (BMC controller) — no separate Rufio install is needed.

### Step 4 — Tinkerbell stack (`_install_tinkerbell_stack`, line 205)

```python
# Idempotency check
r = subprocess.run(["helm", "--kubeconfig", self._kc,
                    "status", "tinkerbell", "-n", "tinkerbell"],
                   capture_output=True)
if r.returncode == 0:
    return  # already installed

ip = self._cfg.provisioning_ip or self._cfg.host  # "10.10.0.9"
chart = self._local_chart("tinkerbell")
# → assets/tinkerbell/charts/stack-0.6.3.tgz

self._helm(
    "upgrade", "--install", "tinkerbell", chart,
    "--namespace", "tinkerbell", "--create-namespace",
    "--set", f"global.publicIP={ip}",          # "10.10.0.9"
    "--set", f"global.trustedProxies={{{self._cfg.pod_cidr}}}",  # "{172.16.0.0/16}"
    "--wait", "--timeout", "10m",
)
```

The local chart at `assets/tinkerbell/charts/stack-0.6.3.tgz` deploys: tink-server (gRPC), tink-controller (workflow reconciler), SMEE (DHCP/iPXE/TFTP), Hegel (metadata HTTP server), and a nginx reverse proxy. `global.publicIP` is the IP SMEE advertises as the next-server for iPXE and the base URL for tink-server.

### Step 5 — SMEE DHCP patch (`_configure_smee`, line 246)

After the chart installs, SMEE needs DHCP configuration injected as environment variables and must run with `hostNetwork: true`:

```python
env_patch = {
    "env": [
        {"name": "SMEE_DHCP_IP_FOR_PACKET", "value": "10.10.0.9"},
        {"name": "SMEE_DHCP_RANGE_START",   "value": "10.10.0.200"},
        {"name": "SMEE_DHCP_RANGE_END",     "value": "10.10.0.250"},
        {"name": "SMEE_DHCP_GATEWAY",       "value": "10.10.0.9"},
        {"name": "SMEE_DHCP_DNS",           "value": "8.8.8.8"},
    ]
}
patch = json.dumps({
    "spec": {"template": {"spec": {
        "hostNetwork": True,
        "dnsPolicy": "ClusterFirstWithHostNet",
        "containers": [{"name": "smee", **env_patch}]
    }}}
})
self._kubectl("-n", "tinkerbell", "patch", "deployment/smee",
              "--type=strategic", f"--patch={patch}")
self._kubectl("-n", "tinkerbell", "rollout", "status",
              "deployment/smee", "--timeout=3m")
```

**Why `hostNetwork: true`:** SMEE must receive raw DHCP broadcasts (UDP port 67) from bare-metal nodes on the provisioning LAN (`10.10.0.0/16`). Without `hostNetwork`, SMEE runs in the pod network and the Linux kernel never delivers L2 broadcast packets into the pod network namespace. With `hostNetwork`, SMEE binds directly to the host's `ens19` interface.

### Step 6 — Image server (`_deploy_image_server`, line 405)

An nginx pod serving OS images from the mgmt node's filesystem:

```python
# Deployment (hostNetwork=True, nodeSelector: control-plane)
# mounts /var/www/images (hostPath) as /usr/share/nginx/html
# Service: NodePort 30080, externalIPs=[10.10.0.9]
```

The key design decision is again `hostNetwork: true`. This binds nginx directly to port 80 on the mgmt node's IP. The `externalIPs` Service field makes `http://10.10.0.9/` route correctly. Bare-metal nodes fetch the OS image via `wget -O - http://10.10.0.9/UBUNTU_24.04_NODE_IMAGE_K8S_v1.35.0.raw.gz`.

### Step 7 — Hardware registration (`_register_hardware`, line 304)

See Section 5 for full detail.

### Step 8 — Template (`_create_os_template`, line 527)

```python
template_path = workspace_root / "assets/tinkerbell/templates/ubuntu-kubeadm.yaml"
self._kubectl("apply", "-f", str(template_path))
```

The Template CR is read from disk and applied as-is. No Python-side rendering occurs. See Section 6.

### Step 9 — Workflows (`_create_workflows`, line 553)

```python
workflows_dir = workspace_root / "assets/tinkerbell/workflows"
for wf_path in sorted(workflows_dir.glob("*.yaml")):
    self._kubectl("apply", "-f", str(wf_path))
```

Applies `cp01-workflow.yaml` then `cp02-workflow.yaml` (sorted order). No rendering. See Section 7.

---

## 5. Hardware Object Implementation

### Where It Is Built

**File:** `src/daalu/bootstrap/mgmt/tinkerbell_installer.py:304` — `_register_hardware()`

### Inputs

The input is `self._cfg.hardware` — a `list[TinkerbellHardware]` populated from `cluster-defs/cluster.yaml`:

```yaml
# cluster-defs/cluster.yaml
mgmt_cluster:
  hardware:
    - name: cp01
      mac: "ac:1f:6b:01:b7:20"    # ← INCORRECT (was b7:20, PXE NIC is b7:21)
      ip: "10.10.0.170"
      bmc_endpoint: "https://192.168.0.70"
      bmc_username: "ADMIN"
      bmc_password: "ADMIN"
      disk: "/dev/sda"
    - name: cp02
      mac: "ac:1f:6b:01:b5:eb"
      ip: "10.10.0.171"
      bmc_endpoint: "https://192.168.0.69"
      bmc_username: "ADMIN"
      bmc_password: "ADMIN"
      disk: "/dev/sda"
      uefi: false
```

> **Note on cp01 MAC discrepancy:** The `cluster.yaml` had `mac: ac:1f:6b:01:b7:20` which is `eno1` (management NIC). The PXE-boot NIC on cp01 is `eno2` with MAC `ac:1f:6b:01:b7:21`. This caused cp01's Hardware CR and original Workflow to target the wrong NIC. The Hardware CR on the cluster was manually patched and the Workflow was deleted and recreated with the correct MAC during the initial debugging session. The `cluster.yaml` still contains the incorrect MAC and should be corrected.

### Generated YAML (reconstructed from code)

For each `TinkerbellHardware`, the code builds three Python dicts and serialises them with `yaml.dump_all()`:

```yaml
# ── 1. BMC credentials Secret ─────────────────────────────────────────────────
apiVersion: v1
kind: Secret
metadata:
  name: cp01-bmc-secret
  namespace: tinkerbell
stringData:
  username: ADMIN
  password: ADMIN

---
# ── 2. Rufio Machine CR (BMC controller handle) ───────────────────────────────
apiVersion: bmc.tinkerbell.org/v1alpha1
kind: Machine
metadata:
  name: cp01
  namespace: tinkerbell
spec:
  connection:
    host: https://192.168.0.70
    authSecretRef:
      name: cp01-bmc-secret
      namespace: tinkerbell
    insecureTLS: true

---
# ── 3. Tinkerbell Hardware CR ─────────────────────────────────────────────────
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
  interfaces:
    - dhcp:
        arch: x86_64
        hostname: cp01
        ip:
          address: 10.10.0.170
          family: 4
          gateway: 10.10.0.9        # from cfg.dhcp_gateway
          netmask: 255.255.0.0      # _prefix_to_netmask("16")
        mac: "ac:1f:6b:01:b7:20"   # ← from cfg.hardware[0].mac (currently wrong in cluster.yaml)
        uefi: true
      netboot:
        allowPXE: true
        allowWorkflow: true
```

### How It Is Applied

```python
manifest = yaml.dump_all([bmc_secret, bmc_cr, cr])  # multi-document YAML string
subprocess.run(
    ["kubectl", "--kubeconfig", self._kc, "apply", "-f", "-"],
    input=manifest,
    text=True,
    check=True,
)
```

All three objects for one node are piped as a single multi-document YAML to `kubectl apply`. Because `kubectl apply` is used (not `create`), this is idempotent — re-running will update existing objects.

### `_prefix_to_netmask()` (line 399)

```python
@staticmethod
def _prefix_to_netmask(prefix: str) -> str:
    import ipaddress
    return str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)
    # "16" → "255.255.0.0"
```

### File-based Hardware CRs

In addition to the programmatic generation above, individual hardware CRs also exist as YAML files in `assets/tinkerbell/hardware/`:

**`assets/tinkerbell/hardware/cp01.yaml`** (file on disk, NOT applied by the code — manual reference):

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: cp01-bmc-secret
  namespace: tinkerbell
type: Opaque
stringData:
  username: ADMIN
  password: ADMIN
---
apiVersion: bmc.tinkerbell.org/v1alpha1
kind: Machine
metadata:
  name: cp01
  namespace: tinkerbell
spec:
  connection:
    host: https://192.168.0.70
    authSecretRef:
      name: cp01-bmc-secret
      namespace: tinkerbell
    insecureTLS: true
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
  interfaces:
    - dhcp:
        arch: x86_64
        hostname: cp01
        ip:
          address: 10.10.0.170
          family: 4
          gateway: 10.10.0.9
          netmask: 255.255.0.0
        mac: "ac:1f:6b:01:b7:21"   # ← corrected to PXE NIC MAC
        uefi: true
      netboot:
        allowPXE: true
        allowWorkflow: true
```

**These files are not applied by `TinkerbellInstaller`**. The installer builds Hardware CRs programmatically from `cfg.hardware`. The files in `assets/tinkerbell/hardware/` are supplementary references used for manual `kubectl apply` if needed.

---

## 6. Template Implementation

### Where the Template Lives

**File:** `assets/tinkerbell/templates/ubuntu-kubeadm.yaml`

Applied by `TinkerbellInstaller._create_os_template()` at `tinkerbell_installer.py:527` as a raw `kubectl apply -f`.

**No Python-side rendering.** The Template CR is applied exactly as it appears on disk. All `{{.variable}}` placeholders are Go template syntax that Tinkerbell's `tink-controller` resolves at Workflow creation time by substituting values from `spec.hardwareMap`.

### Full Template YAML

```yaml
apiVersion: tinkerbell.org/v1alpha1
kind: Template
metadata:
  name: ubuntu-kubeadm
  namespace: tinkerbell
spec:
  data: |
    version: "0.1"
    name: ubuntu-kubeadm
    global_timeout: 1800
    tasks:
      - name: "stream-ubuntu-image"
        worker: "{{.device_1}}"
        volumes:
          - /dev:/dev
          - /dev/console:/dev/console
          - /lib/firmware:/lib/firmware:ro
          - /proc:/proc
        actions:

          - name: "image2disk"
            image: busybox:stable
            timeout: 1200
            command:
              - sh
              - -c
              - |
                set -e
                echo "Downloading and writing image to {{.disk}}..."
                wget -O - "{{.image_url}}" | gunzip | dd of="{{.disk}}" bs=16M
                sync
                echo "Image written successfully"
            environment:
              IMG_URL: "{{.image_url}}"
              DEST_DISK: "{{.disk}}"

          - name: "configure-node"
            image: busybox:stable
            timeout: 120
            command:
              - sh
              - -c
              - |
                set -e
                echo "Rereading partition table..."
                blockdev --rereadpt {{.disk}} 2>/dev/null || true
                sleep 3

                # Find root partition (ext4 or xfs)
                ROOT_PART=""
                for part in {{.disk}}1 {{.disk}}2 {{.disk}}3 {{.disk}}4; do
                  [ -b "$part" ] || continue
                  INFO=$(blkid "$part" 2>/dev/null || true)
                  if echo "$INFO" | grep -q 'TYPE="ext4"' || echo "$INFO" | grep -q 'TYPE="xfs"'; then
                    ROOT_PART="$part"
                    break
                  fi
                done
                [ -n "$ROOT_PART" ] || { echo "ERROR: no ext4/xfs partition found"; exit 1; }

                mkdir -p /mnt/target
                mount "$ROOT_PART" /mnt/target

                # Hostname
                echo "{{.hostname}}" > /mnt/target/etc/hostname
                printf "127.0.0.1\tlocalhost\n127.0.1.1\t{{.hostname}}\n" > /mnt/target/etc/hosts

                # Netplan — static IP on provisioning NIC, DHCP on all other en*/eth* NICs
                mkdir -p /mnt/target/etc/netplan
                cat > /mnt/target/etc/netplan/99-daalu-all-dhcp.yaml << NETEOF
                network:
                  version: 2
                  renderer: networkd
                  ethernets:
                    all-en-dhcp:
                      match:
                        name: "en*"
                        macaddress: "{{.prov_mac}}"
                      addresses:
                        - {{.prov_ip}}/{{.prov_prefix}}
                      dhcp4: false
                      dhcp6: false
                    all-en-mgmt:
                      match:
                        name: "en*"
                      dhcp4: true
                      dhcp6: false
                    all-eth:
                      match:
                        name: "eth*"
                      dhcp4: true
                      dhcp6: false
                NETEOF

                # SSH key injection (root + ubuntu user)
                mkdir -p /mnt/target/root/.ssh
                chmod 700 /mnt/target/root/.ssh
                echo "{{.ssh_pub_key}}" > /mnt/target/root/.ssh/authorized_keys
                chmod 600 /mnt/target/root/.ssh/authorized_keys

                if [ -d /mnt/target/home/ubuntu ]; then
                  mkdir -p /mnt/target/home/ubuntu/.ssh
                  chmod 700 /mnt/target/home/ubuntu/.ssh
                  echo "{{.ssh_pub_key}}" > /mnt/target/home/ubuntu/.ssh/authorized_keys
                  chmod 600 /mnt/target/home/ubuntu/.ssh/authorized_keys
                fi

                # Allow root SSH login
                if [ -f /mnt/target/etc/ssh/sshd_config ]; then
                  sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /mnt/target/etc/ssh/sshd_config || true
                fi

                # NoCloud cloud-init seed (bypasses metadata server dependency)
                mkdir -p /mnt/target/var/lib/cloud/seed/nocloud
                cat > /mnt/target/var/lib/cloud/seed/nocloud/meta-data << MDEOF
                instance-id: {{.hostname}}
                local-hostname: {{.hostname}}
                MDEOF
                cat > /mnt/target/var/lib/cloud/seed/nocloud/user-data << UDEOF
                #cloud-config
                hostname: {{.hostname}}
                manage_etc_hosts: true
                users:
                  - name: {{.image_username}}
                    groups: [adm, sudo]
                    sudo: ALL=(ALL) NOPASSWD:ALL
                    shell: /bin/bash
                    lock_passwd: false
                    ssh_authorized_keys:
                      - {{.ssh_pub_key}}
                disable_root: false
                package_update: false
                package_upgrade: false
                UDEOF

                # Force NoCloud datasource (skip waiting for EC2/Azure/GCP)
                mkdir -p /mnt/target/etc/cloud/cloud.cfg.d
                echo 'datasource_list: [NoCloud, None]' > /mnt/target/etc/cloud/cloud.cfg.d/90-datasource.cfg

                umount /mnt/target
                echo "Configuration complete on $ROOT_PART"
            environment:
              BLOCK_DEVICE: "{{.disk}}"

          - name: "reboot"
            image: busybox:stable
            timeout: 90
            command:
              - sh
              - -c
              - |
                echo "Scheduling reboot via host PID namespace..."
                nsenter --target 1 --pid -- sh -c 'sleep 5 && echo b > /proc/sysrq-trigger' &
                echo "Reboot scheduled. Exiting action cleanly."
```

### Template Variable Injection Mechanism

Variables are NOT injected at Template creation time. They are substituted by `tink-controller` when a Workflow CR is created that references this Template. The substitution source is `spec.hardwareMap` in the Workflow:

```
Workflow.spec.hardwareMap.device_1  →  {{.device_1}}  in Template
Workflow.spec.hardwareMap.image_url →  {{.image_url}}
Workflow.spec.hardwareMap.disk      →  {{.disk}}
... etc.
```

The result is stored **immutably** in `status.tasks[]` on the Workflow object. Patching `spec.hardwareMap` after Workflow creation does **not** re-render `status.tasks`. The only way to change a rendered workflow is to delete and recreate it.

### Action Design Choices

| Action | Container | Why busybox instead of `ghcr.io/tinkerbell/actions/*` |
|---|---|---|
| `image2disk` | `busybox:stable` | The official `image2disk` action couldn't stream-decompress from this image URL. busybox's `wget \| gunzip \| dd` chain does. |
| `configure-node` | `busybox:stable` | Needed full shell scripting (blkid, mount, sed) not available in the official action containers. |
| `reboot` | `busybox:stable` | The official `reboot` action exits before tink-worker can report `STATE_SUCCESS`. The `nsenter` + background sleep pattern allows the container to exit cleanly first, then the sysrq fires 5 seconds later. |

### The nsenter Reboot Pattern (Critical)

```bash
nsenter --target 1 --pid -- sh -c 'sleep 5 && echo b > /proc/sysrq-trigger' &
```

- `nsenter --target 1 --pid` — enters the host's PID namespace (PID 1 is the container host's init process)
- `echo b > /proc/sysrq-trigger` — triggers an immediate kernel reboot via sysrq
- Background `&` — the container process exits immediately with exit code 0
- tink-worker receives the clean exit, reports `STATE_SUCCESS` to tink-server
- tink-server sets Workflow to `STATE_SUCCESS`
- 5 seconds later the sysrq fires and the host reboots

Without the background `&`, the sysrq fires synchronously and kills the container host before tink-worker can send the status update, leaving the Workflow stuck in `STATE_RUNNING`.

---

## 7. Workflow Implementation

### Where Workflows Are Applied

**File:** `src/daalu/bootstrap/mgmt/tinkerbell_installer.py:553` — `_create_workflows()`

```python
workflows_dir = workspace_root / "assets/tinkerbell/workflows"
for wf_path in sorted(workflows_dir.glob("*.yaml")):
    self._kubectl("apply", "-f", str(wf_path))
```

Workflows are not built programmatically — they are maintained as YAML files on disk and applied with `kubectl apply`. Sorted glob ensures `cp01-workflow.yaml` is applied before `cp02-workflow.yaml`.

### Workflow YAML: cp01

```yaml
# assets/tinkerbell/workflows/cp01-workflow.yaml
apiVersion: tinkerbell.org/v1alpha1
kind: Workflow
metadata:
  name: cp01-provision
  namespace: tinkerbell
spec:
  templateRef: ubuntu-kubeadm     # references Template CR by name
  hardwareRef: cp01               # references Hardware CR by name
  hardwareMap:
    device_1: "ac:1f:6b:01:b7:21"     # PXE NIC MAC — must match Hardware.dhcp.mac
    disk: "/dev/sda"
    hostname: "cp01"
    image_url: "http://10.10.0.9/UBUNTU_24.04_NODE_IMAGE_K8S_v1.35.0.raw.gz"
    image_username: "builder"
    ssh_pub_key: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGdk/DV6U01MtdXrIoSlKhEoNT2QND0lsKdfWmH3to7C kez@kez-dev-vm-1"
    prov_mac: "ac:1f:6b:01:b7:21"
    prov_ip: "10.10.0.170"
    prov_prefix: "16"
```

### Workflow YAML: cp02

```yaml
# assets/tinkerbell/workflows/cp02-workflow.yaml
apiVersion: tinkerbell.org/v1alpha1
kind: Workflow
metadata:
  name: cp02-provision
  namespace: tinkerbell
spec:
  templateRef: ubuntu-kubeadm
  hardwareRef: cp02
  hardwareMap:
    device_1: "ac:1f:6b:01:b5:eb"
    disk: "/dev/sda"
    hostname: "cp02"
    image_url: "http://10.10.0.9/UBUNTU_24.04_NODE_IMAGE_K8S_v1.35.0.raw.gz"
    image_username: "builder"
    ssh_pub_key: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGdk/DV6U01MtdXrIoSlKhEoNT2QND0lsKdfWmH3to7C kez@kez-dev-vm-1"
    prov_mac: "ac:1f:6b:01:b5:eb"
    prov_ip: "10.10.0.171"
    prov_prefix: "16"
```

### Workflow Lifecycle States

```
STATE_PENDING
    └─► tink-worker connects, workerID MAC matches rendered status.tasks[].worker
STATE_RUNNING   (transitions as actions execute)
    └─► each action: PENDING → RUNNING → SUCCESS
        └─► any failure → STATE_FAILED (terminal)
STATE_SUCCESS   (all actions succeeded)
STATE_FAILED    (any action failed or timed out)
STATE_TIMEOUT   (global_timeout=1800s exceeded)
```

### The Immutability Trap (Critical Bug Encountered)

When tink-controller creates a Workflow, it renders the Template using `hardwareMap` values and stores the result in `status.tasks`. This rendering happens once, at creation time, and the result is **immutable**:

```
Workflow created with hardwareMap.device_1 = "ac:1f:6b:01:b7:20"
  → status.tasks[0].worker = "ac:1f:6b:01:b7:20"  ← rendered, immutable

tink-worker connects as workerID = "ac:1f:6b:01:b7:21"  (actual PXE NIC)
  → tink-server: no matching workflow found for b7:21 (rendered worker is b7:20)
  → node idles in HookOS indefinitely

Fix: kubectl patch workflow... spec.hardwareMap.device_1 = b7:21
  → status.tasks[0].worker STILL = "ac:1f:6b:01:b7:20"  ← patch has no effect on status

Correct fix: kubectl delete workflow cp01-provision && kubectl apply -f cp01-workflow.yaml
  → fresh creation renders with b7:21 → status.tasks[0].worker = b7:21 ← matches
```

### Workflow Status Tracking

There is **no Python code** that polls workflow status after applying the Workflow files. The operator must check manually:

```bash
kubectl get workflow -n tinkerbell
# NAME               TEMPLATE          HARDWARE   STATE
# cp01-provision     ubuntu-kubeadm    cp01       STATE_SUCCESS
# cp02-provision     ubuntu-kubeadm    cp02       STATE_SUCCESS
```

This is a gap — the code applies workflows and returns, with no blocking wait or status check.

---

## 8. Runtime Interaction

### What Happens When a Node Powers On

This traces what actually happened with cp01 and cp02 during the deployment session.

#### Phase 1: DHCP / PXE Boot

```
Node powers on (manual power button or Rufio BMC Job)
    │
    NIC sends DHCP DISCOVER (broadcast) on the provisioning LAN
    │
    dhcrelay on mgmt node (running on management LAN):
      - receives DISCOVER on ens18 (192.168.0.x broadcast)
      - forwards to SMEE at 10.10.0.9:67
      [Only needed for management-LAN-attached nodes reaching SMEE on provisioning LAN]
    │
    OR: node's provisioning NIC directly broadcasts on 10.10.0.0/16
      - SMEE receives it directly (hostNetwork on ens19)
    │
    SMEE checks: does MAC appear in any Hardware CR with allowPXE=true?
      Hardware[cp01].interfaces[0].dhcp.mac = "ac:1f:6b:01:b7:21"
      allowPXE = true  → YES
    │
    SMEE responds with DHCP OFFER:
      - yiaddr: 10.10.0.170 (from Hardware.dhcp.ip.address)
      - next-server: 10.10.0.9
      - filename: "ipxe.efi"  (because Hardware.dhcp.uefi = true → cp01)
                  "undionly.kpxe"  (cp02 has uefi=false)
```

#### Phase 2: iPXE Chainload

```
UEFI firmware TFTP-fetches ipxe.efi from 10.10.0.9 (SMEE TFTP server)
    │
    iPXE boots, sends HTTP GET:
      http://10.10.0.9:8080/auto.ipxe?mac=ac:1f:6b:01:b7:21
    │
    SMEE returns iPXE script:
      #!ipxe
      kernel http://10.10.0.9:8080/vmlinuz-x86_64 \
        console=tty0 console=ttyS0,115200 \
        tink_worker_image=ghcr.io/tinkerbell/tink-worker:latest \
        grpc_authority=10.10.0.9:42113 \
        worker_id=ac:1f:6b:01:b7:21 \
        ...
      initrd http://10.10.0.9:8080/initramfs-x86_64
      boot
    │
    Node downloads kernel + initramfs from SMEE HTTP
    Boots into HookOS (minimal Alpine Linux in RAM)
```

#### Phase 3: tink-worker Connects

```
HookOS init script:
  docker pull ghcr.io/tinkerbell/tink-worker:latest
  docker run ... tink-worker \
    --grpc-address=10.10.0.9:42113 \
    --worker-id=ac:1f:6b:01:b7:21
    │
    tink-worker connects to tink-server gRPC (port 42113)
    Sends: GetWorkflowActions(workerID="ac:1f:6b:01:b7:21")
    │
    tink-server looks up: Workflow where status.tasks[].worker == "ac:1f:6b:01:b7:21"
    → cp01-provision found (status.tasks[0].worker = "ac:1f:6b:01:b7:21" after correct creation)
    Returns: ordered action list [image2disk, configure-node, reboot]
```

#### Phase 4: Action Execution

```
tink-worker executes actions sequentially:

Action 1: image2disk
  docker run --privileged \
    -v /dev:/dev -v /dev/console:/dev/console \
    -v /lib/firmware:/lib/firmware:ro -v /proc:/proc \
    busybox:stable sh -c "wget -O - http://10.10.0.9/UBUNTU_24.04_NODE_IMAGE_K8S_v1.35.0.raw.gz | gunzip | dd of=/dev/sda bs=16M"
  ~4-6 minutes for 5GB image
  tink-worker reports ACTION_SUCCESS to tink-server

Action 2: configure-node
  docker run ... busybox:stable sh -c "..."
  Mounts /dev/sda2 (root partition) to /mnt/target
  Writes:
    /mnt/target/etc/hostname           = "cp01"
    /mnt/target/etc/hosts              = localhost + cp01 entries
    /mnt/target/etc/netplan/99-daalu-all-dhcp.yaml   (static 10.10.0.170/16 on b7:21)
    /mnt/target/root/.ssh/authorized_keys              = ssh-ed25519 AAAA...
    /mnt/target/home/ubuntu/.ssh/authorized_keys       = same
    /mnt/target/etc/ssh/sshd_config    PermitRootLogin yes
    /mnt/target/var/lib/cloud/seed/nocloud/user-data   (cloud-init NoCloud)
    /mnt/target/var/lib/cloud/seed/nocloud/meta-data
    /mnt/target/etc/cloud/cloud.cfg.d/90-datasource.cfg = 'datasource_list: [NoCloud, None]'
  umount /mnt/target
  tink-worker reports ACTION_SUCCESS

Action 3: reboot
  docker run ... busybox:stable sh -c "
    nsenter --target 1 --pid -- sh -c 'sleep 5 && echo b > /proc/sysrq-trigger' &"
  Container exits immediately (exit 0)
  tink-worker reports ACTION_SUCCESS
  tink-server sets Workflow.status.state = STATE_SUCCESS
  5 seconds later: sysrq triggers hardware reboot
```

#### Phase 5: Post-Provision Boot

```
Node reboots
    │
    UEFI tries PXE first (boot order)
    NIC sends DHCP DISCOVER again
    │
    SMEE checks Hardware[cp01].allowPXE
    After workflow success: allowPXE was manually patched to false
      → SMEE does NOT respond to this MAC's DHCP request
    │
    UEFI PXE attempt times out, falls through to next boot device: local disk /dev/sda
    GRUB loads from /dev/sda, boots Ubuntu 24.04
    │
    Ubuntu first boot:
      cloud-init reads /var/lib/cloud/seed/nocloud/user-data
      Applies: hostname=cp01, user=builder (sudo), SSH key
      Sets datasource_list=[NoCloud, None] (skips EC2/Azure metadata wait)
    │
    sshd starts
    cp01 accessible at:
      10.10.0.170 (eno2 static — provisioning LAN)
      192.168.0.173 (eno1 DHCP — management LAN)
```

### Hegel's Role in This Implementation

Hegel (metadata HTTP server on port 50061) is deployed by the Tinkerbell stack chart but is **not used** in this implementation. The `configure-node` action writes cloud-init seed data directly to disk (`/var/lib/cloud/seed/nocloud/`) rather than relying on a metadata URL. The cloud-init datasource is explicitly forced to `NoCloud` via `90-datasource.cfg`, so cloud-init never contacts Hegel.

### Rufio's Role

Rufio (BMC controller) is deployed as part of CAPT (`clusterctl init --infrastructure tinkerbell` installs Rufio as a bundle). Rufio watches `Machine` and `Job` CRs:

- **`Machine` CR** (in `tinkerbell` namespace): holds the Redfish endpoint and auth secret for each server's BMC
- **`Job` CR**: used to issue power and boot-order commands

During the session, a manual Rufio Job was created to force cp01 to boot from disk after the `allowPXE: false` patch was applied:

```yaml
apiVersion: bmc.tinkerbell.org/v1alpha1
kind: Job
metadata:
  name: cp01-boot-disk
  namespace: tinkerbell
spec:
  machineRef:
    name: cp01
  tasks:
    - powerAction: "off"
    - oneTimeBootDeviceAction:
        device: [disk]
        efiBoot: true
    - powerAction: "on"
```

The TinkerbellInstaller does not create Rufio Jobs — BMC interaction is entirely manual or would need to be added as a step.

---

## 9. Cluster API Integration

### Where CAPI Manifests Are Applied

**File:** `src/daalu/cli/app.py:222` — `deploy_cluster_api_tinkerbell()`

```python
def deploy_cluster_api_tinkerbell(*, cfg, workspace_root, mgmt_context):
    manifests_dir = workspace_root / "assets" / "tinkerbell" / "cluster-api"
    kubeconfig = str(Path(cfg.mgmt_cluster.kubeconfig_output_path).expanduser())
    for manifest in sorted(manifests_dir.glob("*.yaml")):
        subprocess.run(["kubectl", "--kubeconfig", kubeconfig, "apply", "-f", str(manifest)],
                       check=True)
```

This is invoked by `daalu deploy cluster-defs/cluster.yaml --install cluster-api` when `cfg.cluster_api.provider == "tinkerbell"`.

### CAPI Manifest Files

Three files in `assets/tinkerbell/cluster-api/`:

#### `tinkerbell-cluster.yaml` — Cluster + TinkerbellCluster

```yaml
apiVersion: cluster.x-k8s.io/v1beta1
kind: Cluster
metadata:
  name: ${ CLUSTER_NAME }
  namespace: ${ NAMESPACE }
spec:
  clusterNetwork:
    services:
      cidrBlocks: [${ SERVICE_CIDR }]
    pods:
      cidrBlocks: [${ POD_CIDR }]
  infrastructureRef:
    apiVersion: infrastructure.cluster.x-k8s.io/v1beta1
    kind: TinkerbellCluster
    name: ${ CLUSTER_NAME }
  controlPlaneRef:
    apiVersion: controlplane.cluster.x-k8s.io/v1beta1
    kind: KubeadmControlPlane
    name: ${ CLUSTER_NAME }
---
apiVersion: infrastructure.cluster.x-k8s.io/v1beta1
kind: TinkerbellCluster
metadata:
  name: ${ CLUSTER_NAME }
  namespace: ${ NAMESPACE }
spec:
  controlPlaneEndpoint:
    host: ${ CLUSTER_APIENDPOINT_HOST }
    port: 6443
  imageLookupFormat: ${ IMAGE_URL }
  imageLookupBaseRegistry: ""
```

#### `tinkerbell-controlplane.yaml` — KubeadmControlPlane + TinkerbellMachineTemplate

```yaml
apiVersion: controlplane.cluster.x-k8s.io/v1beta1
kind: KubeadmControlPlane
metadata:
  name: ${ CLUSTER_NAME }
  namespace: ${ NAMESPACE }
spec:
  replicas: ${ CONTROL_PLANE_MACHINE_COUNT }
  version: ${ KUBERNETES_VERSION }
  machineTemplate:
    infrastructureRef:
      apiVersion: infrastructure.cluster.x-k8s.io/v1beta1
      kind: TinkerbellMachineTemplate
      name: ${ CLUSTER_NAME }-controlplane
  kubeadmConfigSpec:
    initConfiguration:
      nodeRegistration:
        kubeletExtraArgs:
          cloud-provider: external
    joinConfiguration:
      nodeRegistration:
        kubeletExtraArgs:
          cloud-provider: external
    users:
      - name: ${ IMAGE_USERNAME }
        sudo: "ALL=(ALL) NOPASSWD:ALL"
        sshAuthorizedKeys:
          - ${ SSH_PUB_KEY_CONTENT }
---
apiVersion: infrastructure.cluster.x-k8s.io/v1beta1
kind: TinkerbellMachineTemplate
metadata:
  name: ${ CLUSTER_NAME }-controlplane
  namespace: ${ NAMESPACE }
spec:
  template:
    spec:
      templateOverride: |
        version: "0.1"
        name: ubuntu-kubeadm-controlplane
        global_timeout: 1800
        tasks:
          - name: "stream-image"
            worker: "{{.device_1}}"
            volumes:
              - /dev:/dev
              - /dev/console:/dev/console
              - /lib/firmware:/lib/firmware:ro
            actions:
              - name: image2disk
                image: ghcr.io/tinkerbell/actions/image2disk:v0.1.0
                timeout: 600
                environment:
                  IMG_URL: "${ IMAGE_URL }"
                  DEST_DISK: "/dev/sda"
                  COMPRESSED: "true"
              - name: reboot
                image: ghcr.io/tinkerbell/actions/reboot:v0.1.0
                timeout: 90
                volumes:
                  - /worker:/worker
```

#### `tinkerbell-workers.yaml` — MachineDeployment + TinkerbellMachineTemplate + KubeadmConfigTemplate

Same pattern as controlplane but for worker nodes.

### Variable Substitution Problem

**All three files use `${ VAR }` syntax. `deploy_cluster_api_tinkerbell()` applies them raw with no substitution.**

This means applying these files as-is will result in literal `${ CLUSTER_NAME }` strings in the cluster name, namespace, replicas, etc. — which Kubernetes will reject or silently misinterpret. The CAPI provisioning step (`daalu deploy --install cluster-api`) is therefore currently non-functional for Tinkerbell without prior manual substitution.

The correct approach would be to run `envsubst` on each file with the appropriate environment variables set, or to use Python string replacement, before piping to kubectl.

### CAPI Object Relationship (Design Intent)

```
Cluster [cluster.x-k8s.io/v1beta1]
  ├─ infrastructureRef → TinkerbellCluster
  │     └─ spec.controlPlaneEndpoint.host = VIP (10.10.0.249)
  │
  └─ controlPlaneRef → KubeadmControlPlane
        ├─ spec.replicas = 1 (or N)
        └─ machineTemplate.infrastructureRef → TinkerbellMachineTemplate (controlplane)
              └─ spec.template.spec.templateOverride
                    [Tinkerbell template YAML inline — replaces ubuntu-kubeadm Template CR]

MachineDeployment [cluster.x-k8s.io/v1beta1]
  ├─ bootstrap.configRef → KubeadmConfigTemplate
  └─ infrastructureRef → TinkerbellMachineTemplate (workers)
        └─ spec.template.spec.templateOverride

CAPT controller (capt-system/capt-controller-manager):
  Watches: TinkerbellMachine objects (auto-created by KubeadmControlPlane)
  For each unbound TinkerbellMachine:
    1. Finds an available Hardware CR (allowWorkflow=true, no ownerReference)
    2. Sets ownerReference on Hardware pointing to TinkerbellMachine
    3. Creates a NEW Workflow using templateOverride from TinkerbellMachineTemplate
       (not the ubuntu-kubeadm Template CR — the templateOverride replaces it entirely)
    4. Workflow's hardwareMap is populated from KubeadmControlPlane cloud-init userData
    5. Node PXE boots, HookOS runs, tink-worker executes templateOverride actions
    6. kubeadm cloud-init userData runs on first Ubuntu boot (init or join)
```

### templateOverride Gap

The `templateOverride` in both `tinkerbell-controlplane.yaml` and `tinkerbell-workers.yaml` uses `ghcr.io/tinkerbell/actions/image2disk:v0.1.0` and `ghcr.io/tinkerbell/actions/reboot:v0.1.0`. These are the official Tinkerbell action images. However, the actual working `ubuntu-kubeadm` Template uses `busybox:stable` with custom shell scripts — because the official action images proved insufficient for this deployment. The CAPI `templateOverride` has not been updated to match the working busybox approach, meaning CAPI-driven reprovisioning would use different (potentially non-functional) actions.

---

## 10. End-to-End Execution Trace

### Phase 0: Prerequisites (manual)

```
1. Physical bare-metal servers cp01, cp02 exist with:
   - BMC accessible at 192.168.0.70 and 192.168.0.69 (Redfish)
   - Provisioning NIC: eno2 (cp01: ac:1f:6b:01:b7:21, cp02: ac:1f:6b:01:b5:eb)
   - Management NIC: eno1

2. Fresh Ubuntu 24.04 node at 192.168.0.171
   - Two NICs: ens18 (management, DHCP), ens19 (provisioning, will get static 10.10.0.9)
   - SSH accessible as 'kez' with password

3. OS image built and ready at operator's machine

4. cluster-defs/cluster.yaml configured with mgmt_cluster block
   cloud-config/secrets.yaml has ssh_password, bmc credentials

5. WORKSPACE_ROOT environment variable set, or running from project root
```

### Phase 1: `daalu mgmt cluster-defs/cluster.yaml --provider tinkerbell`

```
app.py:1243  mgmt()
  │
  ├─ load_config("cluster-defs/cluster.yaml")
  │    DaaluConfig loaded, deep-merged with cloud-config/secrets.yaml
  │    mgmt_cfg = MgmtClusterConfig(
  │      host="192.168.0.171", ssh_username="kez",
  │      provisioning_ip="10.10.0.9", provisioning_interface="ens19",
  │      dhcp_range_begin="10.10.0.200", dhcp_range_end="10.10.0.250",
  │      hardware=[TinkerbellHardware(cp01...), TinkerbellHardware(cp02...)]
  │    )
  │
  └─ MgmtClusterManager(cfg, WORKSPACE_ROOT).deploy()
       │
       ├─ manager.py:64  _ssh_connect(mgmt_cfg)
       │    paramiko.SSHClient().connect("192.168.0.171", username="kez", password=...)
       │    SSHRunner(client)
       │
       ├─ manager.py:72  K8sInstaller(ssh, mgmt_cfg).install()
       │    k8s_installer.py:42
       │    ├─ _setup_passwordless_sudo()     → /etc/sudoers.d/daalu-mgmt-nopasswd
       │    ├─ _setup_provisioning_interface() → ip addr add 10.10.0.9/16 dev ens19
       │    │                                    netplan write + apply
       │    ├─ _cluster_is_running()          → kubectl get nodes on remote
       │    │   FALSE (fresh node) → continue
       │    ├─ _disable_swap()
       │    ├─ _load_kernel_modules()         → overlay, br_netfilter
       │    ├─ _set_sysctl()                  → ip_forward, bridge-nf-call-iptables
       │    ├─ _install_containerd()          → apt-get, SystemdCgroup=true
       │    ├─ _install_kube_tools()          → apt pkgs.k8s.io v1.30
       │    ├─ _kubeadm_init()               → kubeadm init --pod-network-cidr=172.16.0.0/16
       │    │                                    --skip-phases=addon/kube-proxy
       │    │                                    removes control-plane taint (single-node)
       │    └─ _fetch_kubeconfig()            → cat /etc/kubernetes/admin.conf → string
       │
       ├─ manager.py:79  Write kubeconfig to ~/.kube/daalu-mgmt-config (chmod 600)
       │                  Copy to ~/.kube/config
       │
       ├─ manager.py:98  k8s.install_cilium(kubeconfig_path)
       │    helm upgrade --install cilium cilium/cilium v1.16.0
       │    --set kubeProxyReplacement=true
       │    --set socketLB.enabled=true --set operator.replicas=1 --wait
       │    ssh.run("systemctl restart containerd")
       │    kubectl wait node --all --for=condition=Ready --timeout=5m
       │
       └─ manager.py:113  TinkerbellInstaller(kubeconfig_path, mgmt_cfg, WORKSPACE_ROOT).install()
```

### Phase 2: `TinkerbellInstaller.install()`

```
tinkerbell_installer.py:45

├─ Step 1: _install_cert_manager()
│    _deployment_ready("cert-manager", "cert-manager") → False
│    chart = assets/cert-manager/charts/<dir or .tgz>
│    helm upgrade --install cert-manager <chart> --set installCRDs=true
│    kubectl -n cert-manager rollout status deploy/cert-manager (3 deployments)
│    Poll: kubectl create --dry-run=server -f <Issuer YAML> until OK (120s timeout)
│
├─ Step 2: _install_capi()
│    _deployment_ready("capi-system", "capi-controller-manager") → False
│    clusterctl init --core cluster-api:v1.12.0
│                    --bootstrap kubeadm:v1.12.0
│                    --control-plane kubeadm:v1.12.0
│
├─ Step 3: _install_capt()
│    _deployment_ready("capt-system", "capt-controller-manager") → False
│    _register_capt_in_clusterctl_config("v0.6.0")
│      Writes ~/.config/cluster-api/clusterctl.yaml:
│        providers:
│          - name: tinkerbell
│            url: https://github.com/tinkerbell/.../v0.6.0/infrastructure-components.yaml
│            type: InfrastructureProvider
│    env = {TINKERBELL_IP: "10.10.0.9", ...os.environ}
│    clusterctl init --infrastructure tinkerbell:v0.6.0   (env has TINKERBELL_IP)
│
├─ Step 4: _install_tinkerbell_stack()
│    helm status tinkerbell -n tinkerbell → non-zero (not installed)
│    chart = assets/tinkerbell/charts/stack-0.6.3.tgz
│    helm upgrade --install tinkerbell stack-0.6.3.tgz
│         --set global.publicIP=10.10.0.9
│         --set global.trustedProxies={172.16.0.0/16}
│         --wait --timeout 10m
│
├─ Step 5: _configure_smee()
│    cfg.dhcp_range_begin = "10.10.0.200" (set) → proceed
│    Build JSON strategic-merge patch:
│      hostNetwork: true
│      env: [SMEE_DHCP_IP_FOR_PACKET=10.10.0.9,
│             SMEE_DHCP_RANGE_START=10.10.0.200,
│             SMEE_DHCP_RANGE_END=10.10.0.250,
│             SMEE_DHCP_GATEWAY=10.10.0.9,
│             SMEE_DHCP_DNS=8.8.8.8]
│    kubectl -n tinkerbell patch deployment/smee --type=strategic --patch=<json>
│    kubectl -n tinkerbell rollout status deployment/smee --timeout=3m
│
├─ Step 6: _deploy_image_server()
│    _deployment_ready("tinkerbell", "image-server") → False
│    Build manifest (Python dicts → yaml.dump_all):
│      Namespace: tinkerbell
│      Deployment: image-server (hostNetwork=true, nginx, /var/www/images hostPath)
│      Service: NodePort 30080, externalIPs=[10.10.0.9]
│    kubectl apply -f -  (stdin)
│    kubectl -n tinkerbell rollout status deploy/image-server --timeout=3m
│
│    [Manual step required: copy OS image to /var/www/images on mgmt node]
│
├─ Step 7: _register_hardware()
│    For hw in [cp01, cp02]:
│      Build: bmc_secret + bmc_cr + hardware_cr  (Python dicts)
│      yaml.dump_all([bmc_secret, bmc_cr, cr])
│      kubectl apply -f -  (stdin, multi-document YAML)
│      → creates/updates in namespace tinkerbell:
│          Secret: cp01-bmc-secret
│          Machine: cp01 (bmc.tinkerbell.org/v1alpha1)
│          Hardware: cp01 (tinkerbell.org/v1alpha1)
│
├─ Step 8: _create_os_template()
│    path = assets/tinkerbell/templates/ubuntu-kubeadm.yaml
│    kubectl apply -f assets/tinkerbell/templates/ubuntu-kubeadm.yaml
│    → creates Template: ubuntu-kubeadm in namespace tinkerbell
│
└─ Step 9: _create_workflows()
     workflows_dir = assets/tinkerbell/workflows/
     sorted glob → [cp01-workflow.yaml, cp02-workflow.yaml]
     kubectl apply -f assets/tinkerbell/workflows/cp01-workflow.yaml
     kubectl apply -f assets/tinkerbell/workflows/cp02-workflow.yaml
     → creates Workflow: cp01-provision, cp02-provision
     tink-controller renders Template with hardwareMap values
     Stores rendered actions in status.tasks[]
     Workflow state: STATE_PENDING
```

### Phase 3: Node Provisioning (automatic, no Python involvement)

```
cp01 powers on (manual or BMC Job)
  → DHCP DISCOVER on ens19 (10.10.x.x LAN)
  → SMEE offers 10.10.0.170, next-server=10.10.0.9, filename=ipxe.efi
  → iPXE boots, fetches vmlinuz + initramfs from SMEE
  → HookOS boots in RAM
  → tink-worker: workerID=ac:1f:6b:01:b7:21 → connects tink-server:42113
  → action image2disk: wget | gunzip | dd → /dev/sda  (~5-8 minutes)
  → action configure-node: mount, write files, umount
  → action reboot: nsenter + sysrq (background, 5s delay)
  → STATE_SUCCESS on cp01-provision
  → Hardware.allowPXE patched to false (manual step post-workflow)
  → Rufio BMC Job: powerAction=off, oneTimeBootDeviceAction=disk, powerAction=on
  → Ubuntu boots from /dev/sda, cloud-init applies configuration
  → cp01 SSH-accessible at 10.10.0.170 (eno2) and 192.168.0.173 (eno1)

cp02: same flow, STATE_SUCCESS, up for 12h+ at session time
```

### Phase 4: CAPI Cluster Provisioning (next step, not yet executed)

```
daalu deploy cluster-defs/cluster.yaml --install cluster-api

app.py:877  deploy()
  provider = cfg.cluster_api.provider  → "tinkerbell"
  deploy_cluster_api_tinkerbell(cfg, WORKSPACE_ROOT, mgmt_context)
    │
    manifests_dir = assets/tinkerbell/cluster-api/
    for manifest in [tinkerbell-cluster.yaml, tinkerbell-controlplane.yaml, tinkerbell-workers.yaml]:
      kubectl --kubeconfig ~/.kube/daalu-mgmt-config apply -f <manifest>

[PROBLEM: ${ VAR } substitution not performed — manifests applied with literal placeholders]

If substitution were done correctly:
  → Cluster CR created → CAPT reconciler starts
  → KubeadmControlPlane created (replicas=1)
  → CAPT creates TinkerbellMachine for control plane
  → CAPT finds Hardware[cp01] (allowWorkflow=true, unclaimed)
  → CAPT sets ownerReference on Hardware[cp01] → TinkerbellMachine
  → CAPT creates new Workflow using templateOverride (image2disk + reboot actions)
  → cp01 re-provisions (must have allowPXE=true for this to work)
  → kubeadm init runs via cloud-init on first boot
  → CAPT retrieves kubeconfig, marks TinkerbellMachine Ready
  → KubeadmControlPlane Ready
  → Cluster InfrastructureReady=true, ControlPlaneReady=true
  → Cluster Ready
```

---

## 11. Error Handling and State Management

### Idempotency Mechanisms

| Step | Idempotency Method |
|---|---|
| K8s cluster install | `_cluster_is_running()` → SSH check `kubectl get nodes` |
| cert-manager install | `_deployment_ready("cert-manager", "cert-manager")` |
| CAPI install | `_deployment_ready("capi-system", "capi-controller-manager")` |
| CAPT install | `_deployment_ready("capt-system", "capt-controller-manager")` |
| Tinkerbell stack install | `helm status tinkerbell` exit code |
| Image server install | `_deployment_ready("tinkerbell", "image-server")` |
| Hardware CRs | `kubectl apply` (server-side apply semantics) |
| Template CR | `kubectl apply` |
| Workflow CRs | `kubectl apply` (but see immutability trap — doesn't re-render) |

### `_deployment_ready()` Implementation

```python
def _deployment_ready(self, namespace: str, deploy: str) -> bool:
    r = subprocess.run([
        "kubectl", "--kubeconfig", self._kc,
        "get", f"deploy/{deploy}", "-n", namespace,
        "-o", "jsonpath={.status.readyReplicas}",
    ], capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() not in ("", "0")
```

Returns `True` only if the deployment exists AND has at least one ready replica.

### Error Propagation

- All `subprocess.run(..., check=True)` calls raise `subprocess.CalledProcessError` on non-zero exit
- `K8sInstaller._run()` wraps SSH commands and raises `RuntimeError` on failure with the stderr output
- Exceptions propagate up to `MgmtClusterManager.deploy()` and then to the CLI, which prints the traceback

There is no retry logic anywhere in the Tinkerbell code path. If a step fails, the entire `install()` sequence fails. Re-running `daalu mgmt` will skip completed steps (via idempotency checks) and retry from the failed step.

### Workflow Failure Detection

There is **no Python code that detects workflow failures**. The only detection mechanism is manual:

```bash
kubectl get workflow -n tinkerbell
# Check STATE_FAILED or STATE_TIMEOUT
kubectl describe workflow cp01-provision -n tinkerbell
# Check status.tasks[].actions[].status and message
```

### The `allowPXE` Post-Workflow Step

After a Workflow reaches `STATE_SUCCESS`, `allowPXE` must be set to `false` on the Hardware CR to prevent UEFI-boot nodes from PXE-booting into HookOS again on next reboot. This step is **not automated** — it must be done manually:

```bash
kubectl patch hardware cp01 -n tinkerbell --type=json \
  -p='[{"op":"replace","path":"/spec/interfaces/0/netboot/allowPXE","value":false}]'
```

---

## 12. Observations and Gaps

### Gap 1: `${ VAR }` Substitution Not Implemented

**File:** `app.py:222` — `deploy_cluster_api_tinkerbell()`

The CAPI manifest files (`tinkerbell-cluster.yaml`, etc.) use `${ VAR }` placeholder syntax. The `deploy_cluster_api_tinkerbell()` function applies them raw with no substitution. `kubectl apply` will attempt to create Kubernetes objects with literal `${ CLUSTER_NAME }` as the resource name, which Kubernetes will reject (invalid characters) or accept as a garbage string, depending on the field.

**Fix required:** Before applying, run `envsubst` with all required variables set, or implement Python string substitution with values from `cfg`.

Required variables:
```
CLUSTER_NAME, NAMESPACE, SERVICE_CIDR, POD_CIDR,
CLUSTER_APIENDPOINT_HOST, IMAGE_URL, KUBERNETES_VERSION,
CONTROL_PLANE_MACHINE_COUNT, WORKER_MACHINE_COUNT,
IMAGE_USERNAME, SSH_PUB_KEY_CONTENT
```

---

### Gap 2: Wrong MAC in `cluster.yaml` Hardware Entry for cp01

**File:** `cluster-defs/cluster.yaml` line 228

```yaml
hardware:
  - name: cp01
    mac: "ac:1f:6b:01:b7:20"   # ← eno1 (management NIC), NOT the PXE boot NIC
```

The PXE-boot NIC on cp01 is `eno2` with MAC `ac:1f:6b:01:b7:21`. The cluster.yaml has `b7:20` which is `eno1`. When `TinkerbellInstaller._register_hardware()` runs, it will create a Hardware CR with `dhcp.mac: ac:1f:6b:01:b7:20`, and the Workflow it creates from `cp01-workflow.yaml` (which has the correct `b7:21`) will be inconsistent with the Hardware CR.

**Fix:** Correct the MAC in `cluster.yaml` to `ac:1f:6b:01:b7:21`.

---

### Gap 3: No Workflow Status Polling

After `_create_workflows()`, the installer returns immediately. There is no code that:
- Waits for workflows to complete before declaring success
- Detects `STATE_FAILED` or `STATE_TIMEOUT`
- Reports which action failed and why

The installer prints `[mgmt/tinkerbell] Tinkerbell stack installed successfully` even before the nodes have PXE-booted.

**Fix:** Add a `_wait_for_workflows()` step that polls `kubectl get workflow -n tinkerbell` and blocks until all workflows are `STATE_SUCCESS` or raises on `STATE_FAILED`.

---

### Gap 4: No `allowPXE` Automation After Workflow Completion

After a workflow reaches `STATE_SUCCESS`, `allowPXE` must be manually set to `false` to prevent UEFI nodes from re-PXE-booting into HookOS on every subsequent reboot. This caused cp01 to PXE loop after its first successful provision.

**Fix:** In the workflow poller (Gap 3), after detecting `STATE_SUCCESS` for a hardware node, automatically patch `allowPXE: false` via `kubectl patch hardware <name> -n tinkerbell`.

---

### Gap 5: `templateOverride` Uses Non-Working Action Images

**Files:** `assets/tinkerbell/cluster-api/tinkerbell-controlplane.yaml`, `tinkerbell-workers.yaml`

The `templateOverride` embedded in TinkerbellMachineTemplate uses:
```
ghcr.io/tinkerbell/actions/image2disk:v0.1.0
ghcr.io/tinkerbell/actions/reboot:v0.1.0
```

The working standalone Template (`ubuntu-kubeadm.yaml`) uses `busybox:stable` with custom shell scripts because the official action images proved insufficient. CAPT-driven reprovisioning would use the non-working official images, making CAPT-provisioned nodes fail during their workflow.

**Fix:** Update the `templateOverride` in both files to use the same `busybox:stable` approach with the validated shell scripts from `ubuntu-kubeadm.yaml`.

---

### Gap 6: Image Server Requires Manual OS Image Upload

The `_deploy_image_server()` step deploys nginx and creates `hostPath: /var/www/images` on the mgmt node, but the OS image must be manually placed there:

```bash
# On mgmt node (192.168.0.171):
sudo cp UBUNTU_24.04_NODE_IMAGE_K8S_v1.35.0.raw.gz /var/www/images/
```

There is no code to upload or download the image. The installer logs a reminder but does not block or verify that the image exists before creating workflows.

**Fix:** Before creating workflows, check that `http://10.10.0.9/<image_filename>` returns HTTP 200. If not, fail with a clear error.

---

### Gap 7: Hardcoded SSH Public Key in Workflow Files

The `ssh_pub_key` in `cp01-workflow.yaml` and `cp02-workflow.yaml` is hardcoded:

```yaml
ssh_pub_key: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGdk/DV6U01MtdXrIoSlKhEoNT2QND0lsKdfWmH3to7C kez@kez-dev-vm-1"
```

This is the developer's personal key committed to version-controlled YAML files alongside BMC credentials. The BMC passwords (`ADMIN/ADMIN`) are also in `cluster.yaml` in plaintext.

**Fix:**
- Move `ssh_pub_key` to `secrets.yaml` and inject at workflow generation time
- Build workflows programmatically (not as static files) so `ssh_pub_key` comes from config
- Rotate BMC credentials from default `ADMIN/ADMIN`

---

### Gap 8: No Rufio BMC Power Sequencing

The installer registers Rufio Machine CRs (BMC handles) but never issues power or boot commands. After registering hardware and creating workflows, the installer expects the operator to manually power on the nodes. There is no:

- Automatic power-on via Rufio Job
- Boot order verification
- PXE boot verification

**Fix:** After creating workflows, create a Rufio Job CR for each node:
```yaml
tasks:
  - powerAction: "off"
  - oneTimeBootDeviceAction:
      device: [pxe]
      efiBoot: true   # for uefi=true nodes
  - powerAction: "on"
```

Then poll Rufio Job status before proceeding.

---

### Gap 9: CAPT `clusterctl.yaml` Config Path Assumption

**File:** `tinkerbell_installer.py:592`

```python
config_path = Path.home() / ".config" / "cluster-api" / "clusterctl.yaml"
```

This is correct on Linux but assumes `~/.config/cluster-api/` is the right XDG config path. It also uses a regex-based approach to remove and re-add the `providers:` block from the config file. If the config file has unusual formatting or the regex doesn't match, it silently leaves the old providers block, causing clusterctl to register the provider twice or use an old URL.

---

### Gap 10: SMEE DHCP Does Not Deduplicate Env Vars on Patch

**File:** `tinkerbell_installer.py:261`

The strategic merge patch for SMEE adds environment variables to the containers list. If `_configure_smee()` is called multiple times (e.g., on re-run), the strategic merge will duplicate the env vars in the container spec, because env var names alone are not the merge key for strategic merge of the `env` array. This can cause SMEE to pick up unexpected or conflicting configuration.

---

### Gap 11: No Hardware CR in `assets/tinkerbell/hardware/` Is Applied

**Files:** `assets/tinkerbell/hardware/cp01.yaml`, `cp02.yaml`

These files contain correct Hardware CRs (with the right MACs). However, `TinkerbellInstaller._register_hardware()` builds Hardware CRs programmatically from `cfg.hardware` and does **not** read from `assets/tinkerbell/hardware/`. The files are unused by the automation and exist only as reference. This creates a confusing discrepancy where the files have the correct MAC (`b7:21`) but `cluster.yaml` has the wrong one (`b7:20`).

**Fix:** Either:
- Apply `assets/tinkerbell/hardware/*.yaml` directly (like workflows and templates), removing the programmatic generation; or
- Remove the files and make `cluster.yaml` the single source of truth

---

### Fragile Areas Summary

| Area | Risk | Severity |
|---|---|---|
| Workflow `status.tasks` immutability | Silent failure if MAC in hardwareMap doesn't match PXE NIC | Critical |
| `allowPXE: true` after provisioning | UEFI nodes PXE-loop into HookOS on every reboot | High |
| `${ VAR }` not substituted in CAPI manifests | CAPI provisioning completely broken | High |
| `templateOverride` uses old action images | CAPT-driven provisioning fails at workflow execution | High |
| No workflow status polling | Installer declares success before nodes are provisioned | Medium |
| Hardcoded SSH key and BMC creds in files | Security exposure | Medium |
| No Rufio power sequencing | Manual power-on required for each node | Medium |
| Image not verified before workflow creation | Nodes boot HookOS, fail at `wget`, stuck `STATE_FAILED` | Medium |
| SMEE env var duplication on re-patch | Potential SMEE misconfiguration on idempotent re-run | Low |
