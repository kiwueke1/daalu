"""Thin LLM client used by every agent + briefing generator.

Four tiers are available behind one ``complete()`` call. The router
picks one based on the *requested* tier, the *active SKU's* routing
policy, and per-tenant config:

* **SOVEREIGN** — the tenant's own GPU, reached through their
  federation tunnel. Implemented by ``_dispatch_sovereign``; the URL
  and bearer token come from the tenant row
  (``sovereign_inference_url`` /
  ``sovereign_inference_token_hash``). Zero per-event cost; the
  customer paid for the card. This is what the *customer-facing
  book* calls "local-first AI" — the customer's hardware.
* **LOCAL** — the operator-owned GPU. Single-tenant deployments hit
  the URL in ``settings.llm_local_base_url`` directly; multi-tenant
  hub deployments go through the ``inference-gateway`` Service
  (``settings.daalu_hosted_gateway_url``) which enforces per-tenant
  quotas, rate limits, and metering. The *customer book* calls this
  the "daalu-hosted" tier.
* **EXTERNAL_CLASSIFIER** — third-party OpenAI-compatible endpoint
  (DeepSeek/Together/etc.).
* **EXTERNAL_QUALITY** — Anthropic Claude.

**Terminology note.** The code's ``LOCAL`` tier means *operator-
hosted*. The customer book uses "local" to mean *customer-hosted*.
The two senses are inverted because the code predates the
multi-tenant pivot. To avoid confusion in writing always say
"daalu-hosted" for code's LOCAL and "the customer's federated GPU"
for SOVEREIGN — never just "local" without qualification.

**Purpose constraint.** The optional ``purpose`` argument constrains
the cascade. ``purpose="coding"`` deliberately omits external tiers
so a code-editing agent cannot leak source to the cloud — Daalu
guarantees in the product copy that the coding assistant's LLM
never leaves the customer-or-operator perimeter.

Every successful call writes one ``usage_events`` row tagged with the
tier that actually served it (not the tier that was requested), the
token counts the upstream returned, and the dollar cost computed
against the tenant's current SKU. Failed calls are not billed.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from daalu_automation.config import DEFAULT_TENANT_ID, get_settings
from daalu_automation.core.billing import (
    allows_local,
    compute_cost,
    get_current_sku,
    prefers_local,
)
from daalu_automation.core.metrics import (
    LLM_COST_USD_TOTAL,
    LLM_FALLBACKS_TOTAL,
    LLM_LATENCY_SECONDS,
    LLM_LOCAL_HEALTHY,
    LLM_REQUESTS_TOTAL,
    LLM_TOKENS_TOTAL,
)
from daalu_automation.database import AsyncSessionLocal
from daalu_automation.models.billing import InferenceTier, RoutingPolicy, UsageEvent

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class LLMResult:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Which tier actually served the call. Set by the router; callers
    # rarely need to inspect this directly — the usage_events row is the
    # canonical record. Useful for tests that assert "this call should
    # have hit local".
    served_by: InferenceTier = InferenceTier.EXTERNAL_QUALITY


class LLMUnavailable(RuntimeError):
    """Raised when no LLM credentials are configured *and* no fallback fires."""


# ── Local-NIM health cache ────────────────────────────────────────────────
# A bounded TTL cache so the router doesn't hit the health endpoint on
# every call. The home node sits behind a WG tunnel — a 30-second TTL
# means at most one wasted call per half-minute when the node is down.
_LOCAL_HEALTH_CACHE: dict[str, tuple[float, bool]] = {}


async def _probe_healthy(url: str, ttl_s: float) -> bool:
    """TTL-cached reachability probe for an inference endpoint."""
    now = time.monotonic()
    cached = _LOCAL_HEALTH_CACHE.get(url)
    if cached and now - cached[0] < ttl_s:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(url)
            ok = resp.status_code < 500
    except Exception:
        ok = False
    _LOCAL_HEALTH_CACHE[url] = (now, ok)
    return ok


async def _local_is_healthy() -> bool:
    s = get_settings()
    if not s.llm_local_base_url:
        return False
    url = s.llm_local_base_url.rstrip("/") + s.llm_local_health_path
    ok = await _probe_healthy(url, s.llm_local_health_ttl_s)
    LLM_LOCAL_HEALTHY.set(1 if ok else 0)
    return ok


async def _sovereign_is_healthy(sovereign_url: str) -> bool:
    """Same reachability gate LOCAL has always had.

    SOVEREIGN was added to the plan on the mere *presence* of a URL, so a
    tenant whose GPU node is down paid a full connect timeout on every
    call — and once more per tool-calling round — before falling through.
    On a copilot turn that made three round trips, that is the difference
    between a snappy answer and one that looks hung.

    Fails closed to "unhealthy", which is safe: for ``purpose='coding'``
    the external tiers are suppressed entirely, so a false negative
    degrades to an honest LLMUnavailable rather than leaking a prompt to
    a third party.
    """
    s = get_settings()
    return await _probe_healthy(
        sovereign_url.rstrip("/") + s.llm_local_health_path,
        s.llm_local_health_ttl_s,
    )


# ── Public API ────────────────────────────────────────────────────────────


async def complete(
    *,
    system: str,
    user: str,
    model: str | None = None,
    tier: str = "quality",
    purpose: str = "default",
    max_tokens: int = 1024,
    response_format: str = "text",
    tenant_id: uuid.UUID | None = None,
    source: str = "",
) -> LLMResult:
    """One-shot completion with SKU-aware tier selection.

    ``tier`` is the *requested* tier; the actual tier that serves the
    call depends on the tenant's SKU routing policy and whether the
    local NIM is healthy. Pass ``tenant_id`` for per-tenant billing;
    background jobs without a tenant context default to the bootstrap
    tenant.

    ``source`` is a free-form label written to the usage row so the
    billing UI can show "which feature spent the budget". Pass something
    like ``"infra.alert.triage"`` or ``"infra.brief.daily"``.
    """
    settings = get_settings()
    tenant = tenant_id or DEFAULT_TENANT_ID
    started = time.monotonic()

    # SKU + per-tenant routing config in one DB hop.
    async with AsyncSessionLocal() as db:
        sku = await get_current_sku(db, tenant)
        tenant_cfg = await _load_tenant_routing_config(db, tenant)
    policy = sku.routing_policy if sku else RoutingPolicy.LOCAL_FIRST

    # Decide the *ordered list* of tiers to try. The first one that has
    # credentials + (for local) a healthy endpoint wins; on transport
    # failure we drop to the next.
    plan = await _resolve_plan(
        policy=policy,
        tier=tier,
        purpose=purpose,
        tenant_cfg=tenant_cfg,
        settings=settings,
    )
    if not plan:
        raise LLMUnavailable(
            "No LLM credentials configured. Set ANTHROPIC_API_KEY, LLM_API_KEY, "
            "or LLM_LOCAL_BASE_URL in .env."
        )

    last_err: Exception | None = None
    for attempt_idx, served_by in enumerate(plan):
        tier_started = time.monotonic()
        try:
            result = await _dispatch(
                served_by=served_by,
                system=system,
                user=user,
                model=model,
                max_tokens=max_tokens,
                response_format=response_format,
                tenant_id=tenant,
                tenant_cfg=tenant_cfg,
                purpose=purpose,
                settings=settings,
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            # Success path — emit metrics first so they fire even if
            # the usage_row write fails. Counters / histograms are
            # process-local; they cost nothing.
            LLM_REQUESTS_TOTAL.labels(
                requested_tier=tier,
                served_by=served_by.value,
                outcome="success",
                model=result.model,
            ).inc()
            LLM_TOKENS_TOTAL.labels(
                served_by=served_by.value,
                direction="prompt",
                model=result.model,
            ).inc(result.prompt_tokens)
            LLM_TOKENS_TOTAL.labels(
                served_by=served_by.value,
                direction="completion",
                model=result.model,
            ).inc(result.completion_tokens)
            LLM_LATENCY_SECONDS.labels(
                served_by=served_by.value, model=result.model
            ).observe(latency_ms / 1000.0)
            # Billing row — best-effort. We *want* to await so the row is
            # consistent, but a DB blip must never break an LLM call.
            try:
                cost = await _record_usage(
                    tenant_id=tenant,
                    sku=sku,
                    tier=served_by,
                    result=result,
                    source=source,
                    latency_ms=latency_ms,
                )
                LLM_COST_USD_TOTAL.labels(
                    tenant_id=str(tenant),
                    served_by=served_by.value,
                    model=result.model,
                ).inc(cost)
            except Exception as e:  # pragma: no cover — defensive
                logger.warning("usage_record_failed", error=str(e))
            return result
        except Exception as e:  # noqa: BLE001 — we deliberately try the next tier
            last_err = e
            # Record one fallback per failed tier so the rate of
            # "fallback occurred" tracks real router pressure.
            LLM_FALLBACKS_TOTAL.labels(
                failed_tier=served_by.value,
                reason=type(e).__name__,
            ).inc()
            LLM_REQUESTS_TOTAL.labels(
                requested_tier=tier,
                served_by=served_by.value,
                outcome="failed",
                model=model or "?",
            ).inc()
            logger.warning(
                "llm_tier_failed",
                tier=served_by.value,
                error=str(e),
                will_fallback=attempt_idx < len(plan) - 1,
            )
    # All tiers tried; bubble the last error.
    raise LLMUnavailable(f"All LLM tiers failed: {last_err}") from last_err


async def complete_json(*, system: str, user: str, **kwargs: Any) -> dict[str, Any]:
    """Helper for callers that want JSON back. Raises ``ValueError`` if
    the model didn't produce parseable JSON.
    """
    result = await complete(system=system, user=user, response_format="json", **kwargs)
    try:
        return json.loads(result.text)
    except json.JSONDecodeError as e:
        start = result.text.find("{")
        end = result.text.rfind("}")
        if start != -1 and end > start:
            return json.loads(result.text[start : end + 1])
        raise ValueError(f"LLM did not return JSON: {result.text[:200]}") from e


def sync_complete(**kwargs: Any) -> LLMResult:
    """Sync wrapper for Celery tasks."""
    return asyncio.run(complete(**kwargs))


async def chat_with_tools(
    *,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tenant_id: uuid.UUID | None = None,
    source: str = "",
    max_tokens: int = 2048,
    tool_choice: str = "auto",
) -> dict[str, Any]:
    """Multi-turn chat completion WITH function-calling.

    ``tool_choice`` is forwarded to the OpenAI-compatible endpoint. Pass
    ``"required"`` to force the model to emit at least one tool call this
    turn (used so a model that would rather narrate a remediation plan
    actually emits the gated write tool); ``"auto"`` lets it decide.

    The text-only :func:`complete` has no tool support; this is its
    tool-capable sibling, used by the alert-chat agent so its
    investigation runs on whatever OpenAI-compatible tier the router
    selects (local vLLM / SOVEREIGN / DeepSeek) instead of being
    hardcoded to Anthropic. It reuses the same :func:`_resolve_plan`
    cascade and runs on the first OpenAI-compatible tier that answers.

    The Anthropic (``EXTERNAL_QUALITY``) tier is skipped here — its tool
    wire-format differs from OpenAI's, and the OpenAI-compatible tiers
    already cover local vLLM, DeepSeek and OpenAI. A LOCAL tier that is
    only reachable via the daalu-hosted gateway is also skipped (the
    gateway tool path isn't wired yet); a direct ``llm_local_base_url``
    works.

    Returns ``{"text": str, "tool_calls": [{"id","name","arguments"}],
    "finish_reason": str}``. Raises :class:`LLMUnavailable` if no
    tool-capable tier is configured or all of them fail.
    """
    settings = get_settings()
    tenant = tenant_id or DEFAULT_TENANT_ID
    started = time.monotonic()

    async with AsyncSessionLocal() as db:
        sku = await get_current_sku(db, tenant)
        tenant_cfg = await _load_tenant_routing_config(db, tenant)
    policy = sku.routing_policy if sku else RoutingPolicy.LOCAL_FIRST

    plan = await _resolve_plan(
        policy=policy,
        tier="quality",
        purpose="default",
        tenant_cfg=tenant_cfg,
        settings=settings,
    )

    full_messages = [{"role": "system", "content": system}, *messages]
    last_err: Exception | None = None
    tried = False
    for served_by in plan:
        if served_by is InferenceTier.EXTERNAL_QUALITY:
            continue  # Anthropic tool format differs; covered by OpenAI tiers.
        if served_by is InferenceTier.LOCAL and not settings.llm_local_base_url:
            continue  # gateway-only local; tool path not wired through it yet.
        base_url, api_key, model = _openai_endpoint_for(
            served_by, tenant_id=tenant, tenant_cfg=tenant_cfg, settings=settings
        )
        if not base_url:
            continue
        tried = True
        try:
            out = await _openai_compat_tools(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=full_messages,
                tools=tools,
                max_tokens=max_tokens,
                tool_choice=tool_choice,
                served_by=served_by,
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            LLM_REQUESTS_TOTAL.labels(
                requested_tier="quality",
                served_by=served_by.value,
                outcome="success",
                model=out["result"].model,
            ).inc()
            try:
                cost = await _record_usage(
                    tenant_id=tenant,
                    sku=sku,
                    tier=served_by,
                    result=out["result"],
                    source=source,
                    latency_ms=latency_ms,
                )
                LLM_COST_USD_TOTAL.labels(
                    tenant_id=str(tenant),
                    served_by=served_by.value,
                    model=out["result"].model,
                ).inc(cost)
            except Exception as e:  # pragma: no cover — defensive
                logger.warning("usage_record_failed", error=str(e))
            return {
                "text": out["text"],
                "tool_calls": out["tool_calls"],
                "finish_reason": out["finish_reason"],
            }
        except Exception as e:  # noqa: BLE001 — try the next tier
            last_err = e
            LLM_FALLBACKS_TOTAL.labels(
                failed_tier=served_by.value, reason=type(e).__name__
            ).inc()
            logger.warning("llm_tools_tier_failed", tier=served_by.value, error=str(e))

    if not tried:
        raise LLMUnavailable(
            "No tool-capable LLM tier configured. Set LLM_API_KEY/LLM_BASE_URL "
            "(DeepSeek/OpenAI) or LLM_LOCAL_BASE_URL (local vLLM)."
        )
    raise LLMUnavailable(f"All tool-capable tiers failed: {last_err}") from last_err


# ── Routing plan ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class _TenantRoutingConfig:
    """Per-tenant overlay that shapes the routing plan.

    Loaded once at the start of :func:`complete` to avoid N DB hits
    inside the cascade. Both fields default to "no override" so an
    empty row keeps the single-tenant default behaviour.
    """

    sovereign_url: str | None
    sovereign_token: str | None
    daalu_hosted_enabled: bool
    # Served model id on the tenant's SOVEREIGN endpoint (None → fall
    # back to the operator default ``llm_local_model_classifier``).
    sovereign_model: str | None = None


async def _load_tenant_routing_config(db: Any, tenant_id: uuid.UUID) -> _TenantRoutingConfig:
    """Pull SOVEREIGN + daalu-hosted enablement for one tenant.

    Two columns + one feature-flag JSONB key. Cheap; no caching beyond
    the per-call lifetime. Avoid widening this surface — extra reads
    here multiply across every LLM call.
    """
    from sqlalchemy import select  # local import — keeps the cold-start unchanged

    from daalu_automation.models.tenant import Tenant

    row = (
        await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if row is None:
        return _TenantRoutingConfig(None, None, False)
    flags = row.feature_flags or {}
    # Decrypt the usable bearer token if one was stored (the hash column
    # is verification-only and can't authenticate a call). Most in-cluster
    # vLLM endpoints need no token, so None is the common case.
    token: str | None = None
    enc = getattr(row, "sovereign_inference_token_enc", None)
    if enc:
        try:
            from daalu_automation.core.crypto import decrypt_secret

            token = decrypt_secret(enc)
        except Exception as e:  # noqa: BLE001 — bad/rotated key must not break routing
            logger.warning("sovereign_token_decrypt_failed", error=str(e))
            token = None
    return _TenantRoutingConfig(
        sovereign_url=row.sovereign_inference_url,
        sovereign_token=token,
        daalu_hosted_enabled=bool(flags.get("daalu_hosted_enabled")),
        sovereign_model=getattr(row, "sovereign_model_classifier", None),
    )


async def _resolve_plan(
    *,
    policy: RoutingPolicy,
    tier: str,
    purpose: str,
    tenant_cfg: _TenantRoutingConfig,
    settings: Any,
) -> list[InferenceTier]:
    """Ordered list of tiers to try for one call.

    The first tier that has credentials and (for local) passes the
    health check is attempted; on a transport failure the dispatcher
    falls through to the next.

    Cascade order:

    1. ``SOVEREIGN``       — if the tenant has a federated GPU URL.
    2. ``LOCAL``           — operator-owned vLLM. Direct URL in
       single-tenant mode; via inference-gateway when
       ``daalu_hosted_enabled`` and a gateway URL is configured.
    3. ``EXTERNAL_*``      — third-party fallback, *suppressed* when
       ``purpose='coding'`` (code never leaves the perimeter).

    ``EXTERNAL_ONLY`` routing policy still forbids 1 and 2 even when
    the URLs exist.
    """
    plan: list[InferenceTier] = []
    coding_constrained = purpose == "coding"

    # 1) SOVEREIGN — tenant's own GPU, via tunnel. Gated on reachability
    # for the same reason LOCAL is: an unreachable tier in the plan costs
    # a connect timeout on every single call.
    if (
        tenant_cfg.sovereign_url
        and policy is not RoutingPolicy.EXTERNAL_ONLY
        and await _sovereign_is_healthy(tenant_cfg.sovereign_url)
    ):
        plan.append(InferenceTier.SOVEREIGN)

    # 2) LOCAL — direct URL or via gateway. For purpose='coding' we
    # override the SKU's prefers_local heuristic and always include
    # LOCAL when reachable, because the only legal cascade for coding
    # is local/sovereign — falling through to cloud would break the
    # privacy promise in book-customer §43.
    want_local = coding_constrained or (allows_local(policy) and prefers_local(policy, tier))
    local_direct_ok = bool(settings.llm_local_base_url) and await _local_is_healthy()
    local_via_gateway_ok = bool(settings.daalu_hosted_gateway_url) and tenant_cfg.daalu_hosted_enabled
    if want_local and (local_direct_ok or local_via_gateway_ok):
        plan.append(InferenceTier.LOCAL)

    # 3) External tiers — *skipped entirely* when the caller asked for
    # coding. The product copy promises code prompts never leave the
    # operator's or customer's perimeter; enforce that here at the only
    # routing chokepoint rather than relying on every caller to opt out.
    if not coding_constrained:
        if tier == "classifier":
            if settings.llm_api_key:
                plan.append(InferenceTier.EXTERNAL_CLASSIFIER)
            if settings.anthropic_api_key:
                plan.append(InferenceTier.EXTERNAL_QUALITY)
        else:
            if settings.anthropic_api_key:
                plan.append(InferenceTier.EXTERNAL_QUALITY)
            if settings.llm_api_key:
                plan.append(InferenceTier.EXTERNAL_CLASSIFIER)

    if policy is RoutingPolicy.EXTERNAL_ONLY:
        plan = [t for t in plan if t in (InferenceTier.EXTERNAL_CLASSIFIER, InferenceTier.EXTERNAL_QUALITY)]

    # Deduplicate while preserving order.
    seen: set[InferenceTier] = set()
    out: list[InferenceTier] = []
    for t in plan:
        if t not in seen:
            out.append(t)
            seen.add(t)
    return out


# ── Dispatchers ───────────────────────────────────────────────────────────


async def _dispatch(
    *,
    served_by: InferenceTier,
    system: str,
    user: str,
    model: str | None,
    max_tokens: int,
    response_format: str,
    tenant_id: uuid.UUID,
    tenant_cfg: _TenantRoutingConfig,
    purpose: str,
    settings: Any,
) -> LLMResult:
    if served_by is InferenceTier.SOVEREIGN:
        if not tenant_cfg.sovereign_url:
            raise LLMUnavailable("sovereign URL not configured for tenant")
        # Token in DB is sha256-hashed for storage; the cleartext is
        # injected from a K8s Secret keyed by tenant_id, mounted at
        # /var/run/daalu/sovereign-tokens/<tenant_id>. Falls back to a
        # global env override for dev.
        # Prefer the DB-stored (decrypted) token from the onboarding flow;
        # fall back to the legacy per-tenant Secret file / env.
        api_key = tenant_cfg.sovereign_token or _resolve_sovereign_token(
            tenant_id, settings
        )
        return await _openai_compat(
            base_url=tenant_cfg.sovereign_url,
            api_key=api_key or "not-required",
            model=model or tenant_cfg.sovereign_model or settings.llm_local_model_classifier,
            system=system,
            user=user,
            max_tokens=max_tokens,
            response_format=response_format,
            served_by=InferenceTier.SOVEREIGN,
        )
    if served_by is InferenceTier.LOCAL:
        # Multi-tenant path: route through the gateway so per-tenant
        # quotas + metering apply. Single-tenant path: direct URL.
        if settings.daalu_hosted_gateway_url and tenant_cfg.daalu_hosted_enabled:
            return await _local_via_gateway(
                tenant_id=tenant_id,
                purpose=purpose,
                model=model or settings.llm_local_model_classifier,
                system=system,
                user=user,
                max_tokens=max_tokens,
                response_format=response_format,
                settings=settings,
            )
        return await _openai_compat(
            base_url=settings.llm_local_base_url,
            api_key=settings.llm_local_api_key or "not-required",
            model=model or settings.llm_local_model_classifier,
            system=system,
            user=user,
            max_tokens=max_tokens,
            response_format=response_format,
            served_by=InferenceTier.LOCAL,
        )
    if served_by is InferenceTier.EXTERNAL_CLASSIFIER:
        return await _openai_compat(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=model or settings.llm_model_classifier,
            system=system,
            user=user,
            max_tokens=max_tokens,
            response_format=response_format,
            served_by=InferenceTier.EXTERNAL_CLASSIFIER,
        )
    # external_quality (Anthropic)
    return await _anthropic(
        system=system,
        user=user,
        model=model or settings.anthropic_model,
        max_tokens=max_tokens,
        settings=settings,
    )


def _resolve_sovereign_token(tenant_id: uuid.UUID, settings: Any) -> str:
    """Resolve the cleartext bearer token for a tenant's SOVEREIGN endpoint.

    The DB only stores ``sovereign_inference_token_hash`` (sha256). The
    cleartext is mounted into the pod's filesystem from a K8s Secret —
    one secret per tenant, mounted at
    ``/var/run/daalu/sovereign-tokens/<tenant_uuid>``. Reading the file
    on every call is cheap (the kernel caches the page) and avoids a
    process-level cache that would have to invalidate on rotation.

    Dev / single-tenant fallback: ``settings.llm_local_api_key`` is
    used if the per-tenant file is absent. Production deployments
    should always provision per-tenant secrets.
    """

    path = f"/var/run/daalu/sovereign-tokens/{tenant_id}"
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return settings.llm_local_api_key or ""
    except OSError as e:  # noqa: BLE001 — read failure shouldn't crash the call
        logger.warning("sovereign_token_read_failed", tenant_id=str(tenant_id), error=str(e))
        return settings.llm_local_api_key or ""


async def _local_via_gateway(
    *,
    tenant_id: uuid.UUID,
    purpose: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    response_format: str,
    settings: Any,
) -> LLMResult:
    """Dispatch through the inference-gateway (daalu-hosted tier path).

    Mints a 60-second service-token JWT, calls the gateway's
    ``/v1/chat/completions``, returns the result with served_by=LOCAL.
    The gateway handles quota + metering — we don't double-write here.
    """
    from daalu_automation.core.service_tokens import mint_service_token

    if not settings.service_token_secret_key:
        raise LLMUnavailable("service_token_secret_key not configured; cannot call gateway")
    if not settings.daalu_hosted_gateway_url:
        raise LLMUnavailable("daalu_hosted_gateway_url not configured")

    # Service-token JWT identifies the tenant + purpose for the gateway.
    # Purpose tags every usage row so the by-source breakdown shows
    # what spent the budget ("coding" vs "chat" vs "classifier").
    gw_purpose = purpose if purpose in ("chat", "classifier", "coding", "rca") else "chat"
    token = mint_service_token(
        tenant_id=str(tenant_id),
        user_id="00000000-0000-0000-0000-000000000000",  # system caller
        purpose=gw_purpose,  # type: ignore[arg-type]
        ttl_seconds=60,
    )

    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if response_format == "json":
        body["response_format"] = {"type": "json_object"}
    body.update(_NO_THINKING)  # the gateway forwards the body verbatim to vLLM

    url = settings.daalu_hosted_gateway_url.rstrip("/") + "/v1/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                url,
                json=body,
                headers={"authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as e:
        raise LLMUnavailable(f"gateway transport: {e}") from e

    if resp.status_code == 429:
        # 429 from the gateway with policy_action=cloud_overflow tells
        # the router to fall through cleanly to the next tier. Anything
        # else is a hard refusal that should *not* fall through (caller
        # exceeded their plan) — but the cascade does its own handling
        # via LLMUnavailable; we just pass the signal through.
        action = resp.headers.get("x-daalu-policy-action", "deny")
        raise LLMUnavailable(f"gateway 429 ({action}): {resp.text[:200]}")
    if resp.status_code >= 400:
        raise LLMUnavailable(f"gateway {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    usage = data.get("usage") or {}
    return LLMResult(
        text=msg.get("content", ""),
        model=str(data.get("model", model)),
        prompt_tokens=int(usage.get("prompt_tokens", 0)),
        completion_tokens=int(usage.get("completion_tokens", 0)),
        served_by=InferenceTier.LOCAL,
    )


async def _anthropic(
    *, system: str, user: str, model: str, max_tokens: int, settings: Any
) -> LLMResult:
    try:
        from anthropic import AsyncAnthropic
    except ImportError as e:  # pragma: no cover
        raise LLMUnavailable("anthropic SDK not installed") from e
    if not settings.anthropic_api_key:
        raise LLMUnavailable("ANTHROPIC_API_KEY not set")
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    system_blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
    if settings.anthropic_cache_enabled:
        system_blocks[0]["cache_control"] = {"type": "ephemeral"}
    resp = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_blocks,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    )
    usage = resp.usage
    return LLMResult(
        text=text,
        model=model,
        prompt_tokens=getattr(usage, "input_tokens", 0),
        completion_tokens=getattr(usage, "output_tokens", 0),
        served_by=InferenceTier.EXTERNAL_QUALITY,
    )


# Our own vLLM tiers serve Qwen3.x with ``--reasoning-parser``. Left on,
# thinking lands in ``reasoning_content`` and can spend the whole
# ``max_tokens`` budget, so ``content`` comes back empty (alert triage then
# fails with "LLM did not return JSON"). Every caller reads ``content`` only,
# so turn thinking off. Templates without the kwarg (Llama) ignore it; never
# sent to external APIs, which may reject unknown fields.
_SELF_HOSTED_TIERS = (InferenceTier.SOVEREIGN, InferenceTier.LOCAL)
_NO_THINKING: dict[str, Any] = {"chat_template_kwargs": {"enable_thinking": False}}


async def _openai_compat(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    response_format: str,
    served_by: InferenceTier,
) -> LLMResult:
    try:
        from openai import AsyncOpenAI
    except ImportError as e:  # pragma: no cover
        raise LLMUnavailable("openai SDK not installed") from e
    if not base_url:
        raise LLMUnavailable(f"{served_by.value}: base_url not configured")
    client = AsyncOpenAI(api_key=api_key or "not-required", base_url=base_url)
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if response_format == "json":
        kwargs["response_format"] = {"type": "json_object"}
    if served_by in _SELF_HOSTED_TIERS:
        kwargs["extra_body"] = _NO_THINKING
    resp = await client.chat.completions.create(**kwargs)
    text = resp.choices[0].message.content or ""
    return LLMResult(
        text=text,
        model=model,
        prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
        completion_tokens=resp.usage.completion_tokens if resp.usage else 0,
        served_by=served_by,
    )


def _openai_endpoint_for(
    served_by: InferenceTier,
    *,
    tenant_id: uuid.UUID,
    tenant_cfg: _TenantRoutingConfig,
    settings: Any,
) -> tuple[str, str, str]:
    """``(base_url, api_key, model)`` for an OpenAI-compatible tier.

    Mirrors the OpenAI branches of :func:`_dispatch` so :func:`chat_with_tools`
    targets the same endpoints the text path does.
    """
    if served_by is InferenceTier.SOVEREIGN:
        return (
            tenant_cfg.sovereign_url or "",
            (
                tenant_cfg.sovereign_token
                or _resolve_sovereign_token(tenant_id, settings)
                or "not-required"
            ),
            tenant_cfg.sovereign_model
            or settings.llm_local_model_quality
            or settings.llm_local_model_classifier,
        )
    if served_by is InferenceTier.LOCAL:
        return (
            settings.llm_local_base_url,
            settings.llm_local_api_key or "not-required",
            settings.llm_local_model_quality or settings.llm_local_model_classifier,
        )
    # EXTERNAL_CLASSIFIER — the OpenAI-compatible external endpoint
    # (DeepSeek by default). Prefer the quality model over the classifier
    # model for the agentic tool loop.
    return (
        settings.llm_base_url,
        settings.llm_api_key,
        settings.llm_model or settings.llm_model_classifier,
    )


async def _openai_compat_tools(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int,
    served_by: InferenceTier,
    tool_choice: str = "auto",
) -> dict[str, Any]:
    """OpenAI-compatible chat call carrying a tool catalogue.

    Unlike :func:`_openai_compat` (one-shot text), this passes the full
    message list plus ``tools`` and returns the assistant's text together
    with any ``tool_calls`` normalized to ``{id, name, arguments}`` (the
    arguments JSON parsed into a dict). The caller threads the LLMResult
    out for billing.
    """
    try:
        from openai import AsyncOpenAI
    except ImportError as e:  # pragma: no cover
        raise LLMUnavailable("openai SDK not installed") from e
    if not base_url:
        raise LLMUnavailable(f"{served_by.value}: base_url not configured")
    client = AsyncOpenAI(api_key=api_key or "not-required", base_url=base_url)
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice
    if served_by in _SELF_HOSTED_TIERS:
        kwargs["extra_body"] = _NO_THINKING
    resp = await client.chat.completions.create(**kwargs)
    choice = resp.choices[0]
    msg = choice.message
    tool_calls: list[dict[str, Any]] = []
    for tc in msg.tool_calls or []:
        try:
            args = json.loads(tc.function.arguments or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": args})
    usage = resp.usage
    return {
        "text": msg.content or "",
        "tool_calls": tool_calls,
        "finish_reason": choice.finish_reason,
        "result": LLMResult(
            text=msg.content or "",
            model=model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            served_by=served_by,
        ),
    }


# ── Usage emission ────────────────────────────────────────────────────────


async def _record_usage(
    *,
    tenant_id: uuid.UUID,
    sku: Any,
    tier: InferenceTier,
    result: LLMResult,
    source: str,
    latency_ms: int,
) -> float:
    cost = compute_cost(
        sku=sku,
        tier=tier,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )
    async with AsyncSessionLocal() as db:
        db.add(
            UsageEvent(
                tenant_id=tenant_id,
                tier=tier,
                model=result.model,
                source=source[:128],
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                latency_ms=latency_ms,
                cost_usd=cost,
                sku_id=sku.id if sku else None,
                occurred_at=datetime.now(tz=timezone.utc),
                payload={},
            )
        )
        await db.commit()
    return cost
