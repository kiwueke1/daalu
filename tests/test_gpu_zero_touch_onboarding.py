"""Guards for the zero-touch parts of GPU onboarding.

Each test here pins a manual step that used to be a prerequisite the UI
couldn't do for you, and whose absence failed *late* and unhelpfully — a pod
stuck Pending, or a bare 401 from the customer's apiserver minutes into a
provision. Pure functions and a stub DB; no cluster needed.
"""

from __future__ import annotations

from daalu_automation.core.wireguard import (
    CODER_PROXY_PORT,
    VLLM_PROXY_PORT,
    render_edge_install_snippet,
)
from daalu_automation.gpu_controller.k8s import derive_gpu_class

# ── gpu-class derivation (replaces `kubectl label node …`) ──────────────


def test_derive_gpu_class_from_gfd_vram_label():
    """The class comes from GPU Feature Discovery, not a hand-applied label."""
    assert derive_gpu_class({"nvidia.com/gpu.memory": "49140"}) == "ada-48"
    assert derive_gpu_class({"nvidia.com/gpu.memory": "16376"}) == "ada-16"


def test_derive_gpu_class_falls_through_to_largest_for_unknown_cards():
    """An unlisted bigger card must classify, not vanish from the picker."""
    assert derive_gpu_class({"nvidia.com/gpu.memory": "81920"}) == "ada-48"


def test_derive_gpu_class_returns_none_when_unlabelled():
    """No VRAM label → caller keeps whatever class was explicitly requested."""
    assert derive_gpu_class({}) is None
    assert derive_gpu_class({"nvidia.com/gpu.memory": ""}) is None
    assert derive_gpu_class({"nvidia.com/gpu.memory": "not-a-number"}) is None


# ── edge install snippet (replaces the follow-up `helm upgrade`) ────────


def _snippet(**kw):
    return render_edge_install_snippet(
        operator_endpoint="wg.daalu.io",
        operator_api_url="https://ops.daalu.io",
        hub_pubkey="HUBKEY=",
        cluster_slug="cp03",
        invite_token_plain="TOKEN123",
        tunnel_ip="10.200.0.2",
        edge_image_tag="abc123",
        **kw,
    )


def test_snippet_prewires_both_vllm_service_proxies():
    """Without these the tunnel has no inference listener and the GPU reads
    "offline" even once vLLM is healthy — previously a second helm upgrade."""
    out = _snippet()
    assert f"serviceProxies[0].listenPort={VLLM_PROXY_PORT}" in out
    assert "serviceProxies[0].upstream=llm-classifier.daalu.svc.cluster.local:80" in out
    assert f"serviceProxies[1].listenPort={CODER_PROXY_PORT}" in out
    assert "serviceProxies[1].upstream=llm-coder.daalu.svc.cluster.local:80" in out


def test_classifier_and_coder_ports_are_distinct_and_not_the_edge_proxy():
    """8888 is the forward proxy — it only speaks absolute-form requests, so
    pointing inference at it yields a GPU that looks permanently offline."""
    assert VLLM_PROXY_PORT != CODER_PROXY_PORT
    assert 8888 not in (VLLM_PROXY_PORT, CODER_PROXY_PORT)


def test_onboarding_prefills_the_same_port_the_snippet_opens():
    """The form's suggested URL and the listener the customer installs are
    built from one constant, so they cannot drift apart (they once did: the
    form suggested the coder's port for the classifier)."""
    from daalu_automation.api.routers.onboarding import _VLLM_PROXY_PORT

    assert _VLLM_PROXY_PORT == VLLM_PROXY_PORT
    assert f"serviceProxies[0].listenPort={_VLLM_PROXY_PORT}" in _snippet()


# ── provision is accept-and-poll, not block-until-ready ────────────────


def test_provision_endpoint_returns_202_not_200():
    """Blocking until vLLM was ready outlived the 100s edge timeout, so a
    successful deploy surfaced in the browser as ``Failed to fetch``."""
    from daalu_automation.api.routers import onboarding

    route = next(
        r
        for r in onboarding.router.routes
        if getattr(r, "path", "").endswith("/gpu/provision")
        and "POST" in (getattr(r, "methods", None) or set())
    )
    assert route.status_code == 202


# ── workspaces must reflect a real coder, not the classifier ───────────


def test_coding_health_probes_models_listing_not_chat_completions():
    """The probe must hit /v1/models — POSTing chat/completions to check
    liveness would bill a real inference on every form render."""
    import asyncio
    from types import SimpleNamespace

    import daalu_automation.core.coding_gateway as cg

    seen: list[str] = []

    class _Resp:
        status_code = 200

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            seen.append(url)
            return _Resp()

    import httpx

    orig, httpx.AsyncClient = httpx.AsyncClient, lambda **kw: _Client()
    cg._CODING_HEALTH_CACHE.clear()
    try:
        s = SimpleNamespace(
            coding_local_base_url="http://10.200.0.2:8000/v1",
            daalu_hosted_gateway_url="",
            llm_local_base_url="",
        )
        assert asyncio.run(cg.coding_upstream_healthy(s)) is True
    finally:
        httpx.AsyncClient = orig
        cg._CODING_HEALTH_CACHE.clear()

    assert seen == ["http://10.200.0.2:8000/v1/models"]


