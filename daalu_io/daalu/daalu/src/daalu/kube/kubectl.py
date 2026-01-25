# src/daalu/kube/kubectl.py

from __future__ import annotations

import json
import time
import yaml
from typing import Iterable
from typing import Any


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

    def _run_1(self, cmd: str) -> tuple[int, str, str]:
        full_cmd = f"KUBECONFIG={self.kubeconfig} kubectl {cmd}"
        return self.ssh.run(full_cmd, sudo=True)

    def _run(
        self,
        cmd: str,
        *,
        capture_output: bool = False,
    ) -> tuple[int, str, str]:
        """
        Run a kubectl command.

        Returns:
            (rc, stdout, stderr)
        """
        full_cmd = f"KUBECONFIG={self.kubeconfig} kubectl {cmd}"

        rc, out, err = self.ssh.run(
            full_cmd,
            sudo=True,
            #capture_output=capture_output,
        )

        return rc, out, err


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

    def apply_url(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        header_flags = ""
        if headers:
            for k, v in headers.items():
                # IMPORTANT: use double-quotes, not single-quotes
                header_flags += f' -H "{k}: {v}"'

        cmd = (
            f"curl -fSsL{header_flags} \"{url}\" | "
            f"KUBECONFIG={self.kubeconfig} kubectl apply -f -"
        )

        # Debug print (safe — but consider redacting tokens later)
        print(f"[kubectl.apply_url] Executing on controller:\n{cmd}\n")

        rc, out, err = self.ssh.run(cmd, sudo=True)
        if rc != 0:
            raise KubectlError(
                f"kubectl apply_url failed for {url}: {err or out}"
            )

    def apply_objects(
        self,
        objects: Iterable[dict],
        *,
        remote_path: str = "/tmp/daalu-apply.yaml",
    ) -> None:
        """
        Apply one or more Kubernetes objects expressed as Python dicts.
        """
        manifest = yaml.safe_dump_all(
            objects,
            sort_keys=False,
        )

        self.apply_content(
            content=manifest,
            remote_path=remote_path,
        )

    def get_names(
        self,
        *,
        kind: str,
        namespace: str,
        api_version: str | None = None,
    ) -> list[str]:
        args = [
            "get",
            kind,
            "-n",
            namespace,
            "-o",
            "jsonpath={.items[*].metadata.name}",
        ]

        if api_version:
            args.insert(1, f"--api-version={api_version}")

        rc, out, err = self._run(args)

        if rc != 0:
            return []

        return out.strip().split() if out.strip() else []


    def patch(
        self,
        *,
        api_version: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        patch: dict,
        patch_type: str = "merge",
    ):
        """
        Patch a Kubernetes object (merge or strategic merge).
        Mirrors kubernetes.core.k8s state=patched.
        """
        args = [
            "patch",
            kind.lower(),
            name,
            "--type",
            patch_type,
            "-p",
            yaml.safe_dump(patch),
        ]

        if namespace:
            args.extend(["-n", namespace])

        return self._run(args)



    def get(
        self,
        *,
        api_version: str,
        kind: str,
        name: str,
        namespace: str | None = None,
    ) -> dict:
        """
        kubectl get <kind> <name> -o json
        """
        cmd = f"get {kind.lower()} {name} -o json"
        if namespace:
            cmd += f" -n {namespace}"

        rc, stdout, stderr = self._run(cmd, capture_output=True)

        if rc != 0:
            raise RuntimeError(
                f"kubectl get {kind}/{name} failed: {stderr}"
            )

        if not stdout:
            raise RuntimeError(
                f"kubectl get {kind}/{name} returned empty output"
            )

        try:
            return json.loads(stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Failed to parse kubectl output as JSON: {e}\nOutput:\n{stdout}"
            )


    def run(self, cmd, *, capture_output: bool = False):
        """
        Run a kubectl command.

        Args:
            cmd: list[str] or str (without 'kubectl')
            capture_output: whether to return stdout/stderr

        Returns:
            (rc, stdout, stderr)
        """
        if isinstance(cmd, list):
            cmd = " ".join(cmd)

        rc, stdout, stderr = self._run(cmd)

        if capture_output:
            return rc, stdout, stderr

        return rc, "", ""
