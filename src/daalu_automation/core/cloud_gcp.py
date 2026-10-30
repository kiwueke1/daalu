"""Read-only Google Cloud tools exposed to the alert-chat agent.

Tenants register a GCP connection as an Integration row with
``provider='gcp'`` and ``config={service_account_json, project_id,
[region]}``. ``service_account_json`` is the full SA key JSON as a
string (not pre-parsed) so the operator can paste it directly.

Auth uses ``google.oauth2.service_account.Credentials`` built from
the JSON. We cache one set of credentials per tenant.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from daalu_automation.database import AsyncSessionLocal
from daalu_automation.models import Integration

logger = structlog.get_logger(__name__)


class GCPUnavailable(RuntimeError):
    """No GCP integration registered, or its credentials are malformed."""


_creds_cache: dict[str, Any] = {}


async def _load_gcp_config(tenant_id: uuid.UUID | None) -> dict[str, Any]:
    if tenant_id is None:
        raise GCPUnavailable("tenant context missing — cannot resolve GCP integration")
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                Integration.__table__.select().where(
                    Integration.tenant_id == tenant_id,
                    Integration.provider == "gcp",
                )
            )
        ).first()
    if row is None:
        raise GCPUnavailable(
            "no GCP integration registered for this tenant — add one with "
            "provider='gcp' and config={service_account_json, project_id}."
        )
    cfg = row._mapping["config"] or {}
    if not cfg.get("service_account_json") or not cfg.get("project_id"):
        raise GCPUnavailable("GCP integration is missing service_account_json / project_id.")
    return cfg


async def _credentials(tenant_id: uuid.UUID | None) -> tuple[Any, str]:
    """Return (Credentials, project_id) for this tenant.

    Returns the project_id explicitly because none of the typed
    clients infer it from the service-account JSON reliably in all
    SDK versions — we'd rather be explicit than pull surprise
    projects.
    """
    from google.oauth2 import service_account

    cfg = await _load_gcp_config(tenant_id)
    key = str(tenant_id)
    cached = _creds_cache.get(key)
    if cached is None:
        raw = cfg["service_account_json"]
        info = json.loads(raw) if isinstance(raw, str) else raw
        cached = service_account.Credentials.from_service_account_info(info)
        _creds_cache[key] = cached
    return cached, cfg["project_id"]


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


# ── Read tools ───────────────────────────────────────────────────────────


async def _gcp_list_instances(
    *,
    zone: str | None = None,
    filter_expr: str | None = None,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """compute_v1.InstancesClient — list Compute Engine VMs across zones
    (or one zone if specified)."""
    from google.cloud import compute_v1

    creds, project = await _credentials(_tenant_id)
    client = compute_v1.InstancesClient(credentials=creds)

    def _list_one(z: str) -> list[dict[str, Any]]:
        req = compute_v1.ListInstancesRequest(project=project, zone=z, filter=filter_expr or "")
        items: list[dict[str, Any]] = []
        for inst in client.list(request=req):
            items.append(
                {
                    "name": inst.name,
                    "status": inst.status,
                    "zone": z,
                    "machineType": inst.machine_type.rsplit("/", 1)[-1],
                    "networkIP": (
                        inst.network_interfaces[0].network_i_p
                        if inst.network_interfaces
                        else None
                    ),
                    "externalIP": (
                        inst.network_interfaces[0].access_configs[0].nat_i_p
                        if inst.network_interfaces
                        and inst.network_interfaces[0].access_configs
                        else None
                    ),
                    "creationTimestamp": inst.creation_timestamp,
                    "labels": dict(inst.labels) if inst.labels else {},
                }
            )
        return items

    def _list_all() -> list[dict[str, Any]]:
        if zone:
            return _list_one(zone)
        agg_req = compute_v1.AggregatedListInstancesRequest(
            project=project,
            filter=filter_expr or "",
        )
        out: list[dict[str, Any]] = []
        for scoped_zone, scoped in client.aggregated_list(request=agg_req):
            if not scoped.instances:
                continue
            z = scoped_zone.split("/", 1)[-1]
            for inst in scoped.instances:
                out.append(
                    {
                        "name": inst.name,
                        "status": inst.status,
                        "zone": z,
                        "machineType": inst.machine_type.rsplit("/", 1)[-1],
                        "creationTimestamp": inst.creation_timestamp,
                        "labels": dict(inst.labels) if inst.labels else {},
                    }
                )
        return out

    items = await asyncio.to_thread(_list_all)
    return _json(items)


async def _gcp_query_logging(
    *,
    filter_expr: str,
    since_minutes: int = 60,
    limit: int = 100,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """logging_v2.Client.list_entries — recent log entries matching the
    advanced-filter expression. Use the same filter language the
    Console's Logs Explorer accepts."""
    from google.cloud import logging as glogging

    creds, project = await _credentials(_tenant_id)
    client = glogging.Client(project=project, credentials=creds)
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(minutes=since_minutes)
    full_filter = (
        f'timestamp>="{start.isoformat()}" timestamp<="{end.isoformat()}" {filter_expr}'
    )

    def _list() -> list[str]:
        lines: list[str] = []
        for entry in client.list_entries(
            filter_=full_filter, order_by=glogging.DESCENDING, page_size=limit
        ):
            payload = entry.payload
            if isinstance(payload, dict):
                payload_str = json.dumps(payload, default=str)
            else:
                payload_str = str(payload)
            lines.append(f"{entry.timestamp.isoformat()}  [{entry.severity}]  {payload_str}")
            if len(lines) >= limit:
                break
        return lines

    lines = await asyncio.to_thread(_list)
    return "\n".join(lines) if lines else "(no log entries match this filter)"


