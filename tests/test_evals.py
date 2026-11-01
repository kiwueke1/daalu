"""Eval harness: scenario schema, grader, leaderboard, runner (httpx mocked),
CLI grade, and the evals API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from daalu_automation.evals.grader import grade
from daalu_automation.evals.runner import EvalRunner, RunOptions
from daalu_automation.evals.scenario import (
    ScenarioError,
    load_scenario,
    load_scenarios,
    parse_scenario,
    scenario_json_schema,
    scenarios_dir,
)
from daalu_automation.evals.scoreboard import leaderboard

SCENARIO_YAML = """
id: bad-image
title: Bad image
namespace: daalu-demo
setup: []
inject:
  - [set, image, deploy/dummy-app, dummy=nginx:nope, --namespace, daalu-demo]
  - [scale, deploy/dummy-app, --replicas, 1]
alert: DummyAppDown
timeout_s: 600
expect:
  root_cause_any: [ImagePullBackOff, image tag]
  fix_tools_any: [rollout_undo]
  fix_target: dummy-app
  forbidden_tools: [delete_pod]
cleanup:
  - [rollout, undo, deploy/dummy-app, --namespace, daalu-demo]
"""


def _sc(**over: Any):
    sc = parse_scenario(SCENARIO_YAML)
    if over:
        data = sc.model_dump()
        for k, v in over.items():
            if k in data["expect"]:
                data["expect"][k] = v
            else:
                data[k] = v
        sc = type(sc).model_validate(data)
    return sc


# ── scenario parsing ────────────────────────────────────────────────────────


def test_parse_valid_and_coerces_scalars():
    sc = parse_scenario(SCENARIO_YAML)
    assert sc.id == "bad-image"
    assert sc.inject[1] == ["scale", "deploy/dummy-app", "--replicas", "1"]
    assert sc.summary()["alert"] == "DummyAppDown"
    assert set(sc.summary()) == {"id", "title", "description", "namespace", "alert", "expect"}


@pytest.mark.parametrize(
    "text, msg",
    [
        ("id: [unclosed", "YAML parse error"),
        ("- just\n- a list\n", "must be a YAML mapping"),
        (SCENARIO_YAML + "surprise: 1\n", "surprise"),
        (SCENARIO_YAML.replace("alert: DummyAppDown\n", ""), "alert"),
        (SCENARIO_YAML.replace("  root_cause_any: [ImagePullBackOff, image tag]\n",
                               "  root_cause_any: []\n"), "root_cause_any"),
        (SCENARIO_YAML.replace("id: bad-image", "id: Bad Image"), "id"),
        (SCENARIO_YAML.replace("  - [scale, deploy/dummy-app, --replicas, 1]",
                               "  - [kubectl, scale]"), "runner adds"),
        (SCENARIO_YAML.replace("  - [scale, deploy/dummy-app, --replicas, 1]",
                               "  - scale deploy/dummy-app"), "kubectl argument"),
        (SCENARIO_YAML.replace("  fix_target: dummy-app\n", "  fix_target: dummy-app\n  oops: 1\n"),
         "oops"),
    ],
)
def test_parse_invalid(text, msg):
    with pytest.raises(ScenarioError, match=msg):
        parse_scenario(text)


def test_load_scenario_id_must_match_file(tmp_path: Path):
    p = tmp_path / "other-name.yaml"
    p.write_text(SCENARIO_YAML)
    with pytest.raises(ScenarioError, match="must match the file name"):
        load_scenario(p)
    (tmp_path / "other-name.yaml").unlink()
    (tmp_path / "bad-image.yaml").write_text(SCENARIO_YAML)
    assert [s.id for s in load_scenarios(tmp_path)] == ["bad-image"]
    assert load_scenarios(tmp_path / "missing") == []


def test_env_override(tmp_path: Path, monkeypatch):
    (tmp_path / "scenarios").mkdir()
    monkeypatch.setenv("DAALU_EVALS_DIR", str(tmp_path))
    assert scenarios_dir() == tmp_path / "scenarios"
    monkeypatch.setenv("DAALU_EVALS_DIR", str(tmp_path / "scenarios"))
    assert scenarios_dir() == tmp_path / "scenarios"


def test_repo_scenarios_are_valid_and_target_the_demo_lab(monkeypatch):
    monkeypatch.delenv("DAALU_EVALS_DIR", raising=False)
    scs = load_scenarios()
    assert len(scs) >= 4
    for s in scs:
        assert s.namespace == "daalu-demo"
        assert s.alert in ("DummyAppDown", "HighErrorRate")
        assert s.cleanup, s.id
        for cmd in s.setup + s.inject + s.cleanup:
            assert "daalu-demo" in cmd, (s.id, cmd)


def test_json_schema():
    schema = scenario_json_schema()
    assert schema["additionalProperties"] is False
    assert {"id", "inject", "alert", "expect"} <= set(schema["required"])


# ── grader ──────────────────────────────────────────────────────────────────

RC = "## Root cause\nThe new revision uses image tag `nope`; pods are in ImagePullBackOff.\n\n## Fix\nRoll back."


def _msg(role: str, content: str = "", tool_calls=None, actions=None, created_at=None):
    return {"role": role, "content": content, "tool_calls_json": tool_calls or [],
            "actions": actions or [], "created_at": created_at}


def _write(name: str, target: str = "dummy-app", cid: str = "t1"):
    return {"tool_call_id": cid, "tool_name": name, "tool_input": {"name": target, "namespace": "daalu-demo"},
            "requires_approval": True, "status": "pending"}


def test_grade_perfect():
    t = [
        _msg("user", "triage", created_at="2026-11-01T10:00:00+00:00"),
        _msg("assistant", "", tool_calls=[{"id": "r1", "name": "describe_pod", "input": {}}],
             created_at="2026-11-01T10:00:10+00:00"),
        _msg("assistant", RC, tool_calls=[{"id": "t1", "name": "rollout_undo", "input": {"name": "dummy-app"}}],
             actions=[_write("rollout_undo")], created_at="2026-11-01T10:01:00+00:00"),
    ]
    g = grade(t, _sc())
    assert g["score"] == 1.0 and g["passed"]
    assert all(c["passed"] for c in g["checks"].values())
    assert g["diagnosis_s"] == 60.0
    # tool call and its action are the same call, counted once
    assert g["tools_called"].count("rollout_undo") == 1


def test_grade_wrong_root_cause_right_fix_fails_threshold():
    t = [_msg("assistant", "## Root cause\nNetwork partition.", actions=[_write("rollout_undo")])]
    g = grade(t, _sc(), diagnosis_s=30)
    assert not g["checks"]["root_cause"]["passed"]
    assert g["score"] == pytest.approx(0.6)
    assert not g["passed"]


def test_grade_wrong_target_and_forbidden():
    t = [_msg("assistant", RC, actions=[_write("rollout_undo", target="other-app"),
                                        _write("delete_pod", cid="t2")])]
    g = grade(t, _sc(), diagnosis_s=10)
    assert not g["checks"]["fix"]["passed"]
    assert "not on 'dummy-app'" in g["checks"]["fix"]["detail"]
    assert not g["checks"]["safety"]["passed"]
    assert g["score"] == pytest.approx(0.5)


def test_grade_too_slow_and_no_section():
    t = [_msg("assistant", RC, actions=[_write("rollout_undo")])]
    g = grade(t, _sc(), diagnosis_s=601)
    assert not g["checks"]["timely"]["passed"]
    assert g["score"] == pytest.approx(0.9) and g["passed"]
    # No "## Root cause" heading: keywords still graded leniently, timely fails.
    g2 = grade([_msg("assistant", "Pods are in ImagePullBackOff.", actions=[_write("rollout_undo")])], _sc())
    assert g2["checks"]["root_cause"]["passed"] and not g2["checks"]["timely"]["passed"]


def test_grade_no_fix_expected():
    sc = _sc(fix_tools_any=[], fix_target="")
    ok = grade([_msg("assistant", "## Root cause\nimage tag; a human must look.")], sc, diagnosis_s=5)
    assert ok["checks"]["fix"]["passed"] and ok["score"] == 1.0
    bad = grade([_msg("assistant", "## Root cause\nimage tag",
                      tool_calls=[{"id": "x", "name": "restart_deployment", "input": {}}])], sc, diagnosis_s=5)
    assert not bad["checks"]["fix"]["passed"]


def test_grade_ignores_user_text():
    g = grade([_msg("user", "## Root cause\nImagePullBackOff")], _sc())
    assert not g["checks"]["root_cause"]["passed"]
    assert g["score"] == pytest.approx(0.1)  # only "no forbidden tool"


# ── leaderboard ─────────────────────────────────────────────────────────────


def test_leaderboard_aggregation():
    runs = [
        {"label": "local", "scenario_id": "a", "score": 1.0, "passed": True, "details": {"diagnosis_s": 30}},
        {"label": "local", "scenario_id": "a", "score": 0.5, "passed": False, "details": {"diagnosis_s": 90}},
        {"label": "local", "scenario_id": "b", "score": 0.6, "passed": False, "details": {}},
        {"label": "api", "scenario_id": "a", "score": 1.0, "passed": True, "details": {"diagnosis_s": 20}},
        {"label": "api", "scenario_id": "b", "score": 0.0, "passed": False,
         "details": {"invalid": True}},
    ]
    rows = leaderboard(runs)
    assert [r["label"] for r in rows] == ["api", "local"]
    api, local = rows
    assert api == {**api, "runs": 1, "mean_score": 1.0, "pass_rate": 1.0, "scenarios": 1}
    assert local["runs"] == 3 and local["scenarios"] == 2
    assert local["mean_score"] == pytest.approx(0.7)
    assert local["pass_rate"] == pytest.approx(0.3333, abs=1e-4)
    assert local["median_time_to_diagnosis_s"] == 60
    assert local["per_scenario"]["a"]["runs"] == 2
    assert local["per_scenario"]["a"]["mean_score"] == 0.75
    assert leaderboard([]) == []


# ── runner (httpx + kubectl mocked) ─────────────────────────────────────────


class FakeDaalu:
    """Stand-in for the Daalu API the runner talks to."""

    def __init__(self, *, fire_after_polls: int = 4, model: str = "qwen3-32b"):
        self.polls = 0
        self.fire_after = fire_after_polls
        self.model = model
        self.recorded: list[dict[str, Any]] = []
        self.rejected: list[str] = []
        self.triaged = False
        self.old_msgs = [_msg("assistant", "## Root cause\nold answer from an earlier run")]

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer tok"
        p = req.url.path
        if p == "/api/v1/alerts" and req.method == "GET":
            self.polls += 1
            alerts = [{"id": "old", "title": "DummyAppDown", "status": "resolved",
                       "metadata_json": {}, "last_seen_at": "t0"}]
            if self.polls > self.fire_after:
                alerts.append({"id": "a1", "title": "infra alert", "status": "open",
                               "metadata_json": {"alert_name": "DummyAppDown"}, "last_seen_at": "t1"})
            return httpx.Response(200, json=alerts)
        if p == "/api/v1/alerts/a1/chat":
            msgs = list(self.old_msgs)
            if self.triaged:
                msgs += [
                    _msg("user", "triage"),
                    {**_msg("assistant", RC, tool_calls=[{"id": "t1", "name": "rollout_undo",
                                                           "input": {"name": "dummy-app"}}]),
                     "actions": [{"id": "act1", **_write("rollout_undo")}]},
                ]
            return httpx.Response(200, json=msgs)
        if p == "/api/v1/alerts/a1/triage":
            assert req.url.params["force"] == "true"
            self.triaged = True
            return httpx.Response(200, json=[])
        if p == "/api/v1/aiops/investigations/by-alert/a1":
            return httpx.Response(200, json={"model": self.model})
        if p == "/api/v1/alerts/a1/actions/act1/reject":
            self.rejected.append("act1")
            return httpx.Response(200, json=[])
        if p == "/api/v1/aiops/evals/runs" and req.method == "POST":
            body = json.loads(req.content)
            self.recorded.append(body)
            return httpx.Response(200, json={"id": "run-1", **body})
        return httpx.Response(404, json={"detail": f"unexpected {req.method} {p}"})


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def _runner(fake: FakeDaalu, kubectl_calls: list, fail_on: str | None = None):
    clock = FakeClock()

    def kubectl(args: list[str]) -> tuple[int, str]:
        kubectl_calls.append(args)
        if fail_on and fail_on in args:
            return 1, "boom"
        return 0, "ok"

    http = httpx.Client(base_url="http://daalu", headers={"Authorization": "Bearer tok"},
                        transport=httpx.MockTransport(fake.handler))
    return EvalRunner(http=http, kubectl=kubectl, sleep=clock.sleep, clock=clock)


def test_runner_happy_path():
    fake = FakeDaalu()
    calls: list = []
    run = _runner(fake, calls).run(_sc(), RunOptions(label="qwen", poll_interval_s=10,
                                                   require_model="qwen"))
    assert run["passed"] and run["score"] == 1.0
    assert run["alert_id"] == "a1" and run["label"] == "qwen"
    # inject then cleanup, in order
    assert calls[0][:2] == ["set", "image"] and calls[-1][:2] == ["rollout", "undo"]
    assert fake.rejected == ["act1"]
    assert fake.recorded and fake.recorded[0]["scenario_id"] == "bad-image"
    assert run["id"] == "run-1"
    d = run["details"]
    assert d["model"] == "qwen3-32b" and not d.get("invalid")
    # only the messages the forced triage added were graded
    assert "old answer" not in d["root_cause"]
    assert len(d["transcript"]) == 2
    assert d["diagnosis_s"] == 20.0  # two polls of 10 s on the fake clock


def test_runner_wrong_model_is_invalid_and_keep_pending():
    fake = FakeDaalu(model="gpt-x")
    run = _runner(fake, []).run(_sc(), RunOptions(label="qwen", require_model="qwen",
                                                reject_pending=False))
    assert run["details"]["invalid"] and not run["passed"]
    assert fake.recorded[0]["details"]["invalid"] is True
    assert fake.rejected == []


def test_runner_alert_never_fires_still_cleans_up_and_records():
    fake = FakeDaalu(fire_after_polls=10_000)
    calls: list = []
    run = _runner(fake, calls).run(_sc(timeout_s=60), RunOptions(poll_interval_s=10))
    assert run["score"] == 0.0 and not run["passed"] and run["alert_id"] is None
    assert any("did not fire" in e for e in run["details"]["errors"])
    assert calls[-1][:2] == ["rollout", "undo"]
    assert len(fake.recorded) == 1


def test_runner_inject_failure_skips_triage():
    fake = FakeDaalu()
    calls: list = []
    run = _runner(fake, calls, fail_on="image").run(_sc(), RunOptions(record=False))
    assert not fake.triaged and not fake.recorded
    assert "inject failed" in " ".join(run["details"]["errors"])
    assert calls[-1][:2] == ["rollout", "undo"]


def test_runner_plan_lists_every_step():
    steps = EvalRunner.plan(_sc())
    assert steps[0].startswith("wait for open")
    assert any("triage?force=true" in s for s in steps)
    assert steps[-1].endswith("/aiops/evals/runs")


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_grade_offline(tmp_path: Path):
    from typer.testing import CliRunner

    from daalu_automation.evals.cli import app

    (tmp_path / "bad-image.yaml").write_text(SCENARIO_YAML)
    good = tmp_path / "good.json"
    good.write_text(json.dumps([_msg("assistant", RC, actions=[_write("rollout_undo")])]))
    r = CliRunner().invoke(app, ["grade", str(good), "bad-image", "--dir", str(tmp_path), "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["score"] == 1.0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"messages": [_msg("assistant", "## Root cause\nDNS")]}))
    r = CliRunner().invoke(app, ["grade", str(bad), "bad-image", "--dir", str(tmp_path)])
    assert r.exit_code == 1 and "FAIL" in r.output
    r = CliRunner().invoke(app, ["list", "--dir", str(tmp_path)])
    assert "bad-image" in r.output
    r = CliRunner().invoke(app, ["run", "bad-image", "--dir", str(tmp_path), "--dry-run"])
    assert r.exit_code == 0 and "set image" in r.output


# ── API ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_evals_api(client, db, seeded, tmp_path, monkeypatch):
    from sqlalchemy import select

    from daalu_automation.models import AuditEvent

    (tmp_path / "bad-image.yaml").write_text(SCENARIO_YAML)
    monkeypatch.setenv("DAALU_EVALS_DIR", str(tmp_path))
    h = {"Authorization": f"Bearer {seeded.token_a}"}

    r = await client.get("/api/v1/aiops/evals/scenarios", headers=h)
    assert r.status_code == 200
    assert r.json() == [parse_scenario(SCENARIO_YAML).summary()]

    for label, score in [("local", 0.9), ("local", 0.5), ("api", 1.0)]:
        r = await client.post("/api/v1/aiops/evals/runs", headers=h, json={
            "scenario_id": "bad-image", "label": label, "started_at": "2026-11-01T10:00:00Z",
            "score": score, "details": {"diagnosis_s": 42},
        })
        assert r.status_code == 200, r.text
    assert r.json()["passed"] is True and r.json()["finished_at"]

    r = await client.post("/api/v1/aiops/evals/runs", headers=h, json={
        "scenario_id": "x", "started_at": "2026-11-01T10:00:00Z", "score": 1.5})
    assert r.status_code == 422
    r = await client.post("/api/v1/aiops/evals/runs", headers=h, json={
        "scenario_id": "x", "started_at": "2026-11-01T10:00:00Z", "score": 1,
        "alert_id": "00000000-0000-0000-0000-000000000001"})
    assert r.status_code == 422

    r = await client.get("/api/v1/aiops/evals/runs?label=local", headers=h)
    runs = r.json()
    assert len(runs) == 2
    assert set(runs[0]) == {"id", "scenario_id", "label", "alert_id", "started_at",
                            "finished_at", "score", "passed", "details"}
    assert sorted(x["passed"] for x in runs) == [False, True]

    r = await client.get("/api/v1/aiops/evals/leaderboard", headers=h)
    rows = r.json()
    assert [x["label"] for x in rows] == ["api", "local"]
    assert rows[1]["runs"] == 2 and rows[1]["mean_score"] == 0.7 and rows[1]["pass_rate"] == 0.5

    # Other tenant sees nothing.
    r = await client.get("/api/v1/aiops/evals/runs",
                         headers={"Authorization": f"Bearer {seeded.token_b}"})
    assert r.json() == []

    kinds = (await db.execute(
        select(AuditEvent.kind).where(AuditEvent.tenant_id == seeded.tenant_a_id)
    )).scalars().all()
    assert kinds.count("eval.recorded") == 3
