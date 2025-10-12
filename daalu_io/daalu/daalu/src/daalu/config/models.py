# src/daalu/config/models.py

from typing import Dict, List, Optional, Literal
from pydantic import BaseModel, HttpUrl, Field

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

    # Kubernetes and images
    kubernetes_version: str
    kube_vip_image: str

    # Replicas
    control_plane_replicas: int
    worker_replicas: int

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

    # Proxmox secret fields
    proxmox_secret_name: str
    proxmox_url: str
    proxmox_token: str
    proxmox_secret: str
    
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
