"""Run ``helm`` against a target cluster (local or remote-over-tunnel).

Shells out to the ``helm`` CLI (must be on PATH in the controller image),
writing the values dict and — for customer-cluster mode — the tunnel
kubeconfig to short-lived temp files. The command runner is injectable so
unit tests can assert on the argv without a real ``helm``/cluster.

See docs/design/nv-config-manager-integration.md §6.4, §12.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog
import yaml

logger = structlog.get_logger(__name__)

# (argv, env) -> (returncode, stdout, stderr)
CommandRunner = Callable[
    [list[str], dict[str, str]], Awaitable[tuple[int, str, str]]
]


@dataclass
class HelmResult:
    ok: bool
    stdout: str
    stderr: str
    argv: list[str]


async def _default_runner(
    argv: list[str], env: dict[str, str]
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


class HelmRunner:
    """Idempotent ``helm upgrade --install`` / ``uninstall`` wrapper."""

    def __init__(
        self,
        *,
        chart_path: str,
        runner: CommandRunner | None = None,
        helm_bin: str = "helm",
        timeout: str = "15m",
    ) -> None:
        self._chart_path = chart_path
        self._runner = runner or _default_runner
        self._helm = helm_bin
        self._timeout = timeout

    async def upgrade_install(
        self,
        *,
        release: str,
        namespace: str,
        values: dict[str, Any],
        kubeconfig: dict[str, Any] | None = None,
        wait: bool = True,
    ) -> HelmResult:
        """Create-or-upgrade a release. Returns a :class:`HelmResult`."""
        with _TempFiles(values, kubeconfig) as (values_path, kubeconfig_path):
            argv = [
                self._helm, "upgrade", "--install", release, self._chart_path,
                "-n", namespace, "--create-namespace",
                "-f", values_path, "--timeout", self._timeout,
            ]
            if wait:
                argv.append("--wait")
            env = dict(os.environ)
            if kubeconfig_path:
                env["KUBECONFIG"] = kubeconfig_path
            rc, out, err = await self._runner(argv, env)
        ok = rc == 0
        if not ok:
            logger.warning(
                "config_manager_controller.helm_upgrade_failed",
                release=release,
                namespace=namespace,
                stderr=err[:500],
            )
        return HelmResult(ok=ok, stdout=out, stderr=err, argv=argv)

    async def uninstall(
        self,
        *,
        release: str,
        namespace: str,
        kubeconfig: dict[str, Any] | None = None,
    ) -> HelmResult:
        with _TempFiles(None, kubeconfig) as (_unused, kubeconfig_path):
            argv = [
                self._helm, "uninstall", release, "-n", namespace,
                "--ignore-not-found", "--wait", "--timeout", self._timeout,
            ]
            env = dict(os.environ)
            if kubeconfig_path:
                env["KUBECONFIG"] = kubeconfig_path
            rc, out, err = await self._runner(argv, env)
        return HelmResult(ok=rc == 0, stdout=out, stderr=err, argv=argv)


class _TempFiles:
    """Context manager writing values + kubeconfig to temp files.

    Both are deleted on exit so kubeconfig credentials never linger.
    Returns ``(values_path | "", kubeconfig_path | None)``.
    """

    def __init__(
        self, values: dict[str, Any] | None, kubeconfig: dict[str, Any] | None
    ) -> None:
        self._values = values
        self._kubeconfig = kubeconfig
        self._fhs: list[Any] = []

    def __enter__(self) -> tuple[str, str | None]:
        values_path = ""
        kubeconfig_path: str | None = None
        if self._values is not None:
            vf = tempfile.NamedTemporaryFile(
                mode="w", suffix=".values.yaml", delete=False
            )
            yaml.safe_dump(self._values, vf)
            vf.flush()
            vf.close()
            self._fhs.append(vf.name)
            values_path = vf.name
        if self._kubeconfig is not None:
            kf = tempfile.NamedTemporaryFile(
                mode="w", suffix=".kubeconfig", delete=False
            )
            yaml.safe_dump(self._kubeconfig, kf)
            kf.flush()
            kf.close()
            self._fhs.append(kf.name)
            kubeconfig_path = kf.name
        return values_path, kubeconfig_path

    def __exit__(self, *exc: object) -> None:
        for path in self._fhs:
            try:
                os.unlink(path)
            except OSError:
                pass
