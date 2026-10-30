"""User-facing IDE endpoints on daalu-api.

The browser uses these to ask "do I have a workspace? open it" and
to drive the reverse proxy. Auth is the normal user cookie / PAT —
this router does *not* accept service tokens.

The router talks to the workspace-controller over HTTP (the
controller's URL comes from ``settings.workspace_controller_url``)
using a short-lived service token minted on behalf of the
authenticated user.

The IDE itself is served at ``/ide/{workspace_id}/...`` — the proxy
handler is intentionally separate (and implemented as a Starlette
endpoint rather than FastAPI route) so the WebSocket upgrade path
stays clean.
"""

from __future__ import annotations

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_user
from daalu_automation.config import get_settings
from daalu_automation.core import model_catalog
from daalu_automation.core.coding_gateway import coding_upstream_healthy
from daalu_automation.core.service_tokens import mint_service_token
from daalu_automation.database import get_db
from daalu_automation.models.tenant import Tenant
from daalu_automation.models.user import User

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/ide", tags=["ide"])


class WorkspaceSpec(BaseModel):
    profile: str = Field(default="small", pattern="^(small|medium|large)$")
    git_repo_url: str | None = None
    git_branch: str = Field(default="main", max_length=255)
    # Catalog id of the coding model (``core.model_catalog``). None →
    # the operator's configured default. Validated server-side against
    # ``settings.coding_models_enabled`` in ``create_session``.
    model: str | None = None
    # Write-only personal-access-token for cloning a *private* repo
    # (GitHub / GitLab / self-hosted). Forwarded verbatim to the controller,
    # which encrypts it at rest. Never returned in any response. ``None``
    # leaves a stored token unchanged; ``""`` clears it. Capped to a sane
    # length — real PATs are well under this.
    git_token: str | None = Field(default=None, max_length=512)


class WorkspaceView(BaseModel):
    id: str
    state: str
    profile: str
    model: str
    git_repo_url: str | None
    git_branch: str
    # True when a PAT is stored (private-repo auth). The token is never
    # exposed; this is only for rendering a "private" indicator.
    git_authenticated: bool = False
    ide_url: str  # path-relative URL the browser opens to launch the IDE


class CodingModelView(BaseModel):
    """One row of the model picker the Create-Workspace form renders."""

    id: str
    label: str
    description: str
    gpu_class: str
    vram_gb: int
    available: bool  # vLLM actually deployed (settings.coding_models_enabled)
    default: bool


async def _eligible_tenant(
    db: AsyncSession, user: User
) -> Tenant:
    """Refuse with 403 if the tenant cannot serve a coding workspace.

    Two gates, both of which must pass:

    1. **Entitlement** — one of: ``workspaces_enabled`` (operator override),
       a configured ``sovereign_inference_url`` (customer's own GPU), or
       ``daalu_hosted_enabled`` against a configured hosted gateway.
    2. **A coder is actually serving** — see
       :func:`coding_gateway.coding_upstream_healthy`.

    Gate 2 exists because entitlement used to be the whole test, and it keys
    on the *classifier*: provisioning a GPU sets ``sovereign_inference_url``,
    which flipped workspaces on even though workspaces consume the **coder** —
    a separate vLLM stack that nothing in the UI deploys. The result was a
    workspace that created fine, opened an IDE, and then failed on the first
    prompt with a transport error. Better to say so up front.

    ``workspaces_enabled`` deliberately bypasses gate 2: it's the operator's
    "I know what I'm doing" switch, used to set a workspace up before the
    coder is deployed.

    The LLM router enforces ``purpose='coding'`` blocks all *external*
    tiers (no Anthropic/OpenAI fallback), so any combination above keeps
    code prompts inside the perimeter.
    """
    tenant = await db.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=403, detail="tenant not found")
    flags = tenant.feature_flags or {}
    settings = get_settings()
    hosted_available = bool(settings.daalu_hosted_gateway_url) or bool(
        settings.llm_local_base_url
    )
    override = bool(flags.get("workspaces_enabled"))
    entitled = (
        override
        or bool(tenant.sovereign_inference_url)
        or (bool(flags.get("daalu_hosted_enabled")) and hosted_available)
    )
    if not entitled:
        raise HTTPException(
            status_code=403,
            detail=(
                "coding workspace not enabled for this tenant — "
                "requires SOVEREIGN GPU, daalu-hosted enrolment, or "
                "an operator override (feature_flags.workspaces_enabled)"
            ),
        )
    if not override and not await coding_upstream_healthy(settings):
        raise HTTPException(
            status_code=409,
            detail=(
                "no coding model is currently serving — the coder is a "
                "separate vLLM stack from the classifier and is not deployed "
                "on this cluster. Deploy it (`daalu render-coding-model "
                "<id> | kubectl apply -f -`), or set "
                "feature_flags.workspaces_enabled to override this check."
            ),
        )
    return tenant


