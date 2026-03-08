# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/registry/image_builder.py

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("daalu")

# Import lazily to avoid circular dependency — image_mirror doesn't import image_builder
def _rewrite_image(source: str, harbor_hostname: str, harbor_project: str) -> str:
    from daalu.bootstrap.registry.image_mirror import ImageMirror
    return ImageMirror.rewrite_image_static(source, harbor_hostname, harbor_project)


@dataclass
class BuildSpec:
    """Describes how to build a single image from a Git source."""
    component: str          # asset dir name, e.g. "staffeln"
    source_image: str       # upstream ref, e.g. "ghcr.io/vexxhost/staffeln:v2.2.3"
    git_repo: str           # https://github.com/...
    git_ref: str            # tag or branch, e.g. "v2.2.3"
    dest_tag: str           # tag appended to the rewritten Harbor path, e.g. "v2.2.3-fixed"
    pip_constraints: list[str] = field(default_factory=list)


@dataclass
class BuildReport:
    success: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


class ImageBuilder:
    """
    Builds container images from Git source repos and pushes them to Harbor.

    Each asset component can optionally contain a build_image.yaml that
    describes how to build it when the upstream image is private or requires
    dependency pins not present in the published image.

    build_image.yaml schema:
        git_repo: https://github.com/org/repo
        git_ref: v1.2.3
        dest_tag: v1.2.3-fixed          # tag appended to the Harbor image path
        pip_constraints:                 # optional extra pip install constraints
          - "oslo.db<12.0.0"
          - "SQLAlchemy>=1.4.0,<2.0.0"
    """

    def __init__(
        self,
        *,
        assets_dir: Path,
        harbor_hostname: str,
        harbor_project: str,
        admin_password: str,
    ):
        self.assets_dir = assets_dir
        self.harbor_hostname = harbor_hostname
        self.harbor_project = harbor_project
        self.admin_password = admin_password

    def load_specs(self) -> list[BuildSpec]:
        """Scan assets/*/build_image.yaml and return all build specs."""
        specs = []
        for path in sorted(self.assets_dir.glob("*/build_image.yaml")):
            component = path.parent.name
            try:
                data = yaml.safe_load(path.read_text())
                specs.append(BuildSpec(
                    component=component,
                    source_image=data["source_image"],
                    git_repo=data["git_repo"],
                    git_ref=data["git_ref"],
                    dest_tag=data["dest_tag"],
                    pip_constraints=data.get("pip_constraints", []),
                ))
            except Exception as exc:
                log.warning("[builder] Failed to parse %s: %s", path, exc)
        return specs

    def build_all(self, skip_existing: bool = True) -> BuildReport:
        """Build and push all images defined by build_image.yaml files."""
        specs = self.load_specs()
        report = BuildReport()

        for spec in specs:
            dest = self._dest_image(spec)
            try:
                if skip_existing and self._exists_in_harbor(dest):
                    log.info("[builder] Already exists, skipping: %s", dest)
                    report.skipped.append(dest)
                    continue
                self._build_and_push(spec, dest)
                report.success.append(dest)
            except Exception as exc:
                log.error("[builder] Failed to build %s: %s", dest, exc)
                report.failed.append(dest)

        log.info(
            "[builder] Build complete — success=%d skipped=%d failed=%d",
            len(report.success), len(report.skipped), len(report.failed),
        )
        return report

    def _dest_image(self, spec: BuildSpec) -> str:
        # Rewrite the source image path (strips upstream registry host, prepends Harbor)
        # then replace the tag with our custom dest_tag.
        # e.g. ghcr.io/vexxhost/staffeln:v2.2.3 → 10.10.0.9:30003/openstack/vexxhost/staffeln:v2.2.3-fixed
        base_without_tag = _rewrite_image(
            spec.source_image, self.harbor_hostname, self.harbor_project
        ).rsplit(":", 1)[0]
        return f"{base_without_tag}:{spec.dest_tag}"

    def source_images(self) -> set[str]:
        """Return the set of upstream source_image refs that will be built (not mirrored)."""
        return {spec.source_image for spec in self.load_specs()}

    def _exists_in_harbor(self, dest: str) -> bool:
        result = subprocess.run(
            ["skopeo", "inspect",
             "--creds", f"admin:{self.admin_password}",
             "--tls-verify=false",
             f"docker://{dest}"],
            capture_output=True,
        )
        return result.returncode == 0

    def _build_and_push(self, spec: BuildSpec, dest: str) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "src"
            log.info("[builder] Cloning %s @ %s", spec.git_repo, spec.git_ref)
            subprocess.run(
                ["git", "clone", "--depth=1", "--branch", spec.git_ref,
                 spec.git_repo, str(src)],
                check=True,
            )

            dockerfile = self._generate_dockerfile(spec)
            df_path = src / "Dockerfile.daalu"
            df_path.write_text(dockerfile)

            log.info("[builder] Building %s", dest)
            subprocess.run(
                ["docker", "build", "-f", str(df_path), "-t", dest, str(src)],
                check=True,
            )

            log.info("[builder] Pushing %s", dest)
            subprocess.run(["docker", "push", dest], check=True)

            # Remove the local image to free disk space
            subprocess.run(["docker", "rmi", dest], capture_output=True)

    def _generate_dockerfile(self, spec: BuildSpec) -> str:
        constraint_args = " ".join(f'"{c}"' for c in spec.pip_constraints)
        pip_install = f'pip install /src {constraint_args}' if constraint_args else "pip install /src"

        return f"""\
FROM python:3.10 AS builder
RUN python3 -m venv /venv
ENV PATH=/venv/bin:$PATH
ADD . /src
RUN --mount=type=cache,target=/root/.cache \\
  {pip_install}

FROM python:3.10-slim AS runtime
ENV PATH=/venv/bin:$PATH
COPY --from=builder /venv /venv
"""
