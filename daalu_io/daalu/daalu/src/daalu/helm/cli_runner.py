# src/daalu/helm/cli_runner.py

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import List
import os

import yaml

from .interface import IHelm
from .errors import HelmError, HelmDiffError
from daalu.config.models import RepoSpec, ReleaseSpec


class HelmCliRunner(IHelm):
    """
    A pragmatic wrapper around the `helm` CLI.
    - Mirrors CLI usage: 'repo add/update', 'upgrade --install', 'uninstall', 'diff', 'lint'.
    - Testable by mocking subprocess.run.
    """

    def __init__(
        self,
        *,
        ssh=None,
        kube_context: str | None = None,
        helm_path: str = "/usr/local/bin/helm",
        env: dict[str, str] | None = None,
    ):
        self.ssh = ssh
        self.kube_context = kube_context
        self.helm_path = helm_path

        base_env = os.environ.copy()
        if env:
            base_env.update(env)

        self.env = base_env


    # ------------------------- internal helpers -------------------------

    def _base(self) -> list[str]:
        cmd = [self.helm_path]
        if self.kube_context:
            cmd += ["--kube-context", self.kube_context]
        return cmd

    def _run(
        self,
        argv: List[str],
        allow_rc: set[int] | None = None,
        capture: bool = False,
        stream: bool = False,
        sudo: bool = False,
    ):
        allow_rc = allow_rc or {0}

        # ---------------- REMOTE EXECUTION ----------------
        if self.ssh:
            cmd = " ".join(argv)
            rc, out, err = self.ssh.run(cmd, sudo=sudo)

            if rc not in allow_rc:
                raise HelmError(
                    f"helm failed (rc={rc}) for {argv!r}\n{err}"
                )

            return out

        # ---------------- LOCAL EXECUTION ----------------
        if sudo:
            argv = ["sudo", "-E"] + argv

        if stream:
            process = subprocess.Popen(
                argv,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self.env,
            )
            output = []
            for line in iter(process.stdout.readline, ""):
                print(line, end="")
                output.append(line)
            process.wait()
            rc = process.returncode
            out = "".join(output)
            err = ""
        else:
            cp = subprocess.run(
                argv,
                check=False,
                text=True,
                capture_output=capture,
                env=self.env,
            )
            rc = cp.returncode
            out = cp.stdout or ""
            err = cp.stderr or ""

        if rc not in allow_rc:
            raise HelmError(
                f"helm failed (rc={rc}) for {argv!r}\n{err}"
            )

        return out




    def _values_args(self, rel: ReleaseSpec) -> list[str]:
        args: list[str] = []
        # values from files
        for f in rel.values.files:
            args += ["-f", f]

        # inline values → write to temp file to pass to helm
        if rel.values.inline:
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tf:
                yaml.safe_dump(rel.values.inline, tf)  # type: ignore[arg-type]
                args += ["-f", tf.name]
        return args

    # ------------------------- IHelm methods -------------------------

    def add_repo(self, repo: RepoSpec, debug: bool = False) -> None:
        argv = self._base() + ["repo", "add", repo.name, str(repo.url)]
        if repo.username and repo.password:
            argv += ["--username", repo.username, "--password", repo.password]
        if debug:
            argv.append("--debug")
        # Stream live output when debug is enabled
        self._run(argv, capture=False, stream=debug, sudo=True)

    def update_repos(self, debug: bool = False) -> None:
        argv = self._base() + ["repo", "update"]
        if debug:
            argv.append("--debug")
        # Stream live output when debug is enabled
        self._run(argv, capture=False, stream=debug, sudo=True)


    def upgrade_install(self, rel: ReleaseSpec, debug: bool = False) -> None:
        argv = (
            self._base()
            + ["upgrade", "--install", rel.name, rel.chart, "-n", rel.namespace]
            + self._values_args(rel)
        )
        if rel.version:
            argv += ["--version", rel.version]
        if rel.create_namespace:
            argv += ["--create-namespace"]
        if rel.atomic:
            argv += ["--atomic"]
        if rel.wait:
            argv += ["--wait", "--timeout", f"{rel.timeout_seconds}s"]
        if rel.install_crds:
            argv += ["--install-crds"]
        if debug:
            argv.append("--debug")

        # stream=True ensures we see Helm’s debug logs in real time
        self._run(argv, capture=False, stream=debug, sudo=True)


    def uninstall(self, release_name: str, namespace: str, debug: bool = False) -> None:
        argv = self._base() + ["uninstall", release_name, "-n", namespace]
        if debug:
            argv.append("--debug")
        # Stream output live if debugging
        self._run(argv, capture=False, stream=debug, sudo=True)

    def diff(self, rel: ReleaseSpec, debug: bool = False) -> str:
        # helm-diff returns rc=2 when changes are detected; treat 0 and 2 as success.
        argv = (
            self._base()
            + ["diff", "upgrade", rel.name, rel.chart, "-n", rel.namespace]
            + self._values_args(rel)
        )
        if debug:
            argv.append("--debug")

        # Stream if debug mode, otherwise capture for return
        if debug:
            cp = self._run(argv, allow_rc={0, 2}, capture=False, stream=True, sudo=True)
            return ""  # streamed output already printed
        else:
            cp = self._run(argv, allow_rc={0, 2}, capture=True, sudo=True)
            return cp.stdout

    def lint(self, rel: ReleaseSpec, debug: bool = False) -> None:
        argv = self._base() + ["lint", rel.chart] + self._values_args(rel)
        if debug:
            argv.append("--debug")
        # Stream output if debugging
        self._run(argv, capture=False, stream=debug, sudo=True)


    def install_or_upgrade_1(
        self,
        *,
        name: str,
        chart: str,
        namespace: str,
        values: dict,
        kubeconfig: str | None = None,
        create_namespace: bool = True,
        atomic: bool = True,
        wait: bool = True,
        timeout_seconds: int = 600,
        debug: bool = False,
    ) -> None:
        """
        Imperative Helm API for platform components (CSI, CNI, etc).

        This is a thin adapter over upgrade_install(ReleaseSpec).
        """

        # Write inline values to a temp file
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tf:
            yaml.safe_dump(values, tf)
            values_file = tf.name

        try:
            rel = ReleaseSpec(
                name=name,
                chart=chart,
                namespace=namespace,
                version=None,
                values={
                    "files": [values_file],
                    "inline": None,
                },
                create_namespace=create_namespace,
                atomic=atomic,
                wait=wait,
                timeout_seconds=timeout_seconds,
                install_crds=False,
                dependencies=[],
                hooks=[],
            )

            # If CSI passed a kubeconfig, override context temporarily
            original_ctx = self.kube_context
            if kubeconfig:
                self.kube_context = None
                self.env = {
                    **self.env,
                    "KUBECONFIG": kubeconfig,
                }

            self.upgrade_install(rel, debug=debug)

        finally:
            Path(values_file).unlink(missing_ok=True)
            self.kube_context = original_ctx


    def install_or_upgrade(
        self,
        *,
        name: str,
        chart: str,
        namespace: str,
        values: dict,
        kubeconfig: str | None = None,
        create_namespace: bool = True,
        atomic: bool = True,
        wait: bool = True,
        timeout_seconds: int = 600,
        debug: bool = False,
    ) -> None:
        """
        Imperative Helm API for platform components (CSI, CNI, etc).

        This method ALWAYS executes helm on the controller node via SSH.
        No helm binary or chart path is required on the local dev machine.
        """

        # --- 1. Write values locally ---
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tf:
            yaml.safe_dump(values, tf)
            local_values_file = Path(tf.name)

        # --- 2. Upload values file to controller ---
        remote_values_file = Path(f"/tmp/daalu-values-{name}.yaml")
        self.ssh.put_file(
            local_path=local_values_file,
            remote_path=remote_values_file,
            sudo=False,
        )

        try:
            # --- 3. Build helm command (REMOTE PATHS ONLY) ---
            cmd: list[str] = [
                "/usr/local/bin/helm",
                "upgrade",
                "--install",
                name,
                chart,
                "-n",
                namespace,
                "-f",
                str(remote_values_file),
            ]

            if create_namespace:
                cmd.append("--create-namespace")
            if atomic:
                cmd.append("--atomic")
            if wait:
                cmd.extend(["--wait", "--timeout", f"{timeout_seconds}s"])
            if debug:
                cmd.append("--debug")

            # --- 4. Environment handling ---
            env_prefix = ""
            if kubeconfig:
                env_prefix = f"KUBECONFIG={kubeconfig} "

            # --- 5. Execute helm on controller via SSH ---
            rc, out, err = self.ssh.run(
                env_prefix + " ".join(cmd),
                sudo=True,
            )

            if rc != 0:
                raise HelmError(
                    f"helm upgrade/install failed for {name}\nSTDOUT:\n{out}\nSTDERR:\n{err}"
                )

        finally:
            # --- 6. Cleanup ---
            try:
                local_values_file.unlink(missing_ok=True)
            except Exception:
                pass

            self.ssh.run(
                f"rm -f {remote_values_file}",
                sudo=True,
            )
