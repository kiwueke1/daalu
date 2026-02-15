# Daalu

**Daalu powers independent cloud infrastructure — automated, production-grade, and fully under your control.**

Daalu is a Python-based orchestration platform for deploying and managing private or public cloud infrastructure anywhere. It automates the full lifecycle of bare-metal Kubernetes clusters, OpenStack services, Ceph storage, monitoring stacks, and HPC workloads — from initial provisioning to day-2 operations — enabling organizations to build serious cloud infrastructure without dependency on hyperscalers.

## What It Does

- **Bare-metal provisioning** — Onboards bare metal servers into Kubernetes with Metal3 ClusterAPI provider; Proxmox/libvirt VMs for development.
- **OpenStack deployment** — Deploys a full OpenStack control plane (Keystone, Nova, Neutron, Glance, Heat, Cinder, Manila, Octavia, Horizon, Barbican, and more) via Helm charts
- **Ceph storage** — Bootstraps Ceph clusters and configures RBD CSI drivers
- **Identity management** — Integrates Keycloak for SSO/OIDC across Grafana and OpenStack
- **Monitoring** — Deploys Prometheus, Grafana, Loki, OpenSearch, and Thanos for metrics and log aggregation
- **Infrastructure services** — MetalLB, Ingress-NGINX, ArgoCD, Istio, cert-manager, and more
- **HPC orchestration** — GPU cluster management with Volcano, Ray, and Slurm schedulers

---

---

## Final End Product

- **Kubernetes control plane** — A production Kubernetes cluster running directly on bare-metal servers using Cluster API and Metal3.

- **OpenStack cloud layer** — A fully operational OpenStack control plane providing compute (Nova), networking (Neutron), image services (Glance), block and object storage (Cinder, Manila), and orchestration capabilities.

- **Distributed storage backend** — A Ceph-backed storage system with RBD CSI integration for persistent volumes and cloud storage services.

- **Integrated operations stack** — Centralized identity (OIDC/SSO), monitoring, logging, and GitOps-based lifecycle management.

- **HPC and GPU compute capability** — Optional GPU-enabled nodes and distributed schedulers (Volcano, Ray, Slurm) for large-scale compute and AI workloads.

- **End-to-end AI/ML workload support** — An environment capable of supporting the full lifecycle of machine learning systems: data ingestion, distributed training, experiment tracking, model serving, and production deployment.

In practical terms, the system transforms physical servers into a self-hosted cloud and HPC platform capable of running both general infrastructure workloads and advanced AI/ML environments (similar in functional scope to Kubeflow, but built directly on bare-metal infrastructure).



## Project Structure

```
daalu/
├── src/daalu/                  # Main Python package
│   ├── cli/                    # Typer CLI entry points
│   ├── config/                 # YAML config loading and Pydantic models
│   ├── bootstrap/              # Core provisioning logic
│   │   ├── metal3/             # Metal3 Cluster API provider
│   │   ├── node/               # SSH-based node bootstrap
│   │   ├── ceph/               # Ceph deployment
│   │   ├── csi/                # Container Storage Interface
│   │   ├── openstack/          # OpenStack service components
│   │   ├── infrastructure/     # Infra components (MetalLB, ArgoCD, etc.)
│   │   ├── monitoring/         # Monitoring stack (Prometheus, Grafana, etc.)
│   │   ├── iam/                # Identity & Access Management
│   │   └── shared/             # Shared utilities (Keycloak, etc.)
│   ├── helm/                   # Helm chart runner
│   ├── deploy/                 # Deployment step orchestration
│   ├── observers/              # Event bus and lifecycle logging
│   ├── temporal/               # Temporal workflow integration
│   └── utils/                  # SSH runner, retry helpers
├── cluster-defs/               # Cluster definition YAML files
│   ├── cluster.yaml            # Main cluster configuration
│   ├── hpc/                    # HPC cluster definitions
│   └── cluster-api/            # Cluster API manifest templates
├── cloud-config/               # Cloud configuration
│   ├── secrets.yaml            # Your secrets (git-ignored)
│   └── secrets.yaml.example    # Template showing required keys
├── .env.example                # Environment variable template
├── helm-charts/                # Helm charts for all services
├── helm-values/                # Helm value overrides
├── assets/                     # Additional deployment assets
├── templates/                  # Jinja2 templates for cluster-api setup
├── artifacts/                  # Generated manifests (git-ignored)
└── tests/                      # Test suites
```

## Prerequisites

- Python 3.10+
- `kubectl` configured with access to your management cluster
- `clusterctl` (for Cluster API operations)
- `helm` 3.x
- SSH access to target nodes
- A Proxmox cluster or Metal3-compatible bare-metal environment

## Installation

