"""Autonomy policy as code (AOMM Level 3 and 5).

A tenant's policy is a YAML document (schema: :class:`PolicyDoc`, published as
``spec/autonomy-policy.schema.json``). It decides, for every *write* the agent
wants to make, whether the write is

* ``deny``    — refused; the agent is told why and must find another way,
* ``approve`` — queued for ``approvers`` distinct humans (the Level 2 default),
* ``auto``    — executed immediately, then verified exactly like an approved
  write,
* ``auto_after_trust`` — ``auto`` once the rule has earned it (enough verified
  successes and a low enough failure rate), ``approve`` until then.

Guardrails then narrow ``auto`` back to ``approve``: the tenant kill switch, a
tripped circuit breaker (an auto action of this rule failed verification
recently), a change-freeze window, and an hourly cap on auto actions.

No saved policy → :data:`DEFAULT_POLICY_YAML`: every write needs one approver,
which is exactly how Daalu behaved before AOMM.

:func:`decide` is pure given its inputs (:class:`PolicyContext`), so it is
unit-testable without a database; :func:`decide_for_tenant` loads the inputs.
"""

from __future__ import annotations

import fnmatch
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

Mode = Literal["deny", "approve", "auto", "auto_after_trust"]
EffectiveMode = Literal["deny", "approve", "auto"]

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# ── Schema ──────────────────────────────────────────────────────────────────


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Match(_Strict):
    """Which writes a rule covers. Omitted keys match everything. Values are
    ``fnmatch`` globs."""

    tools: list[str] | None = None
    clusters: list[str] | None = None
    namespaces: list[str] | None = None
    severities: list[str] | None = Field(
        default=None, description="Alert severities (info, warning, critical)."
    )


class Trust(_Strict):
    min_successes: int = Field(default=10, ge=1)
    max_failure_rate: float = Field(default=0.05, ge=0, le=1)
    window_days: int = Field(default=30, ge=1, le=365)


class Rule(_Strict):
    id: str = Field(min_length=1, max_length=128)
    description: str = ""
    match: Match = Field(default_factory=Match)
    mode: Mode
    approvers: int = Field(default=1, ge=1, le=5)
    trust: Trust | None = None
    allow_external_auto: bool = Field(
        default=False,
        description="Let writes proposed by external agents over MCP run "
        "automatically under this rule. Off by default: MCP proposals are "
        "downgraded to approve.",
    )


class Defaults(_Strict):
    mode: Mode = "approve"
    approvers: int = Field(default=1, ge=1, le=5)


class CircuitBreaker(_Strict):
    cooldown_minutes: int = Field(default=60, ge=1)


class FreezeWindow(_Strict):
    days: list[str] = Field(default_factory=lambda: list(_DAYS))
    start: str = "00:00"
    end: str = "23:59"
    tz: str = "UTC"

    @field_validator("days")
    @classmethod
    def _days(cls, v: list[str]) -> list[str]:
        out = [d.lower()[:3] for d in v]
        bad = [d for d in out if d not in _DAYS]
        if bad:
            raise ValueError(f"unknown day(s) {bad}; use {list(_DAYS)}")
        return out

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%H:%M")
        except ValueError as e:
            raise ValueError(f"{v!r} is not HH:MM") from e
        return v

    @field_validator("tz")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"unknown time zone {v!r}") from e
        return v


class Guardrails(_Strict):
    max_auto_actions_per_hour: int | None = Field(default=None, ge=0)
    circuit_breaker: CircuitBreaker | None = None
    freeze_windows: list[FreezeWindow] = Field(default_factory=list)


class PolicyDoc(_Strict):
    """An autonomy policy (AOMM spec v0.1)."""

    version: Literal[1] = 1
    investigate_automatically: bool = Field(
        default=False,
        description="Start an AI investigation on every new alert without "
        "waiting for someone to open it.",
    )
    defaults: Defaults = Field(default_factory=Defaults)
    guardrails: Guardrails = Field(default_factory=Guardrails)
    rules: list[Rule] = Field(default_factory=list)

    @field_validator("rules")
    @classmethod
    def _unique_ids(cls, v: list[Rule]) -> list[Rule]:
        seen: set[str] = set()
        for r in v:
            if r.id in seen:
                raise ValueError(f"duplicate rule id {r.id!r}")
            seen.add(r.id)
        return v


