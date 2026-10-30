"""Public coding endpoint — the laptop CLI's inference front door.

The in-cluster code-server posts straight to the inference-gateway with a
service-token. A laptop can't mint one, so ``daalu-api`` becomes the
authenticated proxy: it accepts the user's PAT (``current_user``), checks the
tenant is eligible, then mints the ``purpose=coding`` service-token *on the
user's behalf* and forwards the request — the same move ``/ide/sessions`` makes
for the workspace controller.

That ``purpose=coding`` token is what preserves sovereignty from the laptop: the
inference router refuses every external LLM tier for coding, so the prompt never
leaves the perimeter even though it originated off-cluster.

Endpoints (prefix ``/coding`` → ``/api/v1/coding``):

* ``GET  /config``               — self-configuration for the CLI after login.
* ``POST /v1/chat/completions``  — OpenAI-compatible passthrough.

Non-streaming in v1 — mirrors the single-shot POST the agent loop already uses.
"""

from __future__ import annotations

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_user
from daalu_automation.api.routers.workspace import _eligible_tenant
from daalu_automation.config import get_settings
from daalu_automation.core import model_catalog
from daalu_automation.core.coding_gateway import resolve_coding_upstream
from daalu_automation.core.service_tokens import mint_service_token
from daalu_automation.database import get_db
from daalu_automation.models.user import User

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/coding", tags=["coding"])


class CodingModelConfig(BaseModel):
    id: str
    label: str
    served_model_name: str
    context_len: int
    available: bool
    default: bool


class CodingConfigView(BaseModel):
    """Everything the CLI needs to talk to inference after login."""

    api_base: str
    chat_completions_path: str
    default_model: str
    models: list[CodingModelConfig]


@router.get("/config", response_model=CodingConfigView)
async def coding_config(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> CodingConfigView:
    """Self-configuration for ``daalu`` — model list + endpoint path.

    Gated by the same eligibility check as the workspace, so a tenant that
    can't serve coding prompts gets a clean 403 instead of a later 503.
    """
    await _eligible_tenant(db, user)
    settings = get_settings()
    enabled = set(settings.coding_models_enabled)
    models = [
        CodingModelConfig(
            id=m.id,
            label=m.label,
            served_model_name=m.served_model_name,
            context_len=m.context_len,
            available=m.id in enabled,
            default=m.default,
        )
        for m in model_catalog.all_models()
    ]
    return CodingConfigView(
        api_base=settings.public_base_url.rstrip("/"),
        chat_completions_path="/api/v1/coding/v1/chat/completions",
        default_model=model_catalog.served_name(settings.coding_model_default),
        models=models,
    )


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> JSONResponse:
    """OpenAI-compatible passthrough, authenticated by the user's PAT."""
    tenant = await _eligible_tenant(db, user)
    settings = get_settings()

    body = await request.json()
    if not isinstance(body, dict) or not body.get("messages"):
        raise HTTPException(422, "request body must include a non-empty 'messages'")

    # Resolve + validate the model server-side. The CLI sends a served-model
    # name (from /coding/config); fall back to the default served name.
    requested = body.get("model")
    default_served = model_catalog.served_name(settings.coding_model_default)
    served_names = {m.served_model_name for m in model_catalog.all_models()}
    body["model"] = requested if requested in served_names else default_served

    upstream = resolve_coding_upstream(settings)
    if upstream is None:
        raise HTTPException(503, "no coding inference is configured for this platform")

    headers = {"content-type": "application/json"}
    if upstream.needs_service_token:
        token = mint_service_token(
            tenant_id=str(tenant.id),
            user_id=str(user.id),
            purpose="coding",
            ttl_seconds=120,
        )
        headers["authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                upstream.chat_completions_url, headers=headers, json=body
            )
    except httpx.HTTPError as e:
        logger.warning("coding.upstream_unreachable", error=str(e))
        raise HTTPException(502, "coding inference upstream unreachable") from e

    if resp.status_code >= 400:
        logger.warning(
            "coding.upstream_error",
            status=resp.status_code,
            body=resp.text[:200],
            tenant_id=str(tenant.id),
        )
        # Surface the upstream status so the CLI can distinguish a 429
        # (quota) from a 5xx (broken) — but don't leak the upstream body.
        raise HTTPException(
            status_code=resp.status_code if resp.status_code < 500 else 502,
            detail="coding inference rejected the request",
        )
    return JSONResponse(resp.json())
