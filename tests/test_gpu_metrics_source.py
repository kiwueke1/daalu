"""Add GPU connects the cluster's Prometheus so AI Factory shows the card."""

from daalu_automation.gpu_controller.metrics_source import pick_prometheus


def _svc(name, ns="monitoring", ip="10.43.0.1", ports=(9090,)):
    return {"name": name, "namespace": ns, "cluster_ip": ip, "ports": list(ports)}


def test_picks_kube_prometheus_stack_server_over_its_siblings():
    services = [
        _svc("kube-prometheus-stack-operator", ports=(443,)),
        _svc("kube-prometheus-stack-prometheus-node-exporter", ports=(9100,)),
        _svc("kube-prometheus-stack-kube-state-metrics", ports=(8080,)),
        _svc("prometheus-operated", ip="None"),
        _svc("kube-prometheus-stack-prometheus", ports=(9090, 8080)),
        _svc("kube-prometheus-stack-coredns", ns="kube-system", ip="None", ports=(9153,)),
    ]
    assert pick_prometheus(services) == (
        "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090"
    )


def test_falls_back_to_headless_operated_service():
    assert pick_prometheus([_svc("prometheus-operated", ip="None")]) == (
        "http://prometheus-operated.monitoring.svc.cluster.local:9090"
    )


def test_plain_prometheus_chart():
    assert pick_prometheus([_svc("prometheus-server", ns="obs", ports=(9090,))]) == (
        "http://prometheus-server.obs.svc.cluster.local:9090"
    )


def test_none_when_cluster_has_no_prometheus():
    assert pick_prometheus([_svc("grafana", ports=(80,)), _svc("alertmanager-operated", ports=(9093,))]) is None