DEFAULT_POLICY_YAML = """\
# Autonomy policy (AI-Ops Maturity Model v0.1).
# With no rules, every write the agent proposes needs one human approval:
# Level 2. Add rules to let proven, low-risk actions run on their own.
version: 1
investigate_automatically: false
defaults:
  mode: approve
  approvers: 1
guardrails:
  max_auto_actions_per_hour: 10
  circuit_breaker:
    cooldown_minutes: 60
  freeze_windows: []
rules: []
"""


class PolicyError(ValueError):
    """The YAML didn't parse or didn't match the schema."""


def parse_policy(text: str) -> PolicyDoc:
    try:
        raw = yaml.safe_load(text) if text and text.strip() else {}
    except yaml.YAMLError as e:
        raise PolicyError(f"YAML parse error: {e}") from e
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise PolicyError("policy must be a YAML mapping")
    try:
        return PolicyDoc.model_validate(raw)
    except ValidationError as e:
        msgs = []
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"])
            msgs.append(f"{loc}: {err['msg']}")
        raise PolicyError("; ".join(msgs)) from e


def policy_json_schema() -> dict[str, Any]:
    schema = PolicyDoc.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://github.com/kiwueke1/daalu/spec/autonomy-policy.schema.json"
    schema["title"] = "AOMM autonomy policy v0.1"
    return schema


# ── Decision ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WriteRequest:
    tool: str
    cluster: str = ""
    namespace: str = ""
    severity: str = ""
    external: bool = False  # proposed by an external agent over MCP


@dataclass(frozen=True)
class TrustStats:
    successes: int = 0
    failures: int = 0

    @property
    def total(self) -> int:
        return self.successes + self.failures

    @property
    def failure_rate(self) -> float:
        return self.failures / self.total if self.total else 0.0


@dataclass
class PolicyContext:
    """Everything :func:`decide` needs besides the policy itself."""

    now: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    kill_switch: bool = False
    breaker: dict[str, str] = field(default_factory=dict)  # rule_id -> until ISO
    auto_actions_last_hour: int = 0
    trust: dict[str, TrustStats] = field(default_factory=dict)  # rule_id -> stats


@dataclass(frozen=True)
class Decision:
    mode: EffectiveMode
    rule_id: str  # "" when the defaults applied
    reason: str
    approvers: int = 1
    configured_mode: Mode = "approve"

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "rule_id": self.rule_id,
            "reason": self.reason,
            "approvers": self.approvers,
            "configured_mode": self.configured_mode,
        }


def _glob_any(value: str, patterns: list[str] | None) -> bool:
    if patterns is None:
        return True
    return any(fnmatch.fnmatchcase(value or "", p) for p in patterns)


def matching_rule(policy: PolicyDoc, req: WriteRequest) -> Rule | None:
    for rule in policy.rules:
        m = rule.match
        if (
            _glob_any(req.tool, m.tools)
            and _glob_any(req.cluster, m.clusters)
            and _glob_any(req.namespace, m.namespaces)
            and _glob_any(req.severity, m.severities)
        ):
            return rule
    return None


def in_freeze_window(windows: list[FreezeWindow], now: datetime) -> FreezeWindow | None:
    for w in windows:
        local = now.astimezone(ZoneInfo(w.tz))
        if _DAYS[local.weekday()] not in w.days:
            continue
        hhmm = local.strftime("%H:%M")
        if w.start <= w.end:
            if w.start <= hhmm <= w.end:
                return w
        elif hhmm >= w.start or hhmm <= w.end:  # crosses midnight
            return w
    return None


