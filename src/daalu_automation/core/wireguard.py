"""WireGuard coordinator core.

The coordinator is the operator-side glue between:

- the ``cluster_tunnels`` table (which holds keypairs, allocated tunnel
  IPs, and the customer-reported pubkey/endpoint), and
- the ``daalu-wireguard-peers`` Secret in-cluster (the wg0.conf the hub
  pod reads — see ``deploy/helm/daalu-automation/templates/wireguard-hub.yaml``).

Responsibilities live here:

- Generate the operator-side X25519 keypair for a new cluster.
- Allocate a free /32 from the configured mesh CIDR (default
  10.200.0.0/16), skipping the hub's own address.
- Render the wg0.conf the hub should run, given the current set of
  ClusterTunnel rows in ``awaiting_handshake`` / ``connected`` /
  ``degraded`` (i.e. rows we expect the hub to talk to).
- Patch the ``daalu-wireguard-peers`` Secret with that rendered conf —
  the hub's inotify loop runs ``wg syncconf`` to apply it without
  tearing existing handshakes down.

The API router (POST /clusters) and the Celery beat task (tunnel-health)
both call into ``sync_hub_secret`` after they mutate ClusterTunnel rows,
so the Secret is always derivable from the DB.
"""

from __future__ import annotations

import base64
import contextlib
import ipaddress
import secrets
from collections.abc import Iterable
from dataclasses import dataclass

import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.core.crypto import decrypt_secret, encrypt_secret
from daalu_automation.models import ClusterTunnel, ClusterTunnelStatus

logger = structlog.get_logger(__name__)

# Tunnel ports the edge fronts the two in-cluster vLLM Services on, via the
# dedicated ``svcproxy-*`` socat sidecars (raw TCP → Service:80) — NOT the
# generic HTTP forward proxy on 8888, which only speaks absolute-form proxy
# requests, so a plain ``GET /v1/models`` against it fails and the GPU reads
# "offline".
#
# These are the single source of truth for the split: they're baked into the
# generated ``helm install`` snippet (``render_edge_install_snippet``) AND used
# to pre-fill the GPU-onboarding form, so the listener and the URL the hub
# dials can't drift apart. Keep aligned with ``settings.llm_local_base_url``
# and ``settings.coding_local_base_url``.
VLLM_PROXY_PORT = 8001  # → llm-classifier (routing/classifier stack)
CODER_PROXY_PORT = 8000  # → llm-coder (coding stack: laptop CLI, workspaces)


# Source of truth for the mesh range — kept in sync with values.yaml's
# wireguard.hubAddress. The hub takes .0.1; we hand out .0.2 onward.
MESH_CIDR = ipaddress.ip_network("10.200.0.0/16")
HUB_TUNNEL_IP = ipaddress.ip_address("10.200.0.1")

# Name of the Secret the hub reads its config from. Must match the
# Secret declared in templates/wireguard-hub.yaml.
HUB_SECRET_NAME = "daalu-wireguard-peers"
HUB_SECRET_KEY = "wg0.conf"


# ── Keypair generation ──────────────────────────────────────────────────


@dataclass(frozen=True)
class GeneratedKeypair:
    """A freshly-generated WireGuard keypair.

    ``privkey_plaintext`` is the wg-format base64 private key — pass it
    to ``encrypt_secret`` before storing.
    """

    pubkey: str
    privkey_plaintext: str


def generate_keypair() -> GeneratedKeypair:
    """Produce a wg-format X25519 keypair.

    WireGuard's private key is just a raw 32-byte X25519 scalar with the
    standard X25519 clamping applied, base64-encoded. The ``cryptography``
    package handles the clamping internally when we serialize the raw
    bytes back out.
    """
    sk = X25519PrivateKey.generate()
    pk = sk.public_key()
    sk_bytes = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pk_bytes = pk.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return GeneratedKeypair(
        pubkey=base64.b64encode(pk_bytes).decode("ascii"),
        privkey_plaintext=base64.b64encode(sk_bytes).decode("ascii"),
    )


# ── IP allocation ───────────────────────────────────────────────────────


class IPPoolExhausted(RuntimeError):
    """Out of free tunnel IPs in MESH_CIDR.

    Practically impossible at /16 (~65k peers), but the allocator surfaces
    this rather than silently re-using addresses.
    """


async def allocate_tunnel_ip(db: AsyncSession) -> str:
    """Pick the lowest unused host in MESH_CIDR, skipping the hub IP.

    Uses an in-Python scan against the existing ``tunnel_ip`` values
    rather than a Postgres-side sequence so a manually-edited row (e.g.
    operator pins a customer to .200.0.42 for VPN-firewall ACL reasons)
    is respected automatically.
    """
    result = await db.execute(select(ClusterTunnel.tunnel_ip))
    used = {str(ip) for (ip,) in result.all() if ip is not None}
    used.add(str(HUB_TUNNEL_IP))
    for host in MESH_CIDR.hosts():
        s = str(host)
        if s not in used:
            return s
    raise IPPoolExhausted(f"no free addresses in {MESH_CIDR}")


