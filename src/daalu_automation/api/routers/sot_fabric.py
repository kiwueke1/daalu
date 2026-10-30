"""HTTP routes for loading a fabric plan — cabling, addressing and intent.

The device import (``/sot/devices/bulk-import``) turns a file into an
*inventory*: twelve boxes, each with a management address and nothing else.
These routes are the rest of the model. Same door, same two-phase shape —
``dry_run=true`` previews, ``dry_run=false`` writes — over three more
artifacts:

* ``cabling`` — the fabric ports and what is supposed to be plugged into what
* ``addressing`` — the prefixes and the addresses that sit on those ports
* ``intent`` — the roles and the role-scoped config contexts

They are separate uploads rather than one because they have a dependency
order: an address cannot land on an interface that does not exist yet. The
preview enforces that with a real error rather than a silent partial import,
so uploading them out of order tells you so instead of half-working.

Everything is idempotent. Re-uploading a plan reports what already matches as
``exists`` and writes only the difference — which is what makes this safe to
re-run against a rebuilt lab.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_admin, current_tenant_id
from daalu_automation.api.routers.sot_devices import ENV_QUERY, _nautobot_client
from daalu_automation.core.sot.fabric_apply import (
    apply_rows,
    count_by_op,
    load_index,
    resolve_rows,
    summarize,
)
from daalu_automation.core.sot.fabric_import import (
    ARTIFACT_ORDER,
    FabricParseError,
    parse_fabric_upload,
)
from daalu_automation.database import get_db
from daalu_automation.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sot/fabric", tags=["sot_fabric"])


# ── Schemas ──────────────────────────────────────────────────────────────


class FabricRowOut(BaseModel):
    """One object in the plan, in both preview and applied form.

    Mirrors ``BulkRowResult`` from the device import so the frontend can reuse
    the same row-rendering idiom; ``op``/``target``/``detail`` replace the
    fixed device columns because a fabric plan holds six kinds of object.
    """

    row: int
    op: str = Field(description="interface | cable | prefix | address | role | config_context")
    target: str = Field(description="what the object is, e.g. 'leaf1:swp1'")
    detail: str = Field("", description="the other half — a far end, a type, an address")
    status: str = Field(description="valid | exists | created | error")
    error: str | None = None
    object_id: str | None = None
    warnings: list[str] = Field(default_factory=list)


class FabricImportResult(BaseModel):
    dry_run: bool
    artifact: str = Field(description="cabling | addressing | intent")
    summary: dict[str, int] = Field(description="total / valid / created / skipped / errors")
    counts: dict[str, int] = Field(
        default_factory=dict,
        description="per-object-type counts — what formed, e.g. {'interface': 71, 'cable': 32}",
    )
    rows: list[FabricRowOut] = Field(default_factory=list)
    errors: list[str] = Field(
        default_factory=list, description="run-level failures, not row-level ones"
    )


ARTIFACT_QUERY = Query(
    None,
    description=(
        "Which artifact this file is expected to be — the tab it was dropped "
        "on. Omit to trust the file's own `kind:` line. When both are present "
        "and disagree, the upload is rejected rather than silently importing "
        "the wrong thing."
    ),
)


# ── Routes ───────────────────────────────────────────────────────────────


@router.get("/_artifacts")
async def artifacts(
    _tenant_id: uuid.UUID = Depends(current_tenant_id),
) -> dict[str, list[str]]:
    """The artifact names, in the order they must be applied.

    The frontend renders its tabs from this rather than hardcoding the list,
    so the dependency order has exactly one home.
    """
    return {"artifacts": list(ARTIFACT_ORDER)}


@router.post("/import", response_model=FabricImportResult)
async def import_fabric_plan(
    file: UploadFile = File(...),
    dry_run: bool = Query(True, description="preview only; no writes"),
    artifact: str | None = ARTIFACT_QUERY,
    env: str | None = ENV_QUERY,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(current_admin),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
) -> FabricImportResult:
    """Preview or apply one fabric-plan file against the tenant's Nautobot."""
    if artifact is not None and artifact not in ARTIFACT_ORDER:
        raise HTTPException(
            400, f"unknown artifact '{artifact}' — expected one of: {', '.join(ARTIFACT_ORDER)}"
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "uploaded file is empty")

    try:
        detected, rows = parse_fabric_upload(
            file.filename or "", file.content_type, raw, expected=artifact
        )
    except FabricParseError as exc:
        raise HTTPException(400, str(exc)) from exc

    async with await _nautobot_client(db, tenant_id, env) as client:
        index = await load_index(client, detected)
        results = resolve_rows(rows, index)

        if dry_run:
            return FabricImportResult(
                dry_run=True,
                artifact=detected,
                summary=summarize(results),
                counts=count_by_op(results),
                rows=[FabricRowOut(**r.__dict__) for r in results],
            )

        applied = await apply_rows(client, rows, results, index)

    logger.info(
        "sot_fabric.imported",
        extra={
            "tenant_id": str(tenant_id),
            "artifact": detected,
            "summary": summarize(applied),
        },
    )
    return FabricImportResult(
        dry_run=False,
        artifact=detected,
        summary=summarize(applied),
        counts=count_by_op(applied),
        rows=[FabricRowOut(**r.__dict__) for r in applied],
    )
