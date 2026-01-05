# src/daalu/bootstrap/metal3/image_manager.py
from __future__ import annotations

import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Where metal3-dev-env's ironic HTTP server serves images from (ON THE MGMT HOST)
IRONIC_IMAGE_DIR = Path("/opt/metal3-dev-env/ironic/html/images")

# Upstream location (download source)
DEFAULT_IMAGE_LOCATION = "https://artifactory.nordix.org/artifactory/metal3/images/k8s_v1.35.0"


def _run_local(cmd: list[str], *, sudo: bool = False) -> None:
    if sudo:
        cmd = ["sudo"] + cmd
    subprocess.run(cmd, check=True)


def _ensure_tool_local(tool: str) -> None:
    if shutil.which(tool):
        return

    if shutil.which("apt"):
        _run_local(["apt", "update"], sudo=True)
        _run_local(["apt", "install", "-y", tool], sudo=True)
    elif shutil.which("dnf"):
        _run_local(["dnf", "install", "-y", tool], sudo=True)
    else:
        raise RuntimeError(f"Required tool '{tool}' not found and cannot auto-install")


def _needs_sudo_local(path: Path) -> bool:
    """
    Returns True if creating or writing to `path` would require sudo.
    Safe for non-existent paths.
    """
    parent = path
    while not parent.exists():
        parent = parent.parent

    try:
        test = parent / ".perm_test"
        test.touch()
        test.unlink()
        return False
    except PermissionError:
        return True


class RemoteExecutor:
    """
    Executes commands on a remote host via ssh.

    Notes:
    - Uses single remote shell command string.
    - Uses shlex.join to preserve quoting.
    """

    def __init__(self, host: str, user: str, *, ssh_opts: Optional[list[str]] = None) -> None:
        self.host = host
        self.user = user
        self.ssh_opts = ssh_opts or ["-o", "BatchMode=yes"]

    def run(self, cmd: list[str], *, sudo: bool = False) -> str:
        remote = shlex.join(cmd)
        if sudo:
            # -n: non-interactive sudo (fails if password required)
            remote = f"sudo -n {remote}"

        ssh_cmd = ["ssh", *self.ssh_opts, f"{self.user}@{self.host}", remote]
        return subprocess.check_output(ssh_cmd, text=True)

    def run_check(self, cmd: list[str], *, sudo: bool = False) -> None:
        remote = shlex.join(cmd)
        if sudo:
            remote = f"sudo -n {remote}"
        ssh_cmd = ["ssh", *self.ssh_opts, f"{self.user}@{self.host}", remote]
        subprocess.run(ssh_cmd, check=True)


@dataclass(frozen=True)
class ImageMetadata:
    image_name: str               # original (qcow2) filename
    raw_image_name: str           # raw filename (served by ironic)
    raw_path: str                 # absolute path on mgmt host
    checksum: str                 # hex sha256 digest
    checksum_type: str            # "sha256"
    checksum_file: str            # absolute checksum file path on mgmt host

    def checksum_for_metal3(self) -> str:
        # Many Metal3 templates use "sha256:<hex>"
        return f"{self.checksum_type}:{self.checksum}"

    def image_url1(self, *, http_base: str) -> str:
        # metal3-dev-env ironic serves /images/ from IRONIC_IMAGE_DIR
        return f"{http_base.rstrip('/')}/images/{self.raw_image_name}"
    
    def image_url(self, http_base: str, *, raw: bool = True) -> str:
        image = self.raw_image_name if raw else self.image_name
        return f"{http_base}/{image}"

