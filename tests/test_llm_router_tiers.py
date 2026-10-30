"""Unit tests for the LLM router's tier cascade.

The router is the highest-risk piece of the daalu-hosted /
SOVEREIGN tier addition — if it routes ``purpose='coding'`` to
cloud, we've leaked source code; if it ignores ``daalu_hosted_enabled``,
quota enforcement is gone.

These tests exercise :func:`_resolve_plan` directly with mocked
settings and tenant config — no DB, no Redis, no network. The
``_local_is_healthy`` cache is patched to a deterministic value
per test so we don't depend on real network reachability.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from daalu_automation.core.llm import (
    _resolve_plan,
    _TenantRoutingConfig,
)
from daalu_automation.models.billing import InferenceTier, RoutingPolicy


def _settings(**overrides):
    """Minimal settings stub used by _resolve_plan."""
    base = dict(
        llm_local_base_url="http://vllm.daalu.svc.cluster.local:8000",
        llm_api_key="sk-deepseek-xxx",
        anthropic_api_key="sk-ant-xxx",
        daalu_hosted_gateway_url="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _cfg(**overrides):
    base = dict(sovereign_url=None, sovereign_token=None, daalu_hosted_enabled=False)
    base.update(overrides)
    return _TenantRoutingConfig(**base)


@pytest.mark.asyncio
async def test_default_local_first_classifier():
    """Classifier on the default local-first policy: LOCAL → ext_clf → ext_q."""
    with patch("daalu_automation.core.llm._local_is_healthy", return_value=True):
        plan = await _resolve_plan(
            policy=RoutingPolicy.LOCAL_FIRST,
            tier="classifier",
            purpose="default",
            tenant_cfg=_cfg(),
            settings=_settings(),
        )
    assert plan == [
        InferenceTier.LOCAL,
        InferenceTier.EXTERNAL_CLASSIFIER,
        InferenceTier.EXTERNAL_QUALITY,
    ]


@pytest.mark.asyncio
async def test_coding_purpose_excludes_cloud_tiers():
    """Purpose='coding' must never include EXTERNAL_* — code can't leak to cloud."""
    with patch("daalu_automation.core.llm._local_is_healthy", return_value=True):
        plan = await _resolve_plan(
            policy=RoutingPolicy.LOCAL_FIRST,
            tier="quality",
            purpose="coding",
            tenant_cfg=_cfg(),
            settings=_settings(),
        )
    assert InferenceTier.EXTERNAL_CLASSIFIER not in plan
    assert InferenceTier.EXTERNAL_QUALITY not in plan
    assert plan == [InferenceTier.LOCAL]


@pytest.mark.asyncio
async def test_coding_with_no_local_or_sovereign_returns_empty():
    """If neither local nor sovereign is available, coding plan is empty.

    The caller sees ``LLMUnavailable`` rather than a cloud leak. This is
    the safety guarantee in the customer-facing book §43.
    """
    with patch("daalu_automation.core.llm._local_is_healthy", return_value=False):
        plan = await _resolve_plan(
            policy=RoutingPolicy.LOCAL_FIRST,
            tier="quality",
            purpose="coding",
            tenant_cfg=_cfg(),
            settings=_settings(llm_local_base_url=""),
        )
    assert plan == []


@pytest.mark.asyncio
async def test_sovereign_first_when_configured():
    """A tenant with a sovereign URL gets SOVEREIGN at the head of the cascade.

    For ``tier='classifier'`` the SKU's local-first heuristic adds LOCAL
    too; for ``tier='quality'`` LOCAL is *not* added (Anthropic is the
    SKU's default for quality calls). The router only adds LOCAL when
    the policy prefers it for the requested tier.
    """
    cfg = _cfg(sovereign_url="http://10.200.0.2:8000")
    with (
        patch("daalu_automation.core.llm._local_is_healthy", return_value=True),
        patch("daalu_automation.core.llm._sovereign_is_healthy", return_value=True),
    ):
        plan = await _resolve_plan(
            policy=RoutingPolicy.LOCAL_FIRST,
            tier="classifier",
            purpose="default",
            tenant_cfg=cfg,
            settings=_settings(),
        )
    # SOVEREIGN comes first; LOCAL second; classifier-tier cloud after.
    assert plan[0] == InferenceTier.SOVEREIGN
    assert plan[1] == InferenceTier.LOCAL


@pytest.mark.asyncio
async def test_unreachable_sovereign_is_dropped_from_the_plan():
    """An unreachable sovereign node must not sit at the head of the cascade.

    It used to be added on the mere presence of a URL, so every call — and
    every tool-calling round within a call — paid a full connect timeout
    before falling through. LOCAL has always been gated this way.
    """
    cfg = _cfg(sovereign_url="http://10.200.0.2:8000")
    with (
        patch("daalu_automation.core.llm._local_is_healthy", return_value=True),
        patch("daalu_automation.core.llm._sovereign_is_healthy", return_value=False),
    ):
        plan = await _resolve_plan(
            policy=RoutingPolicy.LOCAL_FIRST,
            tier="classifier",
            purpose="default",
            tenant_cfg=cfg,
            settings=_settings(),
        )
    assert InferenceTier.SOVEREIGN not in plan
    assert plan[0] == InferenceTier.LOCAL


@pytest.mark.asyncio
async def test_external_only_policy_strips_local_and_sovereign():
    """EXTERNAL_ONLY policy means LOCAL + SOVEREIGN are removed even if available."""
    cfg = _cfg(sovereign_url="http://10.200.0.2:8000")
    with patch("daalu_automation.core.llm._local_is_healthy", return_value=True):
        plan = await _resolve_plan(
            policy=RoutingPolicy.EXTERNAL_ONLY,
            tier="quality",
            purpose="default",
            tenant_cfg=cfg,
            settings=_settings(),
        )
    assert InferenceTier.LOCAL not in plan
    assert InferenceTier.SOVEREIGN not in plan
    assert InferenceTier.EXTERNAL_QUALITY in plan


@pytest.mark.asyncio
async def test_gateway_path_used_when_enabled():
    """daalu_hosted_enabled + gateway URL → LOCAL included even if direct URL unhealthy."""
    cfg = _cfg(daalu_hosted_enabled=True)
    with patch("daalu_automation.core.llm._local_is_healthy", return_value=False):
        plan = await _resolve_plan(
            policy=RoutingPolicy.LOCAL_FIRST,
            tier="classifier",
            purpose="default",
            tenant_cfg=cfg,
            settings=_settings(
                llm_local_base_url="",  # direct path unavailable
                daalu_hosted_gateway_url="http://inference-gateway",
            ),
        )
    assert InferenceTier.LOCAL in plan


@pytest.mark.asyncio
async def test_external_only_with_coding_yields_empty_plan():
    """A tenant whose SKU forbids local cannot use the coding assistant.

    EXTERNAL_ONLY strips LOCAL/SOVEREIGN; coding purpose strips external.
    Intersection is empty — caller sees ``LLMUnavailable`` (the IDE
    would refuse to enable, but this defends in depth at the router).
    """
    cfg = _cfg(sovereign_url="http://10.200.0.2:8000")
    with patch("daalu_automation.core.llm._local_is_healthy", return_value=True):
        plan = await _resolve_plan(
            policy=RoutingPolicy.EXTERNAL_ONLY,
            tier="quality",
            purpose="coding",
            tenant_cfg=cfg,
            settings=_settings(),
        )
    assert plan == []
