"""Reverse proxy from daalu-api to the workspace pod's code-server.

Lives as a Starlette endpoint (not a FastAPI route) so the WebSocket
upgrade path is exact — FastAPI's response model conventions get in
the way otherwise.

URL shape:

  /ide/{workspace_id}/{any subpath}    → workspace pod :8080/{any subpath}

The pod's K8s Service name is derived from the workspace_id via
``workspace_controller.k8s.resource_names``. The proxy enforces that
the *authenticated user* owns the workspace; cross-tenant access is
a 403.

Both HTTP and WebSocket upgrades are handled. code-server uses
WebSocket for the terminal + LSP traffic.
"""

from __future__ import annotations

import httpx
import structlog
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import Response
from starlette.websockets import WebSocket

from daalu_automation.config import get_settings
from daalu_automation.database import AsyncSessionLocal
from daalu_automation.models.workspace import Workspace
from daalu_automation.workspace_controller.k8s import WORKSPACE_NAMESPACE, resource_names

logger = structlog.get_logger(__name__)


async def _check_authorisation(
    *, workspace_id: str, user_cookie_token: str | None
) -> Workspace | None:
    """Confirm the cookie-authenticated user owns the workspace.

    Returns the row on success; ``None`` if anyone but the owner is
    asking. We deliberately don't reveal "workspace exists but you're
    not the owner" vs "no such workspace" — both yield 404 to the
    proxy caller.
    """
    if not user_cookie_token:
        return None
    # Decode the user JWT to extract the user id. The full ``current_user``
    # dependency would re-query the DB; we only need the user id to
    # match against the workspace row.
    try:
        from daalu_automation.core.auth import decode_token

        payload = decode_token(user_cookie_token)
    except Exception:  # noqa: BLE001
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    async with AsyncSessionLocal() as db:
        stmt = select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.user_id == user_id,
            Workspace.state != "destroyed",
        )
        return (await db.execute(stmt)).scalar_one_or_none()


def _pod_url(workspace_id: str, subpath: str) -> str:
    """Resolve the workspace pod's in-cluster URL.

    We hit the Service in the workspaces namespace by name. The
    workspace-controller creates one Service per Deployment with the
    same ``ws-<short>`` name.
    """
    dep_name, _ = resource_names(workspace_id)
    return f"http://{dep_name}.{WORKSPACE_NAMESPACE}.svc.cluster.local:8080{subpath}"


async def ide_proxy_http(request: Request) -> Response:
    """Proxy a plain HTTP request to the workspace pod.

    Mounted via Starlette ``Route("/ide/{workspace_id:str}/{path:path}")``.
    """
    workspace_id = request.path_params["workspace_id"]
    subpath = "/" + request.path_params.get("path", "")
    settings = get_settings()
    cookie_token = request.cookies.get(settings.auth_cookie_name)
    ws = await _check_authorisation(
        workspace_id=workspace_id, user_cookie_token=cookie_token
    )
    if ws is None:
        return Response("workspace not found", status_code=404)

    url = _pod_url(workspace_id, subpath)
    if request.url.query:
        url = url + "?" + request.url.query
    method = request.method
    body = await request.body()
    # Strip cookies before forwarding — code-server should not see
    # the daalu session cookie. ``host``, ``content-length`` get
    # rewritten by httpx; pass everything else.
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "cookie", "content-length", "accept-encoding")
    }
    # Force an uncompressed upstream response. code-server honours the
    # browser's Accept-Encoding and replies ``Content-Encoding: br``/``zstd``,
    # but httpx in this image has no brotli/zstd codec, so ``resp.content``
    # would be the raw compressed bytes — which we'd then forward with the
    # encoding header stripped, and the browser would render as garbage.
    # The in-cluster hop doesn't need compression; ask for identity.
    fwd_headers["accept-encoding"] = "identity"
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
            resp = await client.request(method, url, headers=fwd_headers, content=body)
    except httpx.HTTPError as e:
        logger.warning("ide_proxy.transport_error", error=str(e), workspace_id=workspace_id)
        return Response("workspace unreachable", status_code=502)
    # Update last_heartbeat_at so the reaper doesn't kill the pod
    # while it's actively in use. Best-effort; failure must not
    # block the proxy response.
    from datetime import datetime, timezone
    try:
        async with AsyncSessionLocal() as db:
            ws.last_heartbeat_at = datetime.now(tz=timezone.utc)
            db.add(ws)
            await db.commit()
    except Exception:  # noqa: BLE001
        pass
    # Drop hop-by-hop + length/encoding headers; Starlette recomputes
    # Content-Length from the actual body we return.
    out_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in (
            "content-encoding", "transfer-encoding", "connection", "content-length"
        )
    }
    return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)


