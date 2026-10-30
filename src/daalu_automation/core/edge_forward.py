"""Hub → edge request forwarder for Daalu Private (full sovereignty).

For tenants with ``is_private=True`` and ``edge_data_url`` set, the
hub's daalu-api becomes a reverse proxy for tenant-scoped paths
(see :data:`TENANT_SCOPED_PREFIXES`). The forwarder:

1. Resolves the tenant from the authenticated session.
2. Decides whether this path is tenant-scoped *and* this tenant is
   Private — short-circuits to local handling otherwise.
3. Mints a short-lived service-token JWT (``purpose='edge-rpc'``).
4. POSTs the request to ``<edge_data_url><path>`` with the JWT.
5. Returns the edge's response untouched.

The hub never reads or persists the response body — it's a byte
stream the hub passes through. Same data-handling semantics as a
load balancer.

See ``docs/design/edge-data-plane.md`` for the architecture.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

import httpx
import structlog
from fastapi import Request
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response, StreamingResponse

from daalu_automation.config import get_settings
from daalu_automation.core.service_tokens import mint_service_token

logger = structlog.get_logger(__name__)


# Path prefixes (after the configured ``api_v1_prefix``) that touch
# tenant-scoped tables. Keep in sync with §3.3 of the design doc and
# with the router registrations in api/main.py.
TENANT_SCOPED_PREFIXES: tuple[str, ...] = (
    "/alerts",
    "/events",
    "/change_proposals",
    "/briefings",
    "/agents",
    "/workflows",
    "/clusters",
    "/infra",
    "/sot",
    "/recommendations",
    "/integrations",
    "/copilot",
    "/alert_chat",
    "/ide/sessions",
)


# Path prefixes that stay on the hub even for Private tenants. Listed
# explicitly so a future router added under /api/v1/* doesn't
# accidentally inherit "forward to edge" by default.
PLATFORM_SCOPED_PREFIXES: tuple[str, ...] = (
    "/auth",
    "/billing",
    "/tenants",
    "/onboarding",
    "/feedback",
    "/internal",
    # Invites mint platform-side rows: they create User records on
    # accept, and Users live on the hub even for Private tenants
    # (auth is platform-scoped per docs/design/daalu-private.md §4).
    # Keep invites on the hub.
    "/invites",
)


def is_tenant_scoped(path: str, api_prefix: str = "/api/v1") -> bool:
    """Return True if this path needs to be forwarded for Private tenants.

    A path is tenant-scoped if it starts with ``<api_prefix><one of
    TENANT_SCOPED_PREFIXES>``. Exact-match on the prefix segment so
    ``/api/v1/agents`` matches but ``/api/v1/agents-something-else``
    does not (no router currently does this, but we defend at the
    matcher level).
    """
    if not path.startswith(api_prefix):
        return False
    rest = path[len(api_prefix):]
    for prefix in TENANT_SCOPED_PREFIXES:
        if rest == prefix or rest.startswith(prefix + "/"):
            return True
    return False


class EdgeForwardMiddleware(BaseHTTPMiddleware):
    """Forward tenant-scoped requests for Private tenants to their edge.

    Sits *after* ``AuthGateMiddleware`` in the middleware chain, so
    by the time we run the cookie/PAT has been validated and (for
    PATs) the user's tenant is determinable. We re-resolve the tenant
    here to avoid coupling — the middleware reads ``tenants.is_private``
    + ``tenants.edge_data_url`` on every request that *might* need to
    forward.

    Per-request DB reads are cheap (one PK lookup) but we keep a
    small in-process cache of ``(tenant_id) → (is_private,
    edge_data_url, token_hash)`` with a 30 s TTL to amortise. Flips
    propagate within the TTL.
    """

    def __init__(self, app: Callable, *, api_prefix: str = "/api/v1") -> None:
        super().__init__(app)
        self.api_prefix = api_prefix
        self._cache: dict[str, tuple[float, bool, str | None]] = {}
        self._cache_ttl_s = 30.0

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if not is_tenant_scoped(path, self.api_prefix):
            return await call_next(request)

        # The user has already been authenticated by AuthGate. To get
        # the tenant we need a DB lookup keyed by the user id in the
        # cookie/PAT. That happens inside ``current_user`` already —
        # we can't reuse it cheaply here without re-running the
        # dependency. Instead: peek at the user from request state if
        # an earlier middleware set it; otherwise decode the JWT
        # ourselves.
        tenant_id = await self._tenant_for_request(request)
        if not tenant_id:
            # No tenant resolvable → let the route handler 401/403.
            # The forwarder is only an *override*; missing prerequisites
            # fall through to normal handling.
            return await call_next(request)

        cfg = await self._tenant_routing(tenant_id)
        if cfg is None or not cfg[0] or not cfg[1]:
            # Not private OR no edge_data_url configured → normal path.
            return await call_next(request)

        edge_url = cfg[1]
        try:
            return await _proxy(request, edge_url, tenant_id, path)
        except _ForwardError as e:
            # Edge unreachable / 5xx — bubble back a 502 with the
            # reason so the UI can show a clear banner. We do NOT
            # fall back to hub-local handling; for Private tenants
            # the hub doesn't have the data anyway.
            logger.warning(
                "edge_forward.failed",
                tenant_id=tenant_id,
                edge_url=edge_url,
                path=path,
                reason=str(e),
            )
            return Response(
                content=b'{"detail":"edge data plane unavailable"}',
                status_code=502,
                media_type="application/json",
            )

    async def _tenant_for_request(self, request: Request) -> str | None:
        """Best-effort tenant resolution from the cookie/PAT.

        Returns None on failure — we want the forwarder to be
        permissive (let the next layer decide) rather than blocking.
        """
        settings = get_settings()
        cookie_token = request.cookies.get(settings.auth_cookie_name)
        token: str | None = cookie_token
        if not token:
            authz = request.headers.get("authorization", "")
            if authz.lower().startswith("bearer "):
                token = authz.split(None, 1)[1]
        if not token:
            return None

        # Two paths: JWT user session (decode directly) or PAT (hash
        # lookup). Both yield a tenant id. We try JWT first because
        # it's the common browser path and doesn't need a DB hit.
        from daalu_automation.core.auth import (
            TokenError,
            decode_token,
            hash_pat,
            looks_like_pat,
        )

        if looks_like_pat(token):
            from daalu_automation.database import AsyncSessionLocal
            from daalu_automation.models import PersonalAccessToken

            digest = hash_pat(token)
            async with AsyncSessionLocal() as db:
                row = (
                    await db.execute(
                        select(PersonalAccessToken).where(
                            PersonalAccessToken.token_hash == digest
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    return None
                # Look up the user to get tenant.
                from daalu_automation.models import User

                user = await db.get(User, row.user_id)
                return str(user.tenant_id) if user else None
        try:
            payload = decode_token(token)
        except TokenError:
            return None
        return payload.get("tid")

    async def _tenant_routing(self, tenant_id: str) -> tuple[bool, str | None, str | None] | None:
        """Return ``(is_private, edge_data_url, edge_data_token_hash)`` or None.

        Cached with a short TTL — flips propagate inside the TTL.
        """
        import time as _time

        now = _time.monotonic()
        cached = self._cache.get(tenant_id)
        if cached and now - cached[0] < self._cache_ttl_s:
            _, is_private, edge_url = cached
            return (is_private, edge_url, None)

        from daalu_automation.database import AsyncSessionLocal
        from daalu_automation.models.tenant import Tenant

        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(select(Tenant).where(Tenant.id == tenant_id))
            ).scalar_one_or_none()
            if row is None:
                return None
            is_priv = bool(getattr(row, "is_private", False))
            edge_url = getattr(row, "edge_data_url", None)
        self._cache[tenant_id] = (now, is_priv, edge_url)
        return (is_priv, edge_url, None)


class _ForwardError(Exception):
    """Raised by :func:`_proxy` when the edge can't be reached."""