# ── Invite tokens ───────────────────────────────────────────────────────


def generate_invite_token() -> tuple[str, str]:
    """Return (plaintext_token, sha256_hash).

    The plaintext is shown once in the onboarding UI (so the operator
    can paste it into the customer's edge install snippet) and never
    again. The DB only stores the hash so a leaked database row can't be
    used to bootstrap a new edge connection.
    """
    import hashlib

    plain = secrets.token_urlsafe(32)
    digest = hashlib.sha256(plain.encode("utf-8")).hexdigest()
    return plain, digest


def hash_invite_token(plain: str) -> str:
    import hashlib

    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


# ── Peer config rendering ───────────────────────────────────────────────

# Rows in these statuses are reflected in the hub config. ``pending``
# rows have no customer pubkey yet, so they'd render an invalid Peer
# block — they're held back until the customer-side edge calls back.
_LIVE_STATUSES = (
    ClusterTunnelStatus.awaiting_handshake,
    ClusterTunnelStatus.connected,
    ClusterTunnelStatus.degraded,
)


def render_hub_config(
    *,
    hub_privkey: str,
    listen_port: int,
    hub_address: str,
    peers: Iterable[ClusterTunnel],
) -> str:
    """Build the wg0.conf the hub Deployment reads.

    Only peers with a known ``customer_pubkey`` are emitted; rows that
    haven't completed their bootstrap callback yet are skipped, since
    WireGuard treats a Peer block with a missing PublicKey as a fatal
    parse error and refuses to load the whole config.
    """
    lines = [
        "[Interface]",
        f"PrivateKey = {hub_privkey}",
        f"Address = {hub_address}/16",
        f"ListenPort = {listen_port}",
        "",
    ]
    for peer in peers:
        if not peer.customer_pubkey:
            continue
        lines.append(f"# tenant={peer.tenant_id} cluster={peer.slug}")
        lines.append("[Peer]")
        lines.append(f"PublicKey = {peer.customer_pubkey}")
        lines.append(f"AllowedIPs = {peer.tunnel_ip}/32")
        if peer.customer_endpoint:
            # Optional — only useful for hub-initiated handshakes; without
            # an Endpoint, WireGuard waits passively for the peer to
            # initiate (which is the normal NAT-traversal flow).
            lines.append(f"Endpoint = {peer.customer_endpoint}")
        lines.append("PersistentKeepalive = 25")
        lines.append("")
    return "\n".join(lines)


async def collect_live_peers(db: AsyncSession) -> list[ClusterTunnel]:
    """Return the ClusterTunnel rows that should appear in the hub config."""
    result = await db.execute(
        select(ClusterTunnel).where(ClusterTunnel.status.in_(_LIVE_STATUSES))
    )
    return list(result.scalars().all())


# ── Secret patcher ──────────────────────────────────────────────────────


class HubSecretUnavailable(RuntimeError):
    """The daalu-wireguard-peers Secret cannot be read or patched.

    Surfaces during local dev (no in-cluster SA) and when RBAC has not
    been applied yet. The API layer translates this to a 503 so the UI
    can render an actionable "enable wireguard.enabled in values.yaml"
    hint.
    """


def _load_in_cluster_core_v1():
    """Return a kubernetes CoreV1Api bound to the in-cluster SA.

    Local-dev fallback (~/.kube/config) is intentionally NOT used: a
    coordinator that talks to the wrong cluster could overwrite the
    Secret on the operator's *production* hub from a dev laptop.
    """
    try:
        from kubernetes import client, config  # type: ignore
    except ImportError as e:
        raise HubSecretUnavailable(
            "kubernetes client not installed in this image"
        ) from e
    try:
        config.load_incluster_config()
    except Exception as e:  # ConfigException — broad to dodge import order
        raise HubSecretUnavailable(
            "no in-cluster ServiceAccount available; wireguard coordinator "
            "must run inside the daalu-api pod"
        ) from e
    return client.CoreV1Api(), client


def _resolve_namespace() -> str:
    """Read the pod's namespace from the SA token mount."""
    try:
        with open(
            "/var/run/secrets/kubernetes.io/serviceaccount/namespace",
            encoding="utf-8",
        ) as fh:
            return fh.read().strip()
    except FileNotFoundError as e:
        raise HubSecretUnavailable("not running in-cluster (no SA mount)") from e