async def _controller_call(
    *,
    method: str,
    user: User,
    path: str,
    json: dict | None = None,
) -> httpx.Response:
    """Authenticated HTTP call to the workspace-controller."""
    settings = get_settings()
    url = settings.workspace_controller_url.rstrip("/") + path
    token = mint_service_token(
        tenant_id=str(user.tenant_id),
        user_id=str(user.id),
        purpose="coding",
        ttl_seconds=60,
    )
    headers = {"authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.request(method, url, headers=headers, json=json)


def _view(data: dict) -> WorkspaceView:
    """Map a workspace-controller response to the browser-facing view."""
    return WorkspaceView(
        id=data["id"],
        state=data["state"],
        profile=data["profile"],
        # ``model`` is nullable for rows predating the column; fall back
        # to the catalog default so the UI always has a value to show.
        model=data.get("model") or model_catalog.default_model().id,
        git_repo_url=data.get("git_repo_url"),
        git_branch=data["git_branch"],
        git_authenticated=bool(data.get("git_authenticated")),
        ide_url=f"/ide/{data['id']}/?folder=/workspace",
    )


class EligibilityView(BaseModel):
    """Tells the UI whether to show the Workspace sidebar entry."""

    eligible: bool
    reason: str = ""


@router.get("/eligibility", response_model=EligibilityView)
async def get_eligibility(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> EligibilityView:
    """Cheap is-the-feature-on probe. Used by the frontend to decide
    whether to render the ``Workspace`` sidebar item.

    Same logic as :func:`_eligible_tenant` but returns a structured
    boolean instead of raising; the UI doesn't want a 403 just to
    decide visibility.
    """
    try:
        await _eligible_tenant(db, user)
        return EligibilityView(eligible=True)
    except HTTPException as e:
        return EligibilityView(eligible=False, reason=str(e.detail))


@router.get("/models", response_model=list[CodingModelView])
async def list_models(user: User = Depends(current_user)) -> list[CodingModelView]:
    """Coding models the Create-Workspace form offers.

    Returns the whole catalog; ``available`` marks the ones that are both
    enrolled (``settings.coding_models_enabled``) **and** backed by a coding
    upstream that answers. The UI renders the rest disabled (e.g. the ada-48
    models until the RTX 6000 Ada joins) rather than hiding them, so users
    see what's coming.

    The reachability half matters because ``coding_models_enabled`` is a
    static claim that a vLLM is serving, not a trigger to deploy one — so on
    its own it happily advertises a model whose stack was never deployed or
    lives on a rebuilt node.
    """
    settings = get_settings()
    serving = await coding_upstream_healthy(settings)
    enabled = set(settings.coding_models_enabled) if serving else set()
    return [
        CodingModelView(
            id=m.id,
            label=m.label,
            description=m.description,
            gpu_class=m.gpu_class,
            vram_gb=m.vram_gb,
            available=m.id in enabled,
            default=m.default,
        )
        for m in model_catalog.all_models()
    ]


@router.post("/sessions", response_model=WorkspaceView)
async def create_session(
    spec: WorkspaceSpec,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> WorkspaceView:
    """Get-or-create the caller's workspace, return the IDE URL.

    The browser flow:
      1) POST /api/v1/ide/sessions  → returns ``ide_url``.
      2) Browser opens ``ide_url`` in a new tab.
      3) ``/ide/{id}/...`` proxies to the workspace pod.
    """
    await _eligible_tenant(db, user)
    settings = get_settings()
    # Resolve + validate the model server-side; never trust the client.
    chosen = spec.model or settings.coding_model_default
    if not model_catalog.is_servable(chosen, settings.coding_models_enabled):
        raise HTTPException(
            status_code=422,
            detail=(
                f"coding model '{chosen}' is not available — pick one of: "
                f"{', '.join(settings.coding_models_enabled) or '(none configured)'}"
            ),
        )
    payload = spec.model_dump()
    payload["model"] = chosen
    resp = await _controller_call(
        method="POST",
        user=user,
        path=f"/workspaces/{user.id}",
        json=payload,
    )
    if resp.status_code >= 400:
        logger.warning("ide.create_failed", status=resp.status_code, body=resp.text[:200])
        raise HTTPException(status_code=502, detail="workspace controller error")
    data = resp.json()
    return _view(data)


@router.get("/sessions/me", response_model=WorkspaceView)
async def my_session(user: User = Depends(current_user)) -> WorkspaceView:
    resp = await _controller_call(method="GET", user=user, path=f"/workspaces/{user.id}")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="no workspace yet")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail="workspace controller error")
    return _view(resp.json())


@router.delete("/sessions/me")
async def destroy_session(user: User = Depends(current_user)) -> dict[str, str]:
    resp = await _controller_call(
        method="DELETE", user=user, path=f"/workspaces/{user.id}"
    )
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="no workspace")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail="workspace controller error")
    return {"status": "scheduled"}
