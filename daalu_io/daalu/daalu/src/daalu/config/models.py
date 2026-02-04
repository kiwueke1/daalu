# src/daalu/config/models.py

from typing import Dict, List, Optional, Literal
from pydantic import BaseModel, HttpUrl, Field
from pathlib import Path
from daalu.bootstrap.shared.keycloak.models import KeycloakIAMConfig
from daalu.bootstrap.monitoring.models import KeycloakMonitoringConfig
from daalu.config.monitoring import MonitoringConfig
from daalu.bootstrap.openstack.models import KeycloakOpenstackConfig



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
    provider: Literal["proxmox", "metal3"] = "proxmox"
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

class DaaluConfig(BaseModel):
    environment: Literal["dev", "staging", "prod"] = "dev"
    context: Optional[str] = None

    cluster_api: Optional[ClusterAPI] = None
    repos: List[RepoSpec] = Field(default_factory=list)
    releases: List[ReleaseSpec] = Field(default_factory=list)

    keycloak: Optional[KeycloakConfig] = None
    monitoring: Optional[MonitoringConfig] = None

    model_config = {
        "extra": "forbid"
    }