```bash
# Clone the repository
git clone https://github.com/daalu-io/daalu.git
cd daalu

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install the package
pip install -e .
```

---

## Secrets Management

Daalu never stores credentials in version control. You provide them at runtime using one of two methods (or both together).

### How It Works

When you run `python -m daalu.cli.app deploy cluster-defs/cluster.yaml`, the config loader:

1. Reads `cluster-defs/cluster.yaml`
2. Expands any `${ENV_VAR}` placeholders via `os.path.expandvars()`
3. Looks for a `secrets.yaml` file and deep-merges it into the config
4. Validates the merged result against the Pydantic config model

```
cluster.yaml          secrets.yaml (git-ignored)
 (structure +          (credentials, mirrors
  empty secrets)        the same YAML structure)
       \                    /
        \                  /
     config loader deep-merges
              |
              v
     DaaluConfig (runtime)
```

### Method 1 — secrets.yaml File (Recommended)

Best for local development and single-operator setups.

**Step 1: Create your secrets file**

```bash
cp cloud-config/secrets.yaml.example cloud-config/secrets.yaml
```

**Step 2: Fill in your real values**

The file mirrors the structure of `cluster.yaml`. Any non-empty value here overrides the matching field:

```yaml
# cloud-config/secrets.yaml
cluster_api:
  builder_password: "MySecurePass123"
  image_password: "MySecurePass123"
  image_password_hash: "$6$rounds=4096$salt$hash..."
  ssh_public_key: "ssh-ed25519 AAAAC3Nz... user@host"
  ssh_public_key_path: "/home/youruser/.ssh/daalu-key.pub"
  proxmox_url: "https://192.168.1.100:8006"
  proxmox_token: "root@pam!mytoken"
  proxmox_secret: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  mgmt_host: "192.168.1.50"
  mgmt_user: "admin"

keycloak:
  monitoring:
    password: "KeycloakAdminPass!"
  openstack:
    password: "KeycloakAdminPass!"
    github_token: "ghp_xxxxxxxxxxxxxxxxxxxx"

monitoring:
  thanos:
    access_key: "minio-admin"
    secret_key: "minio-secret-key"
```

**Step 3: Run daalu**

```bash
python -m daalu.cli.app deploy cluster-defs/cluster.yaml \
  --managed-user builder \
  --managed-user-password "MySecurePass123" \
  --ssh-key ~/.ssh/daalu-key
```

The loader automatically finds `cloud-config/secrets.yaml` (relative to `WORKSPACE_ROOT`). No extra flags needed.

**Custom secrets file location:**

```bash
export DAALU_SECRETS_FILE=/secure/path/my-secrets.yaml

python -m daalu.cli.app deploy cluster-defs/cluster.yaml \
  --managed-user builder \
  --managed-user-password "MySecurePass123"
```

### Method 2 — Environment Variables

Best for CI/CD pipelines, containers, and automated deployments.

**Step 1: Create your .env file**

```bash
cp .env.example .env
```

**Step 2: Fill in your values**

```bash
# .env
export DAALU_BUILDER_PASSWORD="MySecurePass123"
export DAALU_IMAGE_PASSWORD="MySecurePass123"
export DAALU_SSH_PUBLIC_KEY="ssh-ed25519 AAAAC3Nz... user@host"
export DAALU_PROXMOX_URL="https://192.168.1.100:8006"
export DAALU_PROXMOX_TOKEN="root@pam!mytoken"
export DAALU_PROXMOX_SECRET="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
export DAALU_MGMT_HOST="192.168.1.50"
export DAALU_MGMT_USER="admin"
export DAALU_KEYCLOAK_ADMIN_PASSWORD="KeycloakAdminPass!"
export DAALU_KEYCLOAK_DB_PASSWORD="KeycloakDBPass!"
export DAALU_GITHUB_TOKEN="ghp_xxxxxxxxxxxxxxxxxxxx"
export DAALU_THANOS_S3_ACCESS_KEY="minio-admin"
export DAALU_THANOS_S3_SECRET_KEY="minio-secret-key"
export DAALU_MYSQL_ROOT_PASSWORD="MySQLRootPass!"
export DAALU_OPENSEARCH_ADMIN_PASSWORD="OpenSearchPass!"
```

**Step 3: Reference them in cluster.yaml (or secrets.yaml)**

Use `${VAR_NAME}` — the loader expands these before parsing:

