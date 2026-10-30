"""Inbox endpoint for the /help feedback form.

POST is open to any authenticated user — every operator can submit. The
list endpoint is gated on ``is_superuser`` because feedback is a
platform-wide inbox, not a per-tenant one (the data is intentionally
visible across tenants to whoever runs the platform).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_superuser, current_user
from daalu_automation.database import get_db
from daalu_automation.models import Feedback, User

router = APIRouter(prefix="/feedback", tags=["feedback"])


class FeedbackCreate(BaseModel):
    message: str = Field(min_length=1, max_length=10_000)
    category: str = Field(default="general", max_length=32)
    page_url: str = Field(default="", max_length=512)


class FeedbackResponse(BaseModel):
    id: str
    category: str
    message: str
    page_url: str
    user_agent: str
    user_email: str | None
    created_at: datetime


@router.post("", response_model=FeedbackResponse, status_code=201)
async def submit_feedback(
    payload: FeedbackCreate,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    ua = request.headers.get("user-agent", "")[:512]
    fb = Feedback(
        user_id=user.id,
        tenant_id=user.tenant_id,
        category=payload.category.strip() or "general",
        message=payload.message,
        page_url=payload.page_url,
        user_agent=ua,
    )
    db.add(fb)
    await db.commit()
    await db.refresh(fb)
    return FeedbackResponse(
        id=str(fb.id),
        category=fb.category,
        message=fb.message,
        page_url=fb.page_url,
        user_agent=fb.user_agent,
        user_email=user.email,
        created_at=fb.created_at,
    )


@router.get("", response_model=list[FeedbackResponse])
async def list_feedback(
    _: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
    limit: int = 100,
):
    """Superuser-only — read every feedback row regardless of tenant.
    Newest first, paginated by ``limit`` (defaults to 100, no offset
    yet because volume is low)."""
    stmt = (
        select(Feedback, User)
        .join(User, Feedback.user_id == User.id, isouter=True)
        .order_by(Feedback.created_at.desc())
        .limit(min(max(limit, 1), 500))
    )
    rows = (await db.execute(stmt)).all()
    return [
        FeedbackResponse(
            id=str(fb.id),
            category=fb.category,
            message=fb.message,
            page_url=fb.page_url,
            user_agent=fb.user_agent,
            user_email=u.email if u else None,
            created_at=fb.created_at,
        )
        for fb, u in rows
    ]
