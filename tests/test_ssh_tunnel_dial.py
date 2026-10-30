"""SSH-over-tunnel dialing: the edge HTTP CONNECT socket helper and the
credentials resolver stamping ``proxy_url`` for devices behind a tunnel.

Covers the path that lets the hub's SSH consumers (LLDP probe, device
terminal, linux_ssh adapter) reach device IPs that only route inside a
tunneled customer cluster (e.g. the containerlab mgmt net
``172.20.20.0/24`` on cp03).
"""

from __future__ import annotations

import asyncio

import pytest

from daalu_automation.core.cluster_proxy import open_connect_socket

# ── a minimal in-process CONNECT proxy ───────────────────────────────


async def _start_fake_proxy(reply: bytes, banner: bytes = b""):
    """Serve one CONNECT request: read headers, send ``reply`` (+banner)."""
    seen: dict = {}

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            chunk = await reader.read(1)
            if not chunk:
                break
            head += chunk
        seen["request"] = head
        # Reply + banner in ONE write: exercises the swallow-the-banner
        # hazard the byte-wise header read exists to avoid.
        writer.write(reply + banner)
        await writer.drain()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port, seen


async def test_open_connect_socket_success_preserves_banner():
    server, port, seen = await _start_fake_proxy(
        b"HTTP/1.1 200 Connection established\r\n\r\n",
        banner=b"SSH-2.0-fake\r\n",
    )
    try:
        sock = await open_connect_socket(
            f"http://127.0.0.1:{port}", "172.20.20.11", 22
        )
        try:
            # The CONNECT line reached the proxy with the right authority.
            assert b"CONNECT 172.20.20.11:22 HTTP/1.1" in seen["request"]
            # The SSH banner sent in the same packet as the proxy reply
            # must still be readable off the socket — nothing swallowed.
            loop = asyncio.get_running_loop()
            data = await asyncio.wait_for(loop.sock_recv(sock, 64), timeout=5)
            assert data == b"SSH-2.0-fake\r\n"
        finally:
            sock.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_open_connect_socket_rejected_by_proxy():
    server, port, _seen = await _start_fake_proxy(
        b"HTTP/1.1 403 Forbidden\r\n\r\n"
    )
    try:
        with pytest.raises(ConnectionError, match="403"):
            await open_connect_socket(f"http://127.0.0.1:{port}", "8.8.8.8", 22)
    finally:
        server.close()
        await server.wait_closed()


async def test_open_connect_socket_malformed_url():
    with pytest.raises(ValueError):
        await open_connect_socket("not-a-url", "10.0.0.1", 22)


# ── resolver stamps proxy_url ────────────────────────────────────────


def _linux_device(ip: str):
    from daalu_automation.core.sot.models import Device

    return Device(
        id=f"dev-{ip}",
        name=f"host-{ip.replace('.', '-')}",
        primary_ip=ip,
        platform="linux",
        transport="linux_ssh",
        tags=[],
        extra={},
    )


async def _seed_tunneled_cm(db, tenant_id, *, connected: bool = True):
    """config_manager Integration wired to a (connected) ClusterTunnel."""
    from daalu_automation.models import (
        ClusterTunnel,
        ClusterTunnelStatus,
        Integration,
        IntegrationStatus,
    )

    tunnel = ClusterTunnel(
        tenant_id=tenant_id,
        slug="cp03",
        name="cp03 lab",
        status=(
            ClusterTunnelStatus.connected
            if connected
            else ClusterTunnelStatus.pending
        ),
        operator_pubkey="op-pub",
        operator_privkey_encrypted="op-priv-ct",
        tunnel_ip="10.200.0.2",
    )
    db.add(tunnel)
    await db.flush()
    db.add(
        Integration(
            tenant_id=tenant_id,
            provider="config_manager",
            module="infra",
            name="NV-CM",
            status=IntegrationStatus.connected,
            cluster_tunnel_id=tunnel.id,
            config={"hosted": True},
        )
    )
    await db.commit()


async def test_resolver_stamps_proxy_for_private_ip_behind_tunnel(
    db, seeded, ssh_credentials_integration
):
    from daalu_automation.core import change_proposals as cps

    await _seed_tunneled_cm(db, seeded.tenant_a_id)
    creds = await cps.resolve_credentials(
        db, seeded.tenant_a_id, _linux_device("172.20.20.11")
    )
    assert creds.proxy_url == "http://10.200.0.2:8888"


async def test_resolver_direct_dial_for_public_ip(
    db, seeded, ssh_credentials_integration
):
    from daalu_automation.core import change_proposals as cps

    await _seed_tunneled_cm(db, seeded.tenant_a_id)
    # NB: not a TEST-NET address — those are "not globally reachable" and so
    # count as private to ``ipaddress`` (and would be proxied, correctly).
    creds = await cps.resolve_credentials(
        db, seeded.tenant_a_id, _linux_device("8.8.8.8")
    )
    assert creds.proxy_url is None


async def test_resolver_direct_dial_without_config_manager(
    db, seeded, ssh_credentials_integration
):
    from daalu_automation.core import change_proposals as cps

    creds = await cps.resolve_credentials(
        db, seeded.tenant_a_id, _linux_device("172.20.20.11")
    )
    assert creds.proxy_url is None


async def test_resolver_direct_dial_when_tunnel_not_connected(
    db, seeded, ssh_credentials_integration
):
    from daalu_automation.core import change_proposals as cps

    await _seed_tunneled_cm(db, seeded.tenant_a_id, connected=False)
    creds = await cps.resolve_credentials(
        db, seeded.tenant_a_id, _linux_device("172.20.20.11")
    )
    assert creds.proxy_url is None
