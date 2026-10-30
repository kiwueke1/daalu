"""Interactive SSH terminal into a fabric device, straight from the topology UI.

A Starlette ``WebSocketRoute`` (not a FastAPI route — the WS upgrade needs the
raw ASGI surface, same reason as ``api/ide_proxy.py``) that bridges the
browser's xterm.js to an ``asyncssh`` PTY on the device.

Security posture (a browser shell into customer gear is powerful, so this is
deliberately tight):

* cookie-authenticated user, resolved from the same session JWT as the REST API;
* the tenant must have ``network_topology_enabled``;
* the user must be a **tenant admin** (``is_admin``);
* the device must resolve in *that tenant's* source of truth (cross-tenant =
  1008 close), and must have SSH-shaped credentials — NV-CM-only devices, which
  have no shell, are refused.

Wire protocol (JSON text frames, browser→server):
    {"t":"i","d":"<keystrokes>"}         input
    {"t":"r","c":<cols>,"r":<rows>}      resize
Server→browser: raw text frames of terminal output (xterm writes them verbatim).
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs

import structlog
from starlette.websockets import WebSocket

from daalu_automation.config import get_settings
from daalu_automation.database import AsyncSessionLocal

logger = structlog.get_logger(__name__)

TOPOLOGY_FLAG = "network_topology_enabled"
_TERMINAL_TRANSPORTS = {"linux_ssh", "eos", "junos", "iosxr"}


async def _authorize(device_id: str, cookie_token: str | None, env: str | None = None):
    """Resolve (creds, device_name) if the caller may open a shell here, else None.

    Bundles every gate: valid session → tenant admin → topology flag → device
    in this tenant's SoT → SSH-shaped credentials. Any miss returns ``None`` and
    the caller closes the socket with 1008 (policy violation).

    ``env`` is the NV-CM environment slug the topology view was showing, so the
    device id resolves against the same Nautobot that produced the node — ids
    are per-environment and would otherwise miss (or, worse, collide) when the
    shell is opened from a non-default environment's fabric.
    """
    if not cookie_token:
        return None
    try:
        from daalu_automation.core.auth import decode_token

        payload = decode_token(cookie_token)
    except Exception:  # noqa: BLE001
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None

    from daalu_automation.core.change_proposals import resolve_credentials
    from daalu_automation.core.sot.nautobot import NautobotSoT
    from daalu_automation.models.tenant import Tenant
    from daalu_automation.models.user import User

    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if user is None or not user.is_admin:
            return None
        tenant_id = user.tenant_id
        tenant = await db.get(Tenant, tenant_id)
        if tenant is None or not (tenant.feature_flags or {}).get(TOPOLOGY_FLAG):
            return None

        device = await NautobotSoT(env=env).get_device(db, tenant_id, device_id)
        if device is None or device.transport not in _TERMINAL_TRANSPORTS:
            return None
        try:
            creds = await resolve_credentials(db, tenant_id, device)
        except Exception:  # noqa: BLE001
            return None
        if creds.extra.get("nvcm_conn") is not None or not (
            creds.private_key_pem or creds.password
        ):
            return None
        return creds, (device.name or device_id)


async def device_terminal_ws(browser: WebSocket) -> None:
    """Bridge xterm.js ⇆ an asyncssh PTY on the device."""
    import asyncssh

    scope = browser.scope
    device_id = scope["path_params"]["device_id"]
    # Raw Starlette route, so there's no FastAPI Query dependency to lean on.
    env = parse_qs(scope.get("query_string", b"").decode()).get("env", [None])[0]
    settings = get_settings()

    # Pull the session cookie out of the raw headers (same as ide_proxy_ws).
    headers = dict(scope.get("headers", []))
    cookie_header = headers.get(b"cookie", b"").decode()
    cookie_token: str | None = None
    for part in cookie_header.split(";"):
        part = part.strip()
        prefix = f"{settings.auth_cookie_name}="
        if part.startswith(prefix):
            cookie_token = part[len(prefix):]
            break

    authorized = await _authorize(device_id, cookie_token, env)
    if authorized is None:
        await browser.close(code=1008)
        return
    creds, device_name = authorized

    connect_kwargs: dict = {
        "host": creds.host,
        "port": creds.port,
        "username": creds.user,
        "known_hosts": None if creds.known_hosts is None else creds.known_hosts,
    }
    if creds.private_key_pem:
        connect_kwargs["client_keys"] = [asyncssh.import_private_key(creds.private_key_pem)]
    if creds.password:
        connect_kwargs["password"] = creds.password

    try:
        if creds.proxy_url:
            # Device IP only routes inside the tunneled customer cluster —
            # splice raw TCP through the edge's HTTP CONNECT proxy.
            from daalu_automation.core.cluster_proxy import open_connect_socket

            connect_kwargs["sock"] = await open_connect_socket(
                creds.proxy_url, creds.host, creds.port
            )
        conn = await asyncio.wait_for(asyncssh.connect(**connect_kwargs), timeout=15.0)
    except Exception as e:  # noqa: BLE001
        sock = connect_kwargs.get("sock")
        if sock is not None:  # asyncssh closes it on handshake failure; belt-and-braces
            sock.close()
        logger.warning("device_terminal.connect_failed", device=device_id, error=str(e))
        await browser.accept()
        await browser.send_text(f"\r\n\x1b[31mSSH connect failed: {e}\x1b[0m\r\n")
        await browser.close(code=1011)
        return

    await browser.accept()
    logger.info("device_terminal.open", device=device_id, name=device_name)

    try:
        proc = await conn.create_process(
            term_type="xterm-256color", term_size=(80, 24)
        )
    except Exception as e:  # noqa: BLE001
        await browser.send_text(f"\r\n\x1b[31mfailed to open shell: {e}\x1b[0m\r\n")
        conn.close()
        await browser.close(code=1011)
        return

    await browser.send_text(f"\x1b[2mconnected to {device_name} ({creds.host})\x1b[0m\r\n")

    async def device_to_browser() -> None:
        try:
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:  # EOF — remote shell exited
                    return
                await browser.send_text(chunk)
        except Exception:  # noqa: BLE001
            return

    async def browser_to_device() -> None:
        try:
            while True:
                msg = await browser.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                text = msg.get("text")
                if text is None:
                    continue
                try:
                    frame = json.loads(text)
                except (ValueError, TypeError):
                    continue
                if frame.get("t") == "i":
                    proc.stdin.write(frame.get("d", ""))
                elif frame.get("t") == "r":
                    try:
                        proc.change_terminal_size(
                            int(frame.get("c", 80)), int(frame.get("r", 24))
                        )
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            return

    try:
        done, pending = await asyncio.wait(
            {
                asyncio.create_task(device_to_browser()),
                asyncio.create_task(browser_to_device()),
            },
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    finally:
        try:
            proc.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await browser.close()
        except Exception:  # noqa: BLE001
            pass
