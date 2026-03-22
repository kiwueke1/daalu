## Copyright

Copyright © 2026 Kezie Iwueke.

# Daalu

**Daalu powers independent cloud infrastructure — automated, production-grade, and fully under your control.**

Daalu is a Python-based orchestration platform for deploying and managing private or public cloud infrastructure anywhere. It models infrastructure components as native Python objects and uses this abstraction layer to automate the full lifecycle provisioning of bare-metal Kubernetes clusters, OpenStack services, Ceph storage, monitoring stacks, and HPC workloads — from initial provisioning to day-2 operations — enabling organizations to build self-hosted production-grade cloud infrastructure without dependency on hyperscalers.

To understand the motivation behind this project, see [The NoCloud (Not Only Cloud) Philosophy](#the-nocloud-not-only-cloud-philosophy).

## What It Does

- **Bare-metal provisioning** — Onboards bare metal servers into Kubernetes with Metal3 ClusterAPI provider
- **OpenStack deployment** — Deploys a full OpenStack control plane (Keystone, Nova, Neutron, Glance, Heat, Cinder, Horizon, and more) via Helm charts
- **Ceph storage** — Bootstraps Ceph clusters and configures RBD CSI drivers
- **Identity management** — Integrates Keycloak for SSO/OIDC across Grafana and OpenStack
- **Monitoring** — Deploys Prometheus, Grafana, Loki, OpenSearch, and Thanos for metrics and log aggregation
- **Infrastructure services** — MetalLB, Ingress-NGINX, ArgoCD, Istio, cert-manager, and more
- **HPC orchestration** — GPU cluster management with Volcano, Ray, and Slurm schedulers

---

## Final End Product

- **Kubernetes control plane** — A production Kubernetes cluster running directly on bare-metal servers using Cluster API and Metal3.
- **OpenStack cloud layer** — A fully operational OpenStack control plane providing compute (Nova), networking (Neutron), image services (Glance), block and object storage (Cinder), and orchestration capabilities.
- **Distributed storage backend** — A Ceph-backed storage system with RBD CSI integration for persistent volumes and cloud storage services.
- **Integrated operations stack** — Centralized identity (OIDC/SSO), monitoring, logging, and GitOps-based lifecycle management.

---

## Project Structure

```
daalu/
├── src/daalu/                  # Main Python package
│   ├── cli/                    # Typer CLI entry points
│   ├── config/                 # YAML config loading and Pydantic models
│   ├── bootstrap/              # Core provisioning logic
│   │   ├── mgmt/               # Management cluster bootstrap + teardown
│   │   ├── metal3/             # Metal3 Cluster API provider
│   │   ├── node/               # SSH-based node bootstrap
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
├── assets/                     # Helm values and chart directories
├── artifacts/                  # Generated manifests (git-ignored)
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
- SSH client (preinstalled on Linux/macOS)

```bash
kubectl version --client
clusterctl version
helm version
python --version
```

### Hardware requirements

- **Management node** — A bare-metal machine or VM running Ubuntu 22.04/24.04. This hosts the management Kubernetes cluster, Metal3/Ironic, and Harbor registry.
- **Workload nodes** — One or more bare-metal servers with IPMI/Redfish BMC access for PXE provisioning via Metal3.
- **Storage node** *(optional)* — A dedicated server for Ceph OSDs.

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
```

---

## Configuration

### 1. Cluster definition

Edit `cluster-defs/cluster.yaml` to match your environment. Non-secret fields (networking, versions, sizing) go here directly. Secret fields are left empty and populated from `secrets.yaml` at runtime.

Key sections:

```yaml
mgmt_cluster:
  host: "192.168.0.163"         # IP of the Ubuntu machine for the mgmt cluster
  ssh_username: "kez"
  provisioning_ip: "10.10.0.9"  # Static IP on the provisioning NIC
  provisioning_interface: "ens19"
  install_harbor: true

registry:
  harbor_hostname: "10.10.0.9"  # Provisioning IP — reachable by workload nodes
  harbor_storage_size: "100Gi"

cluster_api:
  cluster_name: auto-openstack-infra
  control_plane_vip: "10.10.0.249"
  # ...

ceph:
  additional_ceph_hosts:
    - hostname: storage-01
      osd_devices: [/dev/sdb, /dev/sdc, /dev/sdd, /dev/sde]
```

### 2. Secrets file

```bash
cp cloud-config/secrets.yaml.example cloud-config/secrets.yaml
# Edit secrets.yaml and fill in passwords, SSH keys, BMC credentials, etc.
```

The config loader automatically finds `cloud-config/secrets.yaml` and deep-merges it into the cluster definition at runtime. The file is git-ignored.

---

## Usage

### Full workflow

#### Step 1 — Bootstrap the management cluster

Installs Kubernetes on a fresh Ubuntu node, then deploys the full Metal3 stack (cert-manager → CAPI → IrSO → Ironic → BMO → CAPM3) and Harbor registry (formats dedicated disk, mirrors OpenStack images):

```bash
daalu mgmt cluster-defs/cluster.yaml
```

Optional overrides:

```bash
daalu mgmt cluster-defs/cluster.yaml \
  --ssh-host 192.168.0.163 \
  --ssh-password admin123 \
  --provisioning-interface ens19 \
  --kubeconfig-out ~/.kube/daalu-mgmt-config
```

When complete, you will see:

```
Management cluster is ready!

  Kubeconfig  : /home/user/.kube/daalu-mgmt-config
  Harbor UI   : https://10.10.0.9:30003
  Harbor creds: admin / <registry.admin_password from secrets.yaml>
```

#### Step 2 — Deploy OpenStack on bare-metal workload cluster

Provisions bare-metal nodes via Metal3/Ironic, bootstraps Kubernetes on them, deploys Ceph, CSI, infrastructure components, and the full OpenStack control plane:

```bash
daalu deploy cluster-defs/cluster.yaml \
  --managed-user builder \
  --managed-user-password <password> \
  --ssh-key ~/.ssh/openstack-key \
  --local-registry \
  --mgmt-kubeconfig ~/.kube/daalu-mgmt-config
```

#### Step 3 — Tear everything down

```bash
daalu clean cluster-defs/cluster.yaml \
  --mgmt-kubeconfig ~/.kube/daalu-mgmt-config
```

---

### Selective deployment

Install only specific components using `--install`:

```bash
# Re-run only Ceph (e.g. to add OSDs)
daalu deploy cluster-defs/cluster.yaml \
  --install ceph \
  --managed-user builder \
  --managed-user-password <password> \
  --ssh-key ~/.ssh/openstack-key \
  --local-registry \
  --mgmt-kubeconfig ~/.kube/daalu-mgmt-config

# Re-run infrastructure + OpenStack only
daalu deploy cluster-defs/cluster.yaml \
  --install infrastructure,openstack \
  --managed-user builder \
  --managed-user-password <password> \
  --ssh-key ~/.ssh/openstack-key \
  --local-registry \
  --mgmt-kubeconfig ~/.kube/daalu-mgmt-config
```

### Available install targets

| Target           | Description                                       |
|------------------|---------------------------------------------------|
| `cluster-api`    | Provision Kubernetes workload cluster via Metal3  |
| `nodes`          | Bootstrap nodes (SSH, hostname, apparmor, netplan)|
| `ceph`           | Deploy Ceph storage cluster and add OSDs          |
| `csi`            | Install RBD CSI drivers                           |
| `infrastructure` | MetalLB, Ingress, CoreDNS patches, Keycloak, etc. |
| `monitoring`     | Prometheus, Grafana, Loki, OpenSearch, Thanos     |
| `openstack`      | Full OpenStack control plane                      |

### Cloud smoke test (cloud-setup)

After the OpenStack control plane is deployed, the `cloud-setup` component runs automatically as the final step. It verifies the cloud is functional by creating:

- **Glance image** — Ubuntu 22.04 (downloaded from Ubuntu cloud-images and uploaded to Glance)
- **Private network + subnet** — `private-net` / `10.0.2.0/24`
- **Public (external) network + subnet** — `public-net` flat provider network with a floating-IP pool
- **Router** — connects private subnet to the public network (provides outbound internet access to VMs)
- **Security group** — `vm-secgroup` with ICMP (ping) and TCP 22 (SSH) rules
- **Test VM** — (optional) if `vm_key_name` is configured in `cluster.yaml`

All steps are idempotent: if a resource already exists it is silently skipped.

#### Running the smoke test standalone

To re-run only the cloud smoke test (e.g. after a partial failure):

```bash
daalu deploy cluster-defs/cluster.yaml \
  --install openstack \
  --infra cloud-setup \
  --managed-user builder \
  --managed-user-password <password> \
  --ssh-key ~/.ssh/openstack-key \
  --local-registry \
  --mgmt-kubeconfig ~/.kube/daalu-mgmt-config
```

#### Skipping the smoke test

To deploy OpenStack without running the smoke test, use `--infra` to list only the components you want:

```bash
# Deploy all OpenStack components except cloud-setup
daalu deploy cluster-defs/cluster.yaml \
  --install openstack \
  --infra keystone,glance,neutron,nova \
  --managed-user builder \
  --managed-user-password <password> \
  --ssh-key ~/.ssh/openstack-key
```

#### Configuring cloud-setup defaults

The smoke test uses sensible defaults but the key parameters can be overridden in `cluster.yaml` (support coming soon). Current defaults:

| Parameter | Default |
|---|---|
| Image | Ubuntu 22.04 (jammy cloud image) |
| Private network | `private-net` / `10.0.2.0/24` gateway `10.0.2.1` |
| Public network | `public-net` flat provider on `provider` physical net |
| Public subnet | `192.168.0.0/24`, allocation pool `192.168.0.200–250` |
| Router | `router1` |
| Security group | `vm-secgroup` (ICMP + TCP 22) |
| VM | `test-vm1`, flavor `m1.large` (only if `vm_key_name` set) |

---

## CLI Reference

### `daalu mgmt`

Bootstrap a management Kubernetes cluster on a fresh Ubuntu node.

```
daalu mgmt cluster-defs/cluster.yaml [OPTIONS]
```

| Option | Description |
|---|---|
| `--ssh-host` | Override mgmt node IP |
| `--ssh-username` | Override SSH username |
| `--ssh-password` | SSH password |
| `--ssh-key` | Path to SSH private key |
| `--provisioning-interface` | NIC for bare-metal provisioning network |
| `--kubeconfig-out` | Local path to save generated kubeconfig |
| `--skip-harbor` | Skip Harbor deployment |

### `daalu deploy`

Deploy OpenStack and related components onto the workload cluster.

```
daalu deploy cluster-defs/cluster.yaml [OPTIONS]
```

| Option | Description |
|---|---|
| `--install` | Comma-separated targets, or `all` (default) |
| `--infra` | Filter infrastructure sub-components |
| `--managed-user` | **(required)** SSH username on provisioned nodes |
| `--managed-user-password` | **(required)** Password for managed user |
| `--ssh-key` | Path to SSH private key |
| `--local-registry` | Pull images from local Harbor registry |
| `--mgmt-kubeconfig` | Path to management cluster kubeconfig |
| `--dry-run` | Preview without applying |
| `--debug` | Verbose logging |
| `--phase` | Run specific phase: `pre_install`, `helm`, `post_install` |

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

1. Deletes the workload CAPI cluster — triggers Metal3/Ironic to wipe and power off bare-metal nodes
2. Waits up to 5 minutes for all BareMetalHosts to reach `available` state
3. SSH to mgmt node: `kubeadm reset`, flush CNI/iptables, remove k8s data dirs
4. Unmounts Harbor disk, removes fstab entry, runs `wipefs` to clear filesystem signatures
5. Removes Metal3/Ironic state and Docker containers
6. Removes local kubeconfigs and `known_hosts` entries

```bash
# Full teardown (recommended — waits for clean deprovisioning)
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
3. **Mgmt bootstrap** (`src/daalu/bootstrap/mgmt/`) — Installs k8s + Metal3 stack on mgmt node; `MgmtClusterCleaner` for teardown
4. **Bootstrap engine** (`src/daalu/bootstrap/engine/`) — Base `InfraComponent` class with `pre_install()`, `helm_values()`, `post_install()` hooks
5. **Managers** — `CephManager`, `InfrastructureManager`, `MonitoringManager`, `OpenStackManager` coordinate component groups
6. **Helm runner** (`src/daalu/helm/`) — Wraps Helm CLI for SSH-based install/upgrade
7. **Event bus** (`src/daalu/observers/`) — Lifecycle events dispatched to console, log file, and JSON observers

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
