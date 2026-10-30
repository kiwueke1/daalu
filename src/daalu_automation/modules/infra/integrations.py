"""Monitoring + paging integrations for the infra/SRE module.

Adapters: synthetic-infra, prometheus, loki, thanos, pagerduty, aws.
"""

from __future__ import annotations

import hashlib
import random
import uuid

import httpx

from daalu_automation.core.events import EventEnvelope, publish
from daalu_automation.core.integrations import (
    IntegrationAdapter,
    IntegrationDescriptor,
    register_integration,
)
from daalu_automation.core.tenant_settings import get_tenant_config
from daalu_automation.database import AsyncSessionLocal


class SyntheticInfraAdapter(IntegrationAdapter):
    """Emits realistic-looking infra events. Used for demos + tests.

    Mirrors what Alertmanager/Prometheus/PagerDuty would push, so the
    downstream agent + UI paths see exactly the same shapes they will in
    production.
    """

    descriptor = IntegrationDescriptor(
        provider="synthetic-infra",
        module="infra",
        display_name="Synthetic infra (demo)",
        description="Generates fake Prometheus/PagerDuty events for demos.",
        required_settings=(),
    )

    _SERVICES = ("payment-service", "checkout-api", "search-service", "user-profile", "billing")
    _CLUSTERS = ("cluster-east-3", "cluster-west-1", "cluster-eu-2")

    async def ingest(self, tenant_id: uuid.UUID) -> int:
        emitted = 0
        service = random.choice(self._SERVICES)
        cluster = random.choice(self._CLUSTERS)
        scenario = random.choice(
            ["latency", "saturation", "deploy_fail", "capacity", "recover"]
        )
        if scenario == "latency":
            await publish(
                EventEnvelope(
                    tenant_id=str(tenant_id),
                    type="infra.alert.fired",
                    module="infra",
                    source="prometheus",
                    severity="critical",
                    summary=f"API latency p99 > 1.5s on {service}",
                    payload={
                        "alert_name": f"HighLatency_{service}",
                        "service": service,
                        "cluster": cluster,
                        "metric": "http_request_duration_seconds_p99",
                        "value": round(random.uniform(1.5, 4.0), 2),
                        "description": (
                            f"p99 latency on {service} crossed 1.5s — "
                            f"correlates with a deploy 12 minutes ago."
                        ),
                    },
                )
            )
            emitted += 1
        elif scenario == "saturation":
            await publish(
                EventEnvelope(
                    tenant_id=str(tenant_id),
                    type="infra.alert.fired",
                    module="infra",
                    source="prometheus",
                    severity="warning",
                    summary=f"CPU saturation on {cluster}",
                    payload={
                        "alert_name": "ClusterCPUSaturation",
                        "cluster": cluster,
                        "value": round(random.uniform(82, 96), 1),
                        "description": (
                            f"Cluster {cluster} CPU > 80% for 10 minutes."
                        ),
                    },
                )
            )
            emitted += 1
        elif scenario == "deploy_fail":
            await publish(
                EventEnvelope(
                    tenant_id=str(tenant_id),
                    type="infra.deployment.failed",
                    module="infra",
                    source="argocd",
                    severity="critical",
                    summary=f"Deployment failed: {service} on {cluster}",
                    payload={
                        "service": service,
                        "cluster": cluster,
                        "revision": f"abc{random.randint(100, 999)}",
                        "description": "Pod CrashLoopBackOff during rollout.",
                    },
                )
            )
            emitted += 1
        elif scenario == "capacity":
            await publish(
                EventEnvelope(
                    tenant_id=str(tenant_id),
                    type="infra.capacity.warning",
                    module="infra",
                    source="capacity-planner",
                    severity="warning",
                    summary=f"Storage utilization > 82% on {cluster}",
                    payload={
                        "cluster": cluster,
                        "utilization_pct": 84,
                        "explanation": "Growth rate suggests exhaustion in ~17 days.",
                        "recommendation": "Provision 2 additional OSDs",
                    },
                )
            )
            emitted += 1
        else:
            await publish(
                EventEnvelope(
                    tenant_id=str(tenant_id),
                    type="infra.alert.resolved",
                    module="infra",
                    source="prometheus",
                    severity="info",
                    summary=f"Resolved: latency back to normal on {service}",
                    payload={"service": service, "cluster": cluster},
                )
            )
            emitted += 1
        return emitted


