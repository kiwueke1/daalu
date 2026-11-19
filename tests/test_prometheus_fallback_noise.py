"""Prometheus-only clusters: the "no Alertmanager" rule never becomes an alert."""

from __future__ import annotations

import httpx

from daalu_automation.modules.infra.integrations import PrometheusAdapter


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v2/alerts":
        return httpx.Response(404)
    return httpx.Response(200, json={"data": {"alerts": [
        {"state": "firing", "labels": {"alertname": "PrometheusNotConnectedToAlertmanagers"}},
        {"state": "firing", "labels": {"alertname": "OpenStackImagePullFailing", "namespace": "openstack"}},
        {"state": "pending", "labels": {"alertname": "KubePodNotReady"}},
    ]}})


async def test_fallback_drops_no_alertmanager_rule():
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler), base_url="http://prom") as c:
        alerts = await PrometheusAdapter.__new__(PrometheusAdapter)._fetch_active_alerts(c, "http://prom")
    assert [a[0]["alertname"] for a in alerts] == ["OpenStackImagePullFailing"]
