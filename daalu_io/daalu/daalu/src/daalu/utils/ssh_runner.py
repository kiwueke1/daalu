# src/daalu/utils/ssh_runner.py

from __future__ import annotations

from pathlib import Path
import paramiko
import os
from typing import Optional


class SSHCommandError(RuntimeError):
    pass


class SSHRunner:
    def __init__(self, client: paramiko.SSHClient):
        self.client = client

    def run(
        self,
        cmd: str,
        *,
        sudo: bool = False,
        timeout: Optional[int] = None,
    ) -> tuple[int, str, str]:
        #print("=== SSH DEBUG ===")
        #print("Command:", cmd)
        #print("=================")
        if sudo:
            cmd = f"sudo -H -E bash -c '{cmd}'"

        stdin, stdout, stderr = self.client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode()
        err = stderr.read().decode()
        rc = stdout.channel.recv_exit_status()
        return rc, out, err

    def put_text(self, content: str, remote_path: str, *, sudo: bool = False) -> None:
        if sudo:
            tmp = f"/tmp/.daalu.tmp.{os.getpid()}"
            self.put_text(content, tmp)
            self.run(f"mv {tmp} {remote_path}", sudo=True)
            return

        sftp = self.client.open_sftp()
        try:
            with sftp.open(remote_path, "w") as f:
                f.write(content)
        finally:
            sftp.close()

    def put_file(self, local_path: str | Path, remote_path: str, *, sudo: bool = False) -> None:
        if sudo:
            tmp = f"/tmp/.daalu.upload.{os.getpid()}"
            self.put_file(local_path, tmp)
            self.run(f"mv {tmp} {remote_path}", sudo=True)
            return

        sftp = self.client.open_sftp()
        try:
            sftp.put(str(local_path), str(remote_path))
        finally:
            sftp.close()

    def close(self) -> None:
        self.client.close()

    def put_dir(self, local_dir: Path, remote_dir: Path, *, release_name: str | None = None, sudo: bool = False,) -> None:
        """
        Recursively upload a directory to the remote host using SFTP.
        """
        scoped_local = local_dir / release_name
        scoped_remote = remote_dir / release_name
        if sudo:
            tmp = Path(f"/tmp/.daalu.upload.{os.getpid()}")
            self.put_dir(local_dir, tmp, release_name=release_name, sudo=False)
            self.run(f"rm -rf {remote_dir} && mv {tmp} {remote_dir}", sudo=True)

            print(
                f"[ssh] Uploaded directory (sudo): "
                f"{scoped_local} → {scoped_remote}"
            )
            return

        sftp = self.client.open_sftp()
        try:
            self._put_dir_recursive(sftp, local_dir, remote_dir)
        finally:
            sftp.close()

        print(
            f"[ssh] Uploaded directory: "
            f"{local_dir} → {remote_dir}"
        )

    def _put_dir_recursive(self, sftp, local: Path, remote: Path):
        try:
            sftp.mkdir(str(remote))
        except IOError:
            pass  # already exists

        for item in local.iterdir():
            rpath = remote / item.name
            if item.is_dir():
                self._put_dir_recursive(sftp, item, rpath)
            else:
                sftp.put(str(item), str(rpath))
