from __future__ import annotations

from pathlib import Path
import yaml
from dataclasses import dataclass, field


DATA_DIR = Path(__file__).parent / "data"


@dataclass
class DefaultsConfig:
    daalu_version: str
    image_prefix: str
    kubeconfig: str
    network_backend: str
    images: dict[str, str] = field(default_factory=dict)


def load_defaults(
    *,
    image_overrides: dict | None = None,
    image_prefix: str = "",
) -> DefaultsConfig:
    path = DATA_DIR / "defaults_vars.yml"

    with path.open() as f:
        raw = yaml.safe_load(f)

    images = raw["_daalu_images"]

    if image_overrides:
        images = _deep_merge(images, image_overrides)

    if image_prefix:
        images = {
            k: f"{image_prefix}{v}"
            for k, v in images.items()
        }

    return DefaultsConfig(
        daalu_version=raw["daalu_release"],
        image_prefix=image_prefix,
        kubeconfig=raw.get("daalu_kubeconfig", "/etc/kubernetes/admin.conf"),
        network_backend=raw.get("daalu_network_backend", "openvswitch"),
        images=images,
    )


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
