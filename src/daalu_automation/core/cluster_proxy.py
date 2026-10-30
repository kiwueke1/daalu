"""Cross-cluster HTTP forward-proxy lookup.

When an ``Integration`` row has a ``cluster_tunnel_id``, its URL is meant
to be dialed *inside* the named workload cluster — typically because it
points at an in-cluster Service (``*.svc.cluster.local``) that the hub
can't resolve. The daalu-edge pod in that cluster runs a tiny HTTP
forward proxy on the tunnel IP (see ``deploy/edge/proxy.py``); this
module resolves a ``cluster_tunnel_id`` to the proxy URL the hub's
``httpx.AsyncClient(proxy=...)`` should use.

A ``None`` return means: dial the URL directly (current behavior for
publicly-reachable URLs, or for integrations that simply have no cluster
attached).
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.models import ClusterTunnel, ClusterTunnelStatus

# Port the edge-side forward proxy listens on (bound to the wg0 tunnel
# IP only). Kept as a module constant so the hub and the Helm chart's
# `edgeProxy.listenPort` don't drift — change them together.
EDGE_PROXY_PORT = 8888


async def get_proxy_url(
    db: AsyncSession, cluster_tunnel_id: uuid.UUID | None
) -> str | None:
    """Return ``http://<tunnel_ip>:8888`` or ``None``.

    Returns ``None`` when:
    - no cluster is attached to the integration,
    - the cluster_tunnel_id doesn't exist,
    - the tunnel is in a state other than ``connected`` (no point dialing
      a dead tunnel — fail fast at the call site with a clearer error).
    """
    if cluster_tunnel_id is None:
        return None
    row = await db.get(ClusterTunnel, cluster_tunnel_id)
    if row is None or row.status != ClusterTunnelStatus.connected:
        return None
    return f"http://{row.tunnel_ip}:{EDGE_PROXY_PORT}"


async def open_connect_socket(
    proxy_url: str, host: str, port: int, *, timeout: float = 10.0
) -> socket.socket:
    """Open a raw TCP socket to ``host:port`` through an HTTP CONNECT proxy.

    This is how non-HTTP protocols (SSH) reach addresses that only route
    inside a tunneled customer cluster: the edge forward proxy
    (``deploy/edge/proxy.py``) splices ``CONNECT host:port`` to a raw TCP
    stream, and the returned socket is handed to e.g.
    ``asyncssh.connect(sock=...)``.

    The reply headers are read **byte-wise**: the upstream (an SSH server)
    speaks first, so its banner can arrive immediately after the proxy's
    ``\\r\\n\\r\\n`` — a buffered read would swallow it.

    Raises ``ConnectionError`` on a non-200 proxy reply (e.g. the edge's
    403 ``host not allowed``) and ``asyncio.TimeoutError`` on timeout.
    The socket is always closed on failure.
    """
    p = urlparse(proxy_url)
    if not p.hostname or not p.port:
        raise ValueError(f"malformed proxy url: {proxy_url!r}")
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)

    async def _handshake() -> None:
        await loop.sock_connect(sock, (p.hostname, p.port))
        req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n"
        await loop.sock_sendall(sock, req.encode("ascii"))
        buf = b""
        while not buf.endswith(b"\r\n\r\n"):
            b1 = await loop.sock_recv(sock, 1)
            if not b1:
                raise ConnectionError("proxy closed the connection mid-CONNECT")
            buf += b1
            if len(buf) > 8192:
                raise ConnectionError("oversized CONNECT response from proxy")
        status = buf.split(b"\r\n", 1)[0]
        parts = status.split(b" ", 2)
        if len(parts) < 2 or parts[1] != b"200":
            raise ConnectionError(
                f"proxy CONNECT {host}:{port} failed: "
                f"{status.decode('ascii', errors='replace')[:120]}"
            )

    try:
        await asyncio.wait_for(_handshake(), timeout=timeout)
    except BaseException:
        sock.close()
        raise
    return sock