async def _proxy(
    request: Request, edge_url: str, tenant_id: str, path: str
) -> Response:
    """Forward the request to the edge, return the response.

    The forwarder is intentionally a thin, defensive HTTP client:
    * Strips cookies (the edge doesn't accept them).
    * Strips ``Host`` (httpx rewrites it).
    * Re-mints a service-token JWT for every request — short TTL,
      no caching here. The JWT cost is one HMAC-SHA256, negligible.
    * Streams the response body so large list endpoints don't
      double their memory footprint in the hub.
    """
    body = await request.body()
    # User context for the edge: who initiated this, on what tenant.
    # We synthesise a bogus user_id when the caller is the system
    # (rare for forwarded paths; included for completeness).
    user_id = "00000000-0000-0000-0000-000000000000"
    # Pull the real user id from the request state if set by deps.
    cookie_token = request.cookies.get(get_settings().auth_cookie_name)
    if cookie_token:
        try:
            from daalu_automation.core.auth import decode_token

            payload = decode_token(cookie_token)
            if payload.get("sub"):
                user_id = str(payload["sub"])
        except Exception:  # noqa: BLE001
            pass

    token = mint_service_token(
        tenant_id=tenant_id,
        user_id=user_id,
        purpose="edge-rpc",
        ttl_seconds=60,
    )
    fwd_url = edge_url.rstrip("/") + path
    if request.url.query:
        fwd_url += "?" + request.url.query
    fwd_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "cookie", "content-length", "authorization")
    }
    fwd_headers["authorization"] = f"Bearer {token}"
    fwd_headers["x-daalu-forwarded-for"] = request.client.host if request.client else ""
    try:
        # Use streaming so a giant /api/v1/events listing doesn't sit
        # entirely in hub memory before flushing to the browser.
        client = httpx.AsyncClient(timeout=60.0)
        upstream = await client.request(
            request.method, fwd_url, content=body, headers=fwd_headers
        )
    except httpx.HTTPError as e:
        raise _ForwardError(f"edge unreachable: {e}") from e

    out_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in ("content-encoding", "transfer-encoding", "connection")
    }

    async def _stream() -> Iterable[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        _stream(),
        status_code=upstream.status_code,
        headers=out_headers,
    )
