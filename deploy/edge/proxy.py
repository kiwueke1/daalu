#!/usr/bin/env python3
"""daalu-edge HTTP forward proxy.

Runs as a sidecar in the daalu-edge pod. Listens **only on the wg0
tunnel IP** so traffic must arrive over the WireGuard mesh — that
mesh's per-peer cryptography is the auth boundary (matches the
apiserver-proxy socat sidecar's trust model). Anything that lands here
is, by construction, the operator's hub talking to this tenant's edge.

Forwards in two modes:

* **Plain HTTP** (``GET http://host/path``): aiohttp's stock request
  parser hands us the absolute URL the proxy client put on the request
  line. We re-issue it from inside the cluster, where
  ``*.svc.cluster.local`` resolves via kube-dns.

* **HTTPS via CONNECT** (``CONNECT host:443``): open a raw socket to
  ``host:port``, send ``HTTP/1.1 200 Connection established``, then
  splice the two sockets bidirectionally until either side closes.
  TLS is end-to-end; we never see plaintext.

Optional ``ALLOWED_HOSTS`` env var (regex). Defaults to the in-cluster
DNS suffix ``\\.svc(\\.cluster\\.local)?$`` plus IPv4 literals in
RFC1918 ranges — block public dial-outs so a compromised hub can't
turn the edge into an exfiltration relay. Set to ``.*`` to disable.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import sys
from typing import Final

from aiohttp import ClientSession, ClientTimeout, web

LOG = logging.getLogger("edge-proxy")

LISTEN_HOST: Final = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT: Final = int(os.environ.get("LISTEN_PORT", "8888"))
UPSTREAM_TIMEOUT: Final = ClientTimeout(total=60, connect=10)

_DEFAULT_ALLOWED = (
    r"(\.svc(\.cluster\.local)?$)"
    r"|(^10\.\d+\.\d+\.\d+$)"
    r"|(^192\.168\.\d+\.\d+$)"
    r"|(^172\.(1[6-9]|2\d|3[01])\.\d+\.\d+$)"
)
ALLOWED_HOSTS_RE: Final = re.compile(
    os.environ.get("ALLOWED_HOSTS", _DEFAULT_ALLOWED)
)


def _host_allowed(host: str) -> bool:
    return bool(ALLOWED_HOSTS_RE.search(host))


# ── Plain HTTP forward ──────────────────────────────────────────────────


async def http_handler(request: web.Request) -> web.StreamResponse:
    """Forward an absolute-form HTTP request.

    A proper proxy client (httpx, curl -x, …) sends the absolute URL on
    the request line; aiohttp parses it and exposes it as
    ``request.url`` with scheme+host+path intact.
    """
    target = request.url
    if not target.host or target.scheme not in ("http", "https"):
        return web.Response(status=400, text="absolute http(s):// URL required")
    if not _host_allowed(target.host):
        LOG.warning("rejected http target %s (not in ALLOWED_HOSTS)", target.host)
        return web.Response(status=403, text=f"host not allowed: {target.host}")

    body = await request.read() if request.body_exists else None
    # Strip hop-by-hop headers; aiohttp/httpx will reset Host correctly.
    hop = {
        "host", "connection", "keep-alive", "proxy-authenticate",
        "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
        # Consumed below — never forwarded upstream.
        "x-daalu-forward-host",
    }
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in hop}

    # Host preservation for the hub tool-proxy. The hub forwards a tenant
    # tool-UI request by targeting the in-cluster gateway via its
    # svc/RFC1918 address (so the allowlist + kube-dns resolve it here),
    # but the gateway host-routes + runs OIDC on the *public* hostname. The
    # hub passes that public host in ``X-Daalu-Forward-Host``; honor it as
    # the upstream Host instead of letting aiohttp reset it to the svc name.
    forward_host = request.headers.get("X-Daalu-Forward-Host")
    if forward_host:
        fwd_headers["Host"] = forward_host

    async with ClientSession(timeout=UPSTREAM_TIMEOUT) as sess:
        async with sess.request(
            request.method, str(target), headers=fwd_headers, data=body,
            allow_redirects=False,
        ) as upstream:
            # aiohttp transparently decompresses the upstream body, so the
            # upstream Content-Encoding is no longer accurate — and crucially
            # neither is Content-Length (it counts the *compressed* bytes). If
            # we forward the compressed length while streaming the larger
            # decompressed body, the client reads only Content-Length bytes and
            # truncates the response mid-JSON (e.g. gzipped Loki/Prometheus
            # query results cut off at a few hundred bytes). Drop both and let
            # aiohttp chunk the re-framed body.
            resp = web.StreamResponse(status=upstream.status, headers={
                k: v for k, v in upstream.headers.items()
                if k.lower() not in hop
                and k.lower() not in ("content-encoding", "content-length")
            })
            await resp.prepare(request)
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await resp.write(chunk)
            await resp.write_eof()
            return resp


# ── HTTPS CONNECT tunnel ────────────────────────────────────────────────


async def _splice(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(64 * 1024)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def connect_handler(request: web.Request) -> web.StreamResponse:
    """Handle CONNECT host:port — splice raw TCP through the tunnel.

    We hijack the underlying transport rather than letting aiohttp send
    an HTTP response (CONNECT replies are sent on the raw socket and
    everything after is opaque TCP).

    NB: suited to server-speaks-first protocols (SSH — the hub's
    tunnel-aware device dial). A client that pipelines bytes immediately
    after its CONNECT header could lose them to aiohttp's parser buffer
    before we swap protocols below; SSH clients wait for the server
    banner, so the window is moot here.
    """
    # aiohttp puts a CONNECT request's authority-form target in
    # ``raw_path`` (``path_qs`` comes up empty — there's no path to parse).
    authority = request.raw_path or request.path_qs
    if ":" not in authority:
        return web.Response(status=400, text="CONNECT requires host:port")
    host, _, port_s = authority.partition(":")
    try:
        port = int(port_s)
    except ValueError:
        return web.Response(status=400, text="port must be numeric")
    if not _host_allowed(host):
        LOG.warning("rejected CONNECT target %s (not in ALLOWED_HOSTS)", host)
        return web.Response(status=403, text=f"host not allowed: {host}")

    try:
        up_reader, up_writer = await asyncio.open_connection(host, port)
    except OSError as e:
        LOG.warning("upstream dial %s:%s failed: %s", host, port, e)
        return web.Response(status=502, text=f"upstream dial failed: {e}")

    # Hand the client a 200 response, then splice. aiohttp's
    # StreamResponse APIs would re-write headers; we want the raw
    # socket, so do it by hand.
    transport = request.transport
    if transport is None:
        up_writer.close()
        return web.Response(status=500, text="no transport")
    transport.write(b"HTTP/1.1 200 Connection established\r\n\r\n")

    # Get the underlying StreamReader from aiohttp's protocol so we can
    # splice it to the upstream socket.
    # Detach aiohttp entirely: swap the transport's protocol for a plain
    # StreamReaderProtocol so post-CONNECT bytes reach us raw instead of
    # aiohttp's HTTP parser (RequestHandler exposes no usable reader — it
    # would try to parse the SSH stream as a next HTTP request).
    loop = asyncio.get_running_loop()
    cli_reader = asyncio.StreamReader(limit=2**16, loop=loop)
    raw_proto = asyncio.StreamReaderProtocol(cli_reader, loop=loop)
    transport.set_protocol(raw_proto)
    raw_proto.connection_made(transport)
    cli_writer = asyncio.StreamWriter(transport, raw_proto, cli_reader, loop)

    await asyncio.gather(
        _splice(cli_reader, up_writer),
        _splice(up_reader, cli_writer),
        return_exceptions=True,
    )
    # We've fully owned (and by now closed) the connection; the returned
    # response is never written — it just satisfies aiohttp's contract.
    return web.Response(status=200)


# ── App wiring ──────────────────────────────────────────────────────────


def _make_app() -> web.Application:
    # CONNECT must be intercepted in a MIDDLEWARE, not a route: its
    # authority-form target ("host:port", no leading slash) matches no
    # URL pattern, so router-based dispatch 404s before any handler runs
    # — which is exactly what the old wildcard-route dispatch did.
    # Middlewares still run on route-resolution failure.
    @web.middleware
    async def connect_middleware(request: web.Request, handler):  # type: ignore[no-untyped-def]
        if request.method == "CONNECT":
            return await connect_handler(request)
        return await handler(request)

    app = web.Application(
        client_max_size=64 * 1024 * 1024, middlewares=[connect_middleware]
    )
    app.router.add_route("*", "/{tail:.*}", http_handler)
    return app


async def _start_site_with_retry(
    runner: web.AppRunner, host: str, port: int
) -> None:
    """Retry bind(host, port) until wg0 is up.

    The proxy sidecar starts in parallel with the edge container that
    runs ``wg-quick up wg0`` (they share a netns). The wg0 address may
    not exist yet when the proxy boots — bind fails with ``OSError:
    could not bind on any address``. Retry indefinitely with a short
    backoff; once the edge container's entrypoint finishes the wg-quick
    setup, the next attempt succeeds.
    """
    delay = 1.0
    for attempt in range(60):  # ~2 minutes of retries; the edge container
        try:                   # is normally ready in 5-10 seconds.
            site = web.TCPSite(runner, host, port)
            await site.start()
            LOG.info("listening on %s:%d (after %d attempt(s))", host, port, attempt + 1)
            return
        except OSError as e:
            LOG.info("bind %s:%d not ready (%s) — retry in %.1fs", host, port, e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 5.0)
    raise RuntimeError(f"gave up waiting for {host}:{port} to become bindable")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    LOG.info(
        "starting edge proxy on %s:%d (allowed_hosts=%s)",
        LISTEN_HOST, LISTEN_PORT, ALLOWED_HOSTS_RE.pattern,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = _make_app()
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    loop.run_until_complete(_start_site_with_retry(runner, LISTEN_HOST, LISTEN_PORT))

    stop = loop.create_future()

    def _shutdown() -> None:
        if not stop.done():
            stop.set_result(None)

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown)

    try:
        loop.run_until_complete(stop)
    except RuntimeError as e:
        LOG.error("startup failed: %s", e)
        return 1
    finally:
        loop.run_until_complete(runner.cleanup())
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
