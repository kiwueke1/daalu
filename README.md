## Copyright

Copyright © 2026 Kezie Iwueke.

# Daalu

**Daalu powers independent cloud infrastructure — automated, production-grade, and fully under your control.**

Daalu is a Python-based orchestration platform for deploying and managing private or public cloud infrastructure anywhere. It models infrastructure components as native Python objects and uses this abstraction layer to automate the full lifecycle provisioning of bare-metal Kubernetes clusters, OpenStack services, Ceph storage, monitoring stacks, and HPC workloads — from initial provisioning to day-2 operations — enabling organizations to build self-hosted production-grade cloud infrastructure without dependency on hyperscalers.

To understand the motivation behind this project, see [The NoCloud (Not Only Cloud) Philosophy](#the-nocloud-not-only-cloud-philosophy).

## What It Does

- **Bare-metal provisioning** — Onboards bare-metal servers into Kubernetes using Tinkerbell (PXE/iPXE) and Cluster API Provider Tinkerbell (CAPT)
- **OpenStack deployment** — Deploys a full OpenStack control plane (Keystone, Nova, Neutron, Glance, Heat, Cinder, Horizon, and more) via Helm charts
- **Ceph storage** — Bootstraps Ceph clusters and configures RBD CSI drivers
- **Identity management** — Integrates Keycloak for SSO/OIDC across Grafana and OpenStack
- **Monitoring** — Deploys Prometheus, Grafana, Loki, OpenSearch, and Thanos for metrics and log aggregation
- **Infrastructure services** — MetalLB, Ingress-NGINX, ArgoCD, Istio, cert-manager, and more
- **HPC orchestration** — GPU cluster management with Volcano, Ray, and Slurm schedulers

---

## Final End Product

- **Kubernetes control plane** — A production Kubernetes cluster running directly on bare-metal servers using Cluster API and Tinkerbell
- **OpenStack cloud layer** — A fully operational OpenStack control plane providing compute (Nova), networking (Neutron), image services (Glance), block and object storage (Cinder), and orchestration capabilities
- **Distributed storage backend** — A Ceph-backed storage system with RBD CSI integration for persistent volumes and cloud storage services
- **Integrated operations stack** — Centralized identity (OIDC/SSO), monitoring, logging, and GitOps-based lifecycle management

---

## Project Structure

```
daalu/
├── src/daalu/                  # Main Python package
│   ├── cli/                    # Typer CLI entry points
│   ├── config/                 # YAML config loading and Pydantic models
│   ├── bootstrap/              # Core provisioning logic
│   │   ├── mgmt/               # Management cluster bootstrap + teardown
│   │   │   ├── tinkerbell_installer.py  # Tinkerbell/CAPT stack installer
│   │   │   ├── capt_provisioner.py      # CAPT workload cluster provisioning
│   │   │   └── k8s_installer.py         # kubeadm-based mgmt k8s install
│   │   ├── ceph/               # Ceph deployment
│   │   ├── csi/                # Container Storage Interface
│   │   ├── openstack/          # OpenStack service components
│   │   ├── infrastructure/     # Infra components (MetalLB, ArgoCD, etc.)
│   │   ├── monitoring/         # Monitoring stack (Prometheus, Grafana, etc.)
│   │   ├── registry/           # Harbor container registry
│   │   └── shared/             # Shared utilities (Keycloak, etc.)
│   ├── helm/                   # Helm chart runner
│   ├── observers/              # Event bus and lifecycle logging
│   └── utils/                  # SSH runner, retry helpers
├── cluster-defs/               # Cluster definition YAML files
│   └── cluster.yaml            # Main cluster configuration
├── cloud-config/               # Cloud configuration
│   ├── secrets.yaml            # Your secrets (git-ignored)
│   └── secrets.yaml.example    # Template showing required keys
├── assets/                     # Helm values, chart directories, and CAPT manifests
│   └── tinkerbell/
│       └── cluster-api/        # CAPT workload cluster manifests
└── tests/                      # Test suites
```

---

## Prerequisites

### Required CLI tools

The following must be installed on the machine where you run `daalu`:

- [Python 3.10+](https://www.python.org/downloads/)
- [`kubectl`](https://kubernetes.io/docs/tasks/tools/)
- [`clusterctl`](https://cluster-api.sigs.k8s.io/clusterctl/overview.html)
- [`helm` 3.x](https://helm.sh/docs/intro/install/)
- [`cilium` CLI](https://docs.cilium.io/en/stable/gettingstarted/k8s-install-default/#install-the-cilium-cli)
- SSH client (preinstalled on Linux/macOS)

```bash
kubectl version --client
clusterctl version
helm version
cilium version
python --version
```

### Hardware requirements

- **Management node** — A bare-metal machine or VM running Ubuntu 22.04/24.04, with two NICs:
  - **Home/management NIC** — For SSH access and internet connectivity (e.g. `192.168.0.x`)
  - **Provisioning NIC** — Dedicated L2 network for PXE booting workload nodes (e.g. `10.10.0.x`)
- **Workload nodes** — One or more bare-metal servers with:
  - IPMI/Redfish BMC access (for Rufio power control)
  - PXE boot capability on the provisioning network
  - A dedicated disk for the OS (`/dev/sda` by default)
- **Storage node** *(optional)* — A dedicated server with additional disks for Ceph OSDs

---

## Installation

```bash
# Clone the repository
git clone https://github.com/kiwueke1/daalu.git
cd daalu

# Create a virtual environment and install
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

After installation, the `daalu` command is available:

```bash
daalu --help
```

---

## Helm Charts

Each service under `assets/` includes a `values.yaml` (tracked in git) and a `charts/` directory (git-ignored). Download the Helm chart for each service before deploying.

```bash
# General pattern
helm repo add <repo-name> <repo-url>
helm repo update
helm pull <repo-name>/<chart> --untar --untardir assets/<service>/charts/
```

**Examples:**

```bash
helm repo add metallb https://metallb.github.io/metallb
helm pull metallb/metallb --untar --untardir assets/metallb/charts/

helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm pull ingress-nginx/ingress-nginx --untar --untardir assets/ingress-nginx/charts/

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm pull prometheus-community/kube-prometheus-stack --untar --untardir assets/kube-prometheus-stack/charts/

helm repo add grafana https://grafana.github.io/helm-charts
helm pull grafana/loki --untar --untardir assets/loki/charts/

helm repo add bitnami https://charts.bitnami.com/bitnami
helm pull bitnami/keycloak --untar --untardir assets/keycloak/charts/

# Temporal — required only if mgmt_cluster.temporal.enabled (default: true)
helm repo add temporal https://go.temporal.io/helm-charts
helm pull temporal/temporal --untar --untardir assets/temporal/charts/
```

---

## Configuration

### 1. Cluster definition

Edit `cluster-defs/cluster.yaml` to match your environment. Non-secret fields (networking, versions, sizing) go here directly. Secret fields are left empty and populated from `secrets.yaml` at runtime.

Key sections:

```yaml
mgmt_cluster:
  host: "192.168.0.171"          # IP of the Ubuntu management node
  ssh_username: "kez"
  provisioning_ip: "10.10.0.9"  # Static IP on the provisioning NIC
  provisioning_interface: "ens19"
  install_harbor: true

registry:
  harbor_hostname: "10.10.0.9"  # Provisioning IP — reachable by workload nodes
  harbor_storage_size: "100Gi"

cluster_api:
  provider: tinkerbell
  cluster_name: auto-openstack-infra
  namespace: tinkerbell
  control_plane_vip: "10.10.0.249"  # Virtual IP for the workload API server
  kubernetes_version: v1.35.0
  cilium_version: "1.16.0"
  ironic_http_base: http://10.10.0.9  # Image server (also used as Hegel base URL)
  image_url: http://10.10.0.9/UBUNTU_24.04_NODE_IMAGE_K8S_v1.35.0.raw.gz
  control_plane_replicas: 1
  worker_replicas: 1

ceph:
  additional_ceph_hosts:
    - hostname: storage-01
      osd_devices: [/dev/sdb, /dev/sdc]
```

### 2. Secrets file

```bash
cp cloud-config/secrets.yaml.example cloud-config/secrets.yaml
# Edit secrets.yaml and fill in passwords, SSH keys, BMC credentials, etc.
```

The config loader automatically finds `cloud-config/secrets.yaml` and deep-merges it into the cluster definition at runtime. The file is git-ignored.

### 3. Hardware CRs

Each bare-metal workload node must have a Hardware CR in `assets/tinkerbell/hardware/`. These define the node's MAC address, BMC credentials, and provisioning network settings. See the example files in that directory.

---

## Usage

### Full workflow

#### Step 1 — Bootstrap the management cluster

```bash
daalu mgmt cluster-defs/cluster.yaml
```

This single command provisions the entire management stack on a fresh Ubuntu node over SSH:

| Stage | What it installs |
|---|---|
| **Kubernetes** | kubeadm-based single-node cluster |
| **cert-manager** | TLS certificate management (prerequisite for CAPI) |
| **Cluster API core** | CAPI controllers and CRDs |
| **Tinkerbell stack** | Tink Server + Tink Controller, Hegel (metadata server), SMEE (DHCP/iPXE server), Rufio (BMC controller) |
| **CAPT** | Cluster API Provider Tinkerbell — manages workload cluster lifecycle |
| **Image server** | HTTP file server serving OS images to PXE-booting nodes |
| **Hegel patch** | Exposes Hegel on the provisioning IP so bare-metal nodes can reach it from HookOS |
| **Hardware registration** | Creates Hardware CRs for each workload node (MAC, BMC, network config) |
| **Harbor registry** | Formats dedicated storage, deploys Harbor, mirrors all required images |

Optional overrides:

```bash
daalu mgmt cluster-defs/cluster.yaml \
  --ssh-host 192.168.0.171 \
  --ssh-password yourpassword \
  --provisioning-interface ens19 \
  --kubeconfig-out ~/.kube/daalu-mgmt-config
```

When complete:

```
Management cluster is ready!

  Kubeconfig  : /home/user/.kube/daalu-mgmt-config
  Harbor UI   : https://10.10.0.9:30003
  Harbor creds: admin / <registry.admin_password from secrets.yaml>
```

---

#### Step 2 — Deploy everything (single command)

Once the management cluster is up, deploy the full workload cluster and all components with one command:

```bash
daalu deploy cluster-defs/cluster.yaml \
  --install cluster-api,nodes,ceph,csi,infrastructure,openstack
```

This runs all stages in sequence:

| Stage | What happens |
|---|---|
| **cluster-api** | Applies CAPT manifests, PXE-boots nodes via Rufio into HookOS, streams OS image to disk, reboots into Ubuntu, waits for kubeadm init, installs Cilium CNI, waits for all nodes Ready |
| **nodes** | SSH into workload nodes: creates managed user, sets hostname, configures containerd to trust Harbor |
| **ceph** | Deploys Rook-Ceph operator, bootstraps Ceph cluster, adds OSD disks |
| **csi** | Installs Ceph RBD CSI driver and creates StorageClasses |
| **infrastructure** | Deploys MetalLB, Ingress-NGINX, ArgoCD, cert-manager, Keycloak, Istio, CoreDNS patches |
| **openstack** | Deploys full OpenStack control plane: Keystone, Glance, Neutron, Nova, Cinder, Horizon, Heat, Octavia, and more. Runs cloud smoke test at the end. |

---

### Selective deployment

Re-run or deploy individual stages in isolation:

```bash
# Provision the workload Kubernetes cluster only
daalu deploy cluster-defs/cluster.yaml --install cluster-api

# Bootstrap nodes only (after cluster-api is done)
daalu deploy cluster-defs/cluster.yaml --install nodes

# Re-run Ceph only (e.g. to add OSDs)
daalu deploy cluster-defs/cluster.yaml --install ceph

# Re-run infrastructure + OpenStack only
daalu deploy cluster-defs/cluster.yaml --install infrastructure,openstack

# Deploy everything
daalu deploy cluster-defs/cluster.yaml --install all
```

### Available install targets

| Target | Description |
|---|---|
| `cluster-api` | Provision Kubernetes workload cluster via Tinkerbell/CAPT. PXE-boots nodes, streams OS image, runs kubeadm, installs Cilium. |
| `nodes` | SSH-based node bootstrap: managed user, hostname, containerd trust |
| `ceph` | Deploy Rook-Ceph operator and cluster, add OSD disks |
| `csi` | Install Ceph RBD CSI driver and StorageClasses |
| `infrastructure` | MetalLB, Ingress-NGINX, ArgoCD, cert-manager, Keycloak, Istio |
| `monitoring` | Prometheus, Grafana, Loki, OpenSearch, Thanos |
| `openstack` | Full OpenStack control plane + cloud smoke test |

### Cloud smoke test (cloud-setup)

After OpenStack deploys, the `cloud-setup` component runs automatically as the final step. It verifies the cloud is functional by creating:

- **Glance image** — Ubuntu 22.04 (downloaded from Ubuntu cloud-images and uploaded to Glance)
- **Private network + subnet** — `private-net` / `10.0.2.0/24`
- **Public (external) network + subnet** — `public-net` flat provider network with a floating-IP pool
- **Router** — connects private subnet to the public network
- **Security group** — `vm-secgroup` with ICMP and TCP 22 rules
- **Test VM** — (optional) if `vm_key_name` is configured in `cluster.yaml`

All steps are idempotent: if a resource already exists it is silently skipped.

To re-run the smoke test standalone:

```bash
daalu deploy cluster-defs/cluster.yaml --install openstack --infra cloud-setup
```

---

#### Step 3 — Tear everything down

```bash
daalu clean cluster-defs/cluster.yaml \
  --mgmt-kubeconfig ~/.kube/daalu-mgmt-config \
  --yes
```

---

## CLI Reference

### `daalu mgmt`

Bootstrap a management Kubernetes cluster on a fresh Ubuntu node, then install the full Tinkerbell provisioning stack and Harbor registry.

```
daalu mgmt cluster-defs/cluster.yaml [OPTIONS]
```

| Option | Description |
|---|---|
| `--ssh-host` | Override mgmt node IP |
| `--ssh-username` | Override SSH username |
| `--ssh-password` | SSH password |
| `--ssh-key` | Path to SSH private key |
| `--provisioning-interface` | NIC dedicated to the bare-metal provisioning network |
| `--kubeconfig-out` | Local path to save the generated kubeconfig |
| `--skip-harbor` | Skip Harbor deployment |

### `daalu deploy`

Deploy the workload cluster and OpenStack components.

```
daalu deploy cluster-defs/cluster.yaml [OPTIONS]
```

| Option | Description |
|---|---|
| `--install` | Comma-separated targets or `all` — e.g. `cluster-api,nodes,ceph,csi,infrastructure,openstack` |
| `--infra` | Filter infrastructure sub-components |
| `--managed-user` | Linux user to create on workload nodes (default: from config) |
| `--managed-user-password` | Password for managed user |
| `--ssh-key` | Path to SSH private key for workload nodes |
| `--local-registry` | Pull images from local Harbor registry |
| `--mgmt-kubeconfig` | Path to management cluster kubeconfig |
| `--dry-run` | Preview without applying |
| `--debug` | Verbose logging |
| `--phase` | Run specific phase: `pre_install`, `helm`, or `post_install` |

### `daalu clean`

Tear down everything: workload cluster, mgmt cluster k8s, Harbor disk, local state.

```
daalu clean cluster-defs/cluster.yaml [OPTIONS]
```

| Option | Description |
|---|---|
| `--mgmt-kubeconfig` | Path to mgmt kubeconfig (to delete workload cluster first) |
| `--ssh-key` | SSH private key for mgmt node |
| `--ssh-password` | SSH password for mgmt node |
| `--skip-workload-cluster` | Skip CAPI cluster deletion (if already gone) |
| `--no-wait` | Don't wait for bare-metal deprovisioning |
| `--yes` / `-y` | Skip confirmation prompt |

**What `daalu clean` does:**

1. Deletes the CAPI workload cluster — CAPT powers off workload nodes via Rufio
2. Waits for all Hardware CRs to reach deprovisioned state
3. SSH to mgmt node: `kubeadm reset`, flush CNI/iptables, remove k8s data dirs
4. Unmounts Harbor disk, removes fstab entry, runs `wipefs` to clear filesystem signatures
5. Removes Tinkerbell/Rufio state
6. Removes local kubeconfigs and `known_hosts` entries

```bash
# Full teardown
daalu clean cluster-defs/cluster.yaml \
  --mgmt-kubeconfig ~/.kube/daalu-mgmt-config \
  --yes

# Fast teardown — skip waiting
daalu clean cluster-defs/cluster.yaml --no-wait --yes

# Workload cluster already gone
daalu clean cluster-defs/cluster.yaml --skip-workload-cluster --yes
```

### `daalu mirror-images`

Mirror container images from public registries into Harbor.

```
daalu mirror-images --harbor-url 10.10.0.9:30003 [OPTIONS]
```

### `daalu configure-registry-trust`

Configure workload cluster nodes to trust Harbor's self-signed certificate.

```
daalu configure-registry-trust <cluster-kubeconfig> [OPTIONS]
```

---

## Secrets Management

Daalu never stores credentials in version control. Provide them via `secrets.yaml` or environment variables.

### secrets.yaml (recommended)

```bash
cp cloud-config/secrets.yaml.example cloud-config/secrets.yaml
# Fill in passwords, SSH keys, BMC credentials
```

The loader finds `cloud-config/secrets.yaml` automatically and deep-merges it into the cluster definition. The file is git-ignored.

### Environment variables

Use `${VAR_NAME}` placeholders in `cluster.yaml` or `secrets.yaml` — the loader expands them before parsing:

```yaml
mgmt_cluster:
  ssh_password: "${DAALU_MGMT_SSH_PASSWORD}"
```

```bash
export DAALU_MGMT_SSH_PASSWORD="my-password"
daalu mgmt cluster-defs/cluster.yaml
```

---

## Architecture

Daalu follows a component-based architecture:

1. **Config loader** (`src/daalu/config/loader.py`) — Reads cluster YAML + secrets.yaml, expands env vars, deep-merges, validates with Pydantic
2. **CLI layer** (`src/daalu/cli/app.py`) — Typer CLI orchestrating the deployment pipeline
3. **Mgmt bootstrap** (`src/daalu/bootstrap/mgmt/`) — Installs k8s on mgmt node; installs Tinkerbell stack, CAPT, Harbor; `MgmtClusterCleaner` for teardown
4. **CAPT provisioner** (`src/daalu/bootstrap/mgmt/capt_provisioner.py`) — Drives the bare-metal workload cluster lifecycle: Rufio PXE jobs, Workflow waits, Cilium install, node readiness
5. **Bootstrap engine** (`src/daalu/bootstrap/engine/`) — Base `InfraComponent` class with `pre_install()`, `helm_values()`, `post_install()` hooks
6. **Managers** — `CephManager`, `InfrastructureManager`, `MonitoringManager`, `OpenStackManager` coordinate component groups
7. **Helm runner** (`src/daalu/helm/`) — Wraps Helm CLI for install/upgrade
8. **Event bus** (`src/daalu/observers/`) — Lifecycle events dispatched to console, log file, and JSON observers

---

## The NoCloud (Not Only Cloud) Philosophy

Daalu is built on a simple belief:

Modern cloud infrastructure should be a capability — not a dependency.

The internet was designed as a decentralized, peer-to-peer network.
Yet today, a failure in a single hyperscaler region can take down large portions of the internet. That concentration of infrastructure contradicts the resilience principles the internet was built upon.

The NoCloud (Not Only Cloud) philosophy is about restoring balance.

### Core Principles

- **Decentralization matters** — When a single cloud provider outage impacts half the internet, we have reintroduced central points of failure into a system designed to avoid them.
- **Data sovereignty is strategic** — Organizations should maintain full control over where their data lives, how it is governed, and who has access to it.
- **Avoid vendor lock-in** — Deep coupling to proprietary cloud services reduces portability, negotiation leverage, and long-term architectural flexibility.
- **True resilience requires ownership** — The only dual-cloud strategy that truly diversifies risk is combining a public cloud with infrastructure you own and control.
- **Cloud capability, anywhere** — Production-grade cloud infrastructure should be deployable on bare metal, in colocation, or alongside public cloud — without sacrificing automation or operational maturity.

Daalu exists to make this practical.

---

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Make your changes
4. Run tests: `python -m pytest tests/`
5. Submit a pull request

## License

See [LICENSE](LICENSE) for details.
