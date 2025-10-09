# src/daalu/helm/cli_runner.py
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import List

import yaml

from .interface import IHelm
from .errors import HelmError, HelmDiffError
from ..config.models import RepoSpec, ReleaseSpec


class HelmCliRunner(IHelm):
    """
    A pragmatic wrapper around the `helm` CLI.
    - Mirrors human CLI usage: 'repo add/update', 'upgrade --install', 'uninstall', 'diff', 'lint'.
    - Testable by mocking subprocess.run.
    """

    def __init__(self, kube_context: str | None = None, env: dict[str, str] | None = None):
        self.kube_context = kube_context
        self.env = env or {}

    # ------------------------- internal helpers -------------------------

    def _base(self) -> list[str]:
        cmd = ["helm"]
        if self.kube_context:
            cmd += ["--kube-context", self.kube_context]
        return cmd

    def _run(self, argv: List[str], allow_rc: set[int] | None = None, capture: bool = False) -> subprocess.CompletedProcess:
        allow_rc = allow_rc or {0}
        if capture:
            cp = subprocess.run(argv, check=False, text=True, capture_output=True, env=self.env or None)
        else:
            cp = subprocess.run(argv, check=False, env=self.env or None)
        if cp.returncode not in allow_rc:
            stderr = getattr(cp, "stderr", "") or ""
            raise HelmError(f"helm failed (rc={cp.returncode}) for {argv!r}\n{stderr}")
        return cp

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

    def add_repo(self, repo: RepoSpec) -> None:
        argv = self._base() + ["repo", "add", repo.name, str(repo.url)]
        if repo.username and repo.password:
            argv += ["--username", repo.username, "--password", repo.password]
        self._run(argv)

    def update_repos(self) -> None:
        self._run(self._base() + ["repo", "update"])

    def upgrade_install(self, rel: ReleaseSpec) -> None:
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
            argv += ["--install-crds"]  # supported by some charts; harmless if ignored

        self._run(argv)

    def uninstall(self, release_name: str, namespace: str) -> None:
        argv = self._base() + ["uninstall", release_name, "-n", namespace]
        self._run(argv)

    def diff(self, rel: ReleaseSpec) -> str:
        # helm-diff returns rc=2 when changes are detected; treat 0 and 2 as success.
        argv = (
            self._base()
            + ["diff", "upgrade", rel.name, rel.chart, "-n", rel.namespace]
            + self._values_args(rel)
        )
        cp = self._run(argv, allow_rc={0, 2}, capture=True)
        # If diff plugin isn’t installed, helm exits non-zero → handled by _run.
        return cp.stdout

    def lint(self, rel: ReleaseSpec) -> None:
        argv = self._base() + ["lint", rel.chart] + self._values_args(rel)
        self._run(argv)