register_integration(SyntheticInfraAdapter)


def _labels_fingerprint(labels: dict) -> str:
    """Stable fingerprint from a label set — the Prometheus fallback's stand-in
    for Alertmanager's own ``fingerprint`` so re-fires of the same alert collapse
    onto one Alert row."""
    s = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return hashlib.sha1(s.encode()).hexdigest()[:16]


class PrometheusAdapter(IntegrationAdapter):
    descriptor = IntegrationDescriptor(
        provider="prometheus",
        module="infra",
        display_name="Prometheus / Alertmanager",
        description=(
            "Pulls active firing alerts from Alertmanager, or from Prometheus' "
            "own alerting rules when no Alertmanager is present."
        ),
        required_settings=("PROMETHEUS_URL",),
    )

    async def ingest(self, tenant_id: uuid.UUID) -> int:
        from sqlalchemy import select

        from daalu_automation.core.cluster_proxy import get_proxy_url
        from daalu_automation.models import Integration

        async with AsyncSessionLocal() as db:
            cfg = await get_tenant_config(db, tenant_id)
            # Pick up cluster_tunnel_id off the row so the ingest is
            # routed through the same edge proxy the wizard tested against.
            row = (
                await db.execute(
                    select(Integration).where(
                        Integration.tenant_id == tenant_id,
                        Integration.provider == "prometheus",
                    )
                )
            ).scalar_one_or_none()
            proxy = await get_proxy_url(
                db, row.cluster_tunnel_id if row else None
            )
        if not cfg.prometheus_url:
            return 0
        base = cfg.prometheus_url.rstrip("/")
        async with httpx.AsyncClient(timeout=15, proxy=proxy) as client:
            alerts = await self._fetch_active_alerts(client, base)
        # The fingerprints currently firing — anything we previously opened
        # that's no longer in this set has cleared (see _resolve_cleared).
        active_fingerprints = {fp for _labels, _ann, fp in alerts if fp}
        emitted = 0
        for labels, annots, fingerprint in alerts:
            severity = labels.get("severity", "warning")
            # Skip the always-firing heartbeat / non-actionable signals.
            # `Watchdog` carries severity "none" and exists only to prove the
            # alerting pipeline is alive — surfacing it as an Alert is noise.
            if severity in ("none", "") or labels.get("alertname") == "Watchdog":
                continue
            # `publish()` coerces severity into the EventSeverity enum
            # (info/warning/critical); clamp any other label value (e.g.
            # "page") to warning so an unknown severity can't crash the
            # event insert and silently drop the alert.
            if severity not in ("info", "warning", "critical"):
                severity = "warning"
            await publish(
                EventEnvelope(
                    tenant_id=str(tenant_id),
                    type="infra.alert.fired",
                    module="infra",
                    source="prometheus",
                    severity=severity,
                    summary=labels.get("alertname", "Prometheus alert"),
                    payload={
                        "alert_name": labels.get("alertname"),
                        "service": labels.get("service"),
                        "cluster": labels.get("cluster"),
                        # Label-set hash — `compute_fingerprint` trusts this
                        # verbatim so re-fires collapse onto one Alert row.
                        "fingerprint": fingerprint,
                        "labels": labels,
                        "annotations": annots,
                    },
                )
            )
            emitted += 1
        # Auto-close our previously-opened alerts that are no longer firing.
        await self._resolve_cleared(tenant_id, active_fingerprints)
        return emitted

    async def _resolve_cleared(
        self, tenant_id: uuid.UUID, active_fingerprints: set[str]
    ) -> int:
        """Emit ``infra.alert.resolved`` for open alerts sourced from this
        Prometheus that are no longer in the active set, so stale alerts
        auto-close instead of lingering open.

        Scoped via the originating event's ``source`` so we only ever touch
        alerts this adapter opened, and matched on the stored fingerprint —
        the active set carries those same fingerprints.
        """
        from sqlalchemy import select

        from daalu_automation.models import Alert, AlertStatus, Event

        async with AsyncSessionLocal() as db:
            open_alerts = (
                await db.execute(
                    select(Alert)
                    .join(Event, Alert.source_event_id == Event.id)
                    .where(
                        Alert.tenant_id == tenant_id,
                        Alert.status.in_(
                            (AlertStatus.open, AlertStatus.acknowledged)
                        ),
                        Event.source == "prometheus",
                    )
                )
            ).scalars().all()
        cleared = [
            a for a in open_alerts
            if a.fingerprint and a.fingerprint not in active_fingerprints
        ]
        for alert in cleared:
            await publish(
                EventEnvelope(
                    tenant_id=str(tenant_id),
                    type="infra.alert.resolved",
                    module="infra",
                    source="prometheus",
                    severity="info",
                    summary=alert.title,
                    payload={
                        "fingerprint": alert.fingerprint,
                        "alert_name": alert.title,
                    },
                )
            )
        return len(cleared)

    async def _fetch_active_alerts(
        self, client: httpx.AsyncClient, base: str
    ) -> list[tuple[dict, dict, str | None]]:
        """Active alerts as ``(labels, annotations, fingerprint)`` triples.

        Prefers Alertmanager's ``/api/v2/alerts`` (the canonical source — it
        groups, dedups, and honours silences). Many self-hosted GPU clusters
        ship Prometheus *without* Alertmanager, so when that endpoint isn't
        there (404/405) we fall back to Prometheus' own ``/api/v1/alerts`` and
        take the rules currently in the ``firing`` state.
        """
        try:
            r = await client.get(f"{base}/api/v2/alerts")
            r.raise_for_status()
            return [
                (a.get("labels", {}) or {}, a.get("annotations", {}) or {},
                 a.get("fingerprint"))
                for a in (r.json() or [])
            ]
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (404, 405):
                raise
        # Fallback: Prometheus' own alerts API (no Alertmanager present).
        r = await client.get(f"{base}/api/v1/alerts")
        r.raise_for_status()
        out: list[tuple[dict, dict, str | None]] = []
        for a in (r.json().get("data") or {}).get("alerts") or []:
            if a.get("state") != "firing":
                continue
            labels = a.get("labels", {}) or {}
            out.append(
                (labels, a.get("annotations", {}) or {},
                 _labels_fingerprint(labels))
            )
        return out

    async def health(self, tenant_id: uuid.UUID) -> tuple[bool, str]:
        """GET ``<base_url>/-/healthy`` — Prometheus's own readiness URL.

        Routes through the same per-tenant cluster proxy that ``ingest``
        uses, so a tunnel-backed Prometheus probes through the tunnel.
        If the tunnel is down the proxy URL won't resolve and we
        correctly mark the integration as error.
        """
        from sqlalchemy import select

        from daalu_automation.core.cluster_proxy import get_proxy_url
        from daalu_automation.models import Integration

        async with AsyncSessionLocal() as db:
            cfg = await get_tenant_config(db, tenant_id)
            row = (
                await db.execute(
                    select(Integration).where(
                        Integration.tenant_id == tenant_id,
                        Integration.provider == "prometheus",
                    )
                )
            ).scalar_one_or_none()
            proxy = await get_proxy_url(
                db, row.cluster_tunnel_id if row else None
            )
        if not cfg.prometheus_url:
            return False, "PROMETHEUS_URL not configured"
        url = cfg.prometheus_url.rstrip("/") + "/-/healthy"
        try:
            async with httpx.AsyncClient(timeout=5, proxy=proxy) as client:
                r = await client.get(url)
                if r.status_code != 200:
                    return False, f"HTTP {r.status_code}"
            return True, "ok"
        except Exception as e:  # noqa: BLE001 — surfaced verbatim to UI
            return False, str(e)[:500]


