# src/daalu/bootstrap/monitoring/components/ipmi_exporter.py

from pathlib import Path
import yaml

from daalu.bootstrap.engine.component import InfraComponent
from daalu.utils.helpers import load_yaml_file


class IPMIExporterComponent(InfraComponent):
    def __init__(
        self,
        kubeconfig=None,
        namespace: str = "monitoring",
        config_path: Path = None,
        **kwargs
    ):
        super().__init__(
            name="ipmi-exporter",
            repo_name="",
            repo_url="",
            chart="",
            version=None,
            namespace=namespace,
            release_name="ipmi-exporter",
            kubeconfig=kubeconfig,
            uses_helm=False,
            local_chart_dir=None,
            remote_chart_dir=None
        )

        self.config_path = config_path


    def post_install(self, kubectl):
        print("installing ipmi-components")
        kubectl.apply_objects([
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": "ipmi-exporter",
                    "namespace": self.namespace,
                },
                "data": {
                    "config.yml": self.config_path.read_text(),
                },
            },
            {
                "apiVersion": "apps/v1",
                "kind": "DaemonSet",
                "metadata": {
                    "name": "ipmi-exporter",
                    "namespace": self.namespace,
                },
                "spec": {
                    "selector": {
                        "matchLabels": {"application": "ipmi-exporter"}
                    },
                    "template": {
                        "metadata": {
                            "labels": {
                                "application": "ipmi-exporter",
                                "job": "ipmi",
                            }
                        },
                        "spec": {
                            "containers": [
                                {
                                    "name": "exporter",
                                    "image": "prometheuscommunity/ipmi-exporter",
                                    "ports": [{"containerPort": 9290}],
                                    "securityContext": {"privileged": True},
                                    "volumeMounts": [
                                        {"name": "dev-ipmi0", "mountPath": "/dev/ipmi0"},
                                        {"name": "config", "mountPath": "/config.yml", "subPath": "config.yml"},
                                    ],
                                }
                            ],
                            "volumes": [
                                {"name": "dev-ipmi0", "hostPath": {"path": "/dev/ipmi0"}},
                                {"name": "config", "configMap": {"name": "ipmi-exporter"}},
                            ],
                        },
                    },
                },
            },
        ])


    def values(self):
        return load_yaml_file(self.config_path)
