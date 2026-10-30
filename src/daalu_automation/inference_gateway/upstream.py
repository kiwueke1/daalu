"""HTTP client to the vLLM upstream (the daalu-vllm-pool Service).

OpenAI-compatible. The gateway is intentionally a pass-through proxy
plus an accounting layer; we do not transform prompts or responses
beyond stamping tenant headers for downstream tools that may log.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from daalu_automation.config import get_settings

logger = structlog.get_logger(__name__)


class UpstreamError(Exception):
    """Upstream vLLM call failed.

    The gateway converts this to HTTP 502 so the caller knows it's
    a backend problem, not a quota/auth refusal. Carries the upstream
    status code when available for log correlation.
    """

    def __init__(self, msg: str, status_code: int | None = None) -> None:
        super().__init__(msg)
        self.status_code = status_code


async def chat_completion(
    *,
    tenant_id: str,
    purpose: str,
    body: dict[str, Any],
    upstream_url: str | None = None,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Forward an OpenAI chat-completion request to the upstream vLLM.

    ``upstream_url`` is the resolved shared-pool URL (see
    ``inference_gateway.pool.resolve_upstream``). When omitted it falls
    back to ``settings.daalu_hosted_upstream_url`` so callers/tests that
    don't use the pool registry keep working.

    Returns the parsed JSON response. The caller is responsible for
    pulling ``usage.prompt_tokens`` + ``usage.completion_tokens`` out
    of the response for the post-call quota write.

    Streaming requests are *not* supported by this MVP path — vLLM's
    SSE stream is fine but the gateway's quota accounting needs a
    different pipeline for streaming (decrement TPM per chunk). See
    the design doc §10 follow-ups.
    """
    settings = get_settings()
    if body.get("stream"):
        raise UpstreamError(
            "streaming responses not yet supported by the gateway", status_code=400
        )
    upstream_url = upstream_url or settings.daalu_hosted_upstream_url
    if not upstream_url:
        raise UpstreamError("no shared GPU pool and daalu_hosted_upstream_url unset")
    url = upstream_url.rstrip("/") + "/v1/chat/completions"
    headers = {
        "content-type": "application/json",
        # Stamped so anything downstream that logs (vLLM, an L7 proxy)
        # can correlate without trusting the upstream to invent a
        # tenant id.
        "x-daalu-tenant-id": tenant_id,
        "x-daalu-purpose": purpose,
    }
    api_key = settings.daalu_hosted_upstream_api_key
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as e:
        logger.warning("upstream.transport_error", error=str(e), tenant_id=tenant_id)
        raise UpstreamError(f"vLLM transport error: {e}") from e

    if resp.status_code >= 400:
        # vLLM error bodies are useful for debugging but we don't echo
        # them to the client untrimmed — they can leak internal
        # configuration. Log full text at warning level; return only
        # status and a short reason to the caller.
        logger.warning(
            "upstream.error",
            status=resp.status_code,
            body=resp.text[:500],
            tenant_id=tenant_id,
        )
        raise UpstreamError(
            f"vLLM returned {resp.status_code}", status_code=resp.status_code
        )

    return resp.json()