class PagerDutyAdapter(IntegrationAdapter):
    descriptor = IntegrationDescriptor(
        provider="pagerduty",
        module="infra",
        display_name="PagerDuty",
        description="Pulls open incidents from PagerDuty.",
        required_settings=("PAGERDUTY_API_TOKEN",),
    )

    async def ingest(self, tenant_id: uuid.UUID) -> int:
        async with AsyncSessionLocal() as db:
            cfg = await get_tenant_config(db, tenant_id)
        if not cfg.pagerduty_api_token:
            return 0
        headers = {
            "Authorization": f"Token token={cfg.pagerduty_api_token}",
            "Accept": "application/vnd.pagerduty+json;version=2",
        }
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            r = await client.get(
                "https://api.pagerduty.com/incidents",
                params={"statuses[]": ["triggered", "acknowledged"]},
            )
            r.raise_for_status()
            data = r.json()
        emitted = 0
        for inc in data.get("incidents", []):
            await publish(
                EventEnvelope(
                    tenant_id=str(tenant_id),
                    type="infra.incident.opened",
                    module="infra",
                    source="pagerduty",
                    severity="critical" if inc.get("urgency") == "high" else "warning",
                    summary=inc.get("title", "PagerDuty incident"),
                    payload={
                        "external_id": inc.get("id"),
                        "service": (inc.get("service") or {}).get("summary"),
                        "urgency": inc.get("urgency"),
                        "html_url": inc.get("html_url"),
                    },
                )
            )
            emitted += 1
        return emitted

    async def health(self, tenant_id: uuid.UUID) -> tuple[bool, str]:
        """GET ``/abilities`` — cheapest authenticated endpoint.

        Returns 200 if the token is valid and PagerDuty is reachable.
        Doesn't bill against any rate limit worth worrying about.
        """
        async with AsyncSessionLocal() as db:
            cfg = await get_tenant_config(db, tenant_id)
        if not cfg.pagerduty_api_token:
            return False, "PAGERDUTY_API_TOKEN not configured"
        headers = {
            "Authorization": f"Token token={cfg.pagerduty_api_token}",
            "Accept": "application/vnd.pagerduty+json;version=2",
        }
        try:
            async with httpx.AsyncClient(timeout=5, headers=headers) as client:
                r = await client.get("https://api.pagerduty.com/abilities")
                if r.status_code != 200:
                    return False, f"HTTP {r.status_code}"
            return True, "ok"
        except Exception as e:  # noqa: BLE001
            return False, str(e)[:500]


