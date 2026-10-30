"""Service layer for the ChangeProposal lifecycle.

The hard architectural constraint of the SoT / device-management
pipeline lives here: :func:`execute` is the only function in the
codebase that calls :meth:`DeviceAdapter.execute`, and it refuses to
do so unless

1. ``actor.kind == "executor"`` AND
   ``actor.scope == settings.executor_jwt_scope``
2. ``proposal.status == "approved"``
3. the freshly-rendered intended config matches the snapshot taken at
   proposal time.

Everything else (engine, reconciler, API routes) can only call
:func:`propose`, :func:`approve`, :func:`reject`, or
:func:`mark_stale`.

Session note: the project's :data:`AsyncSessionLocal` is configured
with ``expire_on_commit=False`` and ``autoflush=False``. Callers that
need the *post-commit* value of ``status`` (e.g. after
:func:`mark_stale`) should ``await db.refresh(row)`` themselves — this
module mutates and commits but does not refresh on the caller's
behalf.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.config import get_settings
from daalu_automation.core.device.base import DeviceAdapter
from daalu_automation.core.device.models import Credentials
from daalu_automation.core.events import EventEnvelope, publish
from daalu_automation.core.sot.base import SourceOfTruth
from daalu_automation.core.sot.models import Actor, Device, LinuxFacts
from daalu_automation.models import (
    ChangeProposal,
    ChangeProposalKind,
    ChangeProposalStatus,
    Integration,
)

logger = logging.getLogger(__name__)

SSH_CREDENTIALS_PROVIDER = "ssh_credentials"
REDFISH_CREDENTIALS_PROVIDER = "redfish_credentials"
# A single provider covers all three network-OS transports because the
# credential shape (user / password / optional enable_password / port)
# is identical across Junos, IOS-XR, and Arista EOS. Splitting per
# vendor would just multiply the Integration rows a tenant has to
# manage for no real safety benefit.
NETWORK_CREDENTIALS_PROVIDER = "network_credentials"
# NV-CM-backed switch executor: the "credential" is the per-tenant NV-CM
# stack connection (service URLs + Keycloak client), stored on the single
# Integration(provider="config_manager") row and smuggled to the adapter
# via Credentials.extra. See core/device/config_manager.py.
CONFIG_MANAGER_PROVIDER = "config_manager"

# device.transport → Integration.provider that holds its credentials.
# New transports must also be added here (and to the Integrations
# router's _ADAPTERLESS / _REDACT_FIELDS lists) so a tenant can store
# the matching creds via the standard config CRUD.
_CREDS_PROVIDER_BY_TRANSPORT: dict[str, str] = {
    "linux_ssh": SSH_CREDENTIALS_PROVIDER,
    "redfish": REDFISH_CREDENTIALS_PROVIDER,
    "junos": NETWORK_CREDENTIALS_PROVIDER,
    "iosxr": NETWORK_CREDENTIALS_PROVIDER,
    "eos": NETWORK_CREDENTIALS_PROVIDER,
    "config_manager": CONFIG_MANAGER_PROVIDER,
}


class StaleProposalError(RuntimeError):
    """Raised by :func:`execute` when re-rendered intent has drifted."""


class ProposalStatusError(RuntimeError):
    """Raised when a lifecycle method runs against a row in the wrong status."""


# ── Lifecycle ────────────────────────────────────────────────────────


async def propose(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    device_id: str,
    kind: ChangeProposalKind,
    intended_config: str,
    observed_config: str,
    diff: str,
    renderer_version: str,
    evidence: dict,
    actor: Actor,
    environment_slug: str | None = None,
) -> ChangeProposal:
    """Create a new ``pending`` proposal.

    Anyone may call this — the engine, the reconciler, an importer, a
    human via the UI. The approval gate is a separate step.

    ``environment_slug`` names the NV-CM management environment the device
    lives in, so the executor re-resolves it against the same Nautobot this
    proposal was authored against. Omit it for the tenant's default source of
    truth (a BYO Nautobot, or a tenant with a single primary environment).
    """
    row = ChangeProposal(
        tenant_id=tenant_id,
        device_id=device_id,
        environment_slug=environment_slug,
        kind=kind,
        status=ChangeProposalStatus.pending,
        intended_config=intended_config,
        observed_config=observed_config,
        diff=diff,
        renderer_version=renderer_version,
        evidence=evidence or {},
        created_by=actor.user_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await publish(EventEnvelope(
        tenant_id=str(tenant_id),
        type="proposal.created",
        module="sot",
        source="change_proposals",
        severity="info",
        summary=f"{kind.value} proposal for device {device_id}",
        payload={
            "proposal_id": str(row.id),
            "device_id": device_id,
            "environment_slug": environment_slug,
            "kind": kind.value,
            "actor_kind": actor.kind,
        },
    ))
    return row


async def _get_for_tenant(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    proposal_id: uuid.UUID,
    *,
    lock: bool = False,
) -> ChangeProposal:
    stmt = select(ChangeProposal).where(
        ChangeProposal.id == proposal_id,
        ChangeProposal.tenant_id == tenant_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise LookupError(f"proposal {proposal_id} not found for tenant {tenant_id}")
    return row


async def approve(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    proposal_id: uuid.UUID,
    *,
    actor: Actor,
) -> ChangeProposal:
    row = await _get_for_tenant(db, tenant_id, proposal_id, lock=True)
    if row.status != ChangeProposalStatus.pending:
        raise ProposalStatusError(
            f"cannot approve proposal in status {row.status.value}"
        )
    row.status = ChangeProposalStatus.approved
    row.approved_at = datetime.now(tz=timezone.utc)
    row.approved_by = actor.user_id
    await db.commit()
    await db.refresh(row)
    await publish(EventEnvelope(
        tenant_id=str(tenant_id),
        type="proposal.approved",
        module="sot",
        source="change_proposals",
        severity="info",
        summary=f"proposal {row.id} approved",
        payload={"proposal_id": str(row.id), "actor_kind": actor.kind},
    ))
    return row


async def reject(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    proposal_id: uuid.UUID,
    *,
    actor: Actor,
) -> ChangeProposal:
    row = await _get_for_tenant(db, tenant_id, proposal_id, lock=True)
    if row.status != ChangeProposalStatus.pending:
        raise ProposalStatusError(
            f"cannot reject proposal in status {row.status.value}"
        )
    row.status = ChangeProposalStatus.rejected
    row.approved_by = actor.user_id  # who decided, even if "no"
    row.approved_at = datetime.now(tz=timezone.utc)
    await db.commit()
    await db.refresh(row)
    await publish(EventEnvelope(
        tenant_id=str(tenant_id),
        type="proposal.rejected",
        module="sot",
        source="change_proposals",
        severity="info",
        summary=f"proposal {row.id} rejected",
        payload={"proposal_id": str(row.id), "actor_kind": actor.kind},
    ))
    return row


async def mark_stale(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    proposal_id: uuid.UUID,
    *,
    reason: str,
) -> ChangeProposal:
    row = await _get_for_tenant(db, tenant_id, proposal_id, lock=True)
    row.status = ChangeProposalStatus.stale
    # Record the reason inside executor_result.metadata so we don't
    # need a separate column for it.
    meta = dict(row.executor_result or {})
    meta.setdefault("stale", {})["reason"] = reason
    row.executor_result = meta
    await db.commit()
    await db.refresh(row)
    return row


async def execute(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    proposal_id: uuid.UUID,
    *,
    actor: Actor,
    sot: SourceOfTruth,
    adapter: DeviceAdapter,
    creds: Credentials,
) -> ChangeProposal:
    """Apply an approved proposal to the device.

    The single chokepoint between approved intent and a live device.
    Refuses unless ``actor`` is an executor-scoped identity and the
    proposal is currently ``approved``. Re-renders intent from the SoT
    and flips the proposal to ``stale`` if the freshly-rendered files
    map differs from the snapshot taken at propose time.
    """
    settings = get_settings()
    if actor.kind != "executor" or actor.scope != settings.executor_jwt_scope:
        raise PermissionError(
            "change_proposals.execute requires an executor-scoped actor"
        )

    row = await _get_for_tenant(db, tenant_id, proposal_id, lock=True)
    if row.status != ChangeProposalStatus.approved:
        raise ProposalStatusError(
            f"cannot execute proposal in status {row.status.value}"
        )

    # Re-render fresh from the SoT so we never push state that has
    # drifted from current intent since the proposal was authored.
    intended = await sot.get_intended_config(db, tenant_id, row.device_id)
    if intended is None:
        row.status = ChangeProposalStatus.stale
        meta = dict(row.executor_result or {})
        meta.setdefault("stale", {})["reason"] = (
            "intended config no longer present in SoT"
        )
        row.executor_result = meta
        await db.commit()
        raise StaleProposalError("intended config missing in SoT")

    fresh = await adapter.render(intended.facts)
    fresh_blob = _serialize_files(fresh.files)
    if fresh_blob != row.intended_config:
        row.status = ChangeProposalStatus.stale
        meta = dict(row.executor_result or {})
        meta.setdefault("stale", {})["reason"] = (
            "SoT intent changed between propose and execute"
        )
        row.executor_result = meta
        await db.commit()
        raise StaleProposalError("intended config changed since proposal")

    # Now push.
    result = await adapter.execute(creds, fresh)
    row.executed_at = datetime.now(tz=timezone.utc)
    row.executor_result = result.model_dump()
    row.status = (
        ChangeProposalStatus.executed if result.success else ChangeProposalStatus.failed
    )
    await db.commit()
    await db.refresh(row)
    await publish(EventEnvelope(
        tenant_id=str(tenant_id),
        type="proposal.executed",
        module="sot",
        source="change_proposals",
        severity="info" if result.success else "warning",
        summary=(
            f"proposal {row.id} {'executed' if result.success else 'failed'}"
        ),
        payload={
            "proposal_id": str(row.id),
            "device_id": row.device_id,
            "success": result.success,
            "rollback_performed": result.rollback_performed,
            "error": result.error or "",
        },
    ))
    return row


async def execute_provision(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    proposal_id: uuid.UUID,
    *,
    actor: Actor,
    sot: SourceOfTruth,
) -> ChangeProposal:
    """Execute an approved imperative server-lifecycle proposal via Tinkerbell.

    The counterpart to :func:`execute` for ``kind == provision_op``. Same
    identity + status invariants, but instead of the declarative
    render-and-drift-check it does an **observed-state compare**: it
    re-reads the target device from the SoT and refuses (``stale``) if the
    device has disappeared since the proposal was authored. The
    provisioning spec is read from ``proposal.intended_config`` (JSON).
    """
    import json

    from daalu_automation.core.device.tinkerbell_provision import (
        ServerProvisionSpec,
        provision,
    )

    settings = get_settings()
    if actor.kind != "executor" or actor.scope != settings.executor_jwt_scope:
        raise PermissionError(
            "change_proposals.execute_provision requires an executor-scoped actor"
        )

    row = await _get_for_tenant(db, tenant_id, proposal_id, lock=True)
    if row.kind != ChangeProposalKind.provision_op:
        raise ProposalStatusError(
            f"execute_provision only handles provision_op, got {row.kind.value}"
        )
    if row.status != ChangeProposalStatus.approved:
        raise ProposalStatusError(
            f"cannot execute proposal in status {row.status.value}"
        )

    # Observed-state compare: the device must still exist in the SoT. (A
    # fuller compare — e.g. "is it already in the target OS/power state" —
    # is a follow-up; this guards the common drift of a decommissioned host.)
    device = await sot.get_device(db, tenant_id, row.device_id)
    if device is None:
        row.status = ChangeProposalStatus.stale
        meta = dict(row.executor_result or {})
        meta.setdefault("stale", {})["reason"] = "device no longer in SoT"
        row.executor_result = meta
        await db.commit()
        raise StaleProposalError("device missing in SoT")

    try:
        spec = ServerProvisionSpec.model_validate(json.loads(row.intended_config))
    except Exception as exc:  # noqa: BLE001 — malformed spec is a hard fail
        row.status = ChangeProposalStatus.failed
        meta = dict(row.executor_result or {})
        meta["error"] = f"invalid provision spec: {exc}"
        row.executor_result = meta
        await db.commit()
        raise ProposalStatusError(f"invalid provision spec: {exc}") from exc

    # Fail-fast reachability guard: a clear "tinkerbell unreachable …"
    # beats a deep CRD-apply error half-way through provisioning. (The
    # same probe runs continuously in the integration-health beat, so an
    # unhealthy target should already be visible at onboarding time.)
    from daalu_automation.core.tinkerbell.health import check_health

    healthy, detail = await check_health(db, tenant_id)
    if not healthy:
        row.status = ChangeProposalStatus.failed
        meta = dict(row.executor_result or {})
        meta["error"] = f"tinkerbell precheck failed: {detail}"
        row.executor_result = meta
        await db.commit()
        raise ProposalStatusError(f"tinkerbell precheck failed: {detail}")

    kubeconfig, namespace = await _resolve_tinkerbell_target(db, tenant_id)
    result = await provision(spec, kubeconfig=kubeconfig, namespace=namespace)

    row.executed_at = datetime.now(tz=timezone.utc)
    row.executor_result = result.model_dump()
    row.status = (
        ChangeProposalStatus.executed if result.success else ChangeProposalStatus.failed
    )
    await db.commit()
    await db.refresh(row)
    await publish(EventEnvelope(
        tenant_id=str(tenant_id),
        type="proposal.executed",
        module="sot",
        source="change_proposals",
        severity="info" if result.success else "warning",
        summary=f"provision_op {row.id} {'executed' if result.success else 'failed'}",
        payload={
            "proposal_id": str(row.id),
            "device_id": row.device_id,
            "success": result.success,
            "error": result.error or "",
        },
    ))
    return row


async def _resolve_tinkerbell_target(
    db: AsyncSession, tenant_id: uuid.UUID
) -> tuple[dict | None, str]:
    """Resolve (kubeconfig, namespace) for the tenant's Tinkerbell mgmt cluster.

    Thin wrapper over the shared resolver in ``core.tinkerbell.target`` so
    the executor and the health precheck agree on how to reach the cluster.
    """
    from daalu_automation.core.tinkerbell.target import resolve_tinkerbell_target

    return await resolve_tinkerbell_target(db, tenant_id)


# ── Helpers ───────────────────────────────────────────────────────────


def serialize_rendered_files(files: dict[str, str]) -> str:
    """Public counterpart to :func:`_serialize_files`.

    Callers store the result on ``ChangeProposal.intended_config`` so
    the stale-check in :func:`execute` can compare to a re-render
    deterministically.
    """
    return _serialize_files(files)


def _serialize_files(files: dict[str, str]) -> str:
    """Canonical text representation of a RenderedConfig.files mapping.

    Stable across Python versions: sort keys, separate with a header
    line per path so a diff is informative if it does fire.
    """
    chunks: list[str] = []
    for path in sorted(files):
        chunks.append(f"### {path}\n{files[path]}")
    return "\n".join(chunks)


async def _select_credentials_row(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    provider: str,
    device: Device,
) -> Integration:
    """Pick the right Integration row for this device, in priority order.

    1. A row whose ``config.device_id`` exactly matches ``device.id``.
       This is the per-device row pattern — operators provision a
       dedicated key for a high-trust host and the resolver picks it
       automatically, with no Nautobot-side wiring needed.
    2. A row whose ``name`` matches Nautobot custom field
       ``{provider}_ref`` (e.g. ``ssh_credentials_ref="bastion-keys"``
       on the device → the integration row named ``bastion-keys``).
       This is the named-credential pattern — useful when one named
       key serves a fleet (jump host pool, BMC management VLAN).
    3. The tenant-wide fallback — any row without ``config.device_id``
       set. There is normally exactly one, but if a tenant has
       multiple we pick the first (deterministic via row creation
       order).

    Raises :class:`LookupError` with an explicit message when none of
    the three resolutions land — ``{provider}_ref`` set with no
    matching row is treated as a configuration error, not a silent
    fallback to the tenant-wide row, so a typo in Nautobot is loud.
    """
    rows = (
        await db.execute(
            select(Integration)
            .where(
                Integration.tenant_id == tenant_id,
                Integration.provider == provider,
            )
            .order_by(Integration.created_at)
        )
    ).scalars().all()
    if not rows:
        raise LookupError(
            f"no {provider} integration for tenant {tenant_id}"
        )

    # 1. Per-device row.
    for row in rows:
        if (row.config or {}).get("device_id") == device.id:
            return row

    # 2. Named-credential pointer from Nautobot custom fields.
    ref_field = f"{provider}_ref"  # ssh_credentials_ref / redfish_credentials_ref
    ref = device.extra.get(ref_field)
    if ref:
        for row in rows:
            if row.name == ref:
                return row
        raise LookupError(
            f"device {device.id} has {ref_field}={ref!r} but no {provider} "
            f"row named {ref!r} exists for tenant {tenant_id} — fix the "
            f"custom field or create the matching credential row"
        )

    # 3. Tenant-wide fallback (row without device_id).
    untargeted = [r for r in rows if not (r.config or {}).get("device_id")]
    if not untargeted:
        raise LookupError(
            f"all {provider} rows for tenant {tenant_id} target a specific "
            f"device.id; none matches {device.id!r} and no {ref_field} "
            f"custom field is set"
        )
    return untargeted[0]


async def _device_tunnel_proxy(
    db: AsyncSession, tenant_id: uuid.UUID, device: Device
) -> str | None:
    """HTTP CONNECT proxy for SSH-dialing ``device``, or None for direct.

    Devices onboarded into a tunneled NV-CM environment carry private IPs
    (e.g. the containerlab mgmt net ``172.20.20.0/24``) that only route
    *inside* the customer cluster — the hub can't SSH them directly. The
    daalu-edge forward proxy on the tunnel IP splices raw TCP via HTTP
    CONNECT (its allowlist covers RFC1918), so route through it when:

    * the device's primary IP is private (public IPs keep the direct dial —
      the edge allowlist would reject them anyway), and
    * the tenant has a ``config_manager`` integration with a **connected**
      tunnel (NULL tunnel = the env runs hub-side → direct dial reaches it).
    """
    import ipaddress

    ip = (device.primary_ip or "").split("/", 1)[0]
    try:
        if not ipaddress.ip_address(ip).is_private:
            return None
    except ValueError:
        return None
    row = (
        await db.execute(
            select(Integration)
            .where(
                Integration.tenant_id == tenant_id,
                Integration.provider == CONFIG_MANAGER_PROVIDER,
            )
            .order_by(Integration.created_at)
        )
    ).scalars().first()
    if row is None:
        return None
    from daalu_automation.core.cluster_proxy import get_proxy_url

    return await get_proxy_url(db, row.cluster_tunnel_id)


async def resolve_credentials(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    device: Device,
) -> Credentials:
    """Build a :class:`Credentials` for a device.

    Picks the credentials provider by ``device.transport``:

    * ``linux_ssh`` → ``Integration(provider="ssh_credentials")``
    * ``redfish`` → ``Integration(provider="redfish_credentials")``

    When the tenant has multiple rows for the provider, picks one per
    the priority documented on :func:`_select_credentials_row`. Per-
    device user / port overrides via Nautobot custom fields
    (``ssh_user`` / ``ssh_port`` / ``redfish_user`` / ``redfish_port``)
    are honoured on the *chosen* row's defaults.
    """
    # config_manager: NV-CM addresses the device by Nautobot UUID over the
    # tunnel, not by primary_ip/SSH. Resolve the single per-tenant
    # Integration(provider="config_manager") row into an NvcmConn and carry
    # it (plus the device UUID + target filename) in Credentials.extra.
    if device.transport == CONFIG_MANAGER_PROVIDER:
        cm_rows = (
            await db.execute(
                select(Integration)
                .where(
                    Integration.tenant_id == tenant_id,
                    Integration.provider == CONFIG_MANAGER_PROVIDER,
                )
                .order_by(Integration.created_at)
            )
        ).scalars().all()
        if not cm_rows:
            raise LookupError(
                f"no config_manager integration for tenant {tenant_id}"
            )
        from daalu_automation.core.cluster_proxy import get_proxy_url
        from daalu_automation.core.configmgr import conn_from_integration_config

        cm_cfg = cm_rows[0].config or {}
        # Route svc-* calls over the tenant's WireGuard tunnel when its NV-CM
        # runs on a workload cluster (svc-* only resolvable in-cluster). NULL
        # tunnel → direct dial (operator-local cluster), unchanged behavior.
        cm_proxy = await get_proxy_url(db, cm_rows[0].cluster_tunnel_id)
        conn = conn_from_integration_config(cm_cfg, proxy_url=cm_proxy)
        return Credentials(
            user="daalu-automation",
            host=device.primary_ip or "nv-config-manager",
            port=443,
            sudo=False,
            extra={
                "nvcm_conn": conn,
                "device_uuid": device.id,
                "filename": device.extra.get(
                    "config_manager_filename", "startup.yaml"
                ),
            },
        )

    if not device.primary_ip:
        raise LookupError(
            f"device {device.id} has no primary_ip; cannot resolve creds"
        )
    provider = _CREDS_PROVIDER_BY_TRANSPORT.get(device.transport)
    if provider is None:
        raise LookupError(
            f"no credentials provider registered for transport {device.transport!r}"
        )
    row = await _select_credentials_row(db, tenant_id, provider, device)
    cfg = row.config or {}

    from daalu_automation.core.crypto import decrypt_secret

    if device.transport == "redfish":
        # Redfish defaults: HTTPS 443, no private key, BMCs typically
        # ship with self-signed certs so verify_tls defaults to off.
        # known_hosts="verify" is the opt-in sentinel for callers that
        # have wired up a real cert chain on their BMCs.
        user = device.extra.get("redfish_user") or cfg.get("user") or "admin"
        port = int(device.extra.get("redfish_port") or cfg.get("port") or 443)
        password_ct = cfg.get("password_ciphertext")
        password = decrypt_secret(password_ct) if password_ct else None
        known_hosts = "verify" if cfg.get("verify_tls") else None
        return Credentials(
            user=user,
            host=device.primary_ip,
            port=port,
            private_key_pem=None,
            password=password,
            known_hosts=known_hosts,
            sudo=False,
        )

    if provider == NETWORK_CREDENTIALS_PROVIDER:
        # Junos / IOS-XR / Arista EOS. Default port 22 — the NETCONF
        # adapters override to 830 internally when ``creds.port`` is
        # the SSH default; a tenant who explicitly sets port 830 on
        # the integration row gets that pushed through to NETCONF.
        # ``enable_password`` is only meaningful for IOS-XR (exec-
        # mode escalation) and some EOS deployments under AAA; left
        # ``None`` it's harmless for Junos.
        user = device.extra.get("network_user") or cfg.get("user") or "admin"
        port = int(device.extra.get("network_port") or cfg.get("port") or 22)
        password_ct = cfg.get("password_ciphertext")
        private_key_ct = cfg.get("private_key_ciphertext")
        enable_ct = cfg.get("enable_password_ciphertext")
        password = decrypt_secret(password_ct) if password_ct else None
        private_key_pem = (
            decrypt_secret(private_key_ct) if private_key_ct else None
        )
        enable_password = decrypt_secret(enable_ct) if enable_ct else None
        return Credentials(
            user=user,
            host=device.primary_ip,
            port=port,
            private_key_pem=private_key_pem,
            password=password,
            known_hosts=None,
            sudo=False,
            enable_password=enable_password,
            proxy_url=await _device_tunnel_proxy(db, tenant_id, device),
        )

    # linux_ssh (default)
    user = device.extra.get("ssh_user") or cfg.get("user") or "daalu"
    port = int(device.extra.get("ssh_port") or cfg.get("port") or 22)
    sudo = bool(cfg.get("sudo", True))
    private_key_ct = cfg.get("private_key_ciphertext")
    password_ct = cfg.get("password_ciphertext")
    private_key_pem = decrypt_secret(private_key_ct) if private_key_ct else None
    password = decrypt_secret(password_ct) if password_ct else None
    return Credentials(
        user=user,
        host=device.primary_ip,
        port=port,
        private_key_pem=private_key_pem,
        password=password,
        sudo=sudo,
        proxy_url=await _device_tunnel_proxy(db, tenant_id, device),
    )


# ── Sentinels for static analysis ────────────────────────────────────
# A reviewer can grep for ``LinuxFacts`` in this module to confirm we
# never *create* observed/intended state here — only the SoT and the
# adapter do that. Keeping the import here makes the dependency
# explicit at the top of the file.
_ = LinuxFacts  # noqa: F841 — re-export only for grep-ability
