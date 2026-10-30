"""FastAPI app for the workspace-controller.

Two surfaces:

* REST endpoints called by daalu-api when a user opens the IDE,
  configures their workspace, or destroys it.
* A background reconcile loop that brings K8s state in line with the
  ``workspaces`` table (handles pod crashes, rolling restarts of the
  controller itself, missed events).

Authentication: same service-token JWT as the inference-gateway.
The controller does not accept user cookies; it's an internal API.
"""

from __future__ import annotations

import asyncio
import uuid as _uuid
from datetime import datetime, timezone

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.core.crypto import decrypt_secret, encrypt_secret
from daalu_automation.core.service_tokens import (
    ServiceTokenClaims,
    ServiceTokenError,
    verify_service_token,
)
from daalu_automation.database import get_db
from daalu_automation.models.workspace import Workspace
from daalu_automation.workspace_controller import k8s

logger = structlog.get_logger(__name__)


class WorkspaceSpec(BaseModel):
    profile: str = Field(default="small")
    git_repo_url: str | None = None
    git_branch: str = Field(default="main")
    # Catalog id of the coding model. daalu-api validates it against the
    # enabled set before calling us; None falls back to the catalog
    # default at env-injection time.
    model: str | None = None
    # Personal-access-token for a *private* repo. Write-only: encrypted at
    # rest, never echoed back. Semantics:
    #   None → leave whatever is stored unchanged (the usual update case —
    #          the browser only ever sends a token on first creation).
    #   ""   → explicitly clear a stored token (drops the git Secret).
    #   else → (re-)encrypt and store; forces a re-provision.
    git_token: str | None = None


class WorkspaceView(BaseModel):
    id: str
    state: str
    profile: str
    model: str | None
    pod_name: str | None
    last_heartbeat_at: datetime | None
    git_repo_url: str | None
    git_branch: str
    # True when a PAT is stored for this workspace. The token itself is
    # never returned — this is purely so the UI can show a "private / authed"
    # indicator.
    git_authenticated: bool = False


