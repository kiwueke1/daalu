# src/daalu/kube/kubectl.py

from __future__ import annotations

import json
import time
import yaml
import base64
import subprocess
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
        logger = None,
    ):
        self.ssh = ssh
        self.kubeconfig = kubeconfig
        self.logger = logger


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
        #print("=== KUBECTL DEBUG ===")
        #print("kubectl command:", cmd)
        #print("kubectl ssh runner:", self.ssh)
        #print("=====================")
        full_cmd = f"KUBECONFIG={self.kubeconfig} kubectl {cmd}"

        rc, out, err = self.ssh.run(
            full_cmd,
            sudo=True,
            #capture_output=capture_output,
        )

        return rc, out, err



    def apply_file(
        self,
        path: str,
        *,
        server_side: bool = False,
        force_conflicts: bool = False,
    ) -> None:
        flags = []
        if server_side:
            flags.append("--server-side")
        if force_conflicts:
            flags.append("--force-conflicts")

        flag_str = " ".join(flags)
        rc, out, err = self._run(f"apply {flag_str} -f {path}")
        if rc != 0:
            raise KubectlError(f"kubectl apply failed: {err or out}")


    def apply_content(
        self,
        *,
        content: str,
        remote_path: str,
        server_side: bool = False,
        force_conflicts: bool = False,
    ) -> None:
        self.ssh.put_text(content, remote_path)
        self.apply_file(
            remote_path,
            server_side=server_side,
            force_conflicts=force_conflicts,
        )


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
        server_side: bool = False,
        force_conflicts: bool = False,
    ) -> None:
        objects = list(objects)

        if not objects:
            if self.logger:
                self.logger.log_event(
                    "kubectl.apply.skip",
                    reason="no_objects",
                )
            return

        manifest = yaml.safe_dump_all(objects, sort_keys=False)

        try:
            self.apply_content(
                content=manifest,
                remote_path=remote_path,
                server_side=server_side,
                force_conflicts=force_conflicts,
            )
        except Exception as e:
            # Hard failure (kubectl itself failed)
            for obj in objects:
                kind = obj.get("kind", "<unknown>")
                name = obj.get("metadata", {}).get("name", "<unknown>")
                ns = obj.get("metadata", {}).get("namespace", "default")

                if self.logger:
                    self.logger.log_event(
                        "kubectl.apply.failed",
                        kind=kind,
                        name=name,
                        namespace=ns,
                        error=str(e),
                    )
            raise

        # Success path — report each object
        for obj in objects:
            kind = obj.get("kind", "<unknown>")
            name = obj.get("metadata", {}).get("name", "<unknown>")
            ns = obj.get("metadata", {}).get("namespace", "default")

            if self.logger:
                self.logger.log_event(
                    "kubectl.apply.success",
                    kind=kind,
                    name=name,
                    namespace=ns,
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


    def run(self, args: list[str]) -> tuple[int, str, str]:
        cmd = ["kubectl"] + args

        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        return proc.returncode, proc.stdout, proc.stderr

    def apply_file_server_side(
        self,
        path: str,
        *,
        force_conflicts: bool = True,
    ) -> None:
        """
        Server-side apply avoids storing the huge
        kubectl.kubernetes.io/last-applied-configuration annotation,
        which can break large CRDs (Prometheus Operator CRDs are common offenders).
        """
        extra = " --force-conflicts" if force_conflicts else ""
        rc, out, err = self._run(f"apply --server-side{extra} -f {path}")
        if rc != 0:
            raise KubectlError(f"kubectl server-side apply failed: {err or out}")

    def wait_for_statefulset_ready(
        self,
        *,
        name: str,
        namespace: str,
        retries: int = 60,
        delay: int = 5,
    ) -> None:
        """
        Wait until a StatefulSet has all replicas ready.
        """
        for attempt in range(1, retries + 1):
            rc, out, err = self._run(
                f"get statefulset {name} -n {namespace} -o json"
            )

            if rc != 0:
                raise KubectlError(
                    f"kubectl get statefulset {name} failed: {err or out}"
                )

            data = json.loads(out)

            spec_replicas = data.get("spec", {}).get("replicas", 0)
            status = data.get("status", {})

            ready = status.get("readyReplicas", 0)
            current = status.get("currentReplicas", 0)

            if ready == spec_replicas and current == spec_replicas:
                print(
                    f"[kubectl] StatefulSet {name} ready "
                    f"({ready}/{spec_replicas}) ✓"
                )
                return

            print(
                f"[kubectl] Waiting for StatefulSet {name} "
                f"({ready}/{spec_replicas}) "
                f"[attempt {attempt}/{retries}]"
            )
            time.sleep(delay)

        raise KubectlError(
            f"Timed out waiting for StatefulSet {name} in namespace {namespace}"
        )

    def wait_for(
        self,
        *,
        kind: str,
        name: str,
        namespace: str | None = None,
        timeout_seconds: int = 60,
        interval_seconds: int = 5,
    ) -> None:
        import time

        start = time.time()

        while True:
            args = ["get", kind, name]
            if namespace:
                args.extend(["-n", namespace])

            rc, stdout, stderr = self.run(
                args,
                capture_output=True,
            )

            if rc == 0:
                return

            if time.time() - start > timeout_seconds:
                raise TimeoutError(
                    f"Timed out waiting for {kind}/{name} "
                    f"in namespace {namespace}. Last error: {stderr.strip()}"
                )

            time.sleep(interval_seconds)

    def wait_for_condition(
        self,
        *,
        api_version: str,
        kind: str,
        name: str,
        namespace: str,
        condition_type: str,
        condition_status: str = "True",
        timeout_seconds: int = 120,
    ) -> None:
        """
        Wrapper around:
        kubectl wait --for=condition=TYPE=STATUS
        """

        condition = f"condition={condition_type}={condition_status}"

        cmd = [
            "wait",
            f"--for={condition}",
            f"{kind.lower()}/{name}",
            "-n",
            namespace,
            f"--timeout={timeout_seconds}s",
        ]

        # apiVersion isn't directly passed to kubectl wait,
        # but included here for symmetry & future CRD handling
        self.run(cmd)


    def get_object(
        self,
        *,
        api_version: str,
        kind: str,
        name: str,
        namespace: Optional[str] = None,
    ) -> Optional[dict]:
        args = [
            "get",
            kind.lower(),
            name,
            "-o",
            "json",
        ]

        if namespace:
            args.extend(["-n", namespace])

        try:
            rc, stdout, stderr = self.run(args)
        except RuntimeError as e:
            if "NotFound" in str(e):
                return None
            raise

        if not stdout:
            print(f"[kubectl] get_object: No stdout")
            print(f"[kubectl] get_object: {stderr}")
            return None

        print(f"[kubectl] get_object: {json.loads(stdout)}")

        return json.loads(stdout)

    def b64decode_str(self, b64: str) -> str:
        return base64.b64decode(b64).decode("utf-8", errors="replace")

    def resource_exists(
        self,
        *,
        kind: str,
        name: str,
        namespace: str | None = None,
    ) -> bool:
        """
        Check whether a Kubernetes resource exists.

        Works for core resources and CRDs (e.g. Istio VirtualService).
        """
        args = ["get", kind, name]
        if namespace:
            args.extend(["-n", namespace])

        rc, _, _ = self.run(args)
        return rc == 0

    def wait_for_deployment_ready(
        self,
        name: str,
        namespace: str,
        timeout: int = 300,
    ):
        """
        Wait until a Deployment has all desired replicas available.
        """
        print(f"[kubectl] Waiting for deployment/{name} in {namespace}")

        self.run(
            [
                "rollout",
                "status",
                f"deployment/{name}",
                "-n",
                namespace,
                f"--timeout={timeout}s",
            ]
        )
