"""Resolve where a coding-completion request should be proxied.

The public coding endpoint (``api/routers/coding.py``) is the laptop CLI's
front door. It authenticates the user (PAT) and then has to forward the
OpenAI-compatible body to *some* upstream — and decide whether that upstream
needs a Daalu service-token.

Three upstreams, in priority order:

1. ``coding_local_base_url`` — a raw vLLM endpoint (no auth) that serves the
   *coding* models directly (the qwen-coder stack). Takes precedence so the
   coder upstream is wired independently of the classifier and the hosted
   tier. This is what prod uses today: the coder vLLM over the WG tunnel.
2. ``daalu_hosted_gateway_url`` — the in-cluster inference-gateway. It verifies
   a ``purpose=coding`` **service-token**, does quota/metering/pool-resolution,
   and routes to the shared GPU pool. This is the same gateway the in-pod
   code-server posts to.
3. ``llm_local_base_url`` — a raw vLLM endpoint (no auth). Last-resort fallback
   for single-GPU dev installs. NOTE: in prod this points at the llama
   *classifier* (:8001), which does NOT serve coding models — so a coding
   request that reaches this fallback 404s. ``coding_local_base_url`` exists
   precisely to keep coding off it.

Returning ``needs_service_token`` keeps the auth decision next to the URL
decision so the router can't accidentally send an unauthenticated request to
the gateway (401) or a service-token to a raw vLLM (ignored, but sloppy).
"""

from __future__ import annotations

from dataclasses import dataclass

from daalu_automation.config import Settings


@dataclass(frozen=True, slots=True)
class CodingUpstream:
    chat_completions_url: str
    needs_service_token: bool


def _chat_completions_url(base: str) -> str:
    """Append the OpenAI chat-completions path, tolerating a trailing /v1.

    vLLM and the gateway both expose ``/v1/chat/completions``; a configured
    base may or may not already include the ``/v1`` segment.
    """
    base = base.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def resolve_coding_upstream(settings: Settings) -> CodingUpstream | None:
    """Pick the upstream for coding completions, or ``None`` if unconfigured."""
    if settings.coding_local_base_url:
        return CodingUpstream(
            chat_completions_url=_chat_completions_url(settings.coding_local_base_url),
            needs_service_token=False,
        )
    if settings.daalu_hosted_gateway_url:
        return CodingUpstream(
            chat_completions_url=_chat_completions_url(
                settings.daalu_hosted_gateway_url
            ),
            needs_service_token=True,
        )
    if settings.llm_local_base_url:
        return CodingUpstream(
            chat_completions_url=_chat_completions_url(settings.llm_local_base_url),
            needs_service_token=False,
        )
    return None


# ── Is a coder actually serving? ───────────────────────────────────────
#
# Being *configured* and being *reachable* are different things, and the gap
# between them is user-visible: the coder is a separate vLLM stack from the
# classifier, nothing in the UI deploys it, and settings.coding_models_enabled
# is a claim rather than a trigger. So a tenant could be offered a workspace,
# have it create successfully, open the IDE, and only then discover that chat
# fails — because coding_local_base_url pointed at a stack that was never
# deployed (or lived on a node that has since been rebuilt).
#
# Probing turns that late, confusing failure into an honest "no coding model
# is currently serving" before a workspace is ever created.

_CODING_HEALTH_CACHE: dict[str, tuple[float, bool]] = {}
_CODING_HEALTH_TTL_S = 30.0


async def coding_upstream_healthy(settings: Settings) -> bool:
    """Is the resolved coding upstream actually answering?

    Probes the OpenAI ``/v1/models`` listing rather than ``/health`` — the
    hosted gateway doesn't expose the latter, and a model listing is the
    thing that must work for a workspace to be usable anyway.

    Cached briefly so the Create-Workspace form and the eligibility check
    don't each dial the tunnel on every render. Failures are cached too: a
    coder that isn't deployed is the steady state we're detecting, not an
    exception worth retrying hard.
    """
    import time

    import httpx

    upstream = resolve_coding_upstream(settings)
    if upstream is None:
        return False
    # chat_completions_url ends in /v1/chat/completions; the sibling listing
    # is /v1/models.
    url = upstream.chat_completions_url.rsplit("/chat/completions", 1)[0] + "/models"

    now = time.monotonic()
    cached = _CODING_HEALTH_CACHE.get(url)
    if cached and now - cached[0] < _CODING_HEALTH_TTL_S:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url)
            # The gateway 401s an unauthenticated probe — that still proves
            # something is listening and routing, which is what we're asking.
            ok = resp.status_code < 500
    except Exception:  # noqa: BLE001 — unreachable is the answer, not an error
        ok = False
    _CODING_HEALTH_CACHE[url] = (now, ok)
    return ok
