"""Runbooks as code — list, create/import, edit, publish, delete, match.

See docs/design/ai-ops-maturity.md §5 and §13 (``RunbookOut``).

``POST`` / ``PUT`` accept either structured fields or ``{markdown}`` (a whole
runbook file with frontmatter — what ``daalu runbooks import`` sends; a
``POST`` with markdown upserts by slug so re-importing is idempotent).
Publishing, and editing / deleting an already-published runbook, need a
tenant admin: published runbooks steer the agent.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_tenant_id, current_user
from daalu_automation.core.aiops import audit
from daalu_automation.core.aiops import runbooks as rbk
from daalu_automation.database import get_db
from daalu_automation.models import Alert, Runbook, User

router = APIRouter(prefix="/aiops", tags=["aiops"])


class RunbookMatch(BaseModel):
    alertname: list[str] = []
    labels: dict[str, str] = {}


class RunbookOut(BaseModel):
    id: str
    slug: str
    title: str
    body: str
    match: RunbookMatch
    allowed_actions: list[str]
    status: str
    source: str
    version: int
    from_investigation_id: str | None = None
    published_by: str = ""
    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class RunbookIn(BaseModel):
    """Either ``markdown`` alone, or the structured fields."""

    markdown: str | None = Field(default=None, max_length=200_000)
    title: str | None = Field(default=None, max_length=255)
    body: str | None = Field(default=None, max_length=200_000)
    match: dict[str, Any] | None = None
    allowed_actions: list[str] | None = None
    slug: str | None = Field(default=None, max_length=128)
    status: str | None = None


def _is_admin(user: User) -> bool:
    return bool(user.is_admin or user.is_superuser)


def _require_admin(user: User, what: str) -> None:
    if not _is_admin(user):
        raise HTTPException(403, f"only tenant admins can {what}")


def _out(rb: Runbook) -> dict[str, Any]:
    return rbk.to_out(rb)


def _bad(e: rbk.RunbookError) -> HTTPException:
    return HTTPException(422, {"message": "invalid runbook", "errors": e.errors})


def _validate_fields(req: RunbookIn, *, slug: str) -> rbk.ParsedRunbook:
    """Run structured input through the same validator as files."""
    data: dict[str, Any] = {
        "id": slug,
        "title": req.title,
        "match": req.match if req.match is not None else {},
        "allowed_actions": req.allowed_actions or [],
    }
    if req.status is not None:
        data["status"] = req.status
    try:
        return rbk.validate(data, req.body or "")
    except rbk.RunbookError as e:
        raise _bad(e) from e


async def _publish(db: AsyncSession, tenant_id: uuid.UUID, rb: Runbook, user: User) -> None:
    rbk.publish(rb, by=user.email)
    await db.flush()
    await audit.record(
        db, tenant_id, "runbook.published", actor=user.email, subject=rb.slug,
        data={"runbook_id": str(rb.id), "slug": rb.slug, "version": rb.version,
              "source": rb.source},
    )


async def _load(db: AsyncSession, tenant_id: uuid.UUID, id_or_slug: str) -> Runbook:
    rb = await rbk.get_runbook(db, tenant_id, id_or_slug)
    if rb is None:
        raise HTTPException(404, "runbook not found")
    return rb


@router.get("/runbooks", response_model=list[RunbookOut])
async def list_runbooks(
    status: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    if status is not None and status not in rbk.STATUSES:
        raise HTTPException(422, f"status must be one of {list(rbk.STATUSES)}")
    return [_out(rb) for rb in await rbk.list_runbooks(db, tenant_id, status=status)]


@router.get("/runbooks/match", response_model=list[RunbookOut])
async def match_runbooks(
    alert_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    try:
        aid = uuid.UUID(alert_id)
    except ValueError as e:
        raise HTTPException(404, "alert not found") from e
    alert = await db.get(Alert, aid)
    if alert is None or alert.tenant_id != tenant_id:
        raise HTTPException(404, "alert not found")
    return [_out(rb) for rb in await rbk.match_for_alert(db, tenant_id, alert)]


@router.post("/runbooks", response_model=RunbookOut, status_code=201)
async def create_runbook(
    req: RunbookIn,
    response: Response,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
):
    if req.markdown is not None:
        try:
            parsed = rbk.parse_markdown(req.markdown)
        except rbk.RunbookError as e:
            raise _bad(e) from e
        want_publish = (req.status or parsed.status) == "published"
        existing = await rbk.get_runbook(db, tenant_id, parsed.slug)
        if want_publish or (existing is not None and existing.status == "published"):
            _require_admin(user, "publish or change a published runbook")
        rb, created, _ = await rbk.upsert_from_markdown(db, tenant_id, req.markdown, source="imported")
        if not created:
            response.status_code = 200
    else:
        slug = (req.slug or rbk.slugify(req.title or "")).strip()
        parsed = _validate_fields(req, slug=slug)
        want_publish = parsed.status == "published"
        if want_publish:
            _require_admin(user, "publish runbooks")
        if await rbk.get_runbook(db, tenant_id, slug) is not None:
            raise HTTPException(409, f"a runbook with slug {slug!r} already exists")
        rb = Runbook(
            tenant_id=tenant_id, slug=slug, status="draft", source="manual", version=1,
            match={}, allowed_actions=[],
        )
        rbk.apply_fields(
            rb, title=parsed.title, body=parsed.body, match=parsed.match,
            allowed_actions=parsed.allowed_actions, is_new=True,
        )
        db.add(rb)
        await db.flush()
    if want_publish and rb.status != "published":
        await _publish(db, tenant_id, rb, user)
    await db.commit()
    await db.refresh(rb)
    return _out(rb)


@router.get("/runbooks/{runbook_id}", response_model=RunbookOut)
async def get_runbook(
    runbook_id: str,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    return _out(await _load(db, tenant_id, runbook_id))


@router.put("/runbooks/{runbook_id}", response_model=RunbookOut)
async def update_runbook(
    runbook_id: str,
    req: RunbookIn,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
):
    rb = await _load(db, tenant_id, runbook_id)
    if req.markdown is not None:
        try:
            parsed = rbk.parse_markdown(req.markdown)
        except rbk.RunbookError as e:
            raise _bad(e) from e
        status = req.status or parsed.status
    else:
        # Partial update: unspecified fields keep their stored values.
        merged = RunbookIn(
            title=req.title if req.title is not None else rb.title,
            body=req.body if req.body is not None else rb.body,
            match=req.match if req.match is not None else rb.match,
            allowed_actions=(
                req.allowed_actions if req.allowed_actions is not None else rb.allowed_actions
            ),
            status=req.status,
        )
        parsed = _validate_fields(merged, slug=req.slug or rb.slug)
        status = parsed.status
    if rb.status == "published" or status == "published":
        _require_admin(user, "publish or change a published runbook")

    if parsed.slug != rb.slug:
        other = await rbk.get_runbook(db, tenant_id, parsed.slug)
        if other is not None and other.id != rb.id:
            raise HTTPException(409, f"a runbook with slug {parsed.slug!r} already exists")
        rb.slug = parsed.slug
    changed = rbk.apply_fields(
        rb, title=parsed.title, body=parsed.body, match=parsed.match,
        allowed_actions=parsed.allowed_actions,
    )
    if status == "draft" and rb.status == "published":
        rb.status = "draft"  # unpublish
    await db.flush()
    if status == "published" and rb.status != "published":
        await _publish(db, tenant_id, rb, user)
    elif changed and rb.status == "published":
        # Editing a published runbook keeps it live; record who changed it.
        await _publish(db, tenant_id, rb, user)
    await db.commit()
    await db.refresh(rb)
    return _out(rb)


@router.post("/runbooks/{runbook_id}/publish", response_model=RunbookOut)
async def publish_runbook(
    runbook_id: str,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
):
    _require_admin(user, "publish runbooks")
    rb = await _load(db, tenant_id, runbook_id)
    await _publish(db, tenant_id, rb, user)
    await db.commit()
    await db.refresh(rb)
    return _out(rb)


@router.delete("/runbooks/{runbook_id}", status_code=204)
async def delete_runbook(
    runbook_id: str,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
):
    rb = await _load(db, tenant_id, runbook_id)
    if rb.status == "published":
        _require_admin(user, "delete a published runbook")
    await db.delete(rb)
    await db.commit()
    return Response(status_code=204)
