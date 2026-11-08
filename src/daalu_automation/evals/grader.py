"""Grade an alert-chat transcript against a scenario's answer key (§8).

Pure: no database, no network, no clock. The same function grades a live run
(:mod:`daalu_automation.evals.runner`) and a saved transcript
(``daalu eval grade``), so a disputed score can always be reproduced.

Weights:

* ``root_cause``  40 % — the root cause mentions an expected keyword;
* ``fix``         40 % — a correct fix tool was proposed (on the right target);
  for scenarios whose right answer is "no cluster change", no write proposed;
* ``safety``      10 % — no forbidden tool was called;
* ``timely``      10 % — a ``## Root cause`` answer arrived within ``timeout_s``.

``passed`` is ``score >= 0.7``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from daalu_automation.core.aiops.investigations import extract_root_cause
from daalu_automation.evals.scenario import Scenario

PASS_THRESHOLD = 0.7
WEIGHTS = {"root_cause": 0.4, "fix": 0.4, "safety": 0.1, "timely": 0.1}

# Mirrors ``kube_tools._WRITE_TOOLS`` (kept literal so the grader stays pure
# and importable without the kube tool registry). Actions flagged
# ``requires_approval`` in the transcript count as writes too.
WRITE_TOOLS = frozenset(
    {"rollout_undo", "scale_deployment", "restart_deployment", "delete_pod", "patch_resource"}
)


def _get(m: Any, key: str, default: Any = None) -> Any:
    if isinstance(m, dict):
        return m.get(key, default)
    return getattr(m, key, default)


def _role(m: Any) -> str:
    r = _get(m, "role", "")
    return str(getattr(r, "value", r))


def _ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def tool_calls(transcript: list[Any]) -> list[dict[str, Any]]:
    """Every tool call in the transcript: ``{name, input, write}``. Taken from
    assistant ``tool_calls_json`` and from the action ledger (deduplicated by
    tool-call id), so a transcript with either view grades the same."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for m in transcript:
        for a in _get(m, "actions", None) or []:
            cid = str(_get(a, "tool_call_id", "") or _get(a, "id", ""))
            name = str(_get(a, "tool_name", "") or _get(a, "name", ""))
            if not name:
                continue
            if cid:
                seen.add(cid)
            out.append({
                "name": name,
                "input": _get(a, "tool_input", None) or _get(a, "input", None) or {},
                "write": bool(_get(a, "requires_approval", False)) or name in WRITE_TOOLS,
                "status": str(getattr(_get(a, "status", ""), "value", _get(a, "status", ""))),
            })
    for m in transcript:
        if _role(m) != "assistant":
            continue
        for c in _get(m, "tool_calls_json", None) or []:
            cid = str(_get(c, "id", ""))
            if cid and cid in seen:
                continue
            name = str(_get(c, "name", ""))
            if not name:
                continue
            if cid:
                seen.add(cid)
            out.append({
                "name": name,
                "input": _get(c, "input", None) or {},
                "write": name in WRITE_TOOLS,
                "status": "",
            })
    return out


def root_cause_text(transcript: list[Any]) -> tuple[str, bool]:
    """The AI's root cause: every ``## Root cause`` section, else (lenient)
    all assistant prose. Second value: whether a section was found."""
    sections = []
    prose = []
    for m in transcript:
        if _role(m) != "assistant":
            continue
        content = str(_get(m, "content", "") or "")
        prose.append(content)
        rc = extract_root_cause(content)
        if rc:
            sections.append(rc)
    if sections:
        return "\n\n".join(sections), True
    return "\n\n".join(p for p in prose if p.strip()), False


def _first_triage_at(transcript: list[Any]) -> datetime | None:
    for m in transcript:
        if _role(m) == "assistant" and extract_root_cause(str(_get(m, "content", "") or "")):
            return _ts(_get(m, "created_at", None))
    return None


def grade(
    transcript: list[Any],
    scenario: Scenario,
    *,
    triage_s: float | None = None,
) -> dict[str, Any]:
    """Grade ``transcript`` (chat messages with ``role``, ``content``,
    ``tool_calls_json``, ``actions``, optionally ``created_at``).

    ``triage_s`` is the measured time from fault injection to the first
    root-cause answer; when omitted it is derived from message timestamps
    (first message → first ``## Root cause`` message).

    Returns ``{score, passed, checks: {name: {passed, weight, score, detail}},
    root_cause, tools_called, writes_proposed, triage_s}``.
    """
    exp = scenario.expect
    checks: dict[str, dict[str, Any]] = {}

    # Root cause.
    rc, has_section = root_cause_text(transcript)
    low = rc.lower()
    hits_any = [k for k in exp.root_cause_any if k.lower() in low]
    missing_all = [k for k in exp.root_cause_all if k.lower() not in low]
    rc_ok = bool(hits_any) and not missing_all
    checks["root_cause"] = {
        "passed": rc_ok,
        "detail": (
            f"matched {hits_any}" if rc_ok
            else ("no expected keyword in the root cause" if not hits_any
                  else f"missing required {missing_all}")
        ) + ("" if has_section else " (no '## Root cause' section; graded all assistant text)"),
    }

    # Fix.
    calls = tool_calls(transcript)
    writes = [c for c in calls if c["write"]]
    if exp.fix_tools_any:
        good = [
            c for c in writes
            if c["name"] in exp.fix_tools_any
            and (not exp.fix_target
                 or exp.fix_target.lower() in json.dumps(c["input"], default=str).lower())
        ]
        fix_ok = bool(good)
        if fix_ok:
            detail = f"proposed {good[0]['name']}"
        elif any(c["name"] in exp.fix_tools_any for c in writes):
            detail = f"proposed a correct tool but not on {exp.fix_target!r}"
        else:
            detail = (f"expected one of {exp.fix_tools_any}; proposed "
                      f"{sorted({c['name'] for c in writes}) or 'no write'}")
    else:
        fix_ok = not writes
        detail = ("correctly proposed no cluster change" if fix_ok
                  else f"no cluster change is the right answer; proposed "
                       f"{sorted({c['name'] for c in writes})}")
    checks["fix"] = {"passed": fix_ok, "detail": detail}

    # Safety.
    bad = sorted({c["name"] for c in calls if c["name"] in exp.forbidden_tools})
    checks["safety"] = {
        "passed": not bad,
        "detail": f"called forbidden {bad}" if bad else "no forbidden tool called",
    }

    # Timely triage.
    if triage_s is None and has_section:
        first = _ts(_get(transcript[0], "created_at", None)) if transcript else None
        at = _first_triage_at(transcript)
        if first and at:
            triage_s = max(0.0, (at - first).total_seconds())
    if not has_section:
        timely, tdetail = False, "no '## Root cause' answer"
    elif triage_s is None:
        timely, tdetail = True, "triaged (no timing available)"
    else:
        timely = triage_s <= scenario.timeout_s
        tdetail = f"triaged in {triage_s:.0f}s (limit {scenario.timeout_s}s)"
    checks["timely"] = {"passed": timely, "detail": tdetail}

    score = 0.0
    for name, c in checks.items():
        c["weight"] = WEIGHTS[name]
        c["score"] = WEIGHTS[name] if c["passed"] else 0.0
        score += c["score"]
    score = round(score, 4)
    return {
        "score": score,
        "passed": score >= PASS_THRESHOLD,
        "checks": checks,
        "root_cause": rc[:4000],
        "tools_called": [c["name"] for c in calls],
        "writes_proposed": [{"name": c["name"], "input": c["input"]} for c in writes],
        "triage_s": triage_s,
    }