def decide(policy: PolicyDoc, req: WriteRequest, ctx: PolicyContext) -> Decision:
    rule = matching_rule(policy, req)
    rule_id = rule.id if rule else ""
    configured: Mode = rule.mode if rule else policy.defaults.mode
    approvers = rule.approvers if rule else policy.defaults.approvers
    label = f"rule {rule_id!r}" if rule else "policy defaults"

    def out(mode: EffectiveMode, reason: str) -> Decision:
        return Decision(
            mode=mode, rule_id=rule_id, reason=reason,
            approvers=approvers, configured_mode=configured,
        )

    if configured == "deny":
        return out("deny", f"{label} denies {req.tool}")
    if configured == "approve":
        return out("approve", f"{label} requires {approvers} approval(s)")

    # configured is auto or auto_after_trust from here on.
    if configured == "auto_after_trust":
        t = (rule.trust if rule and rule.trust else Trust())
        stats = ctx.trust.get(rule_id, TrustStats())
        if stats.successes < t.min_successes:
            return out(
                "approve",
                f"{label} has earned {stats.successes}/{t.min_successes} verified "
                f"successes; approval required until then",
            )
        if stats.failure_rate > t.max_failure_rate:
            return out(
                "approve",
                f"{label} failure rate {stats.failure_rate:.0%} is above "
                f"{t.max_failure_rate:.0%}; approval required",
            )

    if ctx.kill_switch:
        return out("approve", "kill switch is on: all automatic actions paused")
    if req.external and not (rule and rule.allow_external_auto):
        return out("approve", f"{label} would auto-run, but proposals from external "
                   "agents need approval unless the rule sets allow_external_auto")
    until = ctx.breaker.get(rule_id or "__defaults__")
    if until:
        try:
            until_dt = datetime.fromisoformat(until)
        except ValueError:
            until_dt = None
        if until_dt and ctx.now < until_dt:
            return out(
                "approve",
                f"circuit breaker for {label} is tripped until {until_dt:%Y-%m-%d %H:%M} UTC "
                "(a recent automatic action failed verification)",
            )
    w = in_freeze_window(policy.guardrails.freeze_windows, ctx.now)
    if w:
        return out("approve", f"change freeze ({','.join(w.days)} {w.start}-{w.end} {w.tz})")
    cap = policy.guardrails.max_auto_actions_per_hour
    if cap is not None and ctx.auto_actions_last_hour >= cap:
        return out("approve", f"hourly cap of {cap} automatic actions reached")

    why = "trust earned" if configured == "auto_after_trust" else "auto"
    return out("auto", f"{label} allows {req.tool} to run automatically ({why})")


# ── Persistence ─────────────────────────────────────────────────────────────


