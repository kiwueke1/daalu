# src/daalu/bootstrap/infrastructure/engine/helm_engine.py

from daalu.bootstrap.infrastructure.engine.chart_manager import prepare_chart
from daalu.bootstrap.infrastructure.engine.values import deep_merge
from daalu.kube.kubectl import KubectlRunner
from daalu.config.models import RepoSpec



class HelmInfraEngine:
    def __init__(self, *, helm, ssh):
        self.helm = helm
        self.ssh = ssh

    def base_values(self, component) -> dict:
        """
        Engine-wide defaults applied to all components.
        """
        return {}

    def deploy(self, component):
        kubectl = KubectlRunner(
            ssh=self.ssh,
            kubeconfig=component.kubeconfig,
        )

        # 1. Helm repo
        self.helm.add_repo(
            RepoSpec(
                name=component.repo_name,
                url=component.repo_url,
            )
        )
        self.helm.update_repos()

        # 2. Chart prep
        chart_path = prepare_chart(
            ssh=self.ssh,
            component=component,
        )

        # 3. Values layering
        values = deep_merge(
            self.base_values(component),
            component.values(),
        )

        # 4. Install / upgrade
        self.helm.install_or_upgrade(
            name=component.release_name,
            chart=str(chart_path),
            namespace=component.namespace,
            values=values,
            kubeconfig=component.kubeconfig,
        )

        # 5. Wait
        if component.wait_for_pods:
            kubectl.wait_for_pods_running(
                namespace=component.namespace,
                min_running=component.min_running_pods,
            )

        # 6. Post-install
        component.post_install(kubectl)
