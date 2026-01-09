# src/daalu/bootstrap/csi/helm_values.py

from daalu.bootstrap.container_images import image

def rbd_values(*, fsid, monitors, user, key, pool):
    return {
        "csiConfig": [{
            "clusterID": fsid,
            "monitors": monitors,
        }],
        "storageClass": {
            "create": True,
            "name": "general",
            "annotations": {
                "storageclass.kubernetes.io/is-default-class": "true",
            },
            "clusterID": fsid,
            "pool": pool,
            "mountOptions": ["discard"],
        },
        "secret": {
            "create": True,
            "userID": user,
            "userKey": key,
        },
        "nodeplugin": {
            "plugin": {
                "image": {
                    "repository": image("csi_rbd_plugin"),
                }
            }
        },
    }


def local_path_values():
    return {
        "storageClass": {
            "defaultClass": True,
            "name": "general",
        },
        "image": {
            "repository": image("local_path_provisioner"),
        },
        "helperImage": {
            "repository": image("local_path_provisioner_helper"),
        },
    }
