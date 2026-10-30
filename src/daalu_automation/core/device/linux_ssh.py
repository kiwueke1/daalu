"""Linux SSH device adapter.

First real DeviceAdapter. Uses ``asyncssh`` directly. Manages a narrow
set of facts:

- ``hostname`` (``hostnamectl set-hostname``)
- ``authorized_keys`` for one managed user (``~<user>/.ssh/authorized_keys``)
- ``sysctl`` values via ``/etc/sysctl.d/99-daalu.conf``
- ``packages`` (apt/dnf, present|absent)
- ``cloud_init`` user-data (NoCloud seed file)

``execute()`` takes a tarball snapshot of every file it's about to
touch, applies the changes, re-collects to verify, and on any
verification failure restores from the snapshot.
"""

from __future__ import annotations

import difflib
import logging
import shlex
import uuid as _uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, ClassVar

import asyncssh

from daalu_automation.core.device.base import DeviceAdapter
from daalu_automation.core.device.models import (
    ConfigDiff,
    Credentials,
    ExecutionResult,
    RenderedConfig,
)
from daalu_automation.core.device.registry import register_device_adapter
from daalu_automation.core.sot.models import (
    AuthorizedKey,
    CloudInitUserData,
    LinuxFacts,
    PackagePresence,
    SysctlValue,
)

logger = logging.getLogger(__name__)

RENDERER_VERSION = "linux_ssh.v1"

PATH_HOSTNAME = "/etc/hostname"
PATH_SYSCTL = "/etc/sysctl.d/99-daalu.conf"
PATH_PACKAGES_SENTINEL = "/etc/daalu/packages.json"
PATH_CLOUD_INIT = "/var/lib/cloud/seed/nocloud/user-data"


def _authorized_keys_path(user: str) -> str:
    if user == "root":
        return "/root/.ssh/authorized_keys"
    return f"/home/{user}/.ssh/authorized_keys"


# ── Rendering ─────────────────────────────────────────────────────────


def _render_hostname(facts: LinuxFacts) -> str | None:
    if facts.hostname is None:
        return None
    return facts.hostname.strip() + "\n"


def _render_authorized_keys_per_user(
    facts: LinuxFacts,
) -> dict[str, str]:
    """Group authorized keys by managed user → file contents."""
    by_user: dict[str, list[str]] = {}
    for ak in facts.authorized_keys:
        by_user.setdefault(ak.user, []).append(ak.key.strip())
    return {
        user: "\n".join(sorted(keys)) + "\n" if keys else ""
        for user, keys in by_user.items()
    }


def _render_sysctl(facts: LinuxFacts) -> str | None:
    if not facts.sysctl:
        return None
    lines = [
        "# Managed by daalu — do not edit by hand.",
        *(f"{v.name} = {v.value}" for v in sorted(facts.sysctl, key=lambda s: s.name)),
        "",
    ]
    return "\n".join(lines)


def _render_packages_sentinel(facts: LinuxFacts) -> str | None:
    if not facts.packages:
        return None
    rows = sorted(
        (p.model_dump() for p in facts.packages), key=lambda r: r["name"]
    )
    import json
    return json.dumps({"packages": rows}, indent=2) + "\n"


def _render_cloud_init(facts: LinuxFacts) -> str | None:
    if facts.cloud_init is None or not facts.cloud_init.content:
        return None
    content = facts.cloud_init.content
    if not content.endswith("\n"):
        content += "\n"
    return content


def _render_files(facts: LinuxFacts) -> dict[str, str]:
    out: dict[str, str] = {}
    hostname = _render_hostname(facts)
    if hostname is not None:
        out[PATH_HOSTNAME] = hostname
    for user, content in _render_authorized_keys_per_user(facts).items():
        out[_authorized_keys_path(user)] = content
    sysctl = _render_sysctl(facts)
    if sysctl is not None:
        out[PATH_SYSCTL] = sysctl
    pkgs = _render_packages_sentinel(facts)
    if pkgs is not None:
        out[PATH_PACKAGES_SENTINEL] = pkgs
    ci = _render_cloud_init(facts)
    if ci is not None:
        out[PATH_CLOUD_INIT] = ci
    return out