async def _gcp_query_monitoring(
    *,
    metric_type: str,
    filter_expr: str | None = None,
    since_minutes: int = 60,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """monitoring_v3.MetricServiceClient.list_time_series — pull one
    metric over a window."""
    from google.cloud import monitoring_v3

    creds, project = await _credentials(_tenant_id)
    client = monitoring_v3.MetricServiceClient(credentials=creds)
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(minutes=since_minutes)
    interval = monitoring_v3.TimeInterval(
        {
            "end_time": {"seconds": int(end.timestamp())},
            "start_time": {"seconds": int(start.timestamp())},
        }
    )
    pieces = [f'metric.type="{metric_type}"']
    if filter_expr:
        pieces.append(filter_expr)
    request_filter = " AND ".join(pieces)

    def _list() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for series in client.list_time_series(
            name=f"projects/{project}",
            filter=request_filter,
            interval=interval,
            view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        ):
            out.append(
                {
                    "metric": dict(series.metric.labels),
                    "resource": dict(series.resource.labels),
                    "points": [
                        {
                            "time": p.interval.end_time.isoformat(),
                            "value": (
                                p.value.double_value
                                if p.value.double_value
                                else p.value.int64_value
                            ),
                        }
                        for p in series.points
                    ],
                }
            )
        return out

    items = await asyncio.to_thread(_list)
    return _json(items)


async def _gcp_describe_sql_instance(
    *,
    instance: str,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """Cloud SQL Admin API — describe one instance via the discovery
    client (typed SDK is split across packages)."""
    from googleapiclient.discovery import build

    creds, project = await _credentials(_tenant_id)

    def _describe() -> dict[str, Any]:
        svc = build("sqladmin", "v1beta4", credentials=creds, cache_discovery=False)
        return svc.instances().get(project=project, instance=instance).execute()

    return _json(await asyncio.to_thread(_describe))


async def _gcp_describe_function(
    *,
    function_name: str,
    region: str,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """Cloud Functions v2 — describe one function (config + state)."""
    from google.cloud import functions_v2

    creds, project = await _credentials(_tenant_id)
    client = functions_v2.FunctionServiceClient(credentials=creds)
    name = f"projects/{project}/locations/{region}/functions/{function_name}"

    def _describe() -> dict[str, Any]:
        fn = client.get_function(name=name)
        return {
            "name": fn.name,
            "state": fn.state.name,
            "buildConfig": {
                "runtime": fn.build_config.runtime,
                "entryPoint": fn.build_config.entry_point,
            },
            "serviceConfig": {
                "uri": fn.service_config.uri,
                "availableMemory": fn.service_config.available_memory,
                "timeoutSeconds": fn.service_config.timeout_seconds,
                "minInstanceCount": fn.service_config.min_instance_count,
                "maxInstanceCount": fn.service_config.max_instance_count,
            },
            "updateTime": fn.update_time.isoformat() if fn.update_time else None,
        }

    return _json(await asyncio.to_thread(_describe))


# ── Registry ─────────────────────────────────────────────────────────────


def tool_specs() -> dict[str, dict[str, Any]]:
    return {
        "gcp_list_instances": {
            "description": (
                "List Compute Engine VM instances for this tenant's GCP "
                "project. Defaults to aggregated-list across all zones; pass "
                "a `zone` to scope to one. Use `filter_expr` for advanced "
                "filtering (e.g. 'labels.env=prod')."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "zone": {"type": "string"},
                    "filter_expr": {"type": "string"},
                },
            },
            "handler": _gcp_list_instances,
        },
        "gcp_query_logging": {
            "description": (
                "Query Cloud Logging using the same advanced-filter syntax "
                "the Console's Logs Explorer accepts (e.g. "
                "'resource.type=\"gce_instance\" severity>=ERROR'). Use first "
                "for any GCP-side alert that references logs."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "filter_expr": {"type": "string"},
                    "since_minutes": {"type": "integer", "default": 60},
                    "limit": {"type": "integer", "default": 100, "maximum": 1000},
                },
                "required": ["filter_expr"],
            },
            "handler": _gcp_query_logging,
        },
        "gcp_query_monitoring": {
            "description": (
                "Read a Cloud Monitoring metric over a window (e.g. "
                "'compute.googleapis.com/instance/cpu/utilization'). Optional "
                "`filter_expr` for extra MQL/MetricFilter clauses."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "metric_type": {"type": "string"},
                    "filter_expr": {"type": "string"},
                    "since_minutes": {"type": "integer", "default": 60},
                },
                "required": ["metric_type"],
            },
            "handler": _gcp_query_monitoring,
        },
        "gcp_describe_sql_instance": {
            "description": (
                "Describe a Cloud SQL instance (settings, replica state, "
                "maintenance window, current operations)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "instance": {"type": "string"},
                },
                "required": ["instance"],
            },
            "handler": _gcp_describe_sql_instance,
        },
        "gcp_describe_function": {
            "description": (
                "Describe a Cloud Functions (gen2) function — runtime, "
                "entry point, memory, scaling, state."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "function_name": {"type": "string"},
                    "region": {"type": "string"},
                },
                "required": ["function_name", "region"],
            },
            "handler": _gcp_describe_function,
        },
    }
