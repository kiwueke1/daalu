"""Cluster onboarding + WireGuard coordinator tests.

Two layers:

- **Unit** (no DB): crypto round-trip, keypair shape, peer-config
  rendering, invite-token hash. These run fast and would catch a
  regression that breaks the contract with the daalu-edge container
  even before any HTTP layer is touched.
- **Integration** (DB + HTTP): exercise POST /clusters → callback →
  list, including cross-tenant isolation and the one-shot token rule.
  The hub-Secret patch path is monkeypatched out — we don't have a real
  k8s API in the test harness, and the unit layer already covers the
  config we *would* have written.

The property to break: an attacker reuses a leaked invite token after
the first edge has bootstrapped. The test
``test_callback_token_is_one_shot`` proves the row's hash is cleared
on first success and a second call returns 409.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Unit tests — no DB, no HTTP
# ---------------------------------------------------------------------------


def test_crypto_round_trip():
    from daalu_automation.core.crypto import decrypt_secret, encrypt_secret

    ct = encrypt_secret("a-wireguard-private-key=")
    assert ct != "a-wireguard-private-key="
    assert decrypt_secret(ct) == "a-wireguard-private-key="


def test_keypair_shape_is_x25519_base64():
    import base64

    from daalu_automation.core.wireguard import generate_keypair

    kp = generate_keypair()
    pub = base64.b64decode(kp.pubkey)
    priv = base64.b64decode(kp.privkey_plaintext)
    # Raw X25519 = 32 bytes each.
    assert len(pub) == 32
    assert len(priv) == 32
    # Two consecutive calls must produce distinct keypairs.
    kp2 = generate_keypair()
    assert kp.pubkey != kp2.pubkey
    assert kp.privkey_plaintext != kp2.privkey_plaintext


def test_invite_token_hash_is_stable_and_url_safe():
    from daalu_automation.core.wireguard import (
        generate_invite_token,
        hash_invite_token,
    )

    plain, digest = generate_invite_token()
    # token_urlsafe(32) → 43 chars, no padding, alphabet [A-Za-z0-9_-].
    assert 40 <= len(plain) <= 64
    assert all(c.isalnum() or c in "-_" for c in plain)
    assert hash_invite_token(plain) == digest
    # The hash MUST be different from the plaintext — leaking the row
    # must not leak the token itself.
    assert digest != plain


def test_render_hub_config_skips_peers_with_no_pubkey():
    """A peer row without customer_pubkey would render an unparseable
    Peer block and crash wg-quick — the renderer must skip these so the
    rest of the config still loads.
    """
    from types import SimpleNamespace

    from daalu_automation.core.wireguard import render_hub_config

    peers = [
        SimpleNamespace(
            slug="a", tenant_id="t", customer_pubkey="AAAA",
            tunnel_ip="10.200.0.2", customer_endpoint=None,
        ),
        SimpleNamespace(
            slug="b", tenant_id="t", customer_pubkey=None,
            tunnel_ip="10.200.0.3", customer_endpoint=None,
        ),
    ]
    conf = render_hub_config(
        hub_privkey="HUB=", listen_port=51820,
        hub_address="10.200.0.1", peers=peers,
    )
    assert "[Interface]" in conf
    assert "PrivateKey = HUB=" in conf
    assert "cluster=a" in conf
    assert "PublicKey = AAAA" in conf
    assert "cluster=b" not in conf  # skipped because no pubkey


def test_classify_handshake_age_buckets():
    from datetime import datetime, timedelta, timezone

    from daalu_automation.models import ClusterTunnelStatus
    from daalu_automation.workers.wireguard_health import _classify

    now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    fresh = int((now - timedelta(seconds=30)).timestamp())
    stale = int((now - timedelta(seconds=450)).timestamp())
    dead = int((now - timedelta(seconds=3600)).timestamp())

    cw, dw = 180, 600
    assert _classify(fresh, connected_window=cw, degraded_window=dw, now=now) == ClusterTunnelStatus.connected
    assert _classify(stale, connected_window=cw, degraded_window=dw, now=now) == ClusterTunnelStatus.degraded
    assert _classify(dead, connected_window=cw, degraded_window=dw, now=now) == ClusterTunnelStatus.error
    assert _classify(0, connected_window=cw, degraded_window=dw, now=now) == ClusterTunnelStatus.awaiting_handshake
    assert _classify(None, connected_window=cw, degraded_window=dw, now=now) == ClusterTunnelStatus.awaiting_handshake


def test_parse_wg_dump_skips_interface_line():
    from daalu_automation.workers.wireguard_health import parse_wg_dump

    sample = (
        "PRIV=\tPUB=\t51820\toff\n"
        "PEER1\t(none)\t1.2.3.4:51820\t10.200.0.2/32\t1747449500\t100\t200\toff\n"
        "PEER2\t(none)\t(none)\t10.200.0.3/32\t0\t0\t0\toff\n"
    )
    out = parse_wg_dump(sample)
    assert out == {"PEER1": 1747449500, "PEER2": 0}


# ---------------------------------------------------------------------------
# Integration tests — DB + HTTP
# ---------------------------------------------------------------------------


def _stub_hub_secret(monkeypatch):
    """Stub out the in-cluster Secret writer.

    We don't have a kube API in the test harness, and the unit layer
    already verifies the rendered wg0.conf. Tests just need the calls
    not to crash.
    """
    async def _noop(*args, **kwargs):
        return 0

    monkeypatch.setattr(
        "daalu_automation.core.wireguard.sync_hub_secret", _noop
    )
    monkeypatch.setattr(
        "daalu_automation.api.routers.clusters.sync_hub_secret", _noop
    )
    # _load_hub_pubkey is best-effort; return None so the snippet uses
    # the documented placeholder string.
    async def _no_pubkey():
        return None

    monkeypatch.setattr(
        "daalu_automation.api.routers.clusters._load_hub_pubkey", _no_pubkey
    )


@pytest.mark.asyncio
async def test_onboard_returns_install_snippet_once(client, seeded, monkeypatch):
    """POST /clusters echoes the invite token + snippet exactly once.

    The follow-up GET must NOT include the token — it lives in the row
    only as a sha256, so a stolen DB dump can't redeem the cluster.
    """
    _stub_hub_secret(monkeypatch)
    monkeypatch.setenv("WIREGUARD_PUBLIC_ENDPOINT", "wg.example.com")
    monkeypatch.setenv("OPERATOR_API_PUBLIC_URL", "https://ops.example.com")
    # Reset cached settings so the env override takes effect.
    from daalu_automation.config import get_settings

    get_settings.cache_clear()

    r = await client.post(
        "/api/v1/clusters",
        json={"slug": "acme", "name": "ACME prod"},
        headers={"Authorization": f"Bearer {seeded.token_a}"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["slug"] == "acme"
    assert body["status"] == "pending"
    assert body["tunnel_ip"].startswith("10.200.0.")
    assert len(body["invite_token"]) > 30
    assert "helm install daalu-edge" in body["install_snippet"]

    # Second GET — token must be gone.
    r2 = await client.get(
        "/api/v1/clusters/acme",
        headers={"Authorization": f"Bearer {seeded.token_a}"},
    )
    assert r2.status_code == 200
    assert "invite_token" not in r2.json()


@pytest.mark.asyncio
async def test_onboard_requires_endpoint(client, seeded, monkeypatch):
    """Onboarding fails fast (503) when wireguard_public_endpoint is unset."""
    _stub_hub_secret(monkeypatch)
    monkeypatch.setenv("WIREGUARD_PUBLIC_ENDPOINT", "")
    monkeypatch.setenv("OPERATOR_API_PUBLIC_URL", "https://ops.example.com")
    from daalu_automation.config import get_settings

    get_settings.cache_clear()

    r = await client.post(
        "/api/v1/clusters",
        json={"slug": "no-endpoint", "name": "X"},
        headers={"Authorization": f"Bearer {seeded.token_a}"},
    )
    assert r.status_code == 503
    assert "wireguard_public_endpoint" in r.json()["detail"]


@pytest.mark.asyncio
async def test_onboard_requires_api_public_url(client, seeded, monkeypatch):
    """Onboarding fails fast (503) when operator_api_public_url is unset.

    Without it the snippet would bake the chart's empty default into the
    customer's edge, which then fails at entrypoint with a clearer error
    — but the operator should never have shipped the snippet in the
    first place.
    """
    _stub_hub_secret(monkeypatch)
    monkeypatch.setenv("WIREGUARD_PUBLIC_ENDPOINT", "wg.example.com")
    monkeypatch.setenv("OPERATOR_API_PUBLIC_URL", "")
    from daalu_automation.config import get_settings

    get_settings.cache_clear()

    r = await client.post(
        "/api/v1/clusters",
        json={"slug": "no-api-url", "name": "X"},
        headers={"Authorization": f"Bearer {seeded.token_a}"},
    )
    assert r.status_code == 503
    assert "operator_api_public_url" in r.json()["detail"]


@pytest.mark.asyncio
async def test_onboard_rejects_duplicate_slug(client, seeded, monkeypatch):
    _stub_hub_secret(monkeypatch)
    monkeypatch.setenv("WIREGUARD_PUBLIC_ENDPOINT", "wg.example.com")
    monkeypatch.setenv("OPERATOR_API_PUBLIC_URL", "https://ops.example.com")
    from daalu_automation.config import get_settings

    get_settings.cache_clear()

    headers = {"Authorization": f"Bearer {seeded.token_a}"}
    r1 = await client.post(
        "/api/v1/clusters",
        json={"slug": "dup", "name": "first"},
        headers=headers,
    )
    assert r1.status_code == 201
    r2 = await client.post(
        "/api/v1/clusters",
        json={"slug": "dup", "name": "second"},
        headers=headers,
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_clusters_are_tenant_scoped(client, seeded, monkeypatch):
    """Tenant A's cluster must not appear in tenant B's list or GET."""
    _stub_hub_secret(monkeypatch)
    monkeypatch.setenv("WIREGUARD_PUBLIC_ENDPOINT", "wg.example.com")
    monkeypatch.setenv("OPERATOR_API_PUBLIC_URL", "https://ops.example.com")
    from daalu_automation.config import get_settings

    get_settings.cache_clear()

    r = await client.post(
        "/api/v1/clusters",
        json={"slug": "a-only", "name": "A only"},
        headers={"Authorization": f"Bearer {seeded.token_a}"},
    )
    assert r.status_code == 201

    list_b = await client.get(
        "/api/v1/clusters",
        headers={"Authorization": f"Bearer {seeded.token_b}"},
    )
    assert list_b.status_code == 200
    assert list_b.json() == []

    get_b = await client.get(
        "/api/v1/clusters/a-only",
        headers={"Authorization": f"Bearer {seeded.token_b}"},
    )
    assert get_b.status_code == 404


@pytest.mark.asyncio
async def test_callback_token_is_one_shot(client, seeded, monkeypatch):
    """Invite token can be redeemed exactly once; replay → 409."""
    _stub_hub_secret(monkeypatch)
    monkeypatch.setenv("WIREGUARD_PUBLIC_ENDPOINT", "wg.example.com")
    monkeypatch.setenv("OPERATOR_API_PUBLIC_URL", "https://ops.example.com")
    from daalu_automation.config import get_settings

    get_settings.cache_clear()

    create = await client.post(
        "/api/v1/clusters",
        json={"slug": "once", "name": "one-shot"},
        headers={"Authorization": f"Bearer {seeded.token_a}"},
    )
    assert create.status_code == 201
    token = create.json()["invite_token"]
    pubkey = "A" * 43 + "="

    r1 = await client.post(
        "/api/v1/clusters/once/callback",
        json={"invite_token": token, "customer_pubkey": pubkey},
    )
    assert r1.status_code == 200
    assert r1.json()["status"] == "awaiting_handshake"
    assert r1.json()["customer_pubkey"] == pubkey

    # Second redemption attempt — even with the same valid token, the
    # row's hash is gone so it must 409, not 401.
    r2 = await client.post(
        "/api/v1/clusters/once/callback",
        json={"invite_token": token, "customer_pubkey": pubkey},
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_callback_rejects_bad_token(client, seeded, monkeypatch):
    _stub_hub_secret(monkeypatch)
    monkeypatch.setenv("WIREGUARD_PUBLIC_ENDPOINT", "wg.example.com")
    monkeypatch.setenv("OPERATOR_API_PUBLIC_URL", "https://ops.example.com")
    from daalu_automation.config import get_settings

    get_settings.cache_clear()

    create = await client.post(
        "/api/v1/clusters",
        json={"slug": "badtok", "name": "bad-tok"},
        headers={"Authorization": f"Bearer {seeded.token_a}"},
    )
    assert create.status_code == 201
    pubkey = "B" * 43 + "="
    r = await client.post(
        "/api/v1/clusters/badtok/callback",
        json={"invite_token": "definitely-not-the-token-xx", "customer_pubkey": pubkey},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_callback_does_not_require_auth_header(client, seeded, monkeypatch):
    """The edge container has no user session — the auth middleware must
    let the callback request through to the route's invite-token check.
    """
    _stub_hub_secret(monkeypatch)
    monkeypatch.setenv("WIREGUARD_PUBLIC_ENDPOINT", "wg.example.com")
    monkeypatch.setenv("OPERATOR_API_PUBLIC_URL", "https://ops.example.com")
    from daalu_automation.config import get_settings

    get_settings.cache_clear()

    # No Authorization header at all → middleware-level 401 would mean
    # the bypass regex is broken.
    r = await client.post(
        "/api/v1/clusters/nonexistent/callback",
        json={"invite_token": "x" * 40, "customer_pubkey": "A" * 43 + "="},
    )
    # Route reached → 404 (no such cluster), not middleware 401.
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_allocate_tunnel_ip_skips_hub_and_existing(client, seeded, monkeypatch):
    """Sequential onboardings must produce distinct IPs and never the hub IP."""
    _stub_hub_secret(monkeypatch)
    monkeypatch.setenv("WIREGUARD_PUBLIC_ENDPOINT", "wg.example.com")
    monkeypatch.setenv("OPERATOR_API_PUBLIC_URL", "https://ops.example.com")
    from daalu_automation.config import get_settings

    get_settings.cache_clear()

    headers = {"Authorization": f"Bearer {seeded.token_a}"}
    ips = []
    for slug in ("c1", "c2", "c3"):
        r = await client.post(
            "/api/v1/clusters",
            json={"slug": slug, "name": slug},
            headers=headers,
        )
        assert r.status_code == 201, r.text
        ips.append(r.json()["tunnel_ip"])
    assert len(set(ips)) == 3
    assert "10.200.0.1" not in ips  # hub address reserved


@pytest.mark.asyncio
async def test_delete_removes_self_registered_kubeconfig(client, seeded, monkeypatch, db):
    """Tearing a cluster down must drop the kubeconfig its edge self-registered.

    The FK is ON DELETE SET NULL, so without explicit cleanup the row lingers
    as a "connected" kubernetes integration pointing at a dead tunnel IP.
    Integrations an operator created by hand are left alone.
    """
    from sqlalchemy import select

    from daalu_automation.models import Integration

    _stub_hub_secret(monkeypatch)
    monkeypatch.setenv("WIREGUARD_PUBLIC_ENDPOINT", "wg.example.com")
    monkeypatch.setenv("OPERATOR_API_PUBLIC_URL", "https://ops.example.com")
    from daalu_automation.config import get_settings

    get_settings.cache_clear()
    headers = {"Authorization": f"Bearer {seeded.token_a}"}

    create = await client.post(
        "/api/v1/clusters", json={"slug": "lab", "name": "Lab"}, headers=headers
    )
    assert create.status_code == 201
    cb = await client.post(
        "/api/v1/clusters/lab/callback",
        json={
            "invite_token": create.json()["invite_token"],
            "customer_pubkey": "B" * 43 + "=",
            "kube_token": "sa-token-from-the-edge",
        },
    )
    assert cb.status_code == 200
    import uuid

    tunnel_id = uuid.UUID(create.json()["id"])
    db.add(
        Integration(
            tenant_id=seeded.tenant_a_id,
            provider="prometheus",
            module="observability",
            name="lab prometheus (by hand)",
            config={},
            cluster_tunnel_id=tunnel_id,
        )
    )
    await db.commit()

    names = lambda: select(Integration.name).where(  # noqa: E731
        Integration.tenant_id == seeded.tenant_a_id
    )
    assert "lab (self-registered)" in (await db.execute(names())).scalars().all()

    r = await client.delete("/api/v1/clusters/lab", headers=headers)
    assert r.status_code == 204

    db.expire_all()
    left = (await db.execute(names())).scalars().all()
    assert "lab (self-registered)" not in left
    assert "lab prometheus (by hand)" in left


async def _two_clusters_with_stores(client, seeded, monkeypatch, db):
    """Clusters ``east`` + ``west``; Prometheus on east, Loki on west, and a
    tenant-level Grafana linked to no cluster."""
    import uuid

    from daalu_automation.models import Integration

    _stub_hub_secret(monkeypatch)
    monkeypatch.setenv("WIREGUARD_PUBLIC_ENDPOINT", "wg.example.com")
    monkeypatch.setenv("OPERATOR_API_PUBLIC_URL", "https://ops.example.com")
    from daalu_automation.config import get_settings

    get_settings.cache_clear()
    headers = {"Authorization": f"Bearer {seeded.token_a}"}
    ids = {}
    for slug in ("east", "west"):
        r = await client.post(
            "/api/v1/clusters", json={"slug": slug, "name": slug}, headers=headers
        )
        assert r.status_code == 201
        ids[slug] = uuid.UUID(r.json()["id"])
    for provider, module, slug, url in (
        ("prometheus", "observability", "east", "http://prom-east:9090"),
        ("loki", "observability", "west", "http://loki-west:3100"),
        ("grafana", "observability", None, "http://grafana:3000"),
    ):
        db.add(
            Integration(
                tenant_id=seeded.tenant_a_id,
                provider=provider,
                module=module,
                name=f"{provider} {slug or 'tenant'}",
                config={"url": url},
                cluster_tunnel_id=ids[slug] if slug else None,
            )
        )
    await db.commit()
    return headers


@pytest.mark.asyncio
async def test_cluster_observability_lists_only_its_own_stores(
    client, seeded, monkeypatch, db
):
    headers = await _two_clusters_with_stores(client, seeded, monkeypatch, db)

    east = await client.get("/api/v1/clusters/east/observability", headers=headers)
    west = await client.get("/api/v1/clusters/west/observability", headers=headers)
    assert east.status_code == west.status_code == 200
    assert [s["provider"] for s in east.json()] == ["prometheus"]
    assert [s["provider"] for s in west.json()] == ["loki"]

    other_tenant = await client.get(
        "/api/v1/clusters/east/observability",
        headers={"Authorization": f"Bearer {seeded.token_b}"},
    )
    assert other_tenant.status_code == 404


@pytest.mark.asyncio
async def test_console_resolves_the_clusters_own_store(
    client, seeded, monkeypatch, db
):
    """A cluster-scoped console uses that cluster's store, and a cluster with
    none says so — it must never fall back to another cluster's store."""
    from daalu_automation.core import observability_console as obs

    await _two_clusters_with_stores(client, seeded, monkeypatch, db)

    async def _no_proxy(*_a, **_k):
        return None

    monkeypatch.setattr(obs, "get_proxy_url", _no_proxy)

    base, _auth, _proxy = await obs._resolve("prometheus", seeded.tenant_a_id, "east")
    assert base == "http://prom-east:9090"
    with pytest.raises(obs.ObsConsoleError, match="integration for cluster 'west'"):
        await obs._resolve("prometheus", seeded.tenant_a_id, "west")
    with pytest.raises(obs.ObsConsoleError, match="cluster 'nope' not found"):
        await obs._resolve("prometheus", seeded.tenant_a_id, "nope")
    # Unscoped (the tenant-wide console) still finds the tenant's store.
    base, _auth, _proxy = await obs._resolve("prometheus", seeded.tenant_a_id)
    assert base == "http://prom-east:9090"

    ov = await obs.metrics_overview("prometheus", seeded.tenant_a_id, "west")
    assert ov.reachable is False and "integration for cluster 'west'" in ov.error


