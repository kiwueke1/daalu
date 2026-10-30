"""Upstream resolution for the public coding endpoint (the laptop CLI path).

Regression guard for the prod misroute where coding requests fell back to
``llm_local_base_url`` — the llama *classifier* on :8001 — and 404'd because
that vLLM doesn't serve coding models. ``coding_local_base_url`` must take
precedence and skip the service token (raw vLLM).
"""

from __future__ import annotations

from types import SimpleNamespace

from daalu_automation.core.coding_gateway import resolve_coding_upstream


def _settings(**kw):
    base = {
        "coding_local_base_url": "",
        "daalu_hosted_gateway_url": "",
        "llm_local_base_url": "",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_coding_local_url_wins_and_needs_no_token():
    up = resolve_coding_upstream(
        _settings(
            coding_local_base_url="http://10.200.0.2:8000/v1",
            # Both lower-priority knobs set: neither should win.
            daalu_hosted_gateway_url="http://gateway.svc",
            llm_local_base_url="http://10.200.0.2:8001/v1",
        )
    )
    assert up is not None
    assert up.chat_completions_url == "http://10.200.0.2:8000/v1/chat/completions"
    assert up.needs_service_token is False


def test_gateway_used_when_no_dedicated_coder_url():
    up = resolve_coding_upstream(
        _settings(daalu_hosted_gateway_url="http://gateway.svc")
    )
    assert up is not None
    assert up.chat_completions_url == "http://gateway.svc/v1/chat/completions"
    assert up.needs_service_token is True


def test_classifier_fallback_is_last_resort():
    # The historical prod bug: only llm_local_base_url set → coding lands on
    # the classifier. Still resolvable (dev single-GPU), but lowest priority.
    up = resolve_coding_upstream(
        _settings(llm_local_base_url="http://10.200.0.2:8001/v1")
    )
    assert up is not None
    assert up.chat_completions_url == "http://10.200.0.2:8001/v1/chat/completions"
    assert up.needs_service_token is False


def test_unconfigured_returns_none():
    assert resolve_coding_upstream(_settings()) is None