def _authenticate(authorization: str | None) -> ServiceTokenClaims:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        return verify_service_token(authorization.split(None, 1)[1])
    except ServiceTokenError as e:
        logger.warning("workspace_controller.auth_failed", error=str(e))
        raise HTTPException(status_code=401, detail="invalid token")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Daalu Workspace Controller",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.on_event("startup")
    async def _startup() -> None:
        # Mirror the hub's signing-key Secret into daalu-workspaces so
        # the token-minter sidecar in every workspace pod can sign
        # service tokens. Best-effort: if the mirror fails the
        # reconcile loop still starts; new pods just won't mint
        # tokens until the operator fixes the Secret manually.
        try:
            import os as _os

            hub_ns = _os.environ.get("DAALU_HUB_NAMESPACE", "daalu-automation")
            await k8s.mirror_secret_into_workspaces(
                source_namespace=hub_ns,
                source_name="daalu-automation-secrets",
            )
        except Exception:  # noqa: BLE001
            logger.exception("workspace_controller.startup.secret_mirror_failed")
        app.state.reconcile_task = asyncio.create_task(_reconcile_loop())

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        app.state.reconcile_task.cancel()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/workspaces/{user_id}", response_model=WorkspaceView)
    async def upsert_workspace(
        user_id: str,
        spec: WorkspaceSpec,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> WorkspaceView:
        """Get-or-create the caller's workspace.

        Idempotent. If a row already exists, returns it; the
        reconciler will bring K8s state in line. Profile / git config
        updates take effect on the next pod rollout.
        """
        claims = _authenticate(authorization)
        if claims.user_id != user_id:
            # The caller's token names a different user than the URL;
            # daalu-api should never do this, so 403 to make the bug
            # obvious. Tenant boundary is enforced by token+row check.
            raise HTTPException(status_code=403, detail="user_id mismatch")
        return await _get_or_create_workspace(
            db=db,
            tenant_id=claims.tenant_id,
            user_id=user_id,
            spec=spec,
        )

    @app.get("/workspaces/{user_id}", response_model=WorkspaceView)
    async def get_workspace(
        user_id: str,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> WorkspaceView:
        claims = _authenticate(authorization)
        if claims.user_id != user_id:
            raise HTTPException(status_code=403, detail="user_id mismatch")
        ws = await _find(db, claims.tenant_id, user_id)
        if ws is None:
            raise HTTPException(status_code=404, detail="no workspace")
        return _to_view(ws)

    @app.delete("/workspaces/{user_id}")
    async def destroy_workspace(
        user_id: str,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> dict[str, str]:
        claims = _authenticate(authorization)
        if claims.user_id != user_id:
            raise HTTPException(status_code=403, detail="user_id mismatch")
        ws = await _find(db, claims.tenant_id, user_id)
        if ws is None:
            raise HTTPException(status_code=404, detail="no workspace")
        ws.state = "destroyed"
        ws.destroyed_at = datetime.now(tz=timezone.utc)
        await db.commit()
        # K8s teardown happens asynchronously in the reconcile loop;
        # we acknowledge immediately so the UI doesn't block.
        return {"status": "scheduled"}

    @app.post("/workspaces/{user_id}/heartbeat")
    async def heartbeat(
        user_id: str,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> dict[str, str]:
        claims = _authenticate(authorization)
        if claims.user_id != user_id:
            raise HTTPException(status_code=403, detail="user_id mismatch")
        ws = await _find(db, claims.tenant_id, user_id)
        if ws is None:
            raise HTTPException(status_code=404, detail="no workspace")
        ws.last_heartbeat_at = datetime.now(tz=timezone.utc)
        if ws.state == "paused":
            # Resume: scale back to 1 and the reconciler will move it
            # to 'active' once the pod is ready.
            ws.state = "provisioning"
        await db.commit()
        return {"status": "ok"}

    return app


# ── Helpers ──────────────────────────────────────────────────────────────


async def _find(db: AsyncSession, tenant_id: str, user_id: str) -> Workspace | None:
    stmt = select(Workspace).where(
        Workspace.tenant_id == tenant_id,
        Workspace.user_id == user_id,
        Workspace.state != "destroyed",
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _to_view(ws: Workspace) -> WorkspaceView:
    return WorkspaceView(
        id=str(ws.id),
        state=ws.state,
        profile=ws.profile,
        model=ws.model,
        pod_name=ws.pod_name,
        last_heartbeat_at=ws.last_heartbeat_at,
        git_repo_url=ws.git_repo_url,
        git_branch=ws.git_branch,
        git_authenticated=ws.git_token_encrypted is not None,
    )


async def _get_or_create_workspace(
    *,
    db: AsyncSession,
    tenant_id: str,
    user_id: str,
    spec: WorkspaceSpec,
) -> WorkspaceView:
    ws = await _find(db, tenant_id, user_id)
    if ws is None:
        ws = Workspace(
            id=_uuid.uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
            state="provisioning",
            profile=spec.profile,
            model=spec.model,
            git_repo_url=spec.git_repo_url,
            git_branch=spec.git_branch,
            git_token_encrypted=(
                # Only store a non-empty token; "" / None → no auth.
                encrypt_secret(spec.git_token) if spec.git_token else None
            ),
            pod_name=k8s.resource_names(str(_uuid.uuid4()))[0],
        )
        db.add(ws)
    else:
        # Update mutable settings; reconciler will roll if anything
        # affecting the pod spec changed. A model swap changes the
        # DAALU_MODEL env, so force a re-provision to roll the pod
        # (apply_workspace patches the Deployment, which only restarts the
        # pod when the spec actually differs — but the env won't change
        # until we re-run it, and the active state never re-applies).
        model_changed = spec.model is not None and spec.model != ws.model
        # Token edits also need a re-provision: the git Secret + pod's
        # credential mount only refresh when apply_workspace re-runs.
        # ``None`` leaves the stored token untouched; a non-None value
        # (re-)stores it ("" clears it) and rolls the pod.
        token_changed = spec.git_token is not None
        if token_changed:
            ws.git_token_encrypted = (
                encrypt_secret(spec.git_token) if spec.git_token else None
            )
        ws.profile = spec.profile
        if spec.model is not None:
            ws.model = spec.model
        ws.git_repo_url = spec.git_repo_url
        ws.git_branch = spec.git_branch
        if ws.state == "paused" or model_changed or token_changed:
            ws.state = "provisioning"
    await db.commit()
    await db.refresh(ws)
    return _to_view(ws)


# ── Reconcile loop ───────────────────────────────────────────────────────


async def _reconcile_loop() -> None:
    """Cooperative reconciler: every 15s, sweep rows and align K8s.

    Production note: the right shape long-term is to *watch* the
    workspaces table (e.g. Postgres LISTEN/NOTIFY) and the K8s
    Deployment list (kubernetes_asyncio watch) and react to change
    events. The polling loop here is the MVP path — see
    ``docs/design/coding-assistant.md`` §11 for the watcher TODO.
    """
    from daalu_automation.database import AsyncSessionLocal

    logger.info("workspace_controller.reconcile_loop_started")
    while True:
        try:
            async with AsyncSessionLocal() as db:
                stmt = select(Workspace).where(Workspace.state != "destroyed")
                rows = (await db.execute(stmt)).scalars().all()
                for ws in rows:
                    await _reconcile_one(db, ws)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — never let one bad row kill the loop
            logger.exception("workspace_controller.reconcile_iteration_failed")
        await asyncio.sleep(15)


_PAUSE_AFTER_SECONDS = 24 * 3600
_DESTROY_AFTER_SECONDS = 14 * 24 * 3600


async def _reconcile_one(db: AsyncSession, ws: Workspace) -> None:
    """Bring K8s state into agreement with one workspace row.

    State transitions:
      provisioning → (apply manifests) → wait for readyReplicas ≥ 1 → active
      active       → noop, unless heartbeat is stale (>24h) → paused
      paused       → (scale to 0); reaped to destroyed after 14d idle
      destroyed    → (delete manifests, leave row for audit)
    """
    if ws.state == "destroyed":
        return
    if ws.state == "provisioning":
        # Idempotent: apply_workspace creates-or-patches each call. The
        # work isn't wasted because server-side apply on identical specs
        # is a no-op against the K8s control plane.
        # Decrypt the PAT (if any) just-in-time so plaintext lives only for
        # the duration of this apply, never on the row. A bad ciphertext
        # (e.g. secret_key rotated) must not wedge the whole loop — fall back
        # to an unauthenticated clone and let the init container surface it.
        git_token: str | None = None
        if ws.git_token_encrypted:
            try:
                git_token = decrypt_secret(ws.git_token_encrypted)
            except ValueError:
                logger.warning("workspace.git_token_decrypt_failed", id=str(ws.id))
        await k8s.apply_workspace(
            workspace_id=str(ws.id),
            tenant_id=str(ws.tenant_id),
            user_id=str(ws.user_id),
            profile=ws.profile,
            model=ws.model,
            git_repo_url=ws.git_repo_url,
            git_branch=ws.git_branch,
            git_token=git_token,
        )
        # Only flip to active once the Deployment reports a ready
        # replica. Without this gate the proxy returns 502s the moment
        # the user clicks "Open IDE" and the UX is "did it break?"
        # until the pod finishes pulling the (1+ GiB) code-server image.
        if await k8s.deployment_ready(str(ws.id)):
            ws.state = "active"
            ws.last_heartbeat_at = datetime.now(tz=timezone.utc)
            await db.commit()
        return
    if ws.state == "active":
        if ws.last_heartbeat_at is not None:
            idle = datetime.now(tz=timezone.utc) - ws.last_heartbeat_at
            if idle.total_seconds() > _PAUSE_AFTER_SECONDS:
                ws.state = "paused"
                await db.commit()
                await k8s.scale_workspace(str(ws.id), replicas=0)
                logger.info(
                    "workspace.paused",
                    id=str(ws.id),
                    idle_hours=idle.total_seconds() / 3600,
                )
        return
    if ws.state == "paused":
        # Scale-to-zero is idempotent — re-asserting on every sweep
        # corrects state if anything outside the controller (an
        # operator's manual kubectl, an Argo sync) bumped replicas.
        await k8s.scale_workspace(str(ws.id), replicas=0)
        # Full destroy after 14 days of continued inactivity. The
        # heartbeat is only bumped by the IDE proxy; if the user hasn't
        # opened the workspace at all since pause, this fires.
        if ws.last_heartbeat_at is not None:
            idle = datetime.now(tz=timezone.utc) - ws.last_heartbeat_at
            if idle.total_seconds() > _DESTROY_AFTER_SECONDS:
                ws.state = "destroyed"
                ws.destroyed_at = datetime.now(tz=timezone.utc)
                await db.commit()
                await k8s.destroy_workspace(str(ws.id), release_pvc=True)
                logger.info(
                    "workspace.destroyed.idle_reaper",
                    id=str(ws.id),
                    idle_days=idle.total_seconds() / 86400,
                )
        return


app = create_app()
