"""Controller-owned telemetry collectors: rendering + defaults."""

from daalu_automation.config_manager_controller.collectors import (
    _DEFAULT_LOKI_URL,
    render_collector_docs,
)


def test_render_substitutes_loki_push_url():
    docs = render_collector_docs("http://my-loki.logging.svc:3100")
    cm = next(d for d in docs if d["kind"] == "ConfigMap")
    body = cm["data"]["config.alloy"]
    assert "http://my-loki.logging.svc:3100/loki/api/v1/push" in body
    assert "__LOKI_PUSH_URL__" not in body


def test_bundle_contains_receiver_deployment():
    kinds = {(d["kind"], d["metadata"]["name"])
             for d in render_collector_docs(_DEFAULT_LOKI_URL)}
    assert ("Deployment", "daalu-network-syslog") in kinds
    assert ("ConfigMap", "daalu-network-syslog") in kinds
