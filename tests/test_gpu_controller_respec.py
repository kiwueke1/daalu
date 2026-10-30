"""Re-spec rules for an existing gpu_tenants row (gpu_controller.app._respec)."""
import uuid

import pytest

from daalu_automation.gpu_controller.app import GpuSpec, _respec
from daalu_automation.models.gpu_tenant import GpuTenant, GpuTenantState


def _row(state: GpuTenantState) -> GpuTenant:
    return GpuTenant(
        tenant_id=uuid.uuid4(), state=state, namespace="daalu", gpu_class="ada-16",
        model_classifier="meta/llama-3.1-8b-instruct", last_error="old failure",
    )


SPEC = GpuSpec(
    target_cluster_tunnel_id=uuid.uuid4(), gpu_node="cp03", gpu_class="ada-48",
    model_classifier="qwen/qwen3.6-35b-a3b",
)


def test_reconnect_after_disconnect_redeploys():
    """Disconnect leaves a ``destroyed`` row; Connect must queue it again
    (it used to answer 202 and never deploy)."""
    row = _row(GpuTenantState.destroyed)
    _respec(row, SPEC)
    assert row.state == GpuTenantState.pending
    assert (row.gpu_node, row.gpu_class, row.model_classifier) == (
        "cp03", "ada-48", "qwen/qwen3.6-35b-a3b")
    assert row.target_cluster_tunnel_id == SPEC.target_cluster_tunnel_id
    assert row.last_error is None


@pytest.mark.parametrize("state", [GpuTenantState.pending, GpuTenantState.error])
def test_unapplied_or_failed_rows_take_the_new_spec(state):
    row = _row(state)
    _respec(row, SPEC)
    assert row.state == GpuTenantState.pending
    assert row.gpu_class == "ada-48"


@pytest.mark.parametrize(
    "state",
    [GpuTenantState.provisioning, GpuTenantState.active, GpuTenantState.deleting],
)
def test_live_stack_keeps_its_spec(state):
    row = _row(state)
    _respec(row, SPEC)
    assert row.state == state
    assert row.gpu_class == "ada-16"