async def load_policy_row(db: AsyncSession, tenant_id: uuid.UUID) -> Any | None:
    from daalu_automation.models import AutonomyPolicy

    return (
        await db.execute(
            select(AutonomyPolicy)
            .where(AutonomyPolicy.tenant_id == tenant_id)
            .order_by(AutonomyPolicy.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def load_policy(db: AsyncSession, tenant_id: uuid.UUID) -> PolicyDoc:
    row = await load_policy_row(db, tenant_id)
    if row is None:
        return parse_policy(DEFAULT_POLICY_YAML)
    try:
        return parse_policy(row.yaml)
    except PolicyError:
        # A stored policy was validated on save; if it no longer parses (schema
        # change), fail safe to the approve-everything default.
        return parse_policy(DEFAULT_POLICY_YAML)


async def save_policy(
    db: AsyncSession, tenant_id: uuid.UUID, text: str, *, user_id: uuid.UUID | None,
    note: str = "",
) -> Any:
    """Validate and store a new version. Raises :class:`PolicyError`."""
    from daalu_automation.models import AutonomyPolicy

    parse_policy(text)
    current = await load_policy_row(db, tenant_id)
    row = AutonomyPolicy(
        tenant_id=tenant_id,
        version=(current.version + 1) if current else 1,
        yaml=text,
        note=note[:512],
        updated_by=user_id,
    )
    db.add(row)
    await db.flush()
    return row


async def get_state(db: AsyncSession, tenant_id: uuid.UUID) -> Any:
    from daalu_automation.models import AIOpsTenantState

    st = await db.get(AIOpsTenantState, tenant_id)
    if st is None:
        st = AIOpsTenantState(tenant_id=tenant_id, kill_switch=False, breaker={})
        db.add(st)
        await db.flush()
    return st


async def trust_stats(
    db: AsyncSession, tenant_id: uuid.UUID, rule_id: str, window_days: int,
) -> TrustStats:
    from daalu_automation.models import AlertAction

    since = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
    rows = (
        await db.execute(
            select(AlertAction.verified_resolved, func.count())
            .where(
                AlertAction.tenant_id == tenant_id,
                AlertAction.policy_rule == rule_id,
                AlertAction.executed_at.is_not(None),
                AlertAction.executed_at >= since,
                AlertAction.verified_resolved.is_not(None),
            )
            .group_by(AlertAction.verified_resolved)
        )
    ).all()
    s = f = 0
    for ok, n in rows:
        if ok:
            s += n
        else:
            f += n
    return TrustStats(successes=s, failures=f)


async def auto_actions_since(db: AsyncSession, tenant_id: uuid.UUID, since: datetime) -> int:
    from daalu_automation.models import AlertAction

    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(AlertAction)
                .where(
                    AlertAction.tenant_id == tenant_id,
                    AlertAction.auto_executed.is_(True),
                    AlertAction.created_at >= since,
                )
            )
        ).scalar_one()
    )


async def build_context(
    db: AsyncSession, tenant_id: uuid.UUID, policy: PolicyDoc, req: WriteRequest,
) -> PolicyContext:
    now = datetime.now(tz=timezone.utc)
    st = await get_state(db, tenant_id)
    ctx = PolicyContext(
        now=now,
        kill_switch=bool(st.kill_switch),
        breaker=dict(st.breaker or {}),
        auto_actions_last_hour=await auto_actions_since(db, tenant_id, now - timedelta(hours=1)),
    )
    rule = matching_rule(policy, req)
    if rule and rule.mode == "auto_after_trust":
        t = rule.trust or Trust()
        ctx.trust[rule.id] = await trust_stats(db, tenant_id, rule.id, t.window_days)
    return ctx


async def decide_for_tenant(
    db: AsyncSession, tenant_id: uuid.UUID, req: WriteRequest, *, pending_auto: int = 0,
) -> Decision:
    """``pending_auto``: writes already decided ``auto`` earlier in the same
    model turn but not yet inserted, so the hourly cap counts them too."""
    policy = await load_policy(db, tenant_id)
    ctx = await build_context(db, tenant_id, policy, req)
    ctx.auto_actions_last_hour += pending_auto
    return decide(policy, req, ctx)


async def trip_breaker(db: AsyncSession, tenant_id: uuid.UUID, rule_id: str) -> datetime | None:
    """An auto action under ``rule_id`` failed verification: demote the rule to
    approve for the configured cooldown. Returns the until-time, or None when
    the policy has no circuit breaker configured."""
    policy = await load_policy(db, tenant_id)
    cb = policy.guardrails.circuit_breaker
    if cb is None:
        return None
    until = datetime.now(tz=timezone.utc) + timedelta(minutes=cb.cooldown_minutes)
    st = await get_state(db, tenant_id)
    breaker = dict(st.breaker or {})
    breaker[rule_id or "__defaults__"] = until.isoformat()
    st.breaker = breaker
    await db.flush()
    return until


def write_request_for(
    tool: str, tool_input: dict[str, Any] | None, *, cluster: str | None,
    severity: str = "", external: bool = False,
) -> WriteRequest:
    inp = tool_input or {}
    return WriteRequest(
        tool=tool,
        cluster=cluster or "",
        namespace=str(inp.get("namespace") or ""),
        severity=severity,
        external=external,
    )
