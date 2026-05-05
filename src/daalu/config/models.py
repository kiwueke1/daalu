# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/config/models.py

from typing import Dict, List, Optional, Literal
from pydantic import BaseModel, HttpUrl, Field
from pathlib import Path
from daalu.bootstrap.shared.keycloak.models import KeycloakIAMConfig
from daalu.bootstrap.monitoring.models import KeycloakMonitoringConfig
from daalu.config.monitoring import MonitoringConfig
from daalu.bootstrap.openstack.models import KeycloakOpenstackConfig
from daalu.bootstrap.mgmt.models import MgmtClusterConfig



class RepoSpec(BaseModel):
    name: str
    url: HttpUrl
    username: Optional[str] = None
    password: Optional[str] = None
    oci: bool = False

class ValuesRef(BaseModel):
    # Either inline dict or path(s) to YAML values
    inline: Optional[Dict] = None
    files: List[str] = Field(default_factory=list)

class Metal3Node(BaseModel):
    """
    Declarative inventory for a Metal3-managed node.
    The name must match the BareMetalHost name.
    """
    name: str
    role: Literal["control-plane", "worker"]
    nics: List[str]

class Metal3Config(BaseModel):
    """
    Metal3-specific configuration block.
    """
    nodes: List[Metal3Node] = Field(default_factory=list)


class ClusterAPI(BaseModel):
    """Configuration for Cluster API and Proxmox provider deployment."""

    # Core identifiers
    cluster_name: str
    namespace: str

    # Networking and infra
    pod_cidr: str
    pod_subnet: str
    control_plane_vip: str
    allowed_nodes: List[str]
    dns_servers: List[str]
    ip_range: str
    gateway: str
    prefix: int
    vip_interface: str
    cert_sans: List[str]
    network_bridge: str
    source_node: str
    template_id: int
    pivot: bool

    # Kubernetes and images
    kubernetes_version: str
    kube_vip_image: str

    # Replicas
    control_plane_replicas: int
    control_plane_count: int
    worker_replicas: int
    worker_count: int

    # Hardware profiles
    control_plane_disk_gb: int
    worker_disk_gb: int
    control_plane_memory_mib: int
    worker_memory_mib: int
    control_plane_cores: int
    worker_cores: int
    control_plane_sockets: int
    worker_sockets: int

    # User bootstrap
    builder_password: str
    ssh_public_key: str
    ssh_public_key_path: Path

    # Proxmox secret fields
    proxmox_secret_name: str
    proxmox_url: str
    proxmox_token: str
    proxmox_secret: str
    provider: Literal["proxmox", "metal3", "tinkerbell"] = "proxmox"
    image_username: str
    image_password: str
    image_password_hash: str
    service_cidr: str
    image_url: str

    # -----------------------
    # Metal3-specific fields
    # -----------------------
    image_os: Optional[Literal["ubuntu", "centos"]] = "ubuntu"
    capm3_release: Optional[str] = None
    capm3_release_branch: Optional[str] = None
    capm3_version: Optional[str] = None
    metal3_templates_path: Optional[Path] = None
    image_flavor: Literal["ubuntu", "centos"] = "ubuntu"
    image_version: str = "22.04"
    metal3: Optional[Metal3Config] = None    

    # Management cluster access (Metal3 dev env host)
    mgmt_host: str
    mgmt_user: str
    mgmt_ssh_key_path: Path = Path.home() / ".ssh" / "id_ed25519"
    metal3_namespace: str = "metal3"

    # Ironic HTTP base (where images are served from)
    ironic_http_base: str

    # Provisioning network default gateway (pfSense/firewall LAN IP).
    # Used in Tinkerbell configure-node to write a single correct default route.
    provisioning_gateway: str = "10.10.0.100"

    # CNI — installed on the workload cluster after KCP initializes
    cilium_version: Optional[str] = None

class ReleaseSpec(BaseModel):
    name: str                        # helm release name
    namespace: str                   # target ns
    chart: str                       # repo/chart or oci:// uri
    version: Optional[str] = None
    values: ValuesRef = ValuesRef()
    create_namespace: bool = True
    atomic: bool = True
    timeout_seconds: int = 600
    wait: bool = True
    install_crds: bool = False
    dependencies: List[str] = Field(default_factory=list)  # release names this one depends on
    hooks: List[str] = Field(default_factory=list)         # names of hook functions

class ClusterConfig(BaseModel):
    context: Optional[str] = None       # Kubernetes context to use
    repos: List[RepoSpec] = Field(default_factory=list)
    releases: List[ReleaseSpec]
    environment: Literal["dev", "staging", "prod"] = "dev"
    cluster_api: Optional[ClusterAPI] = None

    # Helper method
    def by_name(self) -> Dict[str, ReleaseSpec]:
        """
        Returns a dictionary mapping each Helm release name to its ReleaseSpec object.
        Useful for quickly accessing release configurations by name.
        """
        return {r.name: r for r in self.releases}


