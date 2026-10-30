"""Self-hosted tiers turn Qwen3.x thinking off; external tiers don't.

With vLLM's ``--reasoning-parser``, a thinking model can spend the whole
``max_tokens`` budget in ``reasoning_content`` and return empty ``content``.
Alert triage (``complete_json``) then fails with "LLM did not return JSON"
and no Alert row is ever written. These tests pin the request we send per
tier, using a fake ``AsyncOpenAI`` — no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from daalu_automation.core.llm import _openai_compat, _openai_compat_tools
from daalu_automation.models.billing import InferenceTier

NO_THINKING = {"chat_template_kwargs": {"enable_thinking": False}}


class _FakeClient:
    """Records the kwargs of the one ``chat.completions.create`` call."""

    calls: list[dict] = []

    def __init__(self, **_: object) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: object) -> SimpleNamespace:
        _FakeClient.calls.append(kwargs)
        msg = SimpleNamespace(content='{"ok": true}', tool_calls=[])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )


@pytest.fixture
def fake_openai():
    _FakeClient.calls = []
    with patch("openai.AsyncOpenAI", _FakeClient):
        yield _FakeClient.calls


async def _one_shot(served_by: InferenceTier) -> None:
    await _openai_compat(
        base_url="http://vllm:8000/v1",
        api_key="",
        model="qwen/qwen3.6-35b-a3b",
        system="s",
        user="u",
        max_tokens=512,
        response_format="json",
        served_by=served_by,
    )


async def _with_tools(served_by: InferenceTier) -> None:
    await _openai_compat_tools(
        base_url="http://vllm:8000/v1",
        api_key="",
        model="qwen/qwen3.6-35b-a3b",
        messages=[{"role": "user", "content": "u"}],
        tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}],
        max_tokens=2048,
        served_by=served_by,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [_one_shot, _with_tools])
@pytest.mark.parametrize("tier", [InferenceTier.SOVEREIGN, InferenceTier.LOCAL])
async def test_self_hosted_tiers_disable_thinking(fake_openai, call, tier):
    await call(tier)
    assert fake_openai[0]["extra_body"] == NO_THINKING


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [_one_shot, _with_tools])
async def test_external_tier_gets_no_extra_body(fake_openai, call):
    await call(InferenceTier.EXTERNAL_CLASSIFIER)
    assert "extra_body" not in fake_openai[0]
