"""Reports — generic query surface for the Reports → Query tab.

The Query tab needs one shape that can run "show me events where module=infra
in the last hour", "show me alerts that are still open", or "show me change
proposals waiting >24h" without giving the client raw SQL. This router
exposes:

* ``GET /reports/query/schema`` — returns the whitelist of entities + their
  filterable fields + their displayable columns. The frontend uses this to
  build the structured query form.
* ``POST /reports/query`` — runs a structured query against one entity and
  returns rows + ordered columns. Tenant-scoped, equality-only filters for
  now (plus ``since_hours`` for time-windowed entities).

Future:
* ``POST /reports/query/translate`` — Phase 3, natural language → structured
  query via copilot tool-use. Once that lands the frontend's "Ask" mode
  hits ``/translate`` then re-runs ``/query`` with the returned definition.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_tenant_id, current_user
from daalu_automation.core.llm import LLMUnavailable, complete_json
from daalu_automation.database import get_db
from daalu_automation.models import (
    Alert,
    ChangeProposal,
    Dashboard,
    Event,
    Incident,
    Integration,
    ReportSchedule,
    SavedReport,
    User,
)

router = APIRouter(prefix="/reports", tags=["reports"])


# ── Entity registry ─────────────────────────────────────────────────────
#
# Each entry maps an entity slug to:
#   model           — SQLAlchemy mapped class (must have tenant_id)
#   columns         — ordered list of (key, label) for the table view
#   filter_fields   — whitelist of column names the client may filter on
#   time_field      — optional column used for ``since_hours`` filtering and
#                     descending sort
#   sort_field      — column used for default ordering when no time_field
#
# Anything outside this registry is rejected with 400. Raw SQL never reaches
# the driver: filters are applied via SQLAlchemy column comparisons.

class _EntitySpec:
    def __init__(
        self,
        *,
        model: type,
        columns: list[tuple[str, str]],
        filter_fields: list[str],
        time_field: str | None = None,
        sort_field: str | None = None,
    ) -> None:
        self.model = model
        self.columns = columns
        self.filter_fields = filter_fields
        self.time_field = time_field
        self.sort_field = sort_field or time_field or "created_at"


ENTITIES: dict[str, _EntitySpec] = {
    "events": _EntitySpec(
        model=Event,
        columns=[
            ("occurred_at", "When"),
            ("module", "Module"),
            ("source", "Source"),
            ("type", "Type"),
            ("severity", "Severity"),
            ("summary", "Summary"),
        ],
        filter_fields=["module", "source", "type", "severity"],
        time_field="occurred_at",
    ),
    "alerts": _EntitySpec(
        model=Alert,
        columns=[
            ("created_at", "Opened"),
            ("module", "Module"),
            ("severity", "Severity"),
            ("status", "Status"),
            ("title", "Title"),
            ("occurrence_count", "Hits"),
        ],
        filter_fields=["module", "severity", "status"],
        time_field="created_at",
    ),
    "incidents": _EntitySpec(
        model=Incident,
        columns=[
            ("started_at", "Started"),
            ("severity", "Severity"),
            ("status", "Status"),
            ("title", "Title"),
            ("resolved_at", "Resolved"),
        ],
        filter_fields=["severity", "status"],
        time_field="started_at",
    ),
    "change_proposals": _EntitySpec(
        model=ChangeProposal,
        columns=[
            ("created_at", "Created"),
            ("kind", "Kind"),
            ("status", "Status"),
            ("device_id", "Device"),
            ("approved_at", "Approved"),
            ("executed_at", "Executed"),
        ],
        filter_fields=["kind", "status", "device_id"],
        time_field="created_at",
    ),
    "integrations": _EntitySpec(
        model=Integration,
        columns=[
            ("created_at", "Added"),
            ("provider", "Provider"),
            ("module", "Module"),
            ("name", "Name"),
            ("status", "Status"),
        ],
        # ``config`` is excluded from columns + filters on purpose — it may
        # carry credentials and should only flow through the integrations
        # router (which redacts secrets).
        filter_fields=["provider", "module", "status"],
        time_field="created_at",
    ),
}


# ── Request / response shapes ───────────────────────────────────────────


class QueryRequest(BaseModel):
    entity: str
    filters: dict[str, Any] = Field(default_factory=dict)
    since_hours: int | None = Field(default=None, ge=1, le=24 * 30)
    limit: int = Field(default=100, ge=1, le=500)
    display: Literal["table", "count"] = "table"


class QueryColumn(BaseModel):
    key: str
    label: str


class QueryResponse(BaseModel):
    entity: str
    display: str
    columns: list[QueryColumn]
    rows: list[dict[str, Any]]
    total: int


class EntityDescriptor(BaseModel):
    name: str
    columns: list[QueryColumn]
    filter_fields: list[str]
    time_field: str | None


class SchemaResponse(BaseModel):
    entities: list[EntityDescriptor]


# ── Helpers ─────────────────────────────────────────────────────────────


def _serialize_row(spec: _EntitySpec, row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"id": str(row.id)}
    for key, _label in spec.columns:
        val = getattr(row, key, None)
        if isinstance(val, datetime):
            out[key] = val.isoformat()
        elif hasattr(val, "value"):  # enum
            out[key] = val.value
        else:
            out[key] = val
    return out


# ── Routes ──────────────────────────────────────────────────────────────


@router.get("/query/schema", response_model=SchemaResponse)
async def query_schema() -> SchemaResponse:
    """Return the entity whitelist + their displayable columns + filter fields.

    The Query tab calls this once on mount to drive its From/Where pickers.
    """
    return SchemaResponse(
        entities=[
            EntityDescriptor(
                name=name,
                columns=[QueryColumn(key=k, label=label) for k, label in spec.columns],
                filter_fields=spec.filter_fields,
                time_field=spec.time_field,
            )
            for name, spec in ENTITIES.items()
        ]
    )


async def _execute_query(
    req: QueryRequest, db: AsyncSession, tenant_id
) -> QueryResponse:
    """Shared executor used by /query and /query/export.

    Keeping the entity-whitelisting + filter-validation logic in one
    function means the export path can never bypass it — there's no
    second SQL builder to drift from this one.
    """
    spec = ENTITIES.get(req.entity)
    if spec is None:
        raise HTTPException(400, f"unknown entity: {req.entity}")

    model = spec.model
    stmt = select(model).where(model.tenant_id == tenant_id)

    for key, value in (req.filters or {}).items():
        if value in (None, "", []):
            continue
        if key not in spec.filter_fields:
            raise HTTPException(
                400,
                f"unknown filter for {req.entity!r}: {key!r}. "
                f"allowed: {', '.join(spec.filter_fields)}",
            )
        column = getattr(model, key)
        stmt = stmt.where(column == value)

    if req.since_hours is not None and spec.time_field is not None:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=req.since_hours)
        stmt = stmt.where(getattr(model, spec.time_field) >= cutoff)

    stmt = stmt.order_by(desc(getattr(model, spec.sort_field))).limit(req.limit)
    rows = (await db.execute(stmt)).scalars().all()

    if req.display == "count":
        return QueryResponse(
            entity=req.entity,
            display="count",
            columns=[QueryColumn(key="count", label="Count")],
            rows=[{"count": len(rows)}],
            total=len(rows),
        )

    return QueryResponse(
        entity=req.entity,
        display="table",
        columns=[QueryColumn(key=k, label=label) for k, label in spec.columns],
        rows=[_serialize_row(spec, r) for r in rows],
        total=len(rows),
    )


@router.post("/query", response_model=QueryResponse)
async def run_query(
    req: QueryRequest,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
) -> QueryResponse:
    return await _execute_query(req, db, tenant_id)


@router.post("/query/export")
async def export_query(
    req: QueryRequest,
    format: Literal["csv", "json"] = Query(default="csv"),
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
) -> StreamingResponse:
    """Stream the query result as CSV or JSON.

    Respects the same entity whitelist and tenant scoping as /query — it
    just reformats the response body. PDF export is on the roadmap.
    """
    result = await _execute_query(req, db, tenant_id)

    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = f"{result.entity}-{stamp}"

    if format == "json":
        body = json.dumps(
            {
                "entity": result.entity,
                "display": result.display,
                "columns": [c.model_dump() for c in result.columns],
                "rows": result.rows,
                "total": result.total,
            },
            indent=2,
        ).encode("utf-8")
        return StreamingResponse(
            iter([body]),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{base}.json"'},
        )

    # CSV — server-side render, one header row + N data rows.
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([c.label for c in result.columns])
    for row in result.rows:
        writer.writerow([_csv_cell(row.get(c.key)) for c in result.columns])
    body = buf.getvalue().encode("utf-8")
    return StreamingResponse(
        iter([body]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{base}.csv"'},
    )


def _csv_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"))
    return str(v)


# ── Natural-language → structured query ─────────────────────────────────


class TranslateRequest(BaseModel):
    question: str
    entity_hint: str | None = None


class TranslateResponse(BaseModel):
    query: QueryRequest
    rationale: str = ""


_TRANSLATE_SYSTEM = """\
You translate operational questions into a structured Daalu query.

