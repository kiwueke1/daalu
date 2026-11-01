"""Autonomy policy engine (AOMM L3/L5) — pure decision logic, no database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from daalu_automation.core.aiops import policy as pol

POLICY = """
version: 1
investigate_automatically: true
defaults: {mode: approve, approvers: 1}
guardrails:
  max_auto_actions_per_hour: 3
  circuit_breaker: {cooldown_minutes: 30}
  freeze_windows:
    - {days: [sat], start: "00:00", end: "23:59", tz: UTC}
rules:
  - id: no-kube-system
    match: {namespaces: [kube-system]}
    mode: deny
  - id: dev-restarts
    match: {tools: [restart_deployment, delete_pod], clusters: ["dev-*"]}
    mode: auto
  - id: prod-rollback
    match: {tools: [rollout_undo], namespaces: ["prod-*"]}
    mode: approve
    approvers: 2
  - id: scale-earned
    match: {tools: [scale_deployment]}
    mode: auto_after_trust
    trust: {min_successes: 3, max_failure_rate: 0.25, window_days: 30}
"""

WEDNESDAY = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
SATURDAY = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def doc() -> pol.PolicyDoc:
    return pol.parse_policy(POLICY)


def ctx(**kw) -> pol.PolicyContext:
    kw.setdefault("now", WEDNESDAY)
    return pol.PolicyContext(**kw)


def test_default_policy_is_level_2(doc) -> None:
    d = pol.parse_policy(pol.DEFAULT_POLICY_YAML)
    out = pol.decide(d, pol.WriteRequest(tool="rollout_undo", namespace="x"), ctx())
    assert out.mode == "approve" and out.rule_id == "" and out.approvers == 1
    assert d.investigate_automatically is False


def test_empty_yaml_is_default() -> None:
    assert pol.parse_policy("").defaults.mode == "approve"


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("rules: [", "YAML parse error"),
        ("- a\n- b", "mapping"),
        ("defaults: {mode: yolo}", "defaults.mode"),
        ("rules: [{id: a, mode: auto}, {id: a, mode: deny}]", "duplicate rule id"),
        ("unknown_key: 1", "unknown_key"),
        ("guardrails: {freeze_windows: [{days: [funday]}]}", "unknown day"),
        ("guardrails: {freeze_windows: [{tz: Mars/Olympus}]}", "time zone"),
    ],
)
def test_invalid_policies_are_rejected(text, fragment) -> None:
    with pytest.raises(pol.PolicyError) as e:
        pol.parse_policy(text)
    assert fragment in str(e.value)


def test_first_matching_rule_wins_and_deny(doc) -> None:
    out = pol.decide(
        doc,
        pol.WriteRequest(tool="restart_deployment", cluster="dev-1", namespace="kube-system"),
        ctx(),
    )
    assert out.mode == "deny" and out.rule_id == "no-kube-system"


def test_auto_rule(doc) -> None:
    out = pol.decide(doc, pol.WriteRequest(tool="delete_pod", cluster="dev-a", namespace="app"), ctx())
    assert out.mode == "auto" and out.rule_id == "dev-restarts"
    # same tool on prod cluster falls through to defaults
    out = pol.decide(doc, pol.WriteRequest(tool="delete_pod", cluster="prod", namespace="app"), ctx())
    assert out.mode == "approve" and out.rule_id == ""


def test_multi_approver(doc) -> None:
    out = pol.decide(doc, pol.WriteRequest(tool="rollout_undo", namespace="prod-api"), ctx())
    assert out.mode == "approve" and out.approvers == 2


def test_auto_after_trust_thresholds(doc) -> None:
    req = pol.WriteRequest(tool="scale_deployment", namespace="app")
    out = pol.decide(doc, req, ctx(trust={"scale-earned": pol.TrustStats(2, 0)}))
    assert out.mode == "approve" and "2/3" in out.reason
    out = pol.decide(doc, req, ctx(trust={"scale-earned": pol.TrustStats(3, 2)}))
    assert out.mode == "approve" and "failure rate" in out.reason
    out = pol.decide(doc, req, ctx(trust={"scale-earned": pol.TrustStats(6, 1)}))
    assert out.mode == "auto" and "trust earned" in out.reason


def test_guardrails_demote_auto(doc) -> None:
    req = pol.WriteRequest(tool="restart_deployment", cluster="dev-a", namespace="app")
    assert pol.decide(doc, req, ctx(kill_switch=True)).mode == "approve"
    until = (WEDNESDAY + timedelta(minutes=10)).isoformat()
    out = pol.decide(doc, req, ctx(breaker={"dev-restarts": until}))
    assert out.mode == "approve" and "circuit breaker" in out.reason
    expired = (WEDNESDAY - timedelta(minutes=1)).isoformat()
    assert pol.decide(doc, req, ctx(breaker={"dev-restarts": expired})).mode == "auto"
    out = pol.decide(doc, req, ctx(now=SATURDAY))
    assert out.mode == "approve" and "freeze" in out.reason
    out = pol.decide(doc, req, ctx(auto_actions_last_hour=3))
    assert out.mode == "approve" and "hourly cap" in out.reason


def test_guardrails_never_relax_deny(doc) -> None:
    req = pol.WriteRequest(tool="delete_pod", cluster="dev-a", namespace="kube-system")
    assert pol.decide(doc, req, ctx(kill_switch=True)).mode == "deny"


def test_external_agents_need_approval_unless_allowed(doc) -> None:
    req = pol.WriteRequest(tool="delete_pod", cluster="dev-a", namespace="app", external=True)
    assert pol.decide(doc, req, ctx()).mode == "approve"
    allowed = pol.parse_policy(POLICY.replace(
        "    mode: auto\n", "    mode: auto\n    allow_external_auto: true\n", 1))
    assert pol.decide(allowed, req, ctx()).mode == "auto"


def test_freeze_window_crossing_midnight() -> None:
    w = [pol.FreezeWindow(days=["fri"], start="22:00", end="06:00", tz="UTC")]
    assert pol.in_freeze_window(w, datetime(2026, 11, 6, 23, 0, tzinfo=timezone.utc))
    assert not pol.in_freeze_window(w, datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc))


def test_json_schema_is_published() -> None:
    s = pol.policy_json_schema()
    assert s["title"].startswith("AOMM")
    assert "rules" in s["properties"]
