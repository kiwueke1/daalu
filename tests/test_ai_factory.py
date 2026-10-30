"""AI Factory — native GPU metrics, roles, diagnostics, validation.

Pure-function coverage (Prometheus query parsing, dcgmi/nccl builders, role
resolution) runs without a DB; the HTTP-surface tests use the ASGI ``client``
fixture and prove tenant scoping + admin gating. Prometheus is unconfigured in
tests (empty base URL), so the metrics endpoints exercise the graceful
"unavailable" paths and the validation checklist's fail path — no live
Prometheus required.
"""

from __future__ import annotations

import pytest

from daalu_automation.core.prometheus import (
    PrometheusClient,
    escape_label_value,
    tenant_selector,
)
from daalu_automation.gpu_controller import diagnostics as diag
from daalu_automation.models.gpu_tenant import GpuTenant, GpuTenantState
from tests.conftest import auth_headers

# ── pure: prometheus helpers ──────────────────────────────────────────────


def test_tenant_selector_escapes():
    assert tenant_selector("acme") == '{tenant="acme"}'
    assert tenant_selector("acme", {"gpu": "0"}) == '{tenant="acme",gpu="0"}'
    # injection-proofing
    assert escape_label_value('a"b\\c') == 'a\\"b\\\\c'


async def test_query_parses_samples(monkeypatch):
    client = PrometheusClient(base_url="http://prom:9090")

    async def fake_get(self, path, params):  # noqa: ANN001
        return {
            "result": [
                {"metric": {"gpu": "0"}, "value": [1700000000, "73.5"]},
                {"metric": {"gpu": "1"}, "value": [1700000000, "not-a-number"]},
            ]
        }

    monkeypatch.setattr(PrometheusClient, "_get", fake_get)
    rows = await client.query("DCGM_FI_DEV_GPU_TEMP")
    assert len(rows) == 1  # the bad value is dropped
    assert rows[0].value == 73.5
    assert rows[0].metric["gpu"] == "0"


async def test_dcgm_target_up_is_job_name_agnostic(monkeypatch):
    # The DCGM exporter may be scraped under any job name — in-cluster
    # ``nvidia-dcgm`` or, in prod, a tunnel-federated ``gpu-tunnel-dcgm-<tenant>``.
    # The check must intersect ``up`` with the DCGM series, not match a fixed job.
    client = PrometheusClient(base_url="http://prom:9090")
    seen: dict[str, str] = {}

    async def fake_get(self, path, params):  # noqa: ANN001
        seen["query"] = params["query"]
        return {"result": [{"metric": {}, "value": [1700000000, "1"]}]}

    monkeypatch.setattr(PrometheusClient, "_get", fake_get)
    assert await client.dcgm_target_up() is True
    # it asks about DCGM-emitting targets, never a hard-coded job label
    assert "DCGM_FI_DEV_GPU_TEMP" in seen["query"]
    assert "nvidia-dcgm" not in seen["query"]

    async def fake_empty(self, path, params):  # noqa: ANN001
        return {"result": []}

    monkeypatch.setattr(PrometheusClient, "_get", fake_empty)
    assert await client.dcgm_target_up() is False


# ── pure: diagnostics builders/parsers ────────────────────────────────────


def test_dcgmi_cmd_and_clamp():
    assert diag.clamp_level(None) == 1
    assert diag.clamp_level(0) == 1
    assert diag.clamp_level(9) == 3
    assert diag.dcgmi_diag_cmd(2) == ["dcgmi", "diag", "-r", "2", "-j"]


def test_parse_dcgmi_output():
    ok, summ = diag.parse_dcgmi_output(
        '{"DCGM Diagnostic": {"test": {"result": "Pass"}}}'
    )
    assert ok is True and summ["failed"] == 0
    bad, summ2 = diag.parse_dcgmi_output(
        '{"DCGM Diagnostic": {"a": {"result": "Pass"}, "b": {"result": "Fail"}}}'
    )
    assert bad is False and summ2["failed"] == 1
    # text fallback + empty
    assert diag.parse_dcgmi_output("All tests PASS")[0] is True
    assert diag.parse_dcgmi_output("")[0] is False
    # DCGM 4.4.x "status" verdict format (test_summary.status).
    ok4, s4 = diag.parse_dcgmi_output('{"t": {"test_summary": {"status": "Pass"}}}')
    assert ok4 is True and s4["failed"] == 0
    bad4, s4b = diag.parse_dcgmi_output(
        '{"a": {"test_summary": {"status": "Pass"}},'
        ' "b": {"test_summary": {"status": "Fail"}}}'
    )
    assert bad4 is False and s4b["failed"] == 1


def test_nccl_eligibility_and_job():
    assert diag.nccl_eligible(1) is False
    assert diag.nccl_eligible(2) is True
    job = diag.nccl_all_reduce_job("daalu", gpu_class="ada-48", gpus=2)
    assert job["kind"] == "Job"
    assert job["spec"]["template"]["spec"]["nodeSelector"]["gpu-class"] == "ada-48"


