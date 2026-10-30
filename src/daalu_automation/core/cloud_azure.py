"""Read-only Azure tools exposed to the alert-chat agent.

Tenants register an Azure connection as an Integration row with
``provider='azure'`` and ``config={tenant_id, client_id,
client_secret, subscription_id, [region]}``. Auth uses a
``ClientSecretCredential`` from the azure-identity SDK — equivalent
to ``az login --service-principal``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any

import structlog

from daalu_automation.database import AsyncSessionLocal
from daalu_automation.models import Integration

logger = structlog.get_logger(__name__)


class AzureUnavailable(RuntimeError):
    """No Azure integration registered, or its credentials are malformed."""


_creds_cache: dict[str, tuple[Any, str]] = {}


async def _load_azure_config(tenant_id: uuid.UUID | None) -> dict[str, Any]:
    if tenant_id is None:
        raise AzureUnavailable("tenant context missing — cannot resolve Azure integration")
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                Integration.__table__.select().where(
                    Integration.tenant_id == tenant_id,
                    Integration.provider == "azure",
                )
            )
        ).first()
    if row is None:
        raise AzureUnavailable(
            "no Azure integration registered for this tenant — add one with "
            "provider='azure' and config={tenant_id, client_id, client_secret, "
            "subscription_id}."
        )
    cfg = row._mapping["config"] or {}
    for key in ("tenant_id", "client_id", "client_secret", "subscription_id"):
        if not cfg.get(key):
            raise AzureUnavailable(f"Azure integration is missing {key}.")
    return cfg


async def _credentials(tenant_id: uuid.UUID | None) -> tuple[Any, str]:
    """Return (ClientSecretCredential, subscription_id) for this tenant."""
    from azure.identity import ClientSecretCredential

    cfg = await _load_azure_config(tenant_id)
    key = str(tenant_id)
    cached = _creds_cache.get(key)
    if cached is None:
        cred = ClientSecretCredential(
            tenant_id=cfg["tenant_id"],
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
        )
        cached = (cred, cfg["subscription_id"])
        _creds_cache[key] = cached
    return cached


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


# ── Read tools ───────────────────────────────────────────────────────────


async def _azure_list_vms(
    *,
    resource_group: str | None = None,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """Compute Management — list all VMs in the subscription, optionally
    scoped to one resource group."""
    from azure.mgmt.compute import ComputeManagementClient

    cred, sub = await _credentials(_tenant_id)
    client = ComputeManagementClient(cred, sub)

    def _list() -> list[dict[str, Any]]:
        if resource_group:
            it = client.virtual_machines.list(resource_group)
        else:
            it = client.virtual_machines.list_all()
        out: list[dict[str, Any]] = []
        for vm in it:
            out.append(
                {
                    "name": vm.name,
                    "id": vm.id,
                    "location": vm.location,
                    "vmSize": vm.hardware_profile.vm_size if vm.hardware_profile else None,
                    "osType": (
                        vm.storage_profile.os_disk.os_type.value
                        if vm.storage_profile and vm.storage_profile.os_disk
                        else None
                    ),
                    "tags": dict(vm.tags) if vm.tags else {},
                    "provisioningState": vm.provisioning_state,
                }
            )
        return out

    return _json(await asyncio.to_thread(_list))


async def _azure_query_log_analytics(
    *,
    workspace_id: str,
    kusto: str,
    since_minutes: int = 60,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """Azure Monitor — run a Kusto query against a Log Analytics
    workspace. The workspace_id is the GUID (not the resource ID)."""
    from azure.monitor.query import LogsQueryClient

    cred, _sub = await _credentials(_tenant_id)
    client = LogsQueryClient(cred)
    timespan = timedelta(minutes=since_minutes)

    def _query() -> dict[str, Any]:
        resp = client.query_workspace(workspace_id=workspace_id, query=kusto, timespan=timespan)
        # LogsQueryStatus.SUCCESS → resp.tables; PARTIAL → resp.partial_data
        tables = getattr(resp, "tables", None) or getattr(resp, "partial_data", [])
        out: list[dict[str, Any]] = []
        for t in tables:
            cols = [c.name for c in t.columns]
            for row in t.rows:
                out.append({c: v for c, v in zip(cols, row, strict=False)})
        return {"rows": out, "status": str(getattr(resp, "status", "ok"))}

    return _json(await asyncio.to_thread(_query))


async def _azure_query_metrics(
    *,
    resource_id: str,
    metric_names: list[str],
    aggregation: str = "Average",
    since_minutes: int = 60,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """Azure Monitor — query one or more metrics for a single resource.

    ``resource_id`` is the full ARM resource ID (e.g.
    ``/subscriptions/.../resourceGroups/.../providers/Microsoft.Web/sites/myapp``).
    """
    from azure.monitor.query import MetricAggregationType, MetricsQueryClient

    cred, _sub = await _credentials(_tenant_id)
    client = MetricsQueryClient(cred)
    timespan = timedelta(minutes=since_minutes)
    agg_map = {
        "Average": MetricAggregationType.AVERAGE,
        "Total": MetricAggregationType.TOTAL,
        "Minimum": MetricAggregationType.MINIMUM,
        "Maximum": MetricAggregationType.MAXIMUM,
        "Count": MetricAggregationType.COUNT,
    }
    agg = agg_map.get(aggregation, MetricAggregationType.AVERAGE)

    def _query() -> dict[str, Any]:
        resp = client.query_resource(
            resource_uri=resource_id,
            metric_names=metric_names,
            timespan=timespan,
            aggregations=[agg],
        )
        out: list[dict[str, Any]] = []
        for m in resp.metrics:
            series_out: list[dict[str, Any]] = []
            for s in m.timeseries:
                series_out.append(
                    {
                        "metadata": {kv.name: kv.value for kv in s.metadata_values or []},
                        "points": [
                            {
                                "time": p.timestamp.isoformat() if p.timestamp else None,
                                "value": getattr(p, aggregation.lower(), None),
                            }
                            for p in s.data
                        ],
                    }
                )
            out.append({"name": m.name, "unit": m.unit.value if m.unit else None, "series": series_out})
        return {"metrics": out}

    return _json(await asyncio.to_thread(_query))


async def _azure_describe_sql_db(
    *,
    resource_group: str,
    server_name: str,
    database_name: str,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """SQL Management — describe an Azure SQL database (sku, state,
    earliest restore point, etc.)."""
    from azure.mgmt.sql import SqlManagementClient

    cred, sub = await _credentials(_tenant_id)
    client = SqlManagementClient(cred, sub)

    def _describe() -> dict[str, Any]:
        db = client.databases.get(resource_group, server_name, database_name)
        return {
            "name": db.name,
            "id": db.id,
            "location": db.location,
            "status": db.status,
            "sku": {
                "name": db.sku.name if db.sku else None,
                "tier": db.sku.tier if db.sku else None,
                "capacity": db.sku.capacity if db.sku else None,
            },
            "maxSizeBytes": db.max_size_bytes,
            "earliestRestoreDate": (
                db.earliest_restore_date.isoformat() if db.earliest_restore_date else None
            ),
            "currentServiceObjectiveName": db.current_service_objective_name,
        }

    return _json(await asyncio.to_thread(_describe))


async def _azure_describe_function(
    *,
    resource_group: str,
    name: str,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """Web Management — describe a Function App (Azure's FaaS surface
    lives under Microsoft.Web/sites)."""
    from azure.mgmt.web import WebSiteManagementClient

    cred, sub = await _credentials(_tenant_id)
    client = WebSiteManagementClient(cred, sub)

    def _describe() -> dict[str, Any]:
        app = client.web_apps.get(resource_group, name)
        config = client.web_apps.get_configuration(resource_group, name)
        return {
            "name": app.name,
            "state": app.state,
            "kind": app.kind,
            "defaultHostName": app.default_host_name,
            "enabled": app.enabled,
            "httpsOnly": app.https_only,
            "siteConfig": {
                "linuxFxVersion": config.linux_fx_version,
                "appCommandLine": config.app_command_line,
                "alwaysOn": config.always_on,
                "minTlsVersion": config.min_tls_version,
            },
            "lastModifiedTimeUtc": (
                app.last_modified_time_utc.isoformat() if app.last_modified_time_utc else None
            ),
        }

    return _json(await asyncio.to_thread(_describe))


# ── Registry ─────────────────────────────────────────────────────────────


def tool_specs() -> dict[str, dict[str, Any]]:
    return {
        "azure_list_vms": {
            "description": (
                "List Azure Virtual Machines in this tenant's subscription, "
                "optionally scoped to a resource group. Returns name, "
                "location, vmSize, osType, tags, provisioningState."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "resource_group": {"type": "string"},
                },
            },
            "handler": _azure_list_vms,
        },
        "azure_query_log_analytics": {
            "description": (
                "Run a Kusto query against an Azure Log Analytics workspace. "
                "`workspace_id` is the workspace GUID (Properties → Workspace "
                "ID in the Portal). Use first for any Azure-side log query — "
                "App Insights, Container Apps, Function logs all flow through "
                "Log Analytics."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "kusto": {"type": "string"},
                    "since_minutes": {"type": "integer", "default": 60},
                },
                "required": ["workspace_id", "kusto"],
            },
            "handler": _azure_query_log_analytics,
        },
        "azure_query_metrics": {
            "description": (
                "Read Azure Monitor metrics for one resource. `resource_id` is "
                "the full ARM ID. Pass a list of `metric_names` and an "
                "aggregation type."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "resource_id": {"type": "string"},
                    "metric_names": {"type": "array", "items": {"type": "string"}},
                    "aggregation": {
                        "type": "string",
                        "enum": ["Average", "Total", "Minimum", "Maximum", "Count"],
                        "default": "Average",
                    },
                    "since_minutes": {"type": "integer", "default": 60},
                },
                "required": ["resource_id", "metric_names"],
            },
            "handler": _azure_query_metrics,
        },
        "azure_describe_sql_db": {
            "description": (
                "Describe an Azure SQL database — sku, status, max size, "
                "earliest restore date, current service objective."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "resource_group": {"type": "string"},
                    "server_name": {"type": "string"},
                    "database_name": {"type": "string"},
                },
                "required": ["resource_group", "server_name", "database_name"],
            },
            "handler": _azure_describe_sql_db,
        },
        "azure_describe_function": {
            "description": (
                "Describe an Azure Function App — state, kind, host name, "
                "TLS settings, runtime (linuxFxVersion), alwaysOn flag."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "resource_group": {"type": "string"},
                    "name": {"type": "string"},
                },
                "required": ["resource_group", "name"],
            },
            "handler": _azure_describe_function,
        },
    }