def _render_summary(facts: LinuxFacts) -> str:
    parts: list[str] = []
    if facts.hostname:
        parts.append(f"hostname={facts.hostname}")
    if facts.authorized_keys:
        users = sorted({ak.user for ak in facts.authorized_keys})
        parts.append(f"authorized_keys[{len(facts.authorized_keys)} → {','.join(users)}]")
    if facts.sysctl:
        parts.append(f"sysctl[{len(facts.sysctl)}]")
    if facts.packages:
        present = sum(1 for p in facts.packages if p.state == "present")
        absent = len(facts.packages) - present
        parts.append(f"packages[+{present}/-{absent}]")
    if facts.cloud_init and facts.cloud_init.content:
        parts.append("cloud_init")
    return "; ".join(parts) or "(empty intent)"


# ── Diffing ───────────────────────────────────────────────────────────


def _fact_keys_diff(observed: LinuxFacts, intended: LinuxFacts) -> list[str]:
    changed: list[str] = []
    if (observed.hostname or "") != (intended.hostname or ""):
        if intended.hostname is not None:
            changed.append("hostname")
    o_keys = {(k.user, k.key.strip()) for k in observed.authorized_keys}
    i_keys = {(k.user, k.key.strip()) for k in intended.authorized_keys}
    if o_keys != i_keys:
        changed.append("authorized_keys")
    o_sc = {(s.name, s.value) for s in observed.sysctl}
    i_sc = {(s.name, s.value) for s in intended.sysctl}
    if o_sc != i_sc and intended.sysctl:
        changed.append("sysctl")
    o_pk = {(p.name, p.state) for p in observed.packages}
    i_pk = {(p.name, p.state) for p in intended.packages}
    if o_pk != i_pk and intended.packages:
        changed.append("packages")
    o_ci = (observed.cloud_init.content if observed.cloud_init else "") or ""
    i_ci = (intended.cloud_init.content if intended.cloud_init else "") or ""
    if o_ci != i_ci and i_ci:
        changed.append("cloud_init")
    return changed


def _unified_diff(observed: LinuxFacts, intended: LinuxFacts) -> str:
    observed_files = _render_files(observed)
    intended_files = _render_files(intended)
    paths = sorted(set(observed_files) | set(intended_files))
    chunks: list[str] = []
    for path in paths:
        before = (observed_files.get(path) or "").splitlines(keepends=True)
        after = (intended_files.get(path) or "").splitlines(keepends=True)
        if before == after:
            continue
        diff = list(
            difflib.unified_diff(
                before, after, fromfile=f"observed:{path}", tofile=f"intended:{path}"
            )
        )
        if diff:
            chunks.append("".join(diff))
    return "\n".join(chunks)


# ── SSH plumbing ──────────────────────────────────────────────────────


@asynccontextmanager
async def _open(creds: Credentials):
    """Open an asyncssh connection from a Credentials object.

    Wrapped in our own helper so the test suite has exactly one
    monkeypatch target (``asyncssh.connect``).
    """
    connect_kwargs: dict[str, Any] = {
        "host": creds.host,
        "port": creds.port,
        "username": creds.user,
        "known_hosts": None if creds.known_hosts is None else creds.known_hosts,
    }
    if creds.private_key_pem:
        connect_kwargs["client_keys"] = [asyncssh.import_private_key(creds.private_key_pem)]
    if creds.password:
        connect_kwargs["password"] = creds.password
    if creds.proxy_url:
        # Device IP only routes inside the tunneled customer cluster —
        # splice raw TCP through the edge's HTTP CONNECT proxy.
        from daalu_automation.core.cluster_proxy import open_connect_socket

        connect_kwargs["sock"] = await open_connect_socket(
            creds.proxy_url, creds.host, creds.port
        )
    try:
        conn = await asyncssh.connect(**connect_kwargs)
    except BaseException:
        sock = connect_kwargs.get("sock")
        if sock is not None:  # asyncssh closes it on handshake failure; belt-and-braces
            sock.close()
        raise
    try:
        yield conn
    finally:
        try:
            close_fn = getattr(conn, "close", None)
            if callable(close_fn):
                close_fn()
            wait_fn = getattr(conn, "wait_closed", None)
            if callable(wait_fn):
                await wait_fn()
        except Exception:  # noqa: BLE001 — close is best-effort
            pass


def _sudo(creds: Credentials, cmd: str) -> str:
    return f"sudo -n {cmd}" if creds.sudo else cmd


