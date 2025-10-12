# src/daalu/hpc/models.py

from pydantic import BaseModel, Field, FilePath
from pathlib import Path
import yaml
from typing import Dict, List, Optional, Literal
from ..config.models import ClusterAPI


class GPUNodeSpec(BaseModel):
    name: str
    cpus: int
    memory_gb: int
    gpus: int
    gpu_model: str = "generic"
    rdma: bool = False
    nvme_count: int = 1
    node_pool: str = "gpu-pool"

class NetSpec(BaseModel):
    cni: str = "cilium"
    multus: bool = True
    sriov: bool = False
    rdma: bool = False
    pod_cidr: str
    svc_cidr: str

class StorageSpec(BaseModel):
    local_nvme: bool = True
    ceph: bool = True
    minio: bool = True

class SchedulerSpec(BaseModel):
    volcano: bool = True
    ray: bool = False
    slurm: bool = False

class RuntimeSpec(BaseModel):
    nfd: bool = True
    nvidia_gpu_operator: bool = True
    dcgm_exporter: bool = True
    kubectl_context: str | None = None

class HPCConfig(BaseModel):
    name: str
    namespace: str = "default"
    mgmt_context: str
    workload_context: str
    cluster_api: Optional[ClusterAPI] = None
    nodes: list[GPUNodeSpec]
    net: NetSpec
    storage: StorageSpec
    scheduler: SchedulerSpec
    runtime: RuntimeSpec

    @classmethod
    def from_file(cls, path: str | Path) -> "HPCConfig":
        data = yaml.safe_load(Path(path).read_text())
        return cls.model_validate(data)
