# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/registry/image_mirror.py

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field

log = logging.getLogger("daalu")


@dataclass
class MirrorReport:
    success: int = 0
    skipped: int = 0
    failed: int = 0
    failed_images: list = field(default_factory=list)


def _is_custom_registry(image: str) -> bool:
    """True for images like '10.10.0.5:5000/...' that should not be rewritten."""
    if "/" not in image:
        # Bare image name like 'redis:8.2.2-alpine' — Docker Hub library image, not custom.
        return False
    host = image.split("/")[0]
    return ":" in host and not host.startswith("docker.io")


class ImageMirror:
    """
    Mirrors container images from their source registries (docker.io, quay.io, etc.)
    into Harbor using skopeo copy.

    Custom/local images (IP:port style registries) are skipped and left unchanged.
    """

    def __init__(
        self,
        *,
        harbor_hostname: str,
        harbor_project: str,
        admin_password: str,
        insecure: bool = False,
        skip_existing: bool = True,
        docker_username: str | None = None,
        docker_token: str | None = None,
    ):
        self.harbor_hostname = harbor_hostname
        self.harbor_project = harbor_project
        self.admin_password = admin_password
        self.insecure = insecure
        self.skip_existing = skip_existing
        self.docker_username = docker_username
        self.docker_token = docker_token

    def mirror_all(self, images: list[str]) -> MirrorReport:
        """Mirror all images, returning counts of success/skip/fail."""
        report = MirrorReport()

        for image in images:
            if _is_custom_registry(image):
                log.debug("[registry] Skipping custom registry image: %s", image)
                report.skipped += 1
                continue

            try:
                mirrored = self._mirror_one(image)
                if mirrored:
                    report.success += 1
                    log.info("[registry] Mirrored: %s", image)
                else:
                    report.skipped += 1
                    log.debug("[registry] Already exists, skipped: %s", image)
            except Exception as exc:
                log.error("[registry] Failed to mirror %s: %s", image, exc)
                report.failed += 1
                report.failed_images.append(image)

        log.info(
            "[registry] Mirror complete — success=%d skipped=%d failed=%d",
            report.success, report.skipped, report.failed,
        )
        return report

    def _src_registry(self, source: str) -> str:
        """Return the registry host of a source image (e.g. 'docker.io')."""
        parts = source.split("/")
        host = parts[0]
        # Images like 'rabbitmq:tag' have no explicit host — they're docker.io
        if "." not in host and ":" not in host:
            return "docker.io"
        return host

    def _mirror_one(self, source: str) -> bool:
        """Mirror a single image. Returns True if copied, False if skipped."""
        dest = self._dest_image(source)

        if self.skip_existing and self._exists_in_harbor(dest):
            return False

        cmd = ["skopeo", "copy"]

        src_registry = self._src_registry(source)
        if self.docker_username and self.docker_token and src_registry == "docker.io":
            # Authenticate to Docker Hub to avoid pull rate limits
            cmd += ["--src-creds", f"{self.docker_username}:{self.docker_token}"]
        elif src_registry != "docker.io":
            # For non-Docker Hub registries (ghcr.io, quay.io, etc.) where we have
            # no credentials, pass --src-no-creds so skopeo does not attempt a
            # bearer-token exchange that some registries (e.g. ghcr.io) reject with 403.
            cmd += ["--src-no-creds"]

        cmd += ["--dest-creds", f"admin:{self.admin_password}"]

        if self.insecure:
            cmd += ["--dest-tls-verify=false"]

        cmd += [f"docker://{source}", f"docker://{dest}"]

        subprocess.run(cmd, check=True)
        return True

    def _dest_image(self, source: str) -> str:
        """
        Compute the Harbor destination URL for a source image.

        docker.io/openstackhelm/glance:tag  → {harbor}/{project}/openstackhelm/glance:tag
        docker.io/rabbitmq:tag              → {harbor}/{project}/rabbitmq:tag
        quay.io/cephcsi/cephcsi:tag         → {harbor}/{project}/cephcsi/cephcsi:tag
        """
        return self.rewrite_image_static(source, self.harbor_hostname, self.harbor_project)

    def rewrite_image(self, source: str) -> str:
        """Return the Harbor equivalent URL for a source image (for Helm values rewriting)."""
        if _is_custom_registry(source):
            return source
        return self._dest_image(source)

    @staticmethod
    def rewrite_image_static(source: str, registry_url: str, project: str) -> str:
        """
        Static version of rewrite_image for use without an ImageMirror instance.

        Strips the source registry prefix and prepends the Harbor hostname + project.
        Custom (IP:port) images are returned unchanged.
        """
        if _is_custom_registry(source):
            return source

        # Strip the registry host (first slash-delimited segment)
        parts = source.split("/", 1)
        if len(parts) == 2:
            remainder = parts[1]
        else:
            remainder = parts[0]

        return f"{registry_url}/{project}/{remainder}"

    def _exists_in_harbor(self, dest: str) -> bool:
        """Check if an image already exists in Harbor using skopeo inspect."""
        cmd = ["skopeo", "inspect",
               "--creds", f"admin:{self.admin_password}"]
        if self.insecure:
            cmd.append("--tls-verify=false")
        cmd.append(f"docker://{dest}")
        result = subprocess.run(cmd, capture_output=True)
        return result.returncode == 0
