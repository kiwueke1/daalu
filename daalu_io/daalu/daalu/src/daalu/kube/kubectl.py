# src/daalu/kube/kubectl.py

from __future__ import annotations

import json
import time

from daalu.utils.ssh_runner import SSHRunner


class KubectlError(RuntimeError):
    pass


class KubectlRunner:
    """
    kubectl runner executed remotely over SSH.
    """

    def __init__(
        self,
        *,
        ssh: SSHRunner,
        kubeconfig: str = "/etc/kubernetes/admin.conf",
    ):
        self.ssh = ssh
        self.kubeconfig = kubeconfig

    def _run(self, cmd: str) -> tuple[int, str, str]:
        full_cmd = f"KUBECONFIG={self.kubeconfig} kubectl {cmd}"
        return self.ssh.run(full_cmd, sudo=True)

    def apply_file(self, path: str) -> None:
        rc, out, err = self._run(f"apply -f {path}")
        if rc != 0:
            raise KubectlError(f"kubectl apply failed: {err or out}")

    def apply_content(
        self,
        content: str,
        remote_path: str = "/tmp/daalu-apply.yaml",
    ) -> None:
        self.ssh.put_text(content, remote_path)
        self.apply_file(remote_path)

    def get_pods(self, namespace: str) -> list[dict]:
        rc, out, err = self._run(f"get pods -n {namespace} -o json")
        if rc != 0:
            raise KubectlError(f"kubectl get pods failed: {err or out}")
        return json.loads(out).get("items", [])

    def count_running_pods(self, namespace: str) -> int:
        return sum(
            1
            for p in self.get_pods(namespace)
            if p.get("status", {}).get("phase") == "Running"
        )

    def wait_for_pods_running(
        self,
        *,
        namespace: str,
        min_running: int,
        retries: int = 20,
        delay: int = 10,
    ) -> None:
        for _ in range(retries):
            if self.count_running_pods(namespace) >= min_running:
                return
            time.sleep(delay)

        raise KubectlError(
            f"Timed out waiting for {min_running} pods in namespace '{namespace}'"
        )
