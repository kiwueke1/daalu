# src/daalu/bootstrap/images.py

"""
Central image resolver for Daalu bootstrap components.
"""

IMAGES = {
    "csi_rbd_plugin": "quay.io/cephcsi/cephcsi",
    "local_path_provisioner": "rancher/local-path-provisioner",
    "local_path_provisioner_helper": "busybox:1.36",
}


def image(name: str) -> str:
    try:
        return IMAGES[name]
    except KeyError:
        raise KeyError(f"Unknown image key: {name}")
