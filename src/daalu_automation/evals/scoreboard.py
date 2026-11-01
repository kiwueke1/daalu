"""Leaderboard aggregation over eval runs (pure).

One row per model ``label``: how many runs, mean score, pass rate, how many
distinct scenarios it was run on, plus a per-scenario breakdown (so a
scoreboard can show exactly which outage a model lost on) and the median
time to diagnosis. Sorted best first.
"""

from __future__ import annotations

from statistics import median
from typing import Any


def _get(r: Any, key: str, default: Any = None) -> Any:
    if isinstance(r, dict):
        return r.get(key, default)
    return getattr(r, key, default)


def _summ(runs: list[Any]) -> dict[str, Any]:
    n = len(runs)
    scores = [float(_get(r, "score", 0.0) or 0.0) for r in runs]
    passed = sum(1 for r in runs if _get(r, "passed", False))
    ttd = []
    for r in runs:
        d = _get(r, "details", None) or {}
        v = d.get("diagnosis_s") if isinstance(d, dict) else None
        if isinstance(v, (int, float)):
            ttd.append(float(v))
    return {
        "runs": n,
        "mean_score": round(sum(scores) / n, 4) if n else None,
        "pass_rate": round(passed / n, 4) if n else None,
        "median_time_to_diagnosis_s": median(ttd) if ttd else None,
    }


def leaderboard(runs: list[Any]) -> list[dict[str, Any]]:
    """``[{label, runs, mean_score, pass_rate, scenarios, per_scenario,
    median_time_to_diagnosis_s}]``. Runs whose ``details.invalid`` is true
    (e.g. answered by the wrong model) are excluded."""
    by_label: dict[str, list[Any]] = {}
    for r in runs:
        d = _get(r, "details", None) or {}
        if isinstance(d, dict) and d.get("invalid"):
            continue
        by_label.setdefault(str(_get(r, "label", "") or ""), []).append(r)
    rows = []
    for label, lr in by_label.items():
        by_sc: dict[str, list[Any]] = {}
        for r in lr:
            by_sc.setdefault(str(_get(r, "scenario_id", "")), []).append(r)
        rows.append({
            "label": label,
            **_summ(lr),
            "scenarios": len(by_sc),
            "per_scenario": {sid: _summ(sr) for sid, sr in sorted(by_sc.items())},
        })
    rows.sort(key=lambda x: (-(x["mean_score"] or 0.0), -(x["pass_rate"] or 0.0), x["label"]))
    return rows
