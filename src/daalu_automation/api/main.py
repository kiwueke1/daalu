"""FastAPI application entry point.

Importing this module imports every module package, which in turn
registers their agents, briefings, integrations, and workflows. The app
is intentionally side-effecty so a single ``uvicorn daalu_automation.api.main:app``
gives you the whole platform.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

import daalu_automation


def _build_info() -> dict[str, str]:
    """Resolve build identity from env vars set by the Dockerfile.

    BUILD_SHA / BUILD_TIME default to "unknown" so local `uvicorn` still
    serves /version without crashing — the UI degrades gracefully."""
    return {
        "version": daalu_automation.__version__,
        "commit_sha": os.environ.get("BUILD_SHA", "unknown"),
        "built_at": os.environ.get("BUILD_TIME", "unknown"),
    }

# Side-effect imports — register everything with the core registries.
import daalu_automation.modules  # noqa: F401
from daalu_automation.api.routers import (
    agents,
    alert_chat,
    alerts,
    auth,
    billing,
    briefings,
    change_proposals,
    cli_auth,
    clusters,
    coding,
    copilot,
    dashboards,
    events,
    feedback,
    gpu_metrics,
    infra,
    integrations,
    internal_agents,
    invites,
    observability,
    onboarding,
    recommendations,
    reports,
    sot_devices,
    sot_fabric,
    sot_webhooks,
    tenants,
    topology,
    workflows,
)
from daalu_automation.api.routers import (
    workspace as workspace_router,
)
from daalu_automation.config import get_settings
from daalu_automation.core.auth import TokenError, decode_token, looks_like_pat
from daalu_automation.core.bootstrap import ensure_default_tenant
from daalu_automation.database import create_tables
from daalu_automation.observability import init_observability

logger = structlog.get_logger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_observability(component="api")
    logger.info("daalu.startup", env=settings.environment)
    # Local dev convenience — production must run alembic via the init
    # container in deploy/k8s/api/deployment.yaml.
    await create_tables()
    await ensure_default_tenant()
    yield
    logger.info("daalu.shutdown")


class AuthGateMiddleware(BaseHTTPMiddleware):
    """Reject unauthenticated requests early.

    Public paths (health, login, docs in non-prod) pass through. The
    webhook ingest endpoint (``POST {prefix}/events``) is also exempt
    here — it has its own ``X-Daalu-Key`` gate enforced inside the
    route. Everything else needs a valid session cookie or Bearer token.

    Routes still depend on ``current_user`` so they get the user object;
    this middleware is the cheap first-pass that prevents anonymous
    callers from reaching SSE / static-list endpoints at all.
    """

    def __init__(self, app, public_paths: set[str], ingest_path: str) -> None:
        super().__init__(app)
        self._public = public_paths
        self._ingest_path = ingest_path
        # Customer-side daalu-edge POSTs here before any user session
        # exists; the route itself authenticates with the one-shot
        # invite token. Matches /api/v1/clusters/<slug>/callback.
        import re as _re

        self._cluster_callback_re = _re.compile(
            rf"^{_re.escape(ingest_path.rsplit('/', 1)[0])}/clusters/[^/]+/callback$"
        )
        # Nautobot webhooks authenticate via HMAC over the body, not via
        # the cookie/Bearer gate. Slug is the per-tenant URL identifier.
        self._sot_webhook_re = _re.compile(
            rf"^{_re.escape(ingest_path.rsplit('/', 1)[0])}/sot/webhooks/[^/]+$"
        )

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # NV-CM tool-UI traffic (``*.cmtools.daalu.io``) is host-routed to
        # the tool reverse proxy and authenticated by the *tenant* gateway's
        # own OIDC, not the hub session. Skip the hub auth gate for it.
        host = request.headers.get("host", "").split(":", 1)[0].lower()
        cm_zone = settings.cmtools_base_domain.lower()
        if host == cm_zone or host.endswith("." + cm_zone):
            return await call_next(request)
        if path in self._public:
            return await call_next(request)
        if path.startswith("/api/v1/auth/"):
            return await call_next(request)
        # Webhook ingest — POST {prefix}/events — uses X-Daalu-Key, not
        # the user session. Skip the cookie gate so external systems can
        # call it. The route itself still validates the key.
        if request.method == "POST" and path == self._ingest_path:
            return await call_next(request)
        # Customer-side edge bootstrap callback — auth via invite token
        # inside the route, so the cookie gate is bypassed.
        if request.method == "POST" and self._cluster_callback_re.match(path):
            return await call_next(request)
        # Nautobot webhooks — HMAC-verified inside the route.
        if request.method == "POST" and self._sot_webhook_re.match(path):
            return await call_next(request)
        # CORS preflight always passes — CORSMiddleware will handle it.
        if request.method == "OPTIONS":
            return await call_next(request)

        token = request.cookies.get(settings.auth_cookie_name)
        if not token:
            authz = request.headers.get("authorization", "")
            if authz.lower().startswith("bearer "):
                token = authz.split(None, 1)[1]
        if not token:
            return JSONResponse(
                {"detail": "not authenticated"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        # PATs are opaque, not signed — they don't decode as JWTs. Defer
        # validation to ``current_user`` which does the hash lookup. The
        # gate's only job here is to keep anonymous traffic out.
        if not looks_like_pat(token):
            try:
                decode_token(token)
            except TokenError as e:
                return JSONResponse(
                    {"detail": f"invalid token: {e}"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)


class CmtoolsHostRouter:
    """Outer ASGI shim: route ``*.cmtools.daalu.io`` to the NV-CM tool proxy.

    Deliberately a raw-ASGI middleware rather than a Starlette ``Host`` route
    in ``app.routes``: ``prometheus-fastapi-instrumentator`` iterates
    ``app.routes`` and reads ``route.path`` to name the metric, and a ``Host``
    route has no ``.path`` → ``AttributeError`` → 500 on *every* request.
    Registered as the outermost middleware so cmtools traffic is dispatched to
    the tool proxy before entering the metrics/auth stack at all; every other
    host falls through to the FastAPI app unchanged.
    """

    def __init__(self, app, *, zone: str, tool_router) -> None:
        self.app = app
        self.zone = zone
        self.tool_router = tool_router

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            host = ""
            for k, v in scope.get("headers", []):
                if k == b"host":
                    host = v.decode("latin-1").split(":", 1)[0].lower()
                    break
            if host.endswith("." + self.zone):
                await self.tool_router(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(*, mode: str | None = None) -> FastAPI:
    """Construct the FastAPI app.

    ``mode='hub'`` (default; also implied by an unset ``DAALU_MODE``
    env) — the full daalu-api: user cookies / PATs, all routers, hub
    Postgres, edge-forwarder for Private tenants.

    ``mode='edge'`` — daalu-edge-data, the per-tenant edge backend:
    no user-cookie auth, service-token-only via
    :class:`EdgeAuthMiddleware`, all tenant-scoped routers, the
    *local* Postgres (DATABASE_URL points at the bundled edge DB).
    See ``docs/design/edge-data-plane.md``.
    """
    if mode is None:
        mode = os.environ.get("DAALU_MODE", "hub").lower()
    if mode not in ("hub", "edge"):
        raise ValueError(f"unsupported mode: {mode!r}")

    app = FastAPI(
        title="Daalu Automation API" if mode == "hub" else "Daalu Edge Data",
        description=(
            "AI Operations Command Center — event-driven operational "
            "intelligence platform for the enterprise."
        ) if mode == "hub" else (
            "Edge-side tenant data plane. Serves tenant-scoped CRUD "
            "for a single tenant from the customer's own Postgres."
        ),
        version="0.1.0",
        docs_url="/docs" if (mode == "hub" and not settings.is_production) else None,
        redoc_url="/redoc" if (mode == "hub" and not settings.is_production) else None,
        lifespan=lifespan,
    )

    # CORS first — preflight needs to flow even for unauthenticated requests.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if mode == "edge":
        # Edge mode: only the service-token gate. No user cookies,
        # no PATs, no edge-forwarder (we ARE the edge).
        from daalu_automation.api.edge_auth import EdgeAuthMiddleware

        app.add_middleware(EdgeAuthMiddleware)
    else:
        # Hub mode: the existing cookie/PAT gate + edge-forwarder.
        public_paths = {p.strip() for p in settings.auth_public_paths.split(",") if p.strip()}
        app.add_middleware(
            AuthGateMiddleware,
            public_paths=public_paths,
            ingest_path=f"{settings.api_v1_prefix}/events",
        )

        # Edge-data forwarder — runs *after* AuthGate has validated
        # the session, so by the time we look up the tenant we
        # trust the caller. For Private tenants with edge_data_url
        # set, this short-circuits tenant-scoped requests to the
        # customer's edge via the WireGuard tunnel. Standard
        # tenants pay nothing here (one cached lookup per request).
        # See docs/design/edge-data-plane.md.
        from daalu_automation.core.edge_forward import EdgeForwardMiddleware

        app.add_middleware(EdgeForwardMiddleware, api_prefix=settings.api_v1_prefix)

    prefix = settings.api_v1_prefix
    for r in (
        auth.router,
        events.router,
        briefings.router,
        alerts.router,
        alert_chat.router,
        recommendations.router,
        reports.router,
        agents.router,
        workflows.router,
        integrations.router,
        clusters.router,
        dashboards.router,
        observability.router,
        onboarding.router,
        infra.router,
        copilot.router,
        tenants.router,
        change_proposals.router,
        sot_devices.router,
        sot_fabric.router,
        sot_webhooks.router,
        topology.router,
        billing.router,
        gpu_metrics.router,
        feedback.router,
        invites.router,
        workspace_router.router,
        cli_auth.router,
        coding.router,
        internal_agents.router,
    ):
        app.include_router(r, prefix=prefix)

    # IDE reverse proxy — Starlette Route, not a FastAPI APIRouter,
    # because the WebSocket upgrade path needs the raw ASGI surface.
    # Mounted at /ide/* (NOT under /api/v1/) so code-server's assets
    # paths resolve cleanly without an extra prefix to strip.
    from starlette.routing import Route, WebSocketRoute

    from daalu_automation.api.ide_proxy import ide_proxy_http, ide_proxy_ws

    app.router.routes.append(
        Route("/ide/{workspace_id:str}/{path:path}", endpoint=ide_proxy_http,
              methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]),
    )
    app.router.routes.append(
        WebSocketRoute("/ide/{workspace_id:str}/{path:path}", endpoint=ide_proxy_ws),
    )

    # Device SSH terminal — bridges the topology UI's xterm.js to an asyncssh
    # PTY on a fabric device. Starlette WebSocketRoute for the same raw-ASGI
    # reason as the IDE proxy; admin + tenant-flag gated inside the handler.
    from daalu_automation.api.device_terminal import device_terminal_ws

    app.router.routes.append(
        WebSocketRoute("/ws/device-terminal/{device_id:str}", endpoint=device_terminal_ws),
    )

    # NV-CM tool-UI reverse proxy — host-routed on ``*.cmtools.daalu.io``.
    # Inserted at the FRONT so it takes precedence over the host-agnostic
    # ``/``, ``/health`` etc. routes for cmtools hosts; for every other
    # host the Host matcher misses and routing falls through unchanged.
    # The proxy forwards over the tenant's WireGuard tunnel — see
    # api/tool_proxy.py and docs plan streamed-sniffing-wilkinson.
    if mode == "hub":
        from starlette.routing import Router

        from daalu_automation.api.tool_proxy import tool_proxy_http, tool_proxy_ws

        _tool_router = Router(
            routes=[
                Route(
                    "/{path:path}",
                    endpoint=tool_proxy_http,
                    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
                ),
                WebSocketRoute("/{path:path}", endpoint=tool_proxy_ws),
            ]
        )
        # Registered as a top-level ASGI middleware (see CmtoolsHostRouter) —
        # NOT inserted into app.routes — because a Starlette `Host` route has
        # no `.path` and crashes the prometheus instrumentator on every
        # request. Added last below so it sits outermost.
        _cmtools_tool_router = _tool_router

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": _build_info()["version"]}

    @app.get("/version")
    async def version():
        """Public build-identity endpoint, consumed by the /help page.

        Returns version, commit SHA, and build time. SHA + time are
        injected at image build time via the BUILD_SHA / BUILD_TIME env
        vars (see Dockerfile). In local dev where those are unset they
        come back as "unknown" — fine, the /help page handles it."""
        return _build_info()

    @app.get("/")
    async def root():
        return {
            "service": "daalu-automation",
            "docs": "/docs",
            "api": prefix,
        }

    # Prometheus metrics — instrument every HTTP route and expose /metrics.
    # Custom domain counters (LLM router, agents, workflows, …) are
    # registered as a side-effect of importing core.metrics. The
    # instrumentator handles the default process + http_* families.
    try:
        from prometheus_fastapi_instrumentator import Instrumentator

        import daalu_automation.core.metrics  # noqa: F401 — registers collectors

        Instrumentator(
            should_group_status_codes=True,
            should_ignore_untemplated=True,
            should_respect_env_var=False,
            excluded_handlers=["/metrics", "/health"],
        ).instrument(app).expose(
            app,
            endpoint="/metrics",
            include_in_schema=False,
            tags=["observability"],
        )
    except ImportError:  # pragma: no cover — dev image without instrumentator
        logger.warning(
            "prometheus_instrumentator.missing",
            hint="pip install prometheus-fastapi-instrumentator to enable /metrics",
        )

    # Outermost middleware: NV-CM tool proxy host-routing for *.cmtools.daalu.io.
    # Added after the instrumentator so it wraps the entire FastAPI stack and
    # dispatches proxy traffic before it reaches the metrics/route machinery.
    if mode == "hub":
        app.add_middleware(
            CmtoolsHostRouter,
            zone=settings.cmtools_base_domain.lower(),
            tool_router=_cmtools_tool_router,
        )

    return app


app = create_app()