def test_coding_health_false_when_nothing_configured():
    """No coding upstream at all → not serving, so workspaces stay hidden."""
    import asyncio
    from types import SimpleNamespace

    import daalu_automation.core.coding_gateway as cg

    s = SimpleNamespace(
        coding_local_base_url="",
        daalu_hosted_gateway_url="",
        llm_local_base_url="",
    )
    assert asyncio.run(cg.coding_upstream_healthy(s)) is False


def test_coding_health_false_when_upstream_unreachable():
    """A configured-but-undeployed coder is the exact case that used to
    produce a workspace that opened and then failed on first prompt."""
    import asyncio
    from types import SimpleNamespace

    import httpx

    import daalu_automation.core.coding_gateway as cg

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise httpx.ConnectError("connection refused")

    orig, httpx.AsyncClient = httpx.AsyncClient, lambda **kw: _Client()
    cg._CODING_HEALTH_CACHE.clear()
    try:
        s = SimpleNamespace(
            coding_local_base_url="http://10.200.0.2:8000/v1",
            daalu_hosted_gateway_url="",
            llm_local_base_url="",
        )
        assert asyncio.run(cg.coding_upstream_healthy(s)) is False
    finally:
        httpx.AsyncClient = orig
        cg._CODING_HEALTH_CACHE.clear()


# ── the GPU card and the pill must agree on health ─────────────────────


def test_gpu_status_probes_health_instead_of_defaulting_false():
    """`_gpu_status` left `healthy` unset, so it fell back to the schema's
    False default and the card read "Offline" for a serving GPU — while the
    pill on the same page, which probes, read "online"."""
    import asyncio
    from types import SimpleNamespace

    import daalu_automation.api.routers.onboarding as ob

    calls: list[str] = []

    async def _fake_probe(base_url, token_enc=None, **kw):
        calls.append(base_url)
        return True

    orig, ob.probe_openai_endpoint = ob.probe_openai_endpoint, _fake_probe
    try:
        tenant = SimpleNamespace(
            sovereign_inference_url="http://10.200.0.2:8001/v1",
            sovereign_model_classifier="meta/llama-3.1-8b-instruct",
            sovereign_inference_token_enc=None,
        )
        out = asyncio.run(ob._gpu_status(tenant, None))
    finally:
        ob.probe_openai_endpoint = orig

    assert out.configured is True
    assert out.healthy is True, "healthy must reflect a probe, not the default"
    assert calls == ["http://10.200.0.2:8001/v1"]


def test_gpu_status_reports_unhealthy_when_probe_fails():
    """A configured-but-unreachable GPU stays `configured`, goes `healthy=False`."""
    import asyncio
    from types import SimpleNamespace

    import daalu_automation.api.routers.onboarding as ob

    async def _fake_probe(base_url, token_enc=None, **kw):
        return False

    orig, ob.probe_openai_endpoint = ob.probe_openai_endpoint, _fake_probe
    try:
        tenant = SimpleNamespace(
            sovereign_inference_url="http://10.200.0.2:8001/v1",
            sovereign_model_classifier="m",
            sovereign_inference_token_enc=None,
        )
        out = asyncio.run(ob._gpu_status(tenant, None))
    finally:
        ob.probe_openai_endpoint = orig

    assert out.configured is True
    assert out.healthy is False


# ── AI Factory must query the tenant's Prometheus, not only the hub's ──


def test_cached_query_key_includes_endpoint_not_just_promql():
    """Keying the cache on promql alone would serve one tenant's series to
    another once queries can target different Prometheis."""
    import asyncio

    import daalu_automation.core.prometheus as pm

    a = pm.PrometheusClient(base_url="http://hub:9090")
    b = pm.PrometheusClient(base_url="http://10.200.0.2:9090", proxy="http://10.200.0.2:8888")

    async def _fake_a(q):
        return ["A"]

    async def _fake_b(q):
        return ["B"]

    a.query, b.query = _fake_a, _fake_b
    pm._CACHE.clear()
    try:
        assert asyncio.run(pm.cached_query(a, "DCGM_FI_DEV_GPU_UTIL")) == ["A"]
        # Same PromQL, different store — must NOT hit A's cached entry.
        assert asyncio.run(pm.cached_query(b, "DCGM_FI_DEV_GPU_UTIL")) == ["B"]
    finally:
        pm._CACHE.clear()


def test_prometheus_client_carries_proxy_for_tunnel_bound_stores():
    """An in-cluster URL is only resolvable through the edge forward proxy."""
    import daalu_automation.core.prometheus as pm

    c = pm.PrometheusClient(
        base_url="http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090",
        proxy="http://10.200.0.2:8888",
    )
    assert c.configured is True
    assert c.proxy == "http://10.200.0.2:8888"
