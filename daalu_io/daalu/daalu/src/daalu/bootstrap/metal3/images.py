from dataclasses import dataclass

@dataclass(frozen=True)
class Metal3ImageSpec:
    qcow2: str
    raw: str

def resolve_image_spec(
    *,
    flavor: str,
    version: str,
    kubernetes_version: str,
) -> Metal3ImageSpec:
    if flavor == "ubuntu":
        base = f"UBUNTU_{version}_NODE_IMAGE_K8S_{kubernetes_version}"
        return Metal3ImageSpec(
            qcow2=f"{base}.qcow2",
            raw=f"{base}-raw.img",
        )

    if flavor == "centos":
        base = f"CENTOS_NODE_IMAGE_K8S_{kubernetes_version}"
        return Metal3ImageSpec(
            qcow2=f"{base}.qcow2",
            raw=f"{base}-raw.img",
        )

    raise ValueError(f"Unsupported image flavor: {flavor}")
