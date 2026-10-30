"""deploy/edge/proxy.py — CONNECT tunneling and HTTP forwarding.

The edge proxy is a standalone script (not part of the package), loaded
here by path. The CONNECT path carries SSH (LLDP probe, device
terminal) into a tunneled customer cluster, so these tests pin the
regression that motivated the middleware dispatch: aiohttp's router can
never match ``CONNECT host:port`` (the parser drops the authority into
``raw_path``, leaving no URL path), so route-based dispatch 404s every
CONNECT before handler code runs.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys

import pytest

pytest.importorskip("aiohttp")

_PROXY_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "deploy" / "edge" / "proxy.py"
)


def _load_proxy():
    spec = importlib.util.spec_from_file_location("edge_proxy_under_test", _PROXY_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def edge():
    mod = _load_proxy()
    yield mod
    sys.modules.pop("edge_proxy_under_test", None)


async def _start_proxy(edge):
    """The real app (middleware + routes) on an ephemeral loopback port."""
    from aiohttp import web

    runner = web.AppRunner(edge._make_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = next(a[1] for a in runner.addresses)
    return runner, port


@pytest.mark.asyncio(loop_scope="session")
async def test_connect_splices_raw_tcp_upstream_speaks_first(edge, monkeypatch):
    """CONNECT to an allowed host splices opaque TCP both ways.

    The upstream speaks first (an SSH banner) — the proxy must not eat
    or delay those bytes, and client→upstream must flow after the
    protocol swap.
    """
    import re

    monkeypatch.setattr(edge, "ALLOWED_HOSTS_RE", re.compile(r"^127\.0\.0\.1$"))

    async def upstream_handler(r, w):
        w.write(b"SSH-2.0-banner\r\n")
        await w.drain()
        data = await r.read(100)
        w.write(b"echo:" + data)
        await w.drain()
        w.close()

    upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    runner, proxy_port = await _start_proxy(edge)
    try:
        r, w = await asyncio.open_connection("127.0.0.1", proxy_port)
        w.write(
            f"CONNECT 127.0.0.1:{upstream_port} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{upstream_port}\r\n\r\n".encode()
        )
        await w.drain()
        assert b"200" in await r.readline()
        assert await r.readline() == b"\r\n"  # end of proxy reply
        assert await r.readline() == b"SSH-2.0-banner\r\n"
        w.write(b"ping")
        await w.drain()
        assert await r.read(100) == b"echo:ping"
        w.close()
    finally:
        upstream.close()
        await runner.cleanup()


@pytest.mark.asyncio(loop_scope="session")
async def test_connect_rejected_by_allowlist(edge):
    # Default allowlist: 8.8.8.8 is public → refused before any dial.
    runner, proxy_port = await _start_proxy(edge)
    try:
        r, w = await asyncio.open_connection("127.0.0.1", proxy_port)
        # Host header is mandatory in HTTP/1.1 — aiohttp >= 3.14 rejects
        # its absence with 400 before the handler ever runs.
        w.write(b"CONNECT 8.8.8.8:22 HTTP/1.1\r\nHost: 8.8.8.8:22\r\n\r\n")
        await w.drain()
        assert b"403" in await r.readline()
        w.close()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio(loop_scope="session")
async def test_connect_malformed_authority(edge):
    runner, proxy_port = await _start_proxy(edge)
    try:
        r, w = await asyncio.open_connection("127.0.0.1", proxy_port)
        w.write(b"CONNECT nonsense HTTP/1.1\r\nHost: nonsense\r\n\r\n")
        await w.drain()
        assert b"400" in await r.readline()
        w.close()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio(loop_scope="session")
async def test_http_forward_still_works(edge, monkeypatch):
    """Absolute-form HTTP requests keep flowing through the routed app."""
    import re

    from aiohttp import web

    monkeypatch.setattr(edge, "ALLOWED_HOSTS_RE", re.compile(r"^127\.0\.0\.1$"))

    async def hello(request):
        return web.Response(text="hi-from-upstream")

    app = web.Application()
    app.router.add_get("/x", hello)
    up_runner = web.AppRunner(app)
    await up_runner.setup()
    up_site = web.TCPSite(up_runner, "127.0.0.1", 0)
    await up_site.start()
    upstream_port = next(a[1] for a in up_runner.addresses)

    runner, proxy_port = await _start_proxy(edge)
    try:
        r, w = await asyncio.open_connection("127.0.0.1", proxy_port)
        w.write(
            f"GET http://127.0.0.1:{upstream_port}/x HTTP/1.1\r\n"
            f"Host: 127.0.0.1\r\nConnection: close\r\n\r\n".encode()
        )
        await w.drain()
        resp = await r.read(-1)
        assert b"200 OK" in resp
        assert b"hi-from-upstream" in resp
        w.close()
    finally:
        await runner.cleanup()
        await up_runner.cleanup()