Available entities and their filterable fields:
{entity_catalog}

Output **only** a JSON object with this shape:
{{
  "entity": "<one of the entities above>",
  "filters": {{"<field>": "<value>", ...}},
  "since_hours": <integer 1-720 or null>,
  "limit": <integer 1-500>,
  "display": "table" | "count",
  "rationale": "<one short sentence explaining the choice>"
}}

Rules:
- Use only the entities and filter fields listed. If the user asks for
  something not represented, pick the closest entity and leave filters
  empty.
- "today" / "in the last day" → since_hours: 24. "this week" → 168.
  "this hour" → 1. If the user does not mention time, leave since_hours
  null.
- Severity values: info, warning, critical.
- Status values for alerts: open, acknowledged, resolved, suppressed.
- Status values for incidents: open, investigating, mitigated, resolved.
- "how many" / "count" → display: "count". Otherwise "table".
- Default limit 100.
"""


def _entity_catalog_for_prompt() -> str:
    lines: list[str] = []
    for name, spec in ENTITIES.items():
        fields = ", ".join(spec.filter_fields) or "(none)"
        time = f" (time field: {spec.time_field})" if spec.time_field else ""
        lines.append(f"- {name}{time}: filter on {fields}")
    return "\n".join(lines)


@router.post("/query/translate", response_model=TranslateResponse)
async def translate_query(
    req: TranslateRequest,
    tenant_id=Depends(current_tenant_id),  # noqa: ARG001 — tenant gate
) -> TranslateResponse:
    """Translate a natural-language operational question into a structured
    QueryRequest. Returns the request shape the frontend then POSTs to
    /reports/query.

    Uses the same LLM cascade as the rest of the platform via
    :func:`daalu_automation.core.llm.complete_json`. If no LLM is wired
    up, returns a graceful 503 so the frontend can fall back to the
    structured builder.
    """
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(400, "question is required")

    system = _TRANSLATE_SYSTEM.format(entity_catalog=_entity_catalog_for_prompt())
    user = (
        f"Question: {question}"
        + (f"\nUser-suggested entity: {req.entity_hint}" if req.entity_hint else "")
    )

    try:
        payload = await complete_json(system=system, user=user, max_tokens=256)
    except LLMUnavailable as e:
        raise HTTPException(
            503,
            "AI translation unavailable. Configure an LLM provider or use the "
            "structured builder.",
        ) from e
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(502, f"LLM returned invalid JSON: {e}") from e

    rationale = str(payload.pop("rationale", ""))
    try:
        query = QueryRequest(**payload)
    except ValidationError as e:
        raise HTTPException(
            502,
            f"LLM produced an unusable query: {e.errors(include_url=False)[:3]}",
        ) from e

    if query.entity not in ENTITIES:
        raise HTTPException(502, f"LLM picked an unknown entity: {query.entity}")

    return TranslateResponse(query=query, rationale=rationale)


# ── Saved reports ───────────────────────────────────────────────────────


class SavedReportOut(BaseModel):
    id: str
    name: str
    definition: dict[str, Any]
    owner_user_id: str | None
    pinned: bool
    created_at: datetime
    updated_at: datetime


class SavedReportCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    definition: QueryRequest
    pinned: bool = False


class SavedReportUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    definition: QueryRequest | None = None
    pinned: bool | None = None


def _saved_to_out(row: SavedReport) -> SavedReportOut:
    return SavedReportOut(
        id=str(row.id),
        name=row.name,
        definition=row.definition,
        owner_user_id=str(row.owner_user_id) if row.owner_user_id else None,
        pinned=row.pinned,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/saved", response_model=list[SavedReportOut])
async def list_saved_reports(
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
) -> list[SavedReportOut]:
    stmt = (
        select(SavedReport)
        .where(SavedReport.tenant_id == tenant_id)
        .order_by(desc(SavedReport.pinned), desc(SavedReport.updated_at))
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [_saved_to_out(r) for r in rows]


@router.post("/saved", response_model=SavedReportOut, status_code=201)
async def create_saved_report(
    payload: SavedReportCreate,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
) -> SavedReportOut:
    # Non-admins can author and update their own, but cannot pin.
    pinned = bool(payload.pinned and user.is_admin)
    if payload.definition.entity not in ENTITIES:
        raise HTTPException(400, f"unknown entity: {payload.definition.entity}")
    row = SavedReport(
        tenant_id=tenant_id,
        name=payload.name,
        definition=payload.definition.model_dump(mode="json"),
        owner_user_id=user.id,
        pinned=pinned,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _saved_to_out(row)


async def _get_saved(
    db: AsyncSession, report_id: uuid.UUID, tenant_id
) -> SavedReport:
    row = (
        await db.execute(
            select(SavedReport).where(
                SavedReport.id == report_id, SavedReport.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"saved report {report_id} not found")
    return row


@router.get("/saved/{report_id}", response_model=SavedReportOut)
async def get_saved_report(
    report_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
) -> SavedReportOut:
    return _saved_to_out(await _get_saved(db, report_id, tenant_id))


@router.patch("/saved/{report_id}", response_model=SavedReportOut)
async def update_saved_report(
    report_id: uuid.UUID,
    payload: SavedReportUpdate,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
) -> SavedReportOut:
    row = await _get_saved(db, report_id, tenant_id)
    # Authoring + updating own report: ok. Updating someone else's: admin
    # only. Pinning: admin only regardless of authorship.
    is_owner = row.owner_user_id == user.id
    if not is_owner and not user.is_admin:
        raise HTTPException(403, "not your report")
    if payload.name is not None:
        row.name = payload.name
    if payload.definition is not None:
        if payload.definition.entity not in ENTITIES:
            raise HTTPException(400, f"unknown entity: {payload.definition.entity}")
        row.definition = payload.definition.model_dump(mode="json")
    if payload.pinned is not None:
        if not user.is_admin:
            raise HTTPException(403, "only admins can pin reports")
        row.pinned = payload.pinned
    await db.commit()
    await db.refresh(row)
    return _saved_to_out(row)


@router.delete("/saved/{report_id}", status_code=204)
async def delete_saved_report(
    report_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
) -> None:
    row = await _get_saved(db, report_id, tenant_id)
    is_owner = row.owner_user_id == user.id
    if not is_owner and not user.is_admin:
        raise HTTPException(403, "not your report")
    await db.delete(row)
    await db.commit()


# ── Dashboards ──────────────────────────────────────────────────────────


class DashboardTile(BaseModel):
    saved_report_id: uuid.UUID
    render: Literal["table", "number", "line", "bar", "pie"] = "table"
    title: str | None = None
    x: int = 0
    y: int = 0
    w: int = 4
    h: int = 3


class DashboardOut(BaseModel):
    id: str
    name: str
    tiles: list[DashboardTile]
    owner_user_id: str | None
    home_pinned: bool
    created_at: datetime
    updated_at: datetime


class DashboardCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    tiles: list[DashboardTile] = Field(default_factory=list)
    home_pinned: bool = False


class DashboardUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    tiles: list[DashboardTile] | None = None
    home_pinned: bool | None = None


def _dashboard_to_out(row: Dashboard) -> DashboardOut:
    return DashboardOut(
        id=str(row.id),
        name=row.name,
        tiles=[DashboardTile(**t) for t in (row.tiles or [])],
        owner_user_id=str(row.owner_user_id) if row.owner_user_id else None,
        home_pinned=row.home_pinned,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/dashboards", response_model=list[DashboardOut])
async def list_dashboards(
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
) -> list[DashboardOut]:
    rows = (
        await db.execute(
            select(Dashboard)
            .where(Dashboard.tenant_id == tenant_id)
            .order_by(desc(Dashboard.home_pinned), desc(Dashboard.updated_at))
        )
    ).scalars().all()
    return [_dashboard_to_out(r) for r in rows]


@router.post("/dashboards", response_model=DashboardOut, status_code=201)
async def create_dashboard(
    payload: DashboardCreate,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
) -> DashboardOut:
    home_pinned = bool(payload.home_pinned and user.is_admin)
    row = Dashboard(
        tenant_id=tenant_id,
        name=payload.name,
        tiles=[t.model_dump(mode="json") for t in payload.tiles],
        owner_user_id=user.id,
        home_pinned=home_pinned,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _dashboard_to_out(row)


async def _get_dashboard(
    db: AsyncSession, dashboard_id: uuid.UUID, tenant_id
) -> Dashboard:
    row = (
        await db.execute(
            select(Dashboard).where(
                Dashboard.id == dashboard_id, Dashboard.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"dashboard {dashboard_id} not found")
    return row


@router.get("/dashboards/{dashboard_id}", response_model=DashboardOut)
async def get_dashboard(
    dashboard_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
) -> DashboardOut:
    return _dashboard_to_out(await _get_dashboard(db, dashboard_id, tenant_id))


@router.patch("/dashboards/{dashboard_id}", response_model=DashboardOut)
async def update_dashboard(
    dashboard_id: uuid.UUID,
    payload: DashboardUpdate,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
) -> DashboardOut:
    row = await _get_dashboard(db, dashboard_id, tenant_id)
    is_owner = row.owner_user_id == user.id
    if not is_owner and not user.is_admin:
        raise HTTPException(403, "not your dashboard")
    if payload.name is not None:
        row.name = payload.name
    if payload.tiles is not None:
        row.tiles = [t.model_dump(mode="json") for t in payload.tiles]
    if payload.home_pinned is not None:
        if not user.is_admin:
            raise HTTPException(403, "only admins can pin a dashboard to Home")
        row.home_pinned = payload.home_pinned
    await db.commit()
    await db.refresh(row)
    return _dashboard_to_out(row)


@router.delete("/dashboards/{dashboard_id}", status_code=204)
async def delete_dashboard(
    dashboard_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
) -> None:
    row = await _get_dashboard(db, dashboard_id, tenant_id)
    is_owner = row.owner_user_id == user.id
    if not is_owner and not user.is_admin:
        raise HTTPException(403, "not your dashboard")
    await db.delete(row)
    await db.commit()


# ── Schedules ───────────────────────────────────────────────────────────


class ScheduleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    saved_report_id: uuid.UUID
    cron: str = Field(..., min_length=5, max_length=64)
    destination: Literal["slack", "email"]
    recipient: str = Field(default="", max_length=255)
    fmt: Literal["markdown", "csv"] = Field(default="markdown", alias="format")
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    name: str | None = None
    cron: str | None = None
    destination: Literal["slack", "email"] | None = None
    recipient: str | None = None
    fmt: Literal["markdown", "csv"] | None = Field(default=None, alias="format")
    enabled: bool | None = None


class ScheduleOut(BaseModel):
    id: str
    name: str
    saved_report_id: str
    cron: str
    destination: str
    recipient: str
    fmt: str = Field(serialization_alias="format")
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_status: str
    last_error: str | None
    created_at: datetime
    updated_at: datetime


def _schedule_to_out(row: ReportSchedule) -> ScheduleOut:
    return ScheduleOut(
        id=str(row.id),
        name=row.name,
        saved_report_id=str(row.saved_report_id),
        cron=row.cron,
        destination=row.destination,
        recipient=row.recipient,
        fmt=row.fmt,
        enabled=row.enabled,
        next_run_at=row.next_run_at,
        last_run_at=row.last_run_at,
        last_status=row.last_status,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _validate_cron(expr: str) -> datetime:
    """Parse the cron and return the next fire time. Raises 400 on bad input."""
    from daalu_automation.workers.report_dispatch import compute_next_run

    try:
        return compute_next_run(expr)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"invalid cron expression: {e}") from e


@router.get("/schedules", response_model=list[ScheduleOut])
async def list_schedules(
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
) -> list[ScheduleOut]:
    rows = (
        await db.execute(
            select(ReportSchedule)
            .where(ReportSchedule.tenant_id == tenant_id)
            .order_by(desc(ReportSchedule.created_at))
        )
    ).scalars().all()
    return [_schedule_to_out(r) for r in rows]


@router.post("/schedules", response_model=ScheduleOut, status_code=201)
async def create_schedule(
    payload: ScheduleCreate,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
) -> ScheduleOut:
    # The saved report has to live in our tenant.
    saved = (
        await db.execute(
            select(SavedReport).where(
                SavedReport.id == payload.saved_report_id,
                SavedReport.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if saved is None:
        raise HTTPException(404, "saved report not found")
    if payload.destination == "email" and not payload.recipient:
        raise HTTPException(400, "email schedules require a recipient")
    next_run = _validate_cron(payload.cron)
    row = ReportSchedule(
        tenant_id=tenant_id,
        name=payload.name,
        saved_report_id=payload.saved_report_id,
        cron=payload.cron,
        destination=payload.destination,
        recipient=payload.recipient,
        fmt=payload.fmt,
        enabled=payload.enabled,
        next_run_at=next_run,
        last_status="",
        created_by_user_id=user.id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _schedule_to_out(row)


async def _get_schedule(
    db: AsyncSession, schedule_id: uuid.UUID, tenant_id
) -> ReportSchedule:
    row = (
        await db.execute(
            select(ReportSchedule).where(
                ReportSchedule.id == schedule_id,
                ReportSchedule.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"schedule {schedule_id} not found")
    return row


@router.patch("/schedules/{schedule_id}", response_model=ScheduleOut)
async def update_schedule(
    schedule_id: uuid.UUID,
    payload: ScheduleUpdate,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
) -> ScheduleOut:
    row = await _get_schedule(db, schedule_id, tenant_id)
    is_owner = row.created_by_user_id == user.id
    if not is_owner and not user.is_admin:
        raise HTTPException(403, "not your schedule")
    if payload.name is not None:
        row.name = payload.name
    if payload.cron is not None:
        row.next_run_at = _validate_cron(payload.cron)
        row.cron = payload.cron
    if payload.destination is not None:
        row.destination = payload.destination
    if payload.recipient is not None:
        row.recipient = payload.recipient
    if payload.fmt is not None:
        row.fmt = payload.fmt
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if row.destination == "email" and not row.recipient:
        raise HTTPException(400, "email schedules require a recipient")
    await db.commit()
    await db.refresh(row)
    return _schedule_to_out(row)


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(
    schedule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
) -> None:
    row = await _get_schedule(db, schedule_id, tenant_id)
    is_owner = row.created_by_user_id == user.id
    if not is_owner and not user.is_admin:
        raise HTTPException(403, "not your schedule")
    await db.delete(row)
    await db.commit()
