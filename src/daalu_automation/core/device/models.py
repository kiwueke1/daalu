"""Pydantic contracts shared by every :class:`DeviceAdapter`.

These shapes are deliberately narrow: the adapter ABC is the one
boundary every device-config push has to cross before the executor can
actually touch a machine, so we want the contract to be small and the
intent self-explanatory.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Credentials(BaseModel):
    """Per-device transport credentials, already decrypted.

    The reconciler / executor resolves these via
    ``core.change_proposals._resolve_credentials`` — v1 reads a
    tenant-wide ``Integration(provider="ssh_credentials")`` row,
    decrypts the private key, and projects it onto each device's
    ``primary_ip``. PR 2 will extend the resolver to honor per-device
    overrides without changing this shape.
    """

    user: str
    host: str
    port: int = 22
    private_key_pem: str | None = None
    password: str | None = None
    known_hosts: str | None = None
    sudo: bool = True
    # Cisco IOS-XR (and some EOS deployments under AAA) require a
    # separate "enable" secret to drop into exec mode. Stored here
    # decrypted so the adapter never has to reach back into the
    # Integration row. Irrelevant for Linux SSH / Redfish / Junos
    # (Junos has no enable mode); those adapters leave it ``None``.
    enable_password: str | None = None
    # HTTP CONNECT proxy to dial ``host:port`` through — the daalu-edge
    # forward proxy (``http://<tunnel_ip>:8888``) when the device lives
    # behind a tunneled customer cluster and its private IP doesn't route
    # from the hub. ``None`` → direct dial. SSH-shaped consumers
    # (linux_ssh adapter, LLDP probe, device terminal) open the socket via
    # ``core.cluster_proxy.open_connect_socket`` and hand it to asyncssh.
    proxy_url: str | None = None
    # Transport-specific connection context for adapters whose target is a
    # control-plane API rather than an SSH/BMC host (e.g. the NV-CM
    # ``config_manager`` executor needs the per-tenant service URLs + a
    # device UUID, none of which fit the SSH-shaped fields above). Resolved
    # by ``resolve_credentials`` and read by the adapter in ``execute()``.
    # In-memory only — never persisted. Empty for the SSH/BMC/NOS adapters.
    extra: dict[str, Any] = Field(default_factory=dict)


class RenderedConfig(BaseModel):
    """Canonical text representation of intended facts.

    The diff-and-execute pipeline both stores this as the
    ``intended_config`` snapshot on the ChangeProposal AND uses it as
    the source of truth for what gets written. ``renderer_version``
    lets us evolve the renderer without invalidating in-flight
    proposals indiscriminately — stale-detection compares the
    ``files`` mapping directly, not the version string.
    """

    renderer_version: str = "linux_ssh.v1"
    files: dict[str, str] = Field(default_factory=dict)
    summary: str = ""


class ConfigDiff(BaseModel):
    """Per-fact diff output.

    ``facts_changed`` lets the UI render a per-fact card; the
    ``unified_diff`` is what the operator reads before approving.
    """

    facts_changed: list[str] = Field(default_factory=list)
    unified_diff: str = ""
    has_changes: bool = False


class ExecutionResult(BaseModel):
    success: bool
    started_at: str
    finished_at: str
    per_step: list[dict[str, Any]] = Field(default_factory=list)
    rollback_performed: bool = False
    error: str = ""
    snapshot_uri: str | None = None
