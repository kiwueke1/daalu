"""FastAPI app exposing the OpenAI-compatible gateway endpoints.

Runs as a standalone process — ``daalu inference-gateway`` — in its
own K8s Deployment. The only legitimate caller is ``daalu-api``; the
NetworkPolicy on the gateway Service enforces that at L3.

Endpoints:

* ``POST /v1/chat/completions`` — OpenAI-compatible passthrough with
  auth + quota + metering wrapped around it.
* ``GET  /healthz``            — k8s liveness/readiness probe.
* ``GET  /metrics``            — Prometheus metrics (unauthenticated;
  same convention as the rest of the stack).

Auth: ``Authorization: Bearer <service-token-jwt>`` (see
``core/service_tokens``). The JWT names the tenant and the purpose
(``chat`` / ``classifier`` / ``coding`` / etc.). Missing or invalid
token → 401.
"""

from __future__ import annotations

import time

import redis.asyncio as redis_async
import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.config import get_settings
from daalu_automation.core.service_tokens import (
    ServiceTokenClaims,
    ServiceTokenError,
    verify_service_token,
)
from daalu_automation.database import get_db
from daalu_automation.inference_gateway.metering import (
    record_revenue_share,
    write_usage_event,
)
from daalu_automation.inference_gateway.pool import resolve_upstream
from daalu_automation.inference_gateway.quota import (
    QuotaError,
    check_monthly_quota,
    check_rate_limit,
    load_quota,
    record_usage,
)
from daalu_automation.inference_gateway.upstream import UpstreamError, chat_completion

logger = structlog.get_logger(__name__)

# ── Prometheus metrics ───────────────────────────────────────────────────
# Cardinality kept low: by-tenant labels not in the histogram (would
# explode). Per-tenant dashboards aggregate from usage_events instead.
GATEWAY_REQUESTS_TOTAL = Counter(
    "daalu_inference_gateway_requests_total",
    "Requests served by the daalu-hosted inference gateway",
    labelnames=("purpose", "outcome"),
)
GATEWAY_LATENCY_SECONDS = Histogram(
    "daalu_inference_gateway_latency_seconds",
    "Wall-clock latency of gateway requests, including upstream.",
    labelnames=("purpose",),
)


def create_app() -> FastAPI:
    """Build the gateway FastAPI app.

    Factory function so tests can inject a fresh app per test run; the
    Deployment's entrypoint just calls ``create_app()`` and hands it
    to uvicorn.
    """
    app = FastAPI(
        title="Daalu Inference Gateway",
        version="0.1.0",
        # No OpenAPI exposed publicly — the only client is daalu-api,
        # which has the contract baked in. Disable to reduce surface.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.on_event("startup")
    async def _startup() -> None:
        settings = get_settings()
        url = settings.daalu_hosted_redis_url or settings.celery_broker_url
        if not url:
            logger.warning("gateway.no_redis_url — rate limiting disabled")
            app.state.redis = None
        else:
            app.state.redis = redis_async.from_url(url, decode_responses=True)
            logger.info("gateway.redis_connected", url=url.split("@")[-1])

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if app.state.redis is not None:
            await app.state.redis.aclose()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/chat/completions")
    async def chat_completions(
        request: Request,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> JSONResponse:
        claims = _authenticate(authorization)
        body = await request.json()
        purpose = claims.purpose
        started = time.monotonic()

        # 1) Load quota state for the tenant
        try:
            state = await load_quota(db, claims.tenant_id)
        except QuotaError as e:
            GATEWAY_REQUESTS_TOTAL.labels(purpose=purpose, outcome="not_enrolled").inc()
            logger.info("gateway.refused", tenant_id=claims.tenant_id, reason=e.reason)
            raise HTTPException(status_code=403, detail="tier not enabled for tenant")

        # 2) Monthly + per-minute checks
        try:
            check_monthly_quota(state)
            if app.state.redis is not None:
                await check_rate_limit(app.state.redis, state, purpose)
        except QuotaError as e:
            outcome = (
                "cloud_overflow" if e.policy_action == "cloud_overflow" else "rate_limited"
            )
            GATEWAY_REQUESTS_TOTAL.labels(purpose=purpose, outcome=outcome).inc()
            # 429 conveys "you should retry / fall through" semantics
            # to the router; 402 would imply payment required which is
            # not exactly true.
            headers = {"x-daalu-policy-action": e.policy_action}
            raise HTTPException(status_code=429, detail=e.reason, headers=headers)

        # 3) Resolve the shared GPU pool and dispatch to its upstream vLLM
        resolved = await resolve_upstream(db, body.get("model"))
        try:
            resp = await chat_completion(
                tenant_id=claims.tenant_id,
                purpose=purpose,
                body=body,
                upstream_url=resolved.upstream_url,
            )
        except UpstreamError as e:
            GATEWAY_REQUESTS_TOTAL.labels(purpose=purpose, outcome="upstream_error").inc()
            raise HTTPException(status_code=e.status_code or 502, detail=str(e))

        # 4) Post-call accounting — never blocks the response on failure
        usage = resp.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        total_tokens = prompt_tokens + completion_tokens

        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            await record_usage(db, app.state.redis, state, total_tokens=total_tokens)
            gross_usd = await write_usage_event(
                db,
                tenant_id=claims.tenant_id,
                user_id=claims.user_id,
                purpose=purpose,
                model=str(resp.get("model", "")),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
            )
            # Credit the GPU owner for the call (skipped on env-var fallback,
            # where there is no provider to credit).
            if resolved.provider_tenant_id is not None:
                await record_revenue_share(
                    db,
                    consumer_tenant_id=claims.tenant_id,
                    provider_tenant_id=resolved.provider_tenant_id,
                    pool_id=resolved.pool_id,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    gross_usd=gross_usd,
                )
        except Exception:  # noqa: BLE001 — accounting is best-effort
            logger.exception(
                "gateway.accounting_failed",
                tenant_id=claims.tenant_id,
                tokens=total_tokens,
            )

        GATEWAY_LATENCY_SECONDS.labels(purpose=purpose).observe(
            time.monotonic() - started
        )
        GATEWAY_REQUESTS_TOTAL.labels(purpose=purpose, outcome="ok").inc()
        return JSONResponse(resp)

    return app


def _authenticate(authorization: str | None) -> ServiceTokenClaims:
    """Pull bearer token, verify, return claims. Raises 401 on failure.

    Keeps 401 detail intentionally vague — the only legitimate caller
    is ``daalu-api`` which has the right token by definition; everyone
    else gets a generic refusal.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(None, 1)[1]
    try:
        return verify_service_token(token)
    except ServiceTokenError as e:
        logger.warning("gateway.auth_failed", error=str(e))
        raise HTTPException(status_code=401, detail="invalid token")


app = create_app()  # module-level app for `uvicorn daalu_automation.inference_gateway.app:app`
