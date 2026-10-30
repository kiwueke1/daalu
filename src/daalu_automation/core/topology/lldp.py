"""Observed-adjacency collection over SSH (LLDP) — the "what's actually wired"
signal behind the live topology graph.

We SSH each device and read its LLDP neighbour table with
``lldpctl -f keyvalue`` (the most parse-stable lldpd output — one
``lldp.<localif>.<key>=<value>`` per line). Cumulus/NVUE, SONiC and plain
Linux hosts all run lldpd, so one code path covers switches *and* servers.

Design notes:
* **Concurrent + bounded.** All devices are probed in parallel behind a
  semaphore with a hard per-host timeout, so a page load never hangs on one
  unreachable box.
* **TTL-cached per tenant.** UI polling (~10s) is cheap; the underlying SSH
  sweep runs at most once per ``TTL`` seconds.
* **Never fabricates state.** A device we can't reach comes back
  ``reachable=False`` with empty neighbours; the service renders those links
  neutral, not red.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.core.device.models import Credentials
from daalu_automation.core.sot.models import Device

logger = structlog.get_logger(__name__)

# Bounded sweep so a big fabric can't exhaust file descriptors / hang the page.
_MAX_CONCURRENCY = 12
_PER_HOST_TIMEOUT_S = 6.0
_CACHE_TTL_S = 8.0

# tenant_id -> (monotonic_ts, {device_id: NodeObservation})
_CACHE: dict[str, tuple[float, dict[str, NodeObservation]]] = {}


@dataclass(slots=True)
class Neighbor:
    local_if: str
    remote_name: str  # neighbour's LLDP system name (== device hostname in our SoT)
    remote_if: str


@dataclass(slots=True)
class NodeObservation:
    device_id: str
    reachable: bool
    neighbors: list[Neighbor] = field(default_factory=list)
    error: str | None = None


def _parse_lldpctl_keyvalue(text: str) -> list[Neighbor]:
    """Parse ``lldpctl -f keyvalue`` into neighbours — **all** of them.

    Relevant keys (one block of them per neighbour):
        lldp.<localif>.chassis.name = <remote system name>
        lldp.<localif>.port.ifname  = <remote port>   (fallbacks: descr/local/mac)

    lldpd repeats the same key path for every neighbour it holds on an
    interface (a flooded mgmt bridge shows several), so a key re-appearing
    on a ``local_if`` marks the start of the next neighbour's block. The
    old fold-into-one-dict parse garbled those interfaces into a single
    fabricated neighbour — last chassis name paired with first port name.
    Every block is emitted; deciding what a multi-neighbour interface
    *means* (OOB mgmt mesh vs. rogue hub on a fabric port) is the
    service's reconciliation concern, not the parser's.
    """
    # local_if -> one dict per neighbour block seen on that interface.
    acc: dict[str, list[dict[str, str]]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("lldp.") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parts = key.split(".")
        # lldp . <localif> . <section> . <field...>
        if len(parts) < 4:
            continue
        local_if = parts[1]
        section = parts[2]
        field_name = parts[3]
        if section == "chassis" and field_name == "name":
            field_key = "name"
        elif section == "port" and field_name in ("ifname", "descr", "local", "mac"):
            field_key = f"port.{field_name}"
        else:
            continue
        blocks = acc.setdefault(local_if, [])
        if not blocks or field_key in blocks[-1]:
            blocks.append({})
        blocks[-1][field_key] = value

    out: list[Neighbor] = []
    for local_if, blocks in acc.items():
        for b in blocks:
            remote_name = b.get("name", "")
            remote_if = (
                b.get("port.ifname")
                or b.get("port.descr")
                or b.get("port.local")
                or b.get("port.mac")
                or ""
            )
            if remote_name or remote_if:
                out.append(
                    Neighbor(
                        local_if=local_if, remote_name=remote_name, remote_if=remote_if
                    )
                )
    return out


async def _probe_one(creds: Credentials) -> tuple[bool, list[Neighbor], str | None]:
    """SSH a single device and read its LLDP table. Best-effort, bounded."""
    import asyncssh  # local import so daalu-api boots even if the extra is absent

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
        async with asyncssh.connect(**connect_kwargs) as conn:
            result = await conn.run("lldpctl -f keyvalue", check=False)
            text = result.stdout or ""
            if not text and creds.sudo:
                # lldpctl is root-gated for the login user on Cumulus, but
                # the admin user has NOPASSWD sudo (real Cumulus ships it
                # that way) — retry escalated. Hosts where the user can
                # read lldpd directly (via the _lldpd group) never get
                # here; hosts with no sudo at all fall through to the
                # stderr branch below.
                result = await conn.run("sudo -n lldpctl -f keyvalue", check=False)
                text = result.stdout or ""
            if not text and (result.stderr or ""):
                # lldpd missing / not running — reachable, but no adjacency.
                return True, [], (result.stderr or "").strip()[:200]
            return True, _parse_lldpctl_keyvalue(text), None
    except Exception as e:  # noqa: BLE001 — any failure = "not observed", not an error page
        sock = connect_kwargs.get("sock")
        if sock is not None:  # asyncssh closes it on handshake failure; belt-and-braces
            sock.close()
        return False, [], str(e)[:200]


async def _ssh_creds(
    db: AsyncSession, tenant_id: uuid.UUID, device: Device
) -> Credentials | None:
    """Resolve SSH-usable credentials for a device, or None if it has none.

    Uses the standard resolver but keeps only genuinely SSH-shaped results —
    a ``config_manager`` device resolves to an NV-CM API handle (port 443, an
    ``nvcm_conn`` in ``extra``) which we can't ``lldpctl`` over, so we skip it
    rather than dial the wrong thing.
    """
    if not device.primary_ip:
        return None
    from daalu_automation.core.change_proposals import resolve_credentials

    try:
        creds = await resolve_credentials(db, tenant_id, device)
    except Exception:  # noqa: BLE001 — no creds configured for this device
        return None
    if creds.extra.get("nvcm_conn") is not None or creds.port == 443:
        return None
    if not (creds.private_key_pem or creds.password):
        return None
    return creds


async def collect_observations(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    devices: list[Device],
) -> dict[str, NodeObservation]:
    """Sweep every device's LLDP table, concurrently, with a short TTL cache.

    Returns ``{device_id: NodeObservation}``. Devices with no SSH credentials
    are omitted (the service treats them as "not probed", links neutral).
    """
    key = str(tenant_id)
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit is not None and (now - hit[0]) < _CACHE_TTL_S:
        return hit[1]

    sem = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def worker(dev: Device) -> tuple[str, NodeObservation] | None:
        creds = await _ssh_creds(db, tenant_id, dev)
        if creds is None:
            return None
        async with sem:
            try:
                reachable, neighbors, err = await asyncio.wait_for(
                    _probe_one(creds), timeout=_PER_HOST_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                reachable, neighbors, err = False, [], "probe timed out"
        return dev.id, NodeObservation(
            device_id=dev.id, reachable=reachable, neighbors=neighbors, error=err
        )

    results = await asyncio.gather(*(worker(d) for d in devices))
    obs: dict[str, NodeObservation] = {r[0]: r[1] for r in results if r is not None}
    _CACHE[key] = (now, obs)
    logger.info(
        "topology.lldp.sweep",
        tenant_id=key,
        probed=len(obs),
        reachable=sum(1 for o in obs.values() if o.reachable),
    )
    return obs