class AWSCloudWatchAlarmAdapter(IntegrationAdapter):
    """Polls CloudWatch alarms in ALARM state, emits them as events.

    Mirrors PrometheusAdapter's shape but talks to AWS instead of
    Alertmanager. Credentials come from the per-tenant
    Integration row (provider='aws'), reusing the same session-cache
    and assume-role logic the read-only cloud tools use.

    Dedup: this adapter does NOT dedupe across polls (matching
    PrometheusAdapter). An alarm that stays in ALARM state for an
    hour, with a 1-minute beat cadence, will produce ~60 events.
    Add a Redis SET-NX gate keyed on (alarm_arn, state_updated_at)
    if that becomes noisy in practice.
    """

    descriptor = IntegrationDescriptor(
        provider="aws",
        module="infra",
        display_name="AWS account",
        description=(
            "Polls CloudWatch alarms in ALARM state and emits them as "
            "infra.alert.fired events. Configure with read-only AWS "
            "credentials via the wizard."
        ),
        required_settings=(),  # tenant config comes from the Integration row, not env
    )

    async def ingest(self, tenant_id: uuid.UUID) -> int:
        # Lazy imports: keeps boto3 off the hot path for workers /
        # tenants that don't use AWS, and avoids a circular with
        # core.cloud_aws (which imports from this module's siblings).
        import asyncio

        from daalu_automation.core.cloud_aws import AWSUnavailable, _session

        try:
            session = await _session(tenant_id)
        except AWSUnavailable:
            # Tenant hasn't configured AWS yet — silent no-op, same
            # shape as PrometheusAdapter returning 0 when the URL is unset.
            return 0

        cw = session.client("cloudwatch")
        resp = await asyncio.to_thread(
            cw.describe_alarms,
            StateValue="ALARM",
            AlarmTypes=["MetricAlarm", "CompositeAlarm"],
        )
        alarms = list(resp.get("MetricAlarms", [])) + list(
            resp.get("CompositeAlarms", [])
        )

        emitted = 0
        for a in alarms:
            await publish(
                EventEnvelope(
                    tenant_id=str(tenant_id),
                    type="infra.alert.fired",
                    module="infra",
                    source="aws-cloudwatch",
                    severity="warning",
                    summary=f"AWS CloudWatch: {a.get('AlarmName', '(unnamed)')}",
                    payload={
                        "alert_name": a.get("AlarmName"),
                        "alarm_arn": a.get("AlarmArn"),
                        "region": session.region_name,
                        "namespace": a.get("Namespace"),
                        "metric_name": a.get("MetricName"),
                        "state_reason": a.get("StateReason"),
                        "state_updated_at": str(
                            a.get("StateUpdatedTimestamp", "")
                        ),
                        "dimensions": a.get("Dimensions", []),
                    },
                )
            )
            emitted += 1
        return emitted


