"""Read-only AWS tools exposed to the alert-chat agent.

Tenants register an AWS connection as an Integration row with
``provider='aws'`` and ``config={access_key_id, secret_access_key,
region, [session_token, role_arn, external_id]}``; the secrets are stored
sealed (see ``core.cloud_config``). We build a boto3 Session per tenant and
region and cache it until the stored config changes or an assumed role's
credentials are about to expire.

All tools here are **read-only**. Write tools (restart an instance,
redeploy a function) belong in a future cloud_write module so they
flow through the existing Approve UI.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from daalu_automation.core import cloud_config

logger = structlog.get_logger(__name__)


class AWSUnavailable(RuntimeError):
    """No AWS integration registered, or its credentials are malformed."""


# (tenant, region) -> (config fingerprint, session, expires_at or None)
_session_cache: dict[tuple[str, str], tuple[str, Any, datetime | None]] = {}

# Refresh an assumed-role session this long before STS says it expires.
_ROLE_REFRESH_MARGIN = timedelta(minutes=5)
ROLE_SESSION_NAME = "daalu"


async def _load_aws_config(tenant_id: uuid.UUID | None) -> tuple[dict[str, Any], str]:
    if tenant_id is None:
        raise AWSUnavailable("tenant context missing — cannot resolve AWS integration")
    loaded = await cloud_config.load(tenant_id, "aws")
    if loaded is None:
        raise AWSUnavailable(
            "no AWS integration registered for this tenant — add one with "
            "provider='aws' and config={access_key_id, secret_access_key, region}."
        )
    cfg, fp = loaded
    if not cfg.get("access_key_id") or not cfg.get("secret_access_key"):
        raise AWSUnavailable("AWS integration is missing access_key_id / secret_access_key.")
    return cfg, fp


def build_session(cfg: dict[str, Any], region: str) -> tuple[Any, datetime | None]:
    """A boto3 Session from a connection config, assuming ``role_arn`` when set.

    Returns the session and, for an assumed role, when its credentials
    expire. Blocking (STS call); run it in a thread. Shared with the
    connection test so "Test" exercises exactly what the tools use.
    """
    import boto3  # local import keeps the cold-path light on workers that never touch AWS

    session = boto3.session.Session(
        aws_access_key_id=cfg["access_key_id"],
        aws_secret_access_key=cfg["secret_access_key"],
        aws_session_token=cfg.get("session_token") or None,
        region_name=region,
    )
    role_arn = cfg.get("role_arn")
    if not role_arn:
        return session, None
    kwargs: dict[str, Any] = {
        "RoleArn": role_arn,
        "RoleSessionName": ROLE_SESSION_NAME,
        "DurationSeconds": 3600,
    }
    if cfg.get("external_id"):
        kwargs["ExternalId"] = cfg["external_id"]
    creds = session.client("sts").assume_role(**kwargs)["Credentials"]
    assumed = boto3.session.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )
    return assumed, creds.get("Expiration")


async def _session(tenant_id: uuid.UUID | None, region: str | None = None) -> Any:
    """Return a cached boto3.Session for this tenant.

    Region resolution: explicit kwarg > Integration.config.region. We
    cache on (tenant, region) so callers can opt into a non-default
    region without poisoning the default-region session. The cache entry
    is dropped when the stored config changes (key rotated, role edited)
    and, for an assumed role, shortly before its credentials expire.
    """
    cfg, fp = await _load_aws_config(tenant_id)
    region = region or cfg.get("region") or "us-east-1"
    key = (str(tenant_id), region)
    cached = _session_cache.get(key)
    if cached is not None:
        c_fp, c_session, c_expires = cached
        fresh = c_expires is None or c_expires - _ROLE_REFRESH_MARGIN > datetime.now(timezone.utc)
        if c_fp == fp and fresh:
            return c_session
    session, expires = await asyncio.to_thread(build_session, cfg, region)
    _session_cache[key] = (fp, session, expires)
    return session


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


# ── Read tools ───────────────────────────────────────────────────────────


async def _aws_describe_instances(
    *,
    region: str | None = None,
    instance_ids: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """ec2.describe_instances — return reservations slimmed to the
    fields the agent actually reads (state, type, IPs, tags)."""
    session = await _session(_tenant_id, region)
    ec2 = session.client("ec2")
    kwargs: dict[str, Any] = {}
    if instance_ids:
        kwargs["InstanceIds"] = instance_ids
    if filters:
        kwargs["Filters"] = filters
    resp = await asyncio.to_thread(ec2.describe_instances, **kwargs)
    out: list[dict[str, Any]] = []
    for res in resp.get("Reservations", []):
        for inst in res.get("Instances", []):
            out.append(
                {
                    "InstanceId": inst.get("InstanceId"),
                    "State": (inst.get("State") or {}).get("Name"),
                    "InstanceType": inst.get("InstanceType"),
                    "LaunchTime": inst.get("LaunchTime"),
                    "PrivateIpAddress": inst.get("PrivateIpAddress"),
                    "PublicIpAddress": inst.get("PublicIpAddress"),
                    "AvailabilityZone": (inst.get("Placement") or {}).get(
                        "AvailabilityZone"
                    ),
                    "Tags": {t["Key"]: t["Value"] for t in (inst.get("Tags") or [])},
                    "StateTransitionReason": inst.get("StateTransitionReason"),
                }
            )
    return _json(out)


async def _aws_get_cloudwatch_logs(
    *,
    log_group: str,
    log_stream: str | None = None,
    filter_pattern: str | None = None,
    since_minutes: int = 60,
    limit: int = 100,
    region: str | None = None,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """logs.filter_log_events — pull recent CloudWatch Logs lines."""
    session = await _session(_tenant_id, region)
    logs = session.client("logs")
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(minutes=since_minutes)
    kwargs: dict[str, Any] = {
        "logGroupName": log_group,
        "startTime": int(start.timestamp() * 1000),
        "endTime": int(end.timestamp() * 1000),
        "limit": min(limit, 1000),
    }
    if log_stream:
        kwargs["logStreamNames"] = [log_stream]
    if filter_pattern:
        kwargs["filterPattern"] = filter_pattern
    resp = await asyncio.to_thread(logs.filter_log_events, **kwargs)
    lines = [
        f"{datetime.fromtimestamp(ev['timestamp'] / 1000, tz=timezone.utc).isoformat()}  {ev.get('message', '').rstrip()}"
        for ev in resp.get("events", [])
    ]
    return "\n".join(lines) if lines else "(no log events in this window)"


async def _aws_query_cloudwatch_metric(
    *,
    namespace: str,
    metric_name: str,
    dimensions: dict[str, str] | None = None,
    stat: str = "Average",
    since_minutes: int = 60,
    period_seconds: int = 60,
    region: str | None = None,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """cloudwatch.get_metric_statistics — one metric, one window."""
    session = await _session(_tenant_id, region)
    cw = session.client("cloudwatch")
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(minutes=since_minutes)
    dims = [{"Name": k, "Value": v} for k, v in (dimensions or {}).items()]
    resp = await asyncio.to_thread(
        cw.get_metric_statistics,
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=dims,
        StartTime=start,
        EndTime=end,
        Period=period_seconds,
        Statistics=[stat],
    )
    points = sorted(resp.get("Datapoints", []), key=lambda d: d["Timestamp"])
    return _json({"unit": resp.get("Label"), "datapoints": points})


async def _aws_describe_rds_instances(
    *,
    region: str | None = None,
    db_instance_identifier: str | None = None,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """rds.describe_db_instances — pull instance state, engine, storage,
    and the latest event window so the agent can spot maintenance
    interventions."""
    session = await _session(_tenant_id, region)
    rds = session.client("rds")
    kwargs: dict[str, Any] = {}
    if db_instance_identifier:
        kwargs["DBInstanceIdentifier"] = db_instance_identifier
    resp = await asyncio.to_thread(rds.describe_db_instances, **kwargs)
    out: list[dict[str, Any]] = []
    for d in resp.get("DBInstances", []):
        out.append(
            {
                "DBInstanceIdentifier": d.get("DBInstanceIdentifier"),
                "Engine": d.get("Engine"),
                "EngineVersion": d.get("EngineVersion"),
                "DBInstanceClass": d.get("DBInstanceClass"),
                "DBInstanceStatus": d.get("DBInstanceStatus"),
                "Endpoint": d.get("Endpoint"),
                "AllocatedStorage": d.get("AllocatedStorage"),
                "StorageType": d.get("StorageType"),
                "MultiAZ": d.get("MultiAZ"),
                "AvailabilityZone": d.get("AvailabilityZone"),
                "PendingModifiedValues": d.get("PendingModifiedValues"),
            }
        )
    return _json(out)


async def _aws_describe_lambda(
    *,
    function_name: str,
    region: str | None = None,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """lambda.get_function + recent invocation errors from CloudWatch.

    The two-call pattern (config + recent error count) is what an
    operator usually wants when an alert mentions a Lambda — "what
    is it configured to do, and is it actively failing right now?".
    """
    session = await _session(_tenant_id, region)
    lam = session.client("lambda")
    cw = session.client("cloudwatch")
    fn = await asyncio.to_thread(lam.get_function, FunctionName=function_name)
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(minutes=60)
    errors = await asyncio.to_thread(
        cw.get_metric_statistics,
        Namespace="AWS/Lambda",
        MetricName="Errors",
        Dimensions=[{"Name": "FunctionName", "Value": function_name}],
        StartTime=start,
        EndTime=end,
        Period=300,
        Statistics=["Sum"],
    )
    return _json(
        {
            "Configuration": fn.get("Configuration"),
            "Code": {
                # Strip the pre-signed Code.Location URL — it's a giant
                # signed S3 link and the agent doesn't need it.
                "RepositoryType": (fn.get("Code") or {}).get("RepositoryType"),
            },
            "RecentErrors": errors.get("Datapoints", []),
        }
    )


# ── Registry ─────────────────────────────────────────────────────────────


def tool_specs() -> dict[str, dict[str, Any]]:
    """Return the AWS tools in the shape ``kube_tools.TOOLS`` expects.

    We return raw dicts here (not ToolSpec instances) to avoid a
    circular import with ``kube_tools``; the importer wraps each entry
    in a ToolSpec.
    """
    return {
        "aws_describe_instances": {
            "description": (
                "List EC2 instances for this tenant's AWS account, optionally "
                "filtered by instance IDs or filters (e.g. tag:Name=daalu-api). "
                "Returns state, type, IPs, AZ and tags — enough to spot a "
                "stopped or impaired box without dumping the full describe "
                "blob."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "region": {"type": "string"},
                    "instance_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "filters": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Boto3 Filters list, e.g. [{Name: tag:Environment, Values: [prod]}].",
                    },
                },
            },
            "handler": _aws_describe_instances,
        },
        "aws_get_cloudwatch_logs": {
            "description": (
                "Fetch recent CloudWatch Logs lines from a log group, with an "
                "optional filter pattern. Use this first whenever an alert "
                "references an AWS-hosted service emitting logs (Lambda, ECS, "
                "EKS control plane, RDS error logs, ALB access logs, etc.)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "log_group": {"type": "string"},
                    "log_stream": {
                        "type": "string",
                        "description": "Optional specific stream within the group.",
                    },
                    "filter_pattern": {
                        "type": "string",
                        "description": "CloudWatch filter pattern, e.g. 'ERROR' or '?WARN ?ERROR'.",
                    },
                    "since_minutes": {"type": "integer", "default": 60},
                    "limit": {"type": "integer", "default": 100, "maximum": 1000},
                    "region": {"type": "string"},
                },
                "required": ["log_group"],
            },
            "handler": _aws_get_cloudwatch_logs,
        },
        "aws_query_cloudwatch_metric": {
            "description": (
                "Read a single CloudWatch metric over a time window. Use for "
                "any AWS-side latency / error-rate / saturation signal the "
                "alert references (e.g. AWS/ApplicationELB Target5XXCount, "
                "AWS/RDS CPUUtilization, AWS/Lambda Throttles)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "CloudWatch namespace, e.g. 'AWS/RDS', 'AWS/Lambda'.",
                    },
                    "metric_name": {"type": "string"},
                    "dimensions": {
                        "type": "object",
                        "description": "Dimension name → value, e.g. {DBInstanceIdentifier: prod-db}.",
                    },
                    "stat": {
                        "type": "string",
                        "enum": ["Average", "Sum", "Minimum", "Maximum", "SampleCount"],
                        "default": "Average",
                    },
                    "since_minutes": {"type": "integer", "default": 60},
                    "period_seconds": {"type": "integer", "default": 60},
                    "region": {"type": "string"},
                },
                "required": ["namespace", "metric_name"],
            },
            "handler": _aws_query_cloudwatch_metric,
        },
        "aws_describe_rds_instances": {
            "description": (
                "List RDS DB instances for this tenant — state, engine, "
                "storage, endpoint, MultiAZ, pending modifications. Use when "
                "an alert points at a database or when investigating "
                "saturation / failover events."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "db_instance_identifier": {"type": "string"},
                    "region": {"type": "string"},
                },
            },
            "handler": _aws_describe_rds_instances,
        },
        "aws_describe_lambda": {
            "description": (
                "Pull a Lambda function's configuration AND its CloudWatch "
                "Errors metric over the last hour, so the agent can decide "
                "in one call whether the function is failing right now and "
                "how it's wired (runtime, memory, env)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "function_name": {"type": "string"},
                    "region": {"type": "string"},
                },
                "required": ["function_name"],
            },
            "handler": _aws_describe_lambda,
        },
    }