```yaml
# cluster-defs/cluster.yaml (or cloud-config/secrets.yaml)
cluster_api:
  builder_password: "${DAALU_BUILDER_PASSWORD}"
  image_password: "${DAALU_IMAGE_PASSWORD}"
  ssh_public_key: "${DAALU_SSH_PUBLIC_KEY}"
  proxmox_url: "${DAALU_PROXMOX_URL}"
  proxmox_token: "${DAALU_PROXMOX_TOKEN}"
  proxmox_secret: "${DAALU_PROXMOX_SECRET}"
  mgmt_host: "${DAALU_MGMT_HOST}"
  mgmt_user: "${DAALU_MGMT_USER}"

keycloak:
  monitoring:
    password: "${DAALU_KEYCLOAK_ADMIN_PASSWORD}"
  openstack:
    password: "${DAALU_KEYCLOAK_ADMIN_PASSWORD}"
    github_token: "${DAALU_GITHUB_TOKEN}"

monitoring:
  thanos:
    access_key: "${DAALU_THANOS_S3_ACCESS_KEY}"
    secret_key: "${DAALU_THANOS_S3_SECRET_KEY}"
```

**Step 4: Source and run**

```bash
source .env

python -m daalu.cli.app deploy cluster-defs/cluster.yaml \
  --managed-user builder \
  --managed-user-password "${DAALU_BUILDER_PASSWORD}" \
  --ssh-key ~/.ssh/daalu-key
```

**In CI/CD (e.g. GitHub Actions):**

```yaml
- name: Deploy infrastructure
  env:
    DAALU_BUILDER_PASSWORD: ${{ secrets.DAALU_BUILDER_PASSWORD }}
    DAALU_PROXMOX_SECRET: ${{ secrets.DAALU_PROXMOX_SECRET }}
    # ... all other DAALU_* secrets
  run: |
    python -m daalu.cli.app deploy cluster-defs/cluster.yaml \
      --managed-user builder \
      --managed-user-password "${DAALU_BUILDER_PASSWORD}"
```

### Combining Both Methods

You can use both together. For example, keep infrastructure config in `secrets.yaml` and use env vars for the CLI flags and component passwords:

```bash
# secrets.yaml handles cluster_api, keycloak, monitoring fields
# env vars handle component-level passwords
source .env

python -m daalu.cli.app deploy cluster-defs/cluster.yaml \
  --managed-user builder \
  --managed-user-password "${DAALU_BUILDER_PASSWORD}" \
  --ssh-key ~/.ssh/daalu-key
```

### Which Secrets Go Where

| Secret | secrets.yaml field | Env var |
|---|---|---|
| Node builder password | `cluster_api.builder_password` | `DAALU_BUILDER_PASSWORD` |
| Image password | `cluster_api.image_password` | `DAALU_IMAGE_PASSWORD` |
| SSH public key | `cluster_api.ssh_public_key` | `DAALU_SSH_PUBLIC_KEY` |
| Proxmox URL | `cluster_api.proxmox_url` | `DAALU_PROXMOX_URL` |
| Proxmox API token | `cluster_api.proxmox_token` | `DAALU_PROXMOX_TOKEN` |
| Proxmox API secret | `cluster_api.proxmox_secret` | `DAALU_PROXMOX_SECRET` |
| Management host | `cluster_api.mgmt_host` | `DAALU_MGMT_HOST` |
| Keycloak admin password | `keycloak.monitoring.password` | `DAALU_KEYCLOAK_ADMIN_PASSWORD` |
| GitHub token | `keycloak.openstack.github_token` | `DAALU_GITHUB_TOKEN` |
| Thanos S3 access key | `monitoring.thanos.access_key` | `DAALU_THANOS_S3_ACCESS_KEY` |
| Thanos S3 secret key | `monitoring.thanos.secret_key` | `DAALU_THANOS_S3_SECRET_KEY` |
| Keycloak DB password | *(component)* | `DAALU_KEYCLOAK_DB_PASSWORD` |
| MySQL root password | *(component)* | `DAALU_MYSQL_ROOT_PASSWORD` |
| OpenSearch admin password | *(component)* | `DAALU_OPENSEARCH_ADMIN_PASSWORD` |

### Generating Secrets

```bash
# Random 32-char password
openssl rand -base64 24

# Password hash for image_password_hash
openssl passwd -6 'your-password'

# SSH key pair
ssh-keygen -t ed25519 -f ~/.ssh/daalu-key -N ""

# View your public key (to put in secrets.yaml)
cat ~/.ssh/daalu-key.pub
```

---

## Configuration

Edit `cluster-defs/cluster.yaml` to match your environment. All non-secret fields (networking, sizing, versions) go here directly. Secret fields are left empty and filled from `secrets.yaml` or env vars at runtime.

```yaml
environment: dev
context: kubernetes-admin@kubernetes

cluster_api:
  cluster_name: my-cluster
  namespace: baremetal-operator-system
  pod_cidr: 10.201.0.0/16
  control_plane_vip: 10.10.0.249
  # ... networking, sizing, etc.

  # These are empty — populated at runtime from secrets.yaml
  builder_password: ""
  proxmox_url: ""
  proxmox_token: ""
  proxmox_secret: ""
```