class Metal3ImageManager:
    """
    Ensures images exist where Ironic (metal3-dev-env) serves them from.

    Supports two modes:
      - local mode (default): manages files on the current machine
      - remote mode: manages files on the management host via SSH
    """

    def __init__(
        self,
        *,
        image_location: str = DEFAULT_IMAGE_LOCATION,
        # If set, manager will perform all operations on the mgmt host
        mgmt_host: Optional[str] = None,
        mgmt_user: Optional[str] = None,
        ssh_opts: Optional[list[str]] = None,
    ) -> None:
        self.image_location = image_location

        self._remote: Optional[RemoteExecutor] = None
        if mgmt_host and mgmt_user:
            self._remote = RemoteExecutor(mgmt_host, mgmt_user, ssh_opts=ssh_opts)

        if self._remote is None:
            # local setup
            self.use_sudo = _needs_sudo_local(IRONIC_IMAGE_DIR)
            if not IRONIC_IMAGE_DIR.exists():
                if self.use_sudo:
                    _run_local(["mkdir", "-p", str(IRONIC_IMAGE_DIR)], sudo=True)
                else:
                    IRONIC_IMAGE_DIR.mkdir(parents=True, exist_ok=True)

            _ensure_tool_local("wget")
            _ensure_tool_local("qemu-img")
            _ensure_tool_local("sha256sum")
        else:
            # remote setup: ensure dir + tools on mgmt host
            self._remote.run_check(["mkdir", "-p", str(IRONIC_IMAGE_DIR)], sudo=True)
            # Tool checks (do not auto-install remotely here; fail loudly)
            self._remote.run_check(["bash", "-lc", "command -v wget"], sudo=True)
            self._remote.run_check(["bash", "-lc", "command -v qemu-img"], sudo=True)
            self._remote.run_check(["bash", "-lc", "command -v sha256sum"], sudo=True)

    def _remote_or_local_path(self, p: Path) -> str:
        return str(p)

    def download_image(
        self,
        image_name: str,
        raw_image_name: str,
    ) -> ImageMetadata:
        image_path = IRONIC_IMAGE_DIR / image_name
        raw_image_path = IRONIC_IMAGE_DIR / raw_image_name
        sha_path = raw_image_path.with_suffix(raw_image_path.suffix + ".sha256sum")

        if self._remote is None:
            # -------- Local mode --------
            if not image_path.exists():
                print(f"[image] Downloading {image_name}")
                _run_local(
                    ["wget", "-q", f"{self.image_location}/{image_name}", "-O", str(image_path)],
                    sudo=self.use_sudo,
                )
            print(f"[image] ✓ qcow2 image ensured: {image_name}")

            if not raw_image_path.exists():
                print(f"[image] Converting {image_name} -> {raw_image_name}")
                _run_local(
                    ["qemu-img", "convert", "-O", "raw", str(image_path), str(raw_image_path)],
                    sudo=self.use_sudo,
                )
            print(f"[image] ✓ raw image ensured: {raw_image_name}")

            if not sha_path.exists():
                print("[image] Calculating sha256 checksum")
                sha = subprocess.check_output(
                    ["sha256sum", str(raw_image_path)], text=True
                ).split()[0]
                sha_path.write_text(f"{sha}\n", encoding="utf-8")
                if self.use_sudo:
                    _run_local(["chmod", "664", str(sha_path)], sudo=True)
            else:
                sha = sha_path.read_text(encoding="utf-8").strip()

            print("[image] ✓ sha256 checksum ensured")

            return ImageMetadata(
                image_name=image_name,
                raw_image_name=raw_image_name,
                raw_path=str(raw_image_path),
                checksum=sha,
                checksum_type="sha256",
                checksum_file=str(sha_path),
            )

        # -------- Remote mode --------
        r = self._remote
        assert r is not None

        img = str(image_path)
        raw = str(raw_image_path)
        sha_file = str(sha_path)

        print(f"[image] Ensuring qcow2 image exists on mgmt host: {image_name}")
        r.run_check(
            [
                "bash",
                "-lc",
                (
                    f"test -f {shlex.quote(img)} "
                    f"|| wget -q {shlex.quote(self.image_location + '/' + image_name)} -O {shlex.quote(img)}"
                ),
            ],
            sudo=True,
        )
        print(f"[image] ✓ qcow2 image ensured on mgmt host: {image_name}")

        print(f"[image] Ensuring raw image exists on mgmt host: {raw_image_name}")
        r.run_check(
            [
                "bash",
                "-lc",
                (
                    f"test -f {shlex.quote(raw)} "
                    f"|| qemu-img convert -O raw {shlex.quote(img)} {shlex.quote(raw)}"
                ),
            ],
            sudo=True,
        )
        print(f"[image] ✓ raw image ensured on mgmt host: {raw_image_name}")

        print("[image] Ensuring sha256 checksum exists on mgmt host")
        r.run_check(
            [
                "bash",
                "-lc",
                (
                    f"test -f {shlex.quote(sha_file)} "
                    f"|| (sha256sum {shlex.quote(raw)} | awk '{{print $1}}' > {shlex.quote(sha_file)})"
                ),
            ],
            sudo=True,
        )

        checksum = r.run(["cat", sha_file], sudo=True).strip()
        print("[image] ✓ sha256 checksum ensured on mgmt host")

        return ImageMetadata(
            image_name=image_name,
            raw_image_name=raw_image_name,
            raw_path=raw,
            checksum=checksum,
            checksum_type="sha256",
            checksum_file=sha_file,
        )

    def ensure_image(
        self,
        *,
        qcow2_name: str,
        raw_name: str,
    ) -> ImageMetadata:
        """
        High-level semantic wrapper:
        Ensures the image exists and returns metadata.
        """
        return self.download_image(
            image_name=qcow2_name,
            raw_image_name=raw_name,
        )