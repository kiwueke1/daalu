"""Drive one eval scenario against a live Daalu and a real cluster (§8).

Steps, in order:

1. ``setup`` — idempotent kubectl commands restoring the known-good state;
2. wait (bounded) for any still-open alert of this name to clear, then
   snapshot the matching alerts so a stale one is never mistaken for ours;
3. ``inject`` — the kubectl commands that cause the fault (the clock starts);
4. poll ``GET /api/v1/alerts`` until a new (or re-fired) alert whose
   alertname/title is ``scenario.alert`` appears, up to ``timeout_s``;
5. ``POST /api/v1/alerts/{id}/triage?force=true`` — a fresh AI pass;
6. fetch the chat, keep only the messages this pass added, and grade them;
7. reject any pending write the AI proposed (nothing a benchmark proposes is
   ever approved), unless told to keep them;
8. ``cleanup`` — always, even after a failure;
9. ``POST /api/v1/aiops/evals/runs`` — record the run under ``label``.

The HTTP client and the kubectl function are injected, so the whole loop is
unit-testable with ``httpx.MockTransport`` and a fake kubectl.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from daalu_automation.evals.grader import grade
from daalu_automation.evals.scenario import Scenario

KubectlFn = Callable[[list[str]], tuple[int, str]]
API = "/api/v1"


def make_kubectl(kubeconfig: str | None = None, context: str | None = None,
                 *, timeout_s: int = 300) -> KubectlFn:
    """``kubectl [--kubeconfig K] [--context C] <args>`` → (returncode, output)."""

    def run(args: list[str]) -> tuple[int, str]:
        cmd = ["kubectl"]
        if kubeconfig:
            cmd += ["--kubeconfig", kubeconfig]
        if context:
            cmd += ["--context", context]
        cmd += list(args)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        except FileNotFoundError:
            return 127, "kubectl not found on PATH"
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {timeout_s}s"
        return p.returncode, (p.stdout + p.stderr).strip()

    return run


@dataclass
class RunOptions:
    label: str = ""
    poll_interval_s: float = 10.0
    clear_timeout_s: float = 300.0
    reject_pending: bool = True
    require_model: str = ""  # substring the investigation's model must contain
    record: bool = True


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def alert_matches(alert: dict[str, Any], name: str) -> bool:
    md = alert.get("metadata_json") or {}
    labels = md.get("labels") or {}
    return name in (alert.get("title"), md.get("alert_name"), labels.get("alertname"))


def _compact(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A transcript small enough to store on the run, still re-gradable."""
    out = []
    for m in messages:
        out.append({
            "role": m.get("role"),
            "content": str(m.get("content") or "")[:8000],
            "created_at": m.get("created_at"),
            "tool_calls_json": [
                {"id": c.get("id"), "name": c.get("name"), "input": c.get("input")}
                for c in (m.get("tool_calls_json") or [])
            ],
            "actions": [
                {"id": a.get("id"), "tool_call_id": a.get("tool_call_id"),
                 "tool_name": a.get("tool_name"), "tool_input": a.get("tool_input"),
                 "requires_approval": a.get("requires_approval"), "status": a.get("status"),
                 "policy_mode": a.get("policy_mode")}
                for a in (m.get("actions") or [])
            ],
        })
    return out