class ExternalCephHostConfig(BaseModel):
    """
    A dedicated storage host not part of the Kubernetes cluster.
    SSH credentials and an explicit OSD device list are required.
    """
    hostname: str
    address: str
    username: str = "ubuntu"
    port: int = 22
    password: Optional[str] = None
    pkey_path: Optional[str] = None
    osd_devices: List[str] = Field(default_factory=list)  # e.g. ["/dev/sdb", "/dev/sdc"]


class CephConfig(BaseModel):
    """
    Ceph storage tunables.
    Set pool_replication_size=1 for dev (single OSD), 3 for production.
    """
    pool_replication_size: int = 3
    pool_min_size: int = 2
    public_network: Optional[str] = None  # e.g. "192.168.0.0/24"
    additional_ceph_hosts: List[ExternalCephHostConfig] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class OpenStackConfig(BaseModel):
    """
    OpenStack deployment tunables.
    """
    network_backend: Literal["ovn", "linuxbridge"] = "ovn"
    ovn_bgp_agent_enabled: bool = False

    model_config = {"extra": "forbid"}


class KeycloakConfig(BaseModel):
    """
    Top-level Keycloak config.
    """
    k8s_namespace: str = "openstack"
    iam: Optional[KeycloakIAMConfig] = None
    monitoring: Optional[KeycloakMonitoringConfig] = None
    openstack: Optional[KeycloakOpenstackConfig] = None

    model_config = {
        "extra": "forbid"
    }

class RegistryConfig(BaseModel):
    """
    Configuration for the Harbor container registry deployed on the mgmt cluster.
    Images from assets/ values files are mirrored into Harbor once, then every
    subsequent Helm install points at Harbor instead of the public internet.
    """
    mgmt_kubeconfig: Optional[str] = None          # path to mgmt cluster kubeconfig
    harbor_hostname: str = "registry.daalu.io"     # Harbor public FQDN
    harbor_namespace: str = "harbor"
    harbor_storage_size: str = "100Gi"
    project: str = "openstack"                     # Harbor project for mirrored images
    admin_password: Optional[str] = None           # populated via secrets.yaml merge

    # Istio ingress gateway used to expose Harbor (same cluster as Harbor)
    istio_gateway_namespace: str = "istio-ingress"
    istio_gateway_name: str = "gateway"

    # Local disk storage for Harbor images.
    # Set disk_device to a raw block device (e.g. "/dev/sda") and daalu will:
    #   1. Format it as ext4 (skipped if already formatted)
    #   2. Mount it at storage_mount_path
    #   3. Deploy local-path-provisioner pointing at that path
    #   4. Configure all Harbor PVCs to use the "local-path" StorageClass
    disk_device: Optional[str] = None             # e.g. "/dev/sda"
    storage_mount_path: str = "/mnt/harbor-storage"
    storage_class: str = "local-path"             # StorageClass for Harbor PVCs

    # Explicit IP for NodePort access — overrides auto-detected kubectl node IP.
    # Set this to the provisioning-network IP (e.g. "10.10.0.9") so Harbor is
    # reachable via the provisioning NIC instead of the mgmt NIC (ens18/192.168.0.x)
    # which will be taken over by OVN/Neutron later.
    harbor_node_ip: Optional[str] = None

    # Docker Hub credentials for authenticated pulls (avoids 100-pull rate limit).
    # Populated via secrets.yaml registry.docker_username / registry.docker_token.
    docker_username: Optional[str] = None
    docker_token: Optional[str] = None

    model_config = {"extra": "forbid"}


class DaaluConfig(BaseModel):
    environment: Literal["dev", "staging", "prod"] = "dev"
    context: Optional[str] = None

    cluster_api: Optional[ClusterAPI] = None
    repos: List[RepoSpec] = Field(default_factory=list)
    releases: List[ReleaseSpec] = Field(default_factory=list)

    # Insecure (plain HTTP) container registries that K8s nodes must be able
    # to pull from.  Each entry is "host:port", e.g. "10.10.0.5:5000".
    # These are written into /etc/containerd/certs.d/ during node bootstrap.
    insecure_registries: List[str] = Field(default_factory=list)

    keycloak: Optional[KeycloakConfig] = None
    ceph: Optional[CephConfig] = None
    monitoring: Optional[MonitoringConfig] = None
    openstack: Optional[OpenStackConfig] = None
    openstack_secrets: Optional[Dict[str, str]] = None
    registry: Optional[RegistryConfig] = None
    mgmt_cluster: Optional[MgmtClusterConfig] = None

    model_config = {
        "extra": "forbid"
    }