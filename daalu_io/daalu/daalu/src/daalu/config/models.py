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
    context: Optional[str] = None      # kubeconfig context
    repos: List[RepoSpec] = Field(default_factory=list)
    releases: List[ReleaseSpec]
    environment: Literal["dev","staging","prod"] = "dev"

    # computed index
    def by_name(self) -> Dict[str, ReleaseSpec]:
        return {r.name: r for r in self.releases}