---

## Usage

### Full Deployment

Deploy all components (Cluster API, nodes, Ceph, CSI, infrastructure, monitoring, OpenStack):

```bash
python -m daalu.cli.app deploy cluster-defs/cluster.yaml \
  --managed-user builder \
  --managed-user-password "your-password" \
  --ssh-key ~/.ssh/daalu-key
```

### Selective Deployment

Install only specific components:

```bash
# Only Cluster API and node bootstrap
python -m daalu.cli.app deploy cluster-defs/cluster.yaml \
  --install cluster-api,nodes \
  --managed-user builder \
  --managed-user-password "your-password" \
  --ssh-key ~/.ssh/daalu-key

# Only infrastructure components
python -m daalu.cli.app deploy cluster-defs/cluster.yaml \
  --install infrastructure \
  --infra metallb,argocd \
  --managed-user builder \
  --managed-user-password "your-password" \
  --ssh-key ~/.ssh/daalu-key

# Only OpenStack
python -m daalu.cli.app deploy cluster-defs/cluster.yaml \
  --install openstack \
  --managed-user builder \
  --managed-user-password "your-password" \
  --ssh-key ~/.ssh/daalu-key

# Only monitoring
python -m daalu.cli.app deploy cluster-defs/cluster.yaml \
  --install monitoring \
  --managed-user builder \
  --managed-user-password "your-password" \
  --ssh-key ~/.ssh/daalu-key

# Run a specific deploy phase
python -m daalu.cli.app deploy cluster-defs/cluster.yaml \
  --install openstack \
  --phase pre_install \
  --managed-user builder \
  --managed-user-password "your-password" \
  --ssh-key ~/.ssh/daalu-key
```

### HPC Cluster Deployment

```bash
python -m daalu.cli.app hpc deploy cluster-defs/hpc/hpc-cluster.yaml
```

### Dry Run

Preview what would happen without making changes:

```bash
python -m daalu.cli.app deploy cluster-defs/cluster.yaml \
  --dry-run \
  --managed-user builder \
  --managed-user-password "your-password"
```

### Available Install Targets

| Target           | Description                                      |
|------------------|--------------------------------------------------|
| `cluster-api`    | Provision Kubernetes cluster via Cluster API      |
| `nodes`          | Bootstrap nodes (SSH, hostname, apparmor, etc.)   |
| `ceph`           | Deploy Ceph storage cluster                       |
| `csi`            | Install CSI drivers (RBD)                         |
| `infrastructure` | MetalLB, Ingress, ArgoCD, Keycloak, etc.          |
| `monitoring`     | Prometheus, Grafana, Loki, OpenSearch, Thanos      |
| `openstack`      | Full OpenStack control plane                      |

### CLI Reference

```
python -m daalu.cli.app deploy --help
```

| Flag | Description |
|---|---|
| `--install` | Comma-separated list of targets, or `all` |
| `--infra` | Filter infrastructure/monitoring/openstack sub-components |
| `--context` | Kubernetes context for the workload cluster |
| `--mgmt-context` | Kubernetes context for the management cluster |
| `--managed-user` | **(required)** SSH username to create on provisioned nodes |
| `--managed-user-password` | **(required)** Password for the managed user |
| `--ssh-key` | Path to SSH private key |
| `--dry-run` | Preview changes without applying |
| `--debug` | Enable verbose logging |
| `--phase` | Run a specific deploy phase (`pre_install`, `helm`, `post_install`) |
| `--temporal` | Run deployment as a Temporal workflow |

---

## Architecture

Daalu follows a component-based architecture:

1. **Config loader** (`src/daalu/config/loader.py`) — Reads cluster YAML + secrets.yaml, expands `${ENV_VAR}` placeholders, deep-merges, and validates against Pydantic models
2. **CLI layer** (`src/daalu/cli/`) — Typer-based CLI that orchestrates the deployment pipeline
3. **Bootstrap engine** (`src/daalu/bootstrap/engine/`) — Base `InfraComponent` class that each service extends with `pre_install()`, `helm_values()`, and `post_install()` hooks
4. **Managers** — `CephManager`, `InfrastructureManager`, `MonitoringManager`, `OpenStackManager` coordinate groups of components
5. **Helm runner** (`src/daalu/helm/`) — Wraps Helm CLI for install/upgrade operations over SSH
6. **Event bus** (`src/daalu/observers/`) — Lifecycle events dispatched to console, logger, and JSON file observers

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Make your changes
4. Run tests: `python -m pytest tests/`
5. Submit a pull request

## License

See [LICENSE](LICENSE) for details.
