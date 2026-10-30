"""GET /onboarding/gpu/serving: what Daalu deployed, as the controller renders it."""
from __future__ import annotations

from daalu_automation.models.gpu_tenant import GpuTenant, GpuTenantState
from tests.conftest import auth_headers

URL = "/api/v1/onboarding/gpu/serving"


async def test_nothing_deployed_is_null(client, seeded):
    r = await client.get(URL, headers=auth_headers(seeded.token_a))
    assert r.status_code == 200 and r.json() is None


async def test_reports_the_rendered_stack_and_hides_disconnected(client, db, seeded):
    row = GpuTenant(
        tenant_id=seeded.tenant_a_id, state=GpuTenantState.active, namespace="daalu",
        gpu_class="ada-48", gpu_node="gpu-node-1", model_classifier="qwen/qwen3.6-35b-a3b",
    )
    db.add(row)
    await db.commit()

    body = (await client.get(URL, headers=auth_headers(seeded.token_a))).json()
    assert body["model"] == "qwen/qwen3.6-35b-a3b"
    assert body["hf_model"] == "Qwen/Qwen3.6-35B-A3B-FP8"
    assert body["quantization"] == "fp8"  # inferred from the FP8 checkpoint
    assert body["gpu_memory_utilization"] == 0.85  # catalog value, no override
    assert body["context_len"] == 32768 and body["tool_calling"] is True
    assert body["engine"] == "vllm" and body["image"].startswith("vllm/vllm-openai:")
    assert (body["node"], body["gpu_class"], body["namespace"]) == ("gpu-node-1", "ada-48", "daalu")

    row.state = GpuTenantState.destroyed
    await db.commit()
    assert (await client.get(URL, headers=auth_headers(seeded.token_a))).json() is None
