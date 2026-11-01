"""Autonomy policy as code — read, save (versioned), simulate, kill switch,
trust table. See docs/design/ai-ops-maturity.md §3."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_tenant_id, current_user
from daalu_automation.core.aiops import audit
from daalu_automation.core.aiops import policy as pol
from daalu_automation.database import get_db
from daalu_automation.models import AutonomyPolicy, User

router = APIRouter(prefix="/aiops", tags=["aiops"])


def _require_admin(user: User) -> None:
    if not (user.is_admin or user.is_superuser):
        raise HTTPException(403, "only tenant admins can change the autonomy policy")


class PolicyOut(BaseModel):
    yaml: str
    version: int  # 0 = built-in default (never saved)
    updated_at: datetime | None = None
    note: str = ""
    parsed: dict[str, Any]
    kill_switch: bool
    kill_switch_by: str = ""
    kill_switch_at: datetime | None = None
    breaker: dict[str, str] = {}
    json_schema: dict[str, Any] | None = None


class PolicyIn(BaseModel):
    yaml: str = Field(max_length=100_000)
    note: str = Field(default="", max_length=512)


class SimulateIn(BaseModel):
    tool: str
    input: dict[str, Any] = {}
    cluster: str = ""
    severity: str = ""
    external: bool = False
    yaml: str | None = Field(
        default=None, description="Simulate against this draft instead of the saved policy."
    )


class KillSwitchIn(BaseModel):
    enabled: bool


async def _policy_out(db: AsyncSession, tenant_id, *, with_schema: bool = False) -> PolicyOut:
    row = await pol.load_policy_row(db, tenant_id)
    text = row.yaml if row else pol.DEFAULT_POLICY_YAML
    doc = await pol.load_policy(db, tenant_id)
    st = await pol.get_state(db, tenant_id)
    return PolicyOut(
        yaml=text,
        version=row.version if row else 0,
        updated_at=row.updated_at if row else None,
        note=row.note if row else "",
        parsed=doc.model_dump(),
        kill_switch=st.kill_switch,
        kill_switch_by=st.kill_switch_by,
        kill_switch_at=st.kill_switch_at,
        breaker=dict(st.breaker or {}),
        json_schema=pol.policy_json_schema() if with_schema else None,
    )


@router.get("/policy", response_model=PolicyOut)
async def get_policy(
    schema: bool = False,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    out = await _policy_out(db, tenant_id, with_schema=schema)
    await db.commit()  # get_state may have created the state row
    return out


@router.put("/policy", response_model=PolicyOut)
async def put_policy(
    body: PolicyIn,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
):
    _require_admin(user)
    try:
        row = await pol.save_policy(db, tenant_id, body.yaml, user_id=user.id, note=body.note)
    except pol.PolicyError as e:
        raise HTTPException(422, str(e)) from e
    await audit.record(
        db, tenant_id, "policy.updated", actor=user.email, subject=f"v{row.version}",
        data={"version": row.version, "note": body.note, "yaml": body.yaml},
    )
    await db.commit()
    return await _policy_out(db, tenant_id)


@router.get("/policy/history")
async def policy_history(
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(AutonomyPolicy, User.email)
            .outerjoin(User, User.id == AutonomyPolicy.updated_by)
            .where(AutonomyPolicy.tenant_id == tenant_id)
            .order_by(AutonomyPolicy.version.desc())
            .limit(100)
        )
    ).all()
    return [
        {
            "version": r.version, "yaml": r.yaml, "note": r.note,
            "updated_by": email or "", "updated_at": r.updated_at,
        }
        for r, email in rows
    ]


@router.post("/policy/simulate")
async def simulate(
    body: SimulateIn,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
) -> dict[str, Any]:
    """What would the policy decide for this write, right now?"""
    if body.yaml is not None:
        try:
            doc = pol.parse_policy(body.yaml)
        except pol.PolicyError as e:
            raise HTTPException(422, str(e)) from e
    else:
        doc = await pol.load_policy(db, tenant_id)
    req = pol.write_request_for(
        body.tool, body.input, cluster=body.cluster, severity=body.severity,
        external=body.external,
    )
    ctx = await pol.build_context(db, tenant_id, doc, req)
    decision = pol.decide(doc, req, ctx)
    await db.commit()
    return {
        **decision.as_dict(),
        "context": {
            "kill_switch": ctx.kill_switch,
            "auto_actions_last_hour": ctx.auto_actions_last_hour,
            "trust": {
                k: {"successes": v.successes, "failures": v.failures,
                    "failure_rate": v.failure_rate}
                for k, v in ctx.trust.items()
            },
        },
    }


@router.post("/policy/kill-switch", response_model=PolicyOut)
async def kill_switch(
    body: KillSwitchIn,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
):
    """Pause (or resume) every automatic action for the tenant. Anyone may
    turn it ON — stopping automation must never wait for an admin; only admins
    may turn it back OFF."""
    if not body.enabled:
        _require_admin(user)
    st = await pol.get_state(db, tenant_id)
    st.kill_switch = body.enabled
    st.kill_switch_by = user.email
    st.kill_switch_at = datetime.now(tz=timezone.utc)
    await audit.record(
        db, tenant_id, "policy.kill_switch", actor=user.email,
        subject="on" if body.enabled else "off", data={"enabled": body.enabled},
    )
    await db.commit()
    return await _policy_out(db, tenant_id)


@router.post("/policy/breaker/reset", response_model=PolicyOut)
async def reset_breaker(
    rule_id: str = "",
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
):
    _require_admin(user)
    st = await pol.get_state(db, tenant_id)
    breaker = dict(st.breaker or {})
    breaker.pop(rule_id or "__defaults__", None)
    st.breaker = breaker
    await audit.record(
        db, tenant_id, "policy.updated", actor=user.email, subject="breaker-reset",
        data={"rule_id": rule_id},
    )
    await db.commit()
    return await _policy_out(db, tenant_id)


@router.get("/trust")
async def trust_table(
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
) -> list[dict[str, Any]]:
    """Per-rule track record: what each rule has earned toward auto."""
    doc = await pol.load_policy(db, tenant_id)
    st = await pol.get_state(db, tenant_id)
    out = []
    for rule in doc.rules:
        t = rule.trust or pol.Trust()
        stats = await pol.trust_stats(db, tenant_id, rule.id, t.window_days)
        out.append({
            "rule_id": rule.id,
            "mode": rule.mode,
            "tools": rule.match.tools or ["*"],
            "successes": stats.successes,
            "failures": stats.failures,
            "failure_rate": stats.failure_rate,
            "min_successes": t.min_successes if rule.mode == "auto_after_trust" else None,
            "max_failure_rate": t.max_failure_rate if rule.mode == "auto_after_trust" else None,
            "earned": (
                rule.mode == "auto"
                or (rule.mode == "auto_after_trust"
                    and stats.successes >= t.min_successes
                    and stats.failure_rate <= t.max_failure_rate)
            ),
            "breaker_until": (st.breaker or {}).get(rule.id),
        })
    await db.commit()
    return out