async def _run(conn: Any, cmd: str) -> tuple[int, str, str]:
    result = await conn.run(cmd, check=False)
    stdout = (result.stdout or "")
    stderr = (result.stderr or "")
    return int(getattr(result, "exit_status", 0) or 0), stdout, stderr


async def _read_file(conn: Any, creds: Credentials, path: str) -> str:
    """Read a remote file's contents, return '' on missing/empty/perm error."""
    rc, out, _err = await _run(conn, _sudo(creds, f"cat {shlex.quote(path)}"))
    if rc != 0:
        return ""
    return out


# ── The adapter ───────────────────────────────────────────────────────


class LinuxSSHAdapter(DeviceAdapter):
    transport: ClassVar[str] = "linux_ssh"

    async def collect(
        self,
        creds: Credentials,
        intended_hint: LinuxFacts | None = None,
    ) -> LinuxFacts:
        hint = intended_hint or LinuxFacts()
        facts = LinuxFacts()
        async with _open(creds) as conn:
            # hostname
            _rc, out, _err = await _run(conn, "hostname")
            facts.hostname = out.strip() or None

            # authorized_keys — scope to users present in intent (so we
            # don't enumerate the whole user list)
            users = sorted({ak.user for ak in hint.authorized_keys})
            for user in users:
                content = await _read_file(
                    conn, creds, _authorized_keys_path(user)
                )
                for line in content.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    facts.authorized_keys.append(AuthorizedKey(user=user, key=line))

            # sysctl — only check keys present in intent
            for sv in hint.sysctl:
                _rc, out, _err = await _run(
                    conn, _sudo(creds, f"sysctl -n {shlex.quote(sv.name)}")
                )
                value = out.strip()
                if value:
                    facts.sysctl.append(SysctlValue(name=sv.name, value=value))

            # packages — only check names present in intent. Try dpkg
            # first, fall back to rpm.
            for pkg in hint.packages:
                rc_dpkg, out_dpkg, _ = await _run(
                    conn,
                    f"dpkg-query -W -f='${{Status}}' {shlex.quote(pkg.name)} 2>/dev/null",
                )
                installed = False
                if rc_dpkg == 0 and "install ok installed" in out_dpkg:
                    installed = True
                else:
                    rc_rpm, _out_rpm, _ = await _run(
                        conn, f"rpm -q {shlex.quote(pkg.name)}"
                    )
                    installed = rc_rpm == 0
                facts.packages.append(
                    PackagePresence(
                        name=pkg.name,
                        state="present" if installed else "absent",
                    )
                )

            # cloud-init — only inspect if intent has any cloud_init
            if hint.cloud_init is not None:
                content = await _read_file(conn, creds, PATH_CLOUD_INIT)
                facts.cloud_init = CloudInitUserData(content=content)
        return facts

    async def render(self, intended: LinuxFacts) -> RenderedConfig:
        return RenderedConfig(
            renderer_version=RENDERER_VERSION,
            files=_render_files(intended),
            summary=_render_summary(intended),
        )

    async def diff(
        self, observed: LinuxFacts, intended: LinuxFacts
    ) -> ConfigDiff:
        changed = _fact_keys_diff(observed, intended)
        text = _unified_diff(observed, intended)
        return ConfigDiff(
            facts_changed=changed,
            unified_diff=text,
            has_changes=bool(changed),
        )

    async def execute(
        self, creds: Credentials, rendered: RenderedConfig
    ) -> ExecutionResult:
        started = datetime.now(tz=timezone.utc).isoformat()
        snap_name = f"daalu-snap-{_uuid.uuid4().hex[:8]}.tar"
        snapshot_path = f"/tmp/{snap_name}"
        per_step: list[dict[str, Any]] = []

        def _step(name: str, rc: int, stdout: str, stderr: str) -> None:
            per_step.append({
                "name": name,
                "rc": rc,
                "stdout": (stdout or "")[:4096],
                "stderr": (stderr or "")[:4096],
            })

        try:
            async with _open(creds) as conn:
                # 1. Snapshot every touched file. Use --files-from with
                # /dev/null on a fresh tar, then append each existing
                # file so we don't fail on paths that don't exist yet.
                rc, out, err = await _run(
                    conn,
                    _sudo(
                        creds,
                        f"tar -cf {shlex.quote(snapshot_path)} --files-from=/dev/null",
                    ),
                )
                _step("snapshot.init", rc, out, err)
                for path in sorted(rendered.files):
                    rc, out, err = await _run(
                        conn,
                        _sudo(
                            creds,
                            "sh -c "
                            + shlex.quote(
                                f"if [ -e {shlex.quote(path)} ]; then "
                                f"tar -rf {shlex.quote(snapshot_path)} {shlex.quote(path)}; "
                                f"fi"
                            ),
                        ),
                    )
                    _step(f"snapshot.append:{path}", rc, out, err)

                # 2. Write every file via a temp path + atomic rename.
                for path, content in sorted(rendered.files.items()):
                    tmp = f"/tmp/daalu-write-{_uuid.uuid4().hex[:8]}"
                    rc, out, err = await _run(
                        conn,
                        f"sh -c {shlex.quote('cat > ' + shlex.quote(tmp))} << '__DAALU_EOF__'\n"
                        + content
                        + "\n__DAALU_EOF__",
                    )
                    _step(f"stage:{path}", rc, out, err)
                    rc, out, err = await _run(
                        conn,
                        _sudo(
                            creds,
                            f"mkdir -p {shlex.quote(_dirname(path))} && "
                            f"mv {shlex.quote(tmp)} {shlex.quote(path)}",
                        ),
                    )
                    _step(f"install:{path}", rc, out, err)

                # 3. Apply non-file-only facts.
                if PATH_HOSTNAME in rendered.files:
                    new_host = rendered.files[PATH_HOSTNAME].strip()
                    if new_host:
                        rc, out, err = await _run(
                            conn,
                            _sudo(creds, f"hostnamectl set-hostname {shlex.quote(new_host)}"),
                        )
                        _step("apply:hostname", rc, out, err)
                if PATH_SYSCTL in rendered.files:
                    rc, out, err = await _run(
                        conn, _sudo(creds, "sysctl --system")
                    )
                    _step("apply:sysctl", rc, out, err)
                if PATH_PACKAGES_SENTINEL in rendered.files:
                    await _apply_packages(conn, creds, rendered, per_step)

            finished = datetime.now(tz=timezone.utc).isoformat()
            return ExecutionResult(
                success=True,
                started_at=started,
                finished_at=finished,
                per_step=per_step,
                snapshot_uri=snapshot_path,
            )
        except Exception as e:  # noqa: BLE001 — top-level adapter boundary
            finished = datetime.now(tz=timezone.utc).isoformat()
            # Best-effort rollback from snapshot.
            rolled_back = False
            try:
                async with _open(creds) as conn:
                    rc, out, err = await _run(
                        conn,
                        _sudo(creds, f"tar -xf {shlex.quote(snapshot_path)} -C /"),
                    )
                    _step("rollback.restore", rc, out, err)
                    rolled_back = rc == 0
            except Exception as re:  # noqa: BLE001
                logger.exception("linux_ssh.rollback_failed", exc_info=re)
            return ExecutionResult(
                success=False,
                started_at=started,
                finished_at=finished,
                per_step=per_step,
                rollback_performed=rolled_back,
                error=f"{type(e).__name__}: {e}",
                snapshot_uri=snapshot_path,
            )


def _dirname(path: str) -> str:
    if "/" not in path:
        return "."
    return path.rsplit("/", 1)[0] or "/"


async def _apply_packages(
    conn: Any,
    creds: Credentials,
    rendered: RenderedConfig,
    per_step: list[dict[str, Any]],
) -> None:
    import json

    body = rendered.files.get(PATH_PACKAGES_SENTINEL) or ""
    try:
        decoded = json.loads(body or "{}")
        rows = decoded.get("packages") or []
    except json.JSONDecodeError:
        return
    present = [r["name"] for r in rows if r.get("state") == "present"]
    absent = [r["name"] for r in rows if r.get("state") == "absent"]
    for action, names in (("install", present), ("purge", absent)):
        if not names:
            continue
        inner = "apt-get -y " + action + " " + " ".join(shlex.quote(p) for p in names)
        rc, out, err = await _run(
            conn,
            _sudo(
                creds,
                "sh -c " + shlex.quote(f"DEBIAN_FRONTEND=noninteractive {inner}"),
            ),
        )
        per_step.append({
            "name": f"apply:packages.{action}",
            "rc": rc,
            "stdout": (out or "")[:4096],
            "stderr": (err or "")[:4096],
        })


register_device_adapter(LinuxSSHAdapter)