class EvalRunner:
    def __init__(
        self,
        *,
        http: httpx.Client,
        kubectl: KubectlFn,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = lambda _m: None,
    ) -> None:
        self.http = http
        self.kubectl = kubectl
        self.sleep = sleep
        self.clock = clock
        self.log = log

    @classmethod
    def connect(cls, url: str, token: str, kubectl: KubectlFn, **kw: Any) -> EvalRunner:
        http = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(60.0),
        )
        return cls(http=http, kubectl=kubectl, **kw)

    # ── plan / dry run ───────────────────────────────────────────────────────

    @staticmethod
    def plan(scenario: Scenario) -> list[str]:
        steps = [f"kubectl {' '.join(a)}" for a in scenario.setup]
        steps.append(f"wait for open '{scenario.alert}' alerts to clear; snapshot alerts")
        steps += [f"kubectl {' '.join(a)}" for a in scenario.inject]
        steps += [
            f"poll GET {API}/alerts until '{scenario.alert}' fires (≤ {scenario.timeout_s}s)",
            f"POST {API}/alerts/{{id}}/triage?force=true",
            f"GET {API}/alerts/{{id}}/chat → grade",
            "reject pending writes",
        ]
        steps += [f"kubectl {' '.join(a)}" for a in scenario.cleanup]
        steps.append(f"POST {API}/aiops/evals/runs")
        return steps

    # ── HTTP helpers ─────────────────────────────────────────────────────────

    def _alerts(self) -> list[dict[str, Any]]:
        r = self.http.get(f"{API}/alerts", params={"limit": 200})
        r.raise_for_status()
        return r.json()

    def _matching(self, name: str) -> list[dict[str, Any]]:
        return [a for a in self._alerts() if alert_matches(a, name)]

    def _chat(self, alert_id: str) -> list[dict[str, Any]]:
        r = self.http.get(f"{API}/alerts/{alert_id}/chat")
        r.raise_for_status()
        return r.json()

    def _kubectl_all(self, cmds: list[list[str]], phase: str, details: dict[str, Any],
                     *, stop_on_error: bool) -> bool:
        ok = True
        for args in cmds:
            rc, out = self.kubectl(args)
            details["kubectl"].append({"phase": phase, "args": args, "rc": rc, "output": out[-2000:]})
            self.log(f"[{phase}] kubectl {' '.join(args)} → rc={rc}")
            if rc != 0:
                ok = False
                details["errors"].append(f"{phase}: kubectl {' '.join(args)} failed (rc={rc}): {out[-500:]}")
                if stop_on_error:
                    return False
        return ok

    # ── the run ──────────────────────────────────────────────────────────────

    def wait_for_alert(self, name: str, snapshot: dict[str, Any], deadline: float,
                       poll_s: float) -> dict[str, Any] | None:
        while True:
            for a in self._matching(name):
                if a.get("status") in ("resolved", "suppressed"):
                    continue
                aid = str(a.get("id"))
                if aid not in snapshot or snapshot[aid] != a.get("last_seen_at"):
                    return a
            if self.clock() >= deadline:
                return None
            self.sleep(poll_s)

    def run(self, scenario: Scenario, opts: RunOptions | None = None) -> dict[str, Any]:
        opts = opts or RunOptions()
        started_at = _now_iso()
        details: dict[str, Any] = {"kubectl": [], "errors": [], "warnings": []}
        alert_id: str | None = None
        result_grade: dict[str, Any] | None = None
        try:
            if not self._kubectl_all(scenario.setup, "setup", details, stop_on_error=True):
                raise RuntimeError("setup failed")

            # Let a still-firing alert from an earlier run clear first.
            clear_deadline = self.clock() + opts.clear_timeout_s
            while True:
                still = [a for a in self._matching(scenario.alert)
                         if a.get("status") not in ("resolved", "suppressed")]
                if not still or self.clock() >= clear_deadline:
                    break
                self.sleep(opts.poll_interval_s)
            if still:
                details["warnings"].append(
                    f"'{scenario.alert}' was still open before inject; matching by re-fire"
                )
            snapshot = {str(a.get("id")): a.get("last_seen_at") for a in self._matching(scenario.alert)}

            t0 = self.clock()
            if not self._kubectl_all(scenario.inject, "inject", details, stop_on_error=True):
                raise RuntimeError("inject failed")

            alert = self.wait_for_alert(
                scenario.alert, snapshot, t0 + scenario.timeout_s, opts.poll_interval_s
            )
            if alert is None:
                raise RuntimeError(f"alert '{scenario.alert}' did not fire within {scenario.timeout_s}s")
            alert_id = str(alert["id"])
            details["alert_fired_s"] = round(self.clock() - t0, 1)
            self.log(f"alert {alert_id} fired after {details['alert_fired_s']}s; triaging")

            n_before = len(self._chat(alert_id))
            r = self.http.post(
                f"{API}/alerts/{alert_id}/triage", params={"force": "true"},
                timeout=httpx.Timeout(float(max(120, scenario.timeout_s))),
            )
            r.raise_for_status()
            elapsed = round(self.clock() - t0, 1)
            transcript = self._chat(alert_id)[n_before:]
            result_grade = grade(transcript, scenario, triage_s=elapsed)
            details["transcript"] = _compact(transcript)

            # Which model actually answered (contamination check).
            model = ""
            ri = self.http.get(f"{API}/aiops/investigations/by-alert/{alert_id}")
            if ri.status_code == 200:
                model = str(ri.json().get("model") or "")
            details["model"] = model
            if opts.require_model and opts.require_model.lower() not in model.lower():
                details["invalid"] = True
                details["invalid_reason"] = (
                    f"answered by model {model or '(unknown)'!r}, expected {opts.require_model!r}"
                )

            if opts.reject_pending:
                for m in transcript:
                    for a in m.get("actions") or []:
                        if a.get("requires_approval") and a.get("status") == "pending":
                            rr = self.http.post(f"{API}/alerts/{alert_id}/actions/{a['id']}/reject")
                            if rr.status_code >= 400:
                                details["warnings"].append(f"could not reject action {a['id']}")
        except (RuntimeError, httpx.HTTPError, KeyError, ValueError) as e:
            details["errors"].append(str(e))
            self.log(f"error: {e}")
        finally:
            self._kubectl_all(scenario.cleanup, "cleanup", details, stop_on_error=False)

        if result_grade is not None:
            details.update({
                "checks": result_grade["checks"],
                "root_cause": result_grade["root_cause"],
                "tools_called": result_grade["tools_called"],
                "writes_proposed": result_grade["writes_proposed"],
                "triage_s": result_grade["triage_s"],
            })
        score = result_grade["score"] if result_grade else 0.0
        passed = bool(result_grade and result_grade["passed"]) and not details.get("invalid")
        run = {
            "scenario_id": scenario.id,
            "label": opts.label,
            "alert_id": alert_id,
            "started_at": started_at,
            "finished_at": _now_iso(),
            "score": score,
            "passed": passed,
            "details": json.loads(json.dumps(details, default=str)),
        }
        if opts.record:
            try:
                rr = self.http.post(f"{API}/aiops/evals/runs", json=run)
                rr.raise_for_status()
                run["id"] = rr.json().get("id")
            except httpx.HTTPError as e:
                run["record_error"] = str(e)
                self.log(f"could not record the run: {e}")
        return run