async def _probe_integration_url(
    tenant_id: uuid.UUID, provider: str, *health_paths: str
) -> tuple[bool, str]:
    """Probe ``<integration config['url']><health_path>`` for ``provider``.

    Routes through the tenant's cluster proxy (``cluster_tunnel_id``) just like
    the Prometheus adapter, so a tunnel-backed endpoint probes over the tunnel.

    Several ``health_paths`` may be given: they're tried in order and the first
    one that answers marks the integration healthy. That matters because the
    URL operators are told to configure is usually a *gateway* rather than the
    component itself, and a gateway only routes the paths its component serves
    — so the component's own readiness endpoint can 404 while the store is
    perfectly healthy. The last path's failure is what gets reported.

    Auth from the integration config is sent (same shapes the agent's query
    tools accept), and redirects are followed: an auth-gated or
    ingress-redirected store answering 401/302 is reachable, not broken.
    """
    from sqlalchemy import select

    from daalu_automation.core.cluster_proxy import get_proxy_url
    from daalu_automation.core.kube_tools import _auth_header_from_integration
    from daalu_automation.models import Integration

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(Integration).where(
                    Integration.tenant_id == tenant_id,
                    Integration.provider == provider,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return False, f"no {provider} integration for this tenant"
        config = row.config or {}
        url = config.get("url", "")
        proxy = await get_proxy_url(db, row.cluster_tunnel_id)
    if not url:
        return False, "url not configured"

    headers = {}
    auth = _auth_header_from_integration(config)
    if auth:
        headers["Authorization"] = auth

    last = ""
    try:
        async with httpx.AsyncClient(
            timeout=5, proxy=proxy, headers=headers, follow_redirects=True
        ) as client:
            for path in health_paths:
                r = await client.get(url.rstrip("/") + path)
                if r.status_code == 200:
                    return True, "ok"
                last = f"HTTP {r.status_code} from {path}"
        return False, last or "no health path configured"
    except Exception as e:  # noqa: BLE001 — surfaced verbatim to the UI
        return False, str(e)[:500]


class LokiAdapter(IntegrationAdapter):
    """Grafana Loki log store. No event ingest — the alert-chat agent queries
    logs on demand (``core.kube_tools.query_loki`` reads this row's ``url``).
    The probe just keeps the Managed-Infra badge truthful."""

    descriptor = IntegrationDescriptor(
        provider="loki",
        module="infra",
        display_name="Loki (logs)",
        description="Grafana Loki — logs the alert-chat agent queries on demand.",
        required_settings=("LOKI_URL",),
    )

    async def ingest(self, tenant_id: uuid.UUID) -> int:
        return 0  # logs are queried on demand, not ingested as events

    async def health(self, tenant_id: uuid.UUID) -> tuple[bool, str]:
        # Query the label API first, not `/ready`. Operators are told to point
        # this row at the Loki *gateway* (loki-gateway.monitoring.svc), whose
        # nginx only routes `/loki/api/*` and `/api/prom/*` — `/ready` is not a
        # location there, so it 404s and pinned the badge red on a Loki that
        # was serving queries fine. `/loki/api/v1/labels` is the same data path
        # the log console and the agent's query_loki use, so a 200 here means
        # what the badge claims. `/ready` stays as the fallback for rows
        # pointed straight at a Loki read/single-binary target, where the
        # gateway paths don't exist.
        return await _probe_integration_url(
            tenant_id, "loki", "/loki/api/v1/labels", "/ready"
        )


class ThanosAdapter(IntegrationAdapter):
    """Thanos / Prometheus query API for long-history metrics. No event ingest —
    the agent queries metrics on demand (``query_prometheus`` falls back to the
    thanos row's ``url`` for range queries). Speaks the Prometheus HTTP API, so
    its readiness probe is ``/-/healthy`` (works for Thanos Query AND a plain
    Prometheus standing in for it)."""

    descriptor = IntegrationDescriptor(
        provider="thanos",
        module="infra",
        display_name="Thanos (long-history metrics)",
        description="Thanos/Prometheus query API — metrics the agent queries on demand.",
        required_settings=("THANOS_URL",),
    )

    async def ingest(self, tenant_id: uuid.UUID) -> int:
        return 0  # metrics are queried on demand, not ingested as events

    async def health(self, tenant_id: uuid.UUID) -> tuple[bool, str]:
        # `/-/healthy` for a real Thanos Query or a Prometheus standing in for
        # it; the query API as the fallback for a gateway-fronted deployment,
        # for the same reason as Loki above.
        return await _probe_integration_url(
            tenant_id, "thanos", "/-/healthy", "/api/v1/labels"
        )


class OpenSearchAdapter(IntegrationAdapter):
    """OpenSearch log store, the alternative backend to Loki.

    Exists mainly so the health sweep has something to call: without a
    registered adapter ``_probe_one`` falls through to "unknown provider:
    opensearch" and pins the Managed-Infra badge red for every tenant running
    OpenSearch, however healthy the cluster is.
    """

    descriptor = IntegrationDescriptor(
        provider="opensearch",
        module="infra",
        display_name="OpenSearch (logs)",
        description="OpenSearch — log store queried on demand by the log console.",
        required_settings=("OPENSEARCH_URL",),
    )

    async def ingest(self, tenant_id: uuid.UUID) -> int:
        return 0  # logs are queried on demand, not ingested as events

    async def health(self, tenant_id: uuid.UUID) -> tuple[bool, str]:
        # Cluster health first (the honest signal); root as the fallback for
        # deployments that don't expose `_cluster` to the console's role.
        return await _probe_integration_url(
            tenant_id, "opensearch", "/_cluster/health", "/"
        )


# Adapters always register — the Integrations page shows them as
# "needs setup" until any tenant adds the matching integration row.
register_integration(PrometheusAdapter)
register_integration(PagerDutyAdapter)
register_integration(AWSCloudWatchAlarmAdapter)
register_integration(LokiAdapter)
register_integration(ThanosAdapter)
register_integration(OpenSearchAdapter)
