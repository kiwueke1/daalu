"""Auth middleware for ``daalu server --mode=edge``.

The edge data plane does *not* accept user cookies or PATs. Its only
caller is the hub's :class:`EdgeForwardMiddleware`, which mints a
service-token JWT (``purpose='edge-rpc'``) per request. This
middleware verifies the JWT, enforces tenant scoping, and stamps the
request state with the verified identity so downstream handlers can
read it.

Two layers of defense:

1. **Token verification** — standard JWT check via
   :func:`verify_service_token`. Wrong audience, expired, bad
   signature → 401.
2. **Tenant boundary** — the JWT's tenant claim must equal the
   tenant this edge was provisioned for (``DAALU_EDGE_TENANT_ID``
   env). A misrouted forward from the hub (tenant A's request lands
   at tenant B's edge) gets 403. This is the line that makes
   cross-tenant data exposure on the edge impossible — even with a
   bug on the hub side.
"""

from __future__ import annotations

import os

import structlog
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from daalu_automation.core.service_tokens import (
    ServiceTokenError,
    verify_service_token,
)

logger = structlog.get_logger(__name__)


def expected_tenant_id() -> str:
    """The tenant this edge is provisioned for.

    Set as ``DAALU_EDGE_TENANT_ID`` in the daalu-edge-data
    Deployment. An empty value disables the edge auth boundary —
    we refuse to start in that case so a misconfiguration can't
    silently turn an edge into a multi-tenant data lake.
    """
    tid = os.environ.get("DAALU_EDGE_TENANT_ID", "").strip()
    if not tid:
        raise RuntimeError(
            "DAALU_EDGE_TENANT_ID is not set — refusing to run edge mode "
            "without a tenant boundary. Set it in the daalu-edge-data "
            "Deployment env."
        )
    return tid


# Paths the edge serves without auth (k8s probes, scrape).
_EDGE_PUBLIC_PATHS: set[str] = {"/health", "/healthz", "/metrics", "/version"}


class EdgeAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _EDGE_PUBLIC_PATHS:
            return await call_next(request)

        authz = request.headers.get("authorization", "")
        if not authz.lower().startswith("bearer "):
            return JSONResponse({"detail": "missing bearer token"}, status_code=401)
        token = authz.split(None, 1)[1]
        try:
            claims = verify_service_token(token)
        except ServiceTokenError as e:
            logger.warning("edge_auth.invalid_token", error=str(e))
            return JSONResponse({"detail": "invalid token"}, status_code=401)

        if claims.purpose != "edge-rpc":
            logger.warning(
                "edge_auth.wrong_purpose",
                purpose=claims.purpose,
                tenant_id=claims.tenant_id,
            )
            return JSONResponse({"detail": "purpose not permitted"}, status_code=403)

        if claims.tenant_id != expected_tenant_id():
            logger.warning(
                "edge_auth.cross_tenant_attempt",
                claimed_tenant=claims.tenant_id,
                expected_tenant=expected_tenant_id(),
            )
            return JSONResponse({"detail": "tenant mismatch"}, status_code=403)

        # Stamp request state so downstream deps that would normally
        # read it from the user cookie still work. The handlers see
        # the exact same shape as a hub-side authenticated request.
        request.state.tenant_id = claims.tenant_id
        request.state.user_id = claims.user_id
        request.state.is_edge_request = True
        return await call_next(request)