@pytest.mark.asyncio
async def test_agent_tools_scope_metrics_logs_and_kube_to_the_cluster(
    client, seeded, monkeypatch, db
):
    """The copilot's tools, targeted at a cluster by slug, read only that
    cluster's stores and its own kubeconfig — never another cluster's."""
    from daalu_automation.core import kube_tools
    from daalu_automation.models import Integration

    await _two_clusters_with_stores(client, seeded, monkeypatch, db)
    east_id = (
        await client.get(
            "/api/v1/clusters/east",
            headers={"Authorization": f"Bearer {seeded.token_a}"},
        )
    ).json()["id"]
    import uuid

    db.add(
        Integration(
            tenant_id=seeded.tenant_a_id,
            provider="kubernetes",
            module="infra",
            name="east (self-registered)",
            config={"kubeconfig": {"marker": "east"}},
            cluster_tunnel_id=uuid.UUID(east_id),
        )
    )
    await db.commit()

    t = seeded.tenant_a_id
    out = await kube_tools.execute_tool(
        "query_loki", {"query": "{app=\"x\"}"}, tenant_id=t, cluster_slug="east"
    )
    assert "cluster 'east' has no Loki" in out
    out = await kube_tools.execute_tool(
        "query_prometheus", {"query": "up"}, tenant_id=t, cluster_slug="west"
    )
    assert "cluster 'west' has no metrics store" in out
    out = await kube_tools.execute_tool(
        "query_prometheus", {"query": "up"}, tenant_id=t, cluster_slug="nope"
    )
    assert "no cluster named 'nope'" in out

    monkeypatch.setattr(
        kube_tools, "_load_kube", lambda *, tenant_kubeconfig=None: tenant_kubeconfig
    )
    tok = kube_tools._target_tunnel.set("east")
    try:
        assert await kube_tools._tenant_kube_or_default(t) == {"marker": "east"}
    finally:
        kube_tools._target_tunnel.reset(tok)

    # west has no kubeconfig of its own: it must NOT borrow east's.
    tok = kube_tools._target_tunnel.set("west")
    try:
        with pytest.raises(kube_tools.KubeUnavailable, match="'west' has no Kubernetes"):
            await kube_tools._tenant_kube_or_default(t)
    finally:
        kube_tools._target_tunnel.reset(tok)

    # An unlinked kubeconfig that targets west's own tunnel address is west's.
    west_ip = (
        await client.get(
            "/api/v1/clusters/west",
            headers={"Authorization": f"Bearer {seeded.token_a}"},
        )
    ).json()["tunnel_ip"]
    db.add(
        Integration(
            tenant_id=seeded.tenant_a_id,
            provider="kubernetes",
            module="infra",
            name="west (hand-made)",
            config={
                "kubeconfig": {
                    "marker": "west",
                    "clusters": [{"name": "w", "cluster": {"server": f"https://{west_ip}:6443"}}],
                }
            },
        )
    )
    await db.commit()
    tok = kube_tools._target_tunnel.set("west")
    try:
        assert (await kube_tools._tenant_kube_or_default(t))["marker"] == "west"
    finally:
        kube_tools._target_tunnel.reset(tok)