def test_diag_stress_warning_gates_levels():
    # Level 1 is light -> no acknowledgement required.
    assert diag.diag_stress_warning("dcgmi_diag", 1) is None
    assert diag.diag_stress_warning("dcgmi_diag", None) is None
    # Levels 2/3 + nccl are stressful -> a warning is returned.
    assert "r2" in diag.diag_stress_warning("dcgmi_diag", 2)
    assert "r3" in diag.diag_stress_warning("dcgmi_diag", 3)
    assert "interconnect" in diag.diag_stress_warning("nccl_test", None)
    assert diag.diag_stress_warning("observability_validate", None) is None


def test_dcgmi_diag_job_builder():
    job = diag.dcgmi_diag_job("daalu", level=3, gpu_class="ada-16", node="cp01")
    spec = job["spec"]["template"]["spec"]
    # Pinned to the node, requests one GPU slice, runs dcgmi at the clamped level.
    assert spec["nodeSelector"]["kubernetes.io/hostname"] == "cp01"
    assert spec["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert "dcgmi diag -r 3 -j" in spec["containers"][0]["args"][0]
    # Falls back to gpu-class node selector when no node is given.
    job2 = diag.dcgmi_diag_job("daalu", level=1, gpu_class="ada-16", node=None)
    assert job2["spec"]["template"]["spec"]["nodeSelector"]["gpu-class"] == "ada-16"


# ── roles (DB) ────────────────────────────────────────────────────────────


async def _make_gpu(db, tenant_id, *, shared: bool):
    row = GpuTenant(
        tenant_id=tenant_id,
        state=GpuTenantState.active,
        gpu_class="ada-16",
        model_classifier="meta/llama-3.1-8b-instruct",
        shared=shared,
    )
    db.add(row)
    await db.commit()
    return row


async def test_role_owner_and_none(db, seeded):
    from daalu_automation.core.gpu.roles import resolve_factory_view

    # No GPU, no quota -> none
    v = await resolve_factory_view(db, seeded.tenant_b_id)
    assert v.role == "none" and v.has_gpu is False

    # Own (unshared) GPU -> owner, scoped to its own tenant label
    await _make_gpu(db, seeded.tenant_a_id, shared=False)
    v2 = await resolve_factory_view(db, seeded.tenant_a_id)
    assert v2.role == "owner" and v2.has_gpu and v2.tenant_label == str(seeded.tenant_a_id)
    assert "diagnostics" in v2.panels and "validate" in v2.panels


async def test_disconnected_gpu_is_not_owned(db, seeded):
    """Disconnect leaves the row ``destroyed``; AI Factory must drop the card."""
    from daalu_automation.core.gpu.roles import resolve_factory_view

    row = await _make_gpu(db, seeded.tenant_a_id, shared=False)
    for state in (GpuTenantState.deleting, GpuTenantState.destroyed):
        row.state = state
        await db.commit()
        v = await resolve_factory_view(db, seeded.tenant_a_id)
        assert v.role == "none" and v.has_gpu is False and v.gpu_class is None


async def test_role_provider_and_consumer(db, seeded):
    from datetime import datetime, timezone

    from daalu_automation.core.gpu.roles import resolve_factory_view
    from daalu_automation.core.gpu.sharing import grant_provider, register_pool
    from daalu_automation.models.daalu_hosted_quota import DaaluHostedQuota

    # Provider: granted + shared pool
    gpu = await _make_gpu(db, seeded.tenant_a_id, shared=True)
    await grant_provider(db, seeded.tenant_a_id)
    await register_pool(db, gpu_tenant=gpu, upstream_url="http://10.200.0.2:8888")
    v = await resolve_factory_view(db, seeded.tenant_a_id)
    assert v.role == "provider" and "consumers" in v.panels

    # Consumer: enabled hosted quota
    db.add(
        DaaluHostedQuota(
            tenant_id=seeded.tenant_b_id,
            enabled=True,
            monthly_token_limit=1_000_000,
            current_period_used=1234,
            period_started_at=datetime.now(tz=timezone.utc),
        )
    )
    await db.commit()
    vc = await resolve_factory_view(db, seeded.tenant_b_id)
    assert vc.role == "consumer" and "consumer" in vc.panels and vc.has_gpu is False


# ── HTTP surface (ASGI) ───────────────────────────────────────────────────


@pytest.mark.usefixtures("seeded")
async def test_overview_none_then_owner(client, db, seeded):
    r = await client.get(
        "/api/v1/ai-factory/overview", headers=auth_headers(seeded.token_a)
    )
    assert r.status_code == 200
    assert r.json()["role"] == "none"

    await _make_gpu(db, seeded.tenant_a_id, shared=False)
    r2 = await client.get(
        "/api/v1/ai-factory/overview", headers=auth_headers(seeded.token_a)
    )
    body = r2.json()
    assert body["role"] == "owner"
    # Prometheus unconfigured in tests -> metrics unavailable, surfaced honestly
    assert body["metrics_available"] is False


async def test_validate_reports_fail_without_prometheus(client, db, seeded):
    await _make_gpu(db, seeded.tenant_a_id, shared=False)
    r = await client.post(
        "/api/v1/ai-factory/observability/validate",
        headers=auth_headers(seeded.token_a),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False
    assert any(
        c["name"] == "prometheus_configured" and c["status"] == "fail"
        for c in body["checks"]
    )


async def test_diagnostics_requires_gpu_then_enqueues(client, db, seeded):
    # No GPU -> 403
    r = await client.post(
        "/api/v1/ai-factory/gpu/diagnostics",
        headers=auth_headers(seeded.token_a),
        json={"kind": "dcgmi_diag", "level": 2},
    )
    assert r.status_code == 403

    await _make_gpu(db, seeded.tenant_a_id, shared=False)
    # Level 2 is stressful -> requires acknowledgement (412 with a warning).
    r_ack = await client.post(
        "/api/v1/ai-factory/gpu/diagnostics",
        headers=auth_headers(seeded.token_a),
        json={"kind": "dcgmi_diag", "level": 2},
    )
    assert r_ack.status_code == 412
    assert r_ack.json()["detail"]["requires_acknowledgement"] is True
    assert r_ack.json()["detail"]["warning"]

    r2 = await client.post(
        "/api/v1/ai-factory/gpu/diagnostics",
        headers=auth_headers(seeded.token_a),
        json={"kind": "dcgmi_diag", "level": 2, "acknowledged": True},
    )
    assert r2.status_code == 200 and r2.json()["state"] == "pending"

    r3 = await client.get(
        "/api/v1/ai-factory/gpu/diagnostics", headers=auth_headers(seeded.token_a)
    )
    runs = r3.json()["runs"]
    assert len(runs) == 1 and runs[0]["kind"] == "dcgmi_diag" and runs[0]["level"] == 2


async def test_gpu_summary_keys_distinct_cards_not_index(
    client, db, seeded, monkeypatch
):
    """Two single-GPU cards both report gpu="0" — they must surface as TWO
    rows keyed by their unique UUID, not collapse onto the shared index.

    Regression for the cp01 + cp03 (RTX 2000 + RTX 6000) sovereign-tunnel case
    where the AI Factory page showed only one card.
    """
    from daalu_automation.api.routers import gpu_metrics
    from daalu_automation.core.prometheus import InstantSample, PrometheusClient

    await _make_gpu(db, seeded.tenant_a_id, shared=False)
    # Pretend Prometheus is reachable.
    monkeypatch.setattr(PrometheusClient, "configured", property(lambda self: True))

    async def fake_cached_query(_client, query):  # noqa: ANN001
        if "DCGM_FI_DEV_GPU_TEMP" in query:
            return [
                InstantSample(
                    metric={
                        "gpu": "0",
                        "UUID": "GPU-aaaa",
                        "Hostname": "cp01",
                        "modelName": "NVIDIA RTX 2000 Ada Generation",
                    },
                    value=52.0,
                    ts=0.0,
                ),
                InstantSample(
                    metric={
                        "gpu": "0",
                        "UUID": "GPU-bbbb",
                        "Hostname": "cp03",
                        "modelName": "NVIDIA RTX 6000 Ada Generation",
                    },
                    value=53.0,
                    ts=0.0,
                ),
            ]
        return []

    monkeypatch.setattr(gpu_metrics, "cached_query", fake_cached_query)

    r = await client.get(
        "/api/v1/ai-factory/gpu/summary", headers=auth_headers(seeded.token_a)
    )
    assert r.status_code == 200
    gpus = r.json()["gpus"]
    assert len(gpus) == 2, "two cards sharing gpu index 0 must not collapse"
    ids = {g["id"] for g in gpus}
    assert ids == {"GPU-aaaa", "GPU-bbbb"}
    hosts = {g["hostname"] for g in gpus}
    assert hosts == {"cp01", "cp03"}
    # Both keep the on-node display index.
    assert all(g["gpu"] == "0" for g in gpus)


async def test_diagnostics_are_tenant_scoped(client, db, seeded):
    # A enqueues a run; B must not see it.
    await _make_gpu(db, seeded.tenant_a_id, shared=False)
    r = await client.post(
        "/api/v1/ai-factory/gpu/diagnostics",
        headers=auth_headers(seeded.token_a),
        json={"kind": "dcgmi_diag", "level": 1},
    )
    run_id = r.json()["id"]
    # B can't fetch A's run
    rb = await client.get(
        f"/api/v1/ai-factory/gpu/diagnostics/{run_id}",
        headers=auth_headers(seeded.token_b),
    )
    assert rb.status_code == 404
