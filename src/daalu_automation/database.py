"""Async SQLAlchemy session + engine.

A single async engine is shared by the API and Celery workers (workers
open their own session per task via ``AsyncSessionLocal``). The sync
engine is exposed for alembic only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from daalu_automation.config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model."""


engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session."""
    async with AsyncSessionLocal() as session:
        yield session


async def create_tables() -> None:
    """Create every registered table.

    Called at API startup so local/dev installs Just Work without running
    alembic. Production should always go through ``alembic upgrade head``
    (the API deployment's init container does exactly that — see
    ``deploy/k8s/api/deployment.yaml``).
    """
    # Importing the models package registers every table with ``Base.metadata``.
    from daalu_automation import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
