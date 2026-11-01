"""MCP over Streamable HTTP — ``POST /api/v1/mcp``.

docs/design/ai-ops-maturity.md §9. JSON response mode only: one JSON-RPC
message in, one ``application/json`` message out; notifications and client
responses get ``202 Accepted`` with no body. There is no server-initiated
stream, so ``GET`` is ``405``. Stateless: no ``Mcp-Session-Id``.

Authentication is the same as every other route: the auth gate middleware
plus ``current_user`` — in practice a personal access token
(``Authorization: Bearer dpat_…``). The tenant comes from the token's user.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_tenant_id, current_user
from daalu_automation.core.aiops import mcp as mcp_core
from daalu_automation.database import get_db
from daalu_automation.models import User

router = APIRouter(tags=["aiops"])

_MAX_BODY = 1_000_000


@router.post("/mcp")
async def mcp_post(
    request: Request,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
) -> Response:
    version = request.headers.get("mcp-protocol-version")
    if version and version not in mcp_core.SUPPORTED_PROTOCOL_VERSIONS:
        return JSONResponse(
            mcp_core.rpc_error(None, mcp_core.INVALID_REQUEST,
                               f"unsupported MCP-Protocol-Version {version!r}"),
            status_code=400,
        )
    raw = await request.body()
    if len(raw) > _MAX_BODY:
        return JSONResponse(
            mcp_core.rpc_error(None, mcp_core.INVALID_REQUEST, "request too large"),
            status_code=413,
        )
    try:
        message = json.loads(raw)
    except ValueError:
        return JSONResponse(
            mcp_core.rpc_error(None, mcp_core.PARSE_ERROR, "parse error"), status_code=400,
        )
    if isinstance(message, list):
        return JSONResponse(
            mcp_core.rpc_error(None, mcp_core.INVALID_REQUEST,
                               "JSON-RPC batches are not supported (MCP 2025-06-18)"),
            status_code=400,
        )
    reply = await mcp_core.handle(message, db=db, tenant_id=tenant_id, user=user)
    if reply is None:
        return Response(status_code=202)
    return JSONResponse(reply)


@router.get("/mcp")
async def mcp_get() -> Response:
    """No server→client SSE stream is offered (JSON response mode)."""
    return Response(status_code=405, headers={"Allow": "POST"})