async def ide_proxy_ws(browser: WebSocket) -> None:
    """Bidirectional WebSocket bridge between the browser and the pod.

    code-server uses WebSockets for the terminal, file watcher, and LSP
    messages — long-polling fallback exists but is slow + flaky. This
    handler:

    1. Validates the cookie-authenticated user owns the workspace.
    2. Opens a WebSocket to the pod's :8080 with the same subpath and
       query string, forwarding only safe headers (no daalu cookie).
    3. Pumps frames in both directions until either side closes.

    The negotiated subprotocol on the browser side is propagated to the
    pod connection so code-server can use it. Binary and text frames
    are passed through unchanged.

    Registered as a Starlette ``WebSocketRoute`` with a *function*
    endpoint, so Starlette calls it with a single ready ``WebSocket`` —
    NOT a raw ``(scope, receive, send)`` ASGI triple. Getting that
    signature wrong makes the upgrade raise before the handshake, which
    the browser sees as a 1006 abnormal close.
    """
    import asyncio

    scope = browser.scope

    # ``websockets`` ships with uvicorn[standard]; the connect API lives
    # under .client.connect in 13.x but the top-level re-export is
    # stable across the versions we pin (>=11). Import locally so the
    # rest of daalu-api can start even if the lib is missing in dev.
    try:
        from websockets.asyncio.client import connect as ws_connect
    except ImportError:  # pragma: no cover — uvicorn[standard] provides it
        from websockets.client import connect as ws_connect  # type: ignore[no-redef]

    workspace_id = scope["path_params"]["workspace_id"]
    subpath = "/" + scope["path_params"].get("path", "")
    settings = get_settings()
    headers = dict(scope.get("headers", []))
    cookie_header = headers.get(b"cookie", b"").decode()
    cookie_token: str | None = None
    for part in cookie_header.split(";"):
        part = part.strip()
        prefix = f"{settings.auth_cookie_name}="
        if part.startswith(prefix):
            cookie_token = part[len(prefix):]
            break

    ws_row = await _check_authorisation(
        workspace_id=workspace_id, user_cookie_token=cookie_token
    )
    if ws_row is None:
        await browser.close(code=1008)
        return

    # Build the upstream URL — same subpath, preserve query string.
    dep_name, _ = resource_names(workspace_id)
    query = scope.get("query_string", b"").decode()
    upstream_url = (
        f"ws://{dep_name}.{WORKSPACE_NAMESPACE}.svc.cluster.local:8080"
        f"{subpath}"
        + (f"?{query}" if query else "")
    )

    # Forward the browser's chosen subprotocols so code-server can pick.
    subprotocols = browser.scope.get("subprotocols", []) or []

    try:
        upstream = await ws_connect(
            upstream_url,
            subprotocols=subprotocols or None,
            # Forward only the WS handshake headers code-server might
            # care about; strip Authorization / cookies. The pod has
            # no concept of the daalu session cookie.
            open_timeout=15.0,
            ping_interval=20.0,
            ping_timeout=20.0,
            max_size=None,  # let code-server set its own message size
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "ide_proxy.ws.upstream_connect_failed",
            workspace_id=workspace_id,
            error=str(e),
        )
        await browser.close(code=1011)
        return

    # accept() must happen *after* we know the upstream is up, so a
    # connect failure produces a clean close on the browser side rather
    # than an accepted-then-immediately-closed flicker.
    await browser.accept(subprotocol=upstream.subprotocol or None)

    async def browser_to_pod() -> None:
        try:
            while True:
                msg = await browser.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if "bytes" in msg and msg["bytes"] is not None:
                    await upstream.send(msg["bytes"])
                elif "text" in msg and msg["text"] is not None:
                    await upstream.send(msg["text"])
        except Exception:  # noqa: BLE001
            return

    async def pod_to_browser() -> None:
        try:
            async for frame in upstream:
                if isinstance(frame, bytes):
                    await browser.send_bytes(frame)
                else:
                    await browser.send_text(frame)
        except Exception:  # noqa: BLE001
            return

    try:
        # Run both pumps; finish when either direction closes. The
        # other side gets cancelled in the cleanup block.
        done, pending = await asyncio.wait(
            {
                asyncio.create_task(browser_to_pod()),
                asyncio.create_task(pod_to_browser()),
            },
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    finally:
        try:
            await upstream.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await browser.close()
        except Exception:  # noqa: BLE001
            pass