def hub_config_file() -> str | None:
    """Path of the file-backed hub config, or None in Secret mode."""
    from daalu_automation.config import get_settings

    return get_settings().wireguard_hub_config_path or None


def _ensure_hub_config_file(path: str, *, listen_port: int, hub_address: str) -> str:
    """Return the hub config at ``path``, seeding a fresh keypair if absent.

    File mode has no install-time step to seed the hub key (the Helm path
    uses wireguard.hubPrivateKey), so the first caller generates one and
    writes a peerless config — the hub container waits for this file.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            existing = fh.read()
        if existing.strip():
            return existing
    except FileNotFoundError:
        pass
    seeded = render_hub_config(
        hub_privkey=generate_keypair().privkey_plaintext,
        listen_port=listen_port,
        hub_address=hub_address,
        peers=[],
    )
    _write_hub_config_file(path, seeded)
    logger.info("wireguard.hub_config_seeded", path=path)
    return seeded


def _write_hub_config_file(path: str, text: str) -> None:
    """Atomically replace ``path`` (tmp + rename in the same directory).

    The rename lands as a ``moved_to`` event on the hub's directory watch,
    so the hub runs ``wg syncconf`` straight away.
    """
    import os
    import tempfile

    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".wg0.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


async def sync_hub_secret(
    db: AsyncSession,
    *,
    hub_privkey_ciphertext: str | None = None,
    listen_port: int = 51820,
    hub_address: str = "10.200.0.1",
) -> int:
    """Re-render wg0.conf from the DB and patch the hub Secret.

    Returns the number of Peer blocks written. Callers (the API on
    onboard, the beat task after handshake observations) call this any
    time ClusterTunnel state changes; the inotify loop in the hub
    container picks up the new Secret and runs ``wg syncconf``.

    ``hub_privkey_ciphertext`` lets the caller pass the operator-side
    private key explicitly (encrypted, same scheme as cluster privkeys);
    when omitted, the Secret's existing PrivateKey line is preserved so
    we don't accidentally rotate the hub's identity on every peer add.

    Runs the blocking kubernetes-client call in a thread so it doesn't
    block the FastAPI event loop.
    """
    import asyncio
    import re

    peers = await collect_live_peers(db)

    conf_path = hub_config_file()
    if conf_path:
        try:
            existing_file = await asyncio.to_thread(
                _ensure_hub_config_file,
                conf_path,
                listen_port=listen_port,
                hub_address=hub_address,
            )
        except OSError as e:
            raise HubSecretUnavailable(f"hub config {conf_path}: {e}") from e
        m = re.search(r"^PrivateKey\s*=\s*(\S+)", existing_file, flags=re.MULTILINE)
        if hub_privkey_ciphertext is not None:
            file_privkey = decrypt_secret(hub_privkey_ciphertext)
        elif m:
            file_privkey = m.group(1)
        else:
            raise HubSecretUnavailable(f"hub config {conf_path} has no PrivateKey")
        rendered = render_hub_config(
            hub_privkey=file_privkey,
            listen_port=listen_port,
            hub_address=hub_address,
            peers=peers,
        )
        if rendered != existing_file:
            try:
                await asyncio.to_thread(_write_hub_config_file, conf_path, rendered)
            except OSError as e:
                raise HubSecretUnavailable(f"write {conf_path} failed: {e}") from e
        written = sum(1 for p in peers if p.customer_pubkey)
        logger.info("wireguard.sync_hub_config_file", peer_count=written)
        return written

    ns = _resolve_namespace()
    core_v1, _kclient = _load_in_cluster_core_v1()

    def _read_existing() -> str | None:
        try:
            sec = core_v1.read_namespaced_secret(HUB_SECRET_NAME, ns)
        except Exception as e:  # noqa: BLE001
            raise HubSecretUnavailable(
                f"could not read Secret {ns}/{HUB_SECRET_NAME}: {e}"
            ) from e
        data = sec.data or {}
        blob = data.get(HUB_SECRET_KEY)
        if not blob:
            return None
        return base64.b64decode(blob).decode("utf-8")

    existing = await asyncio.to_thread(_read_existing)

    if hub_privkey_ciphertext is not None:
        hub_privkey = decrypt_secret(hub_privkey_ciphertext)
    elif existing:
        m = re.search(r"^PrivateKey\s*=\s*(\S+)", existing, flags=re.MULTILINE)
        if not m:
            raise HubSecretUnavailable(
                "existing hub Secret has no PrivateKey — re-seed it via "
                "wireguard.hubPrivateKey in values.yaml or `daalu init wg-hub`"
            )
        hub_privkey = m.group(1)
    else:
        raise HubSecretUnavailable(
            "hub Secret has no wg0.conf yet — set wireguard.hubPrivateKey "
            "in values.yaml on first install, or run `daalu init wg-hub`"
        )

    rendered = render_hub_config(
        hub_privkey=hub_privkey,
        listen_port=listen_port,
        hub_address=hub_address,
        peers=peers,
    )

    def _patch() -> None:
        body = {
            "stringData": {HUB_SECRET_KEY: rendered},
        }
        try:
            core_v1.patch_namespaced_secret(HUB_SECRET_NAME, ns, body)
        except Exception as e:  # noqa: BLE001
            raise HubSecretUnavailable(
                f"patch Secret {ns}/{HUB_SECRET_NAME} failed: {e}"
            ) from e

    await asyncio.to_thread(_patch)
    logger.info(
        "wireguard.sync_hub_secret",
        peer_count=sum(1 for p in peers if p.customer_pubkey),
        skipped_no_pubkey=sum(1 for p in peers if not p.customer_pubkey),
    )
    return sum(1 for p in peers if p.customer_pubkey)


# ── Helpers for the API layer ───────────────────────────────────────────


def encrypt_keypair(kp: GeneratedKeypair) -> tuple[str, str]:
    """Convenience: return (pubkey, ciphertext_privkey) for storage."""
    return kp.pubkey, encrypt_secret(kp.privkey_plaintext)


def render_edge_install_snippet(
    *,
    operator_endpoint: str,
    operator_api_url: str,
    hub_pubkey: str,
    cluster_slug: str,
    invite_token_plain: str,
    tunnel_ip: str,
    listen_port: int = 51820,
    edge_image_tag: str | None = None,
) -> str:
    """Produce the helm-install command the operator pastes to the customer.

    Kept here so the API router can re-render it on demand for read views
    that don't re-issue the invite token (the token itself is one-shot,
    so a re-render returns the *snippet shape* but with a placeholder).

    ``edge_image_tag`` pins the daalu-edge image to a specific build —
    defaults to the hub's own BUILD_SHA so the customer-side edge tracks
    whatever version the hub is running. Pinning matters because
    ``imagePullPolicy: IfNotPresent`` against a moving ``:latest`` tag
    means the customer's node never re-pulls when we ship a fix.

    The snippet also pre-wires the **vLLM service proxies**. The chart
    defaults ``serviceProxies`` to ``[]``, so without these the tunnel has
    no listener on the inference ports and a later "Connect a GPU" reads
    "offline" even once vLLM is healthy — a second ``helm upgrade`` the
    customer shouldn't have to discover. Declaring them up front is free:
    each is a ~16Mi socat sidecar that simply refuses connections until
    the matching Service exists.

    Ports are the two the hub already dials (keep in sync with
    ``settings.llm_local_base_url`` / ``coding_local_base_url`` and
    ``onboarding._VLLM_PROXY_PORT``):

    * ``8001`` → ``llm-classifier`` — the routing/classifier stack.
    * ``8000`` → ``llm-coder``      — the coding stack (laptop CLI, workspaces).

    Both upstreams are the **Service** name on port **80** (``llama-classifier``
    / ``qwen-coder`` are the *Deployment* names, and 8000 is the in-pod
    container port — neither is dialable here).
    """
    import os

    from daalu_automation.config import get_settings

    settings = get_settings()
    build_sha = os.environ.get("BUILD_SHA", "")
    if build_sha == "unknown":
        build_sha = ""
    tag = edge_image_tag or settings.edge_image_tag or build_sha or "latest"
    return (
        f"helm install daalu-edge {settings.edge_chart_ref} \\\n"
        f"  --set image.tag={tag} \\\n"
        f"  --set cluster.slug={cluster_slug} \\\n"
        f"  --set cluster.tunnelIp={tunnel_ip} \\\n"
        f"  --set hub.endpoint={operator_endpoint}:{listen_port} \\\n"
        f"  --set hub.publicKey={hub_pubkey} \\\n"
        f"  --set operator.apiUrl={operator_api_url} \\\n"
        "  --set 'serviceProxies[0].name=vllm' \\\n"
        f"  --set 'serviceProxies[0].listenPort={VLLM_PROXY_PORT}' \\\n"
        "  --set 'serviceProxies[0].upstream=llm-classifier.daalu.svc.cluster.local:80' \\\n"
        "  --set 'serviceProxies[1].name=coder' \\\n"
        f"  --set 'serviceProxies[1].listenPort={CODER_PROXY_PORT}' \\\n"
        "  --set 'serviceProxies[1].upstream=llm-coder.daalu.svc.cluster.local:80' \\\n"
        f"  --set bootstrap.token={invite_token_plain}"
    )
