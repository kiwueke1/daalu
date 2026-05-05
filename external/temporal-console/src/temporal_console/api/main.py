"""FastAPI entry point — mounts all routers, middleware, static files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from temporal_console import auth
from temporal_console.api.routers import actions, dashboard, schedules, start, workflows
from temporal_console.config import get_settings

logger = structlog.get_logger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Temporal Console",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
    )

    # Authlib needs Starlette session for OAuth state. Unrelated to our
    # own tc_session cookie; Starlette stores this in its own signed
    # cookie named `session`.
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)

    # `_Unauthenticated` → 302 /auth/login
    app.add_exception_handler(auth._Unauthenticated, auth.handle_unauthenticated)

    # Static + templates are local to the package so the Docker image
    # only needs to ship the single package dir.
    here = Path(__file__).parent
    app.mount("/static", StaticFiles(directory=str(here / "static")), name="static")

    templates = Jinja2Templates(directory=str(here / "templates"))
    templates.env.globals["brand"] = {
        "name": settings.brand_name,
        "subtitle": settings.brand_subtitle,
        "accent": settings.brand_accent,
    }
    templates.env.globals["namespaces"] = settings.namespaces
    templates.env.globals["namespace_display"] = settings.namespace_display
    templates.env.globals["deep_link_for"] = settings.deep_link_for
    templates.env.globals["auth_enabled"] = settings.auth_enabled
    app.state.templates = templates

    # ── auth routes ──────────────────────────────────────────────────
    app.get("/auth/login")(auth.login)
    app.get("/auth/callback")(auth.callback)
    app.post("/auth/logout")(auth.logout)

    # ── feature routers ──────────────────────────────────────────────
    app.include_router(dashboard.router)
    app.include_router(workflows.router)
    app.include_router(schedules.router)
    app.include_router(actions.router)
    # Start router needs to register before fallback /workflows/new in
    # workflows.router so /workflows/start/* takes precedence.
    app.include_router(start.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok"}

    return app


app = create_app()


def run() -> None:
    """CLI entry point — `temporal-console` on $PATH."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "temporal_console.api.main:app",
        host="0.0.0.0",
        port=8000,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
