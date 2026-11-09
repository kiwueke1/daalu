# 62. Adding a new domain module

A "domain" in Daalu is a class of operational work — Infra,
Network, Support, Finance, Healthcare. Each lives as a module
under `src/daalu_automation/modules/`. This chapter walks
through adding a new one.

The running example: adding a `support` module for
customer-support workflows.

---

## 1. Scaffold the directory

```
src/daalu_automation/modules/support/
├── __init__.py
├── models.py
├── service.py
├── router.py
├── tasks.py
├── adapters/
└── tests/
```

`__init__.py` is empty initially; you'll re-export the public
interface here.

---

## 2. Define the models

`models.py` (or its equivalent under
`daalu_automation/models/support.py`):

```python
from sqlalchemy.orm import Mapped, mapped_column
from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
)

class SupportTicket(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "support_tickets"
    title: Mapped[str]
    body: Mapped[str]
    status: Mapped[str]   # 'open', 'in_progress', 'resolved'
    priority: Mapped[str] # 'low', 'medium', 'high'
    assignee_id: Mapped[UUID | None]
```

Register the import in `models/__init__.py`:

```python
from daalu_automation.models.support import SupportTicket
__all__ = [..., "SupportTicket"]
```

---

## 3. Write the migration

```bash
alembic revision --autogenerate -m "008-support-module"
```

Review the generated SQL. Apply locally:

```bash
daalu db upgrade
```

---

## 4. Define the service layer

`service.py`:

```python
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from daalu_automation.models import SupportTicket

async def list_tickets(db: AsyncSession, tenant_id: UUID, filters: TicketFilters) -> list[SupportTicket]:
    stmt = select(SupportTicket).where(SupportTicket.tenant_id == tenant_id)
    if filters.status:
        stmt = stmt.where(SupportTicket.status == filters.status)
    return (await db.execute(stmt)).scalars().all()

async def create_ticket(db: AsyncSession, *, tenant_id: UUID, title: str, body: str, priority: str) -> SupportTicket:
    ticket = SupportTicket(
        tenant_id=tenant_id,
        title=title,
        body=body,
        status="open",
        priority=priority,
    )
    db.add(ticket)
    await db.commit()
    return ticket
```

The service layer is where domain logic lives. The router
calls into it; tests target it directly.

---

## 5. Write the router

`router.py`:

```python
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from daalu_automation.api.deps import current_user, get_db
from daalu_automation.models import User
from sqlalchemy.ext.asyncio import AsyncSession
from . import service

router = APIRouter(prefix="/support", tags=["support"])

class TicketCreate(BaseModel):
    title: str
    body: str
    priority: str = "medium"

class TicketResponse(BaseModel):
    id: str
    title: str
    body: str
    status: str
    priority: str

    class Config:
        from_attributes = True

@router.get("/tickets", response_model=list[TicketResponse])
async def list_tickets_endpoint(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    tickets = await service.list_tickets(db, user.tenant_id, TicketFilters())
    return tickets

@router.post("/tickets", response_model=TicketResponse)
async def create_ticket_endpoint(
    payload: TicketCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    return await service.create_ticket(db, tenant_id=user.tenant_id, **payload.model_dump())
```

Mount it in `api/main.py`:

```python
from daalu_automation.api.routers import support
...
app.include_router(support.router, prefix="/api/v1")
```

(Wait — actually the convention is for the router file to
live under `api/routers/` so it's grouped with the other
routes. The module's *adapter* code lives under
`modules/support/`. The router can import from the module.)

---

## 6. Write Celery tasks (if any)

`tasks.py`:

```python
from daalu_automation.workers.celery_app import celery_app

@celery_app.task(name="daalu.support.escalate")
def escalate_ticket(ticket_id: str):
    ...
```

Don't forget to add to the task routing if it should land in
a specific queue (executor, agents, etc.).

---

## 7. Add tests

`tests/integration/test_support_tickets.py`:

- Test the API endpoints (list, create, update).
- Test tenant isolation.
- Test the service layer functions.

---

## 8. Document

- Update the user guide's chapter list if the module is
  user-facing.
- Add to the engineer book's Part II Chapter 8 if it
  changes the module catalog.

---

## 9. Submit a PR

The PR should include:

- New module code.
- Migration.
- Tests.
- Documentation updates.
- Sample data for seed-demo if relevant.

A reviewer will check: tenant isolation tests pass,
migration has a downgrade, model has the right mixins, the
API surface is consistent with existing routers.

---

## What's next

Chapter 63 — adding a new integration adapter.
