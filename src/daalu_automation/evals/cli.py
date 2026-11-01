"""``daalu eval`` — run AI-SRE eval scenarios and read the leaderboard (§8).

    daalu eval list
    daalu eval run dummy-app-bad-image --label qwen3-32b-local --kubeconfig ~/.kube/demo
    daalu eval run-all --label frontier-api --repeat 5
    daalu eval grade transcript.json dummy-app-bad-image     # offline
    daalu eval leaderboard
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import typer

from daalu_automation.evals.grader import grade as grade_transcript
from daalu_automation.evals.runner import EvalRunner, RunOptions, make_kubectl
from daalu_automation.evals.scenario import (
    Scenario,
    ScenarioError,
    get_scenario,
    load_scenarios,
)

app = typer.Typer(help="Run AI-SRE eval scenarios (AI-Ops Maturity Level 4).")

_URL = typer.Option(None, "--url", help="Daalu base URL (default $DAALU_URL).")
_TOKEN = typer.Option(None, "--token", help="API token (default $DAALU_TOKEN).")
_DIR = typer.Option(None, "--dir", help="Scenarios directory (default $DAALU_EVALS_DIR or evals/scenarios).")


def _scenarios(directory: Path | None) -> list[Scenario]:
    try:
        return load_scenarios(directory)
    except ScenarioError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from e


def _creds(url: str | None, token: str | None, *, need: bool = True) -> tuple[str, str]:
    url = url or os.environ.get("DAALU_URL", "") or os.environ.get("DAALU_API", "")
    token = token or os.environ.get("DAALU_TOKEN", "")
    if need and (not url or not token):
        typer.secho("Set --url/--token or DAALU_URL/DAALU_TOKEN.", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    return url, token


@app.command("list")
def list_cmd(directory: Path | None = _DIR) -> None:
    """List the scenarios."""
    scs = _scenarios(directory)
    if not scs:
        typer.echo("No scenarios found.")
        return
    w = max(len(s.id) for s in scs)
    for s in scs:
        fix = ", ".join(s.expect.fix_tools_any) or "(no cluster change)"
        typer.echo(f"{s.id:<{w}}  alert={s.alert:<16} fix={fix:<28} {s.title}")


def _print_run(run: dict[str, Any]) -> None:
    d = run.get("details") or {}
    mark = "PASS" if run["passed"] else "FAIL"
    color = typer.colors.GREEN if run["passed"] else typer.colors.RED
    typer.secho(f"{run['scenario_id']}: {mark} score={run['score']:.2f}", fg=color, bold=True)
    for name, c in (d.get("checks") or {}).items():
        typer.echo(f"  {'✔' if c['passed'] else '✘'} {name:<10} {c['score']:.1f}/{c['weight']:.1f}  {c['detail']}")
    if d.get("invalid"):
        typer.secho(f"  INVALID: {d.get('invalid_reason')}", fg=typer.colors.YELLOW)
    for e in d.get("errors") or []:
        typer.secho(f"  error: {e}", fg=typer.colors.RED)
    if run.get("record_error"):
        typer.secho(f"  not recorded: {run['record_error']}", fg=typer.colors.YELLOW)


def _run_many(
    scenarios: list[Scenario], *, label: str, kubeconfig: str | None, context: str | None,
    url: str | None, token: str | None, dry_run: bool, repeat: int, keep_pending: bool,
    require_model: str, record: bool, save_dir: Path | None,
) -> list[dict[str, Any]]:
    if dry_run:
        for s in scenarios:
            typer.secho(f"{s.id} (dry run):", bold=True)
            for i, step in enumerate(EvalRunner.plan(s), 1):
                typer.echo(f"  {i:>2}. {step}")
        return []
    url, token = _creds(url, token)
    runner = EvalRunner.connect(
        url, token, make_kubectl(kubeconfig, context),
        log=lambda m: typer.echo(f"  · {m}"),
    )
    opts = RunOptions(label=label, reject_pending=not keep_pending,
                      require_model=require_model, record=record)
    runs = []
    for s in scenarios:
        for i in range(repeat):
            typer.secho(f"▶ {s.id} [{label or 'unlabelled'}] run {i + 1}/{repeat}", bold=True)
            run = runner.run(s, opts)
            _print_run(run)
            if save_dir:
                save_dir.mkdir(parents=True, exist_ok=True)
                stamp = run["started_at"].replace(":", "").replace("+", "_")
                (save_dir / f"{s.id}-{label or 'unlabelled'}-{stamp}.json").write_text(
                    json.dumps(run, indent=2, default=str)
                )
            runs.append(run)
    return runs


_LABEL = typer.Option("", "--label", help="Model label to score this run under (e.g. qwen3-32b-local).")
_KUBECONFIG = typer.Option(None, "--kubeconfig", help="kubeconfig for the cluster under test.")
_CONTEXT = typer.Option(None, "--context", help="kubectl context (e.g. kind-daalu-demo).")
_DRY = typer.Option(False, "--dry-run", help="Print the steps without touching the cluster or API.")
_REPEAT = typer.Option(1, "--repeat", min=1, max=50, help="Runs per scenario (report medians from N ≥ 5).")
_KEEP = typer.Option(False, "--keep-pending", help="Don't reject the writes the AI proposed.")
_REQ = typer.Option("", "--require-model", help="Mark the run invalid unless the investigation's model contains this.")
_NOREC = typer.Option(False, "--no-record", help="Don't POST the run to Daalu.")
_SAVE = typer.Option(None, "--save", help="Directory to save each run (with transcript) as JSON.")


@app.command("run")
def run_cmd(
    scenario_id: str,
    label: str = _LABEL, kubeconfig: str | None = _KUBECONFIG, context: str | None = _CONTEXT,
    url: str | None = _URL, token: str | None = _TOKEN, dry_run: bool = _DRY,
    repeat: int = _REPEAT, keep_pending: bool = _KEEP, require_model: str = _REQ,
    no_record: bool = _NOREC, save: Path | None = _SAVE, directory: Path | None = _DIR,
) -> None:
    """Break the cluster per the scenario, let the AI triage, grade, clean up, record."""
    try:
        sc = get_scenario(scenario_id, directory)
    except ScenarioError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from e
    runs = _run_many([sc], label=label, kubeconfig=kubeconfig, context=context, url=url,
                     token=token, dry_run=dry_run, repeat=repeat, keep_pending=keep_pending,
                     require_model=require_model, record=not no_record, save_dir=save)
    if runs and not all(r["passed"] for r in runs):
        raise typer.Exit(1)


@app.command("run-all")
def run_all_cmd(
    label: str = _LABEL, kubeconfig: str | None = _KUBECONFIG, context: str | None = _CONTEXT,
    url: str | None = _URL, token: str | None = _TOKEN, dry_run: bool = _DRY,
    repeat: int = _REPEAT, keep_pending: bool = _KEEP, require_model: str = _REQ,
    no_record: bool = _NOREC, save: Path | None = _SAVE, directory: Path | None = _DIR,
    tag: str | None = typer.Option(None, "--tag", help="Only scenarios with this tag."),
) -> None:
    """Run every scenario in turn."""
    scs = [s for s in _scenarios(directory) if not tag or tag in s.tags]
    runs = _run_many(scs, label=label, kubeconfig=kubeconfig, context=context, url=url,
                     token=token, dry_run=dry_run, repeat=repeat, keep_pending=keep_pending,
                     require_model=require_model, record=not no_record, save_dir=save)
    if runs:
        mean = sum(r["score"] for r in runs) / len(runs)
        passed = sum(1 for r in runs if r["passed"])
        typer.secho(f"\n{label or 'unlabelled'}: {passed}/{len(runs)} passed, mean score {mean:.2f}",
                    bold=True)


@app.command("grade")
def grade_cmd(
    transcript: Path, scenario_id: str, directory: Path | None = _DIR,
    as_json: bool = typer.Option(False, "--json", help="Print the full grade as JSON."),
) -> None:
    """Re-grade a saved transcript offline. Accepts a list of chat messages,
    ``{"messages": [...]}``, or a run saved with ``--save``."""
    try:
        sc = get_scenario(scenario_id, directory)
        raw = json.loads(transcript.read_text())
    except (ScenarioError, OSError, json.JSONDecodeError) as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from e
    diagnosis_s = None
    if isinstance(raw, dict):
        details = raw.get("details") or {}
        diagnosis_s = raw.get("diagnosis_s", details.get("diagnosis_s"))
        raw = raw.get("messages") or details.get("transcript") or []
    if not isinstance(raw, list):
        typer.secho("transcript must be a list of messages", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    g = grade_transcript(raw, sc, diagnosis_s=diagnosis_s)
    if as_json:
        typer.echo(json.dumps(g, indent=2, default=str))
    else:
        _print_run({"scenario_id": sc.id, "score": g["score"], "passed": g["passed"],
                    "details": {"checks": g["checks"]}})
    if not g["passed"]:
        raise typer.Exit(1)


@app.command("leaderboard")
def leaderboard_cmd(
    url: str | None = _URL, token: str | None = _TOKEN,
    days: int | None = typer.Option(None, "--days", help="Only runs from the last N days."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show the per-model leaderboard from Daalu."""
    url, token = _creds(url, token)
    params = {"days": days} if days else {}
    r = httpx.get(f"{url.rstrip('/')}/api/v1/aiops/evals/leaderboard", params=params,
                  headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    rows = r.json()
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("No eval runs yet.")
        return
    w = max(5, *(len(x["label"] or "(none)") for x in rows))
    typer.echo(f"{'label':<{w}}  runs  scenarios  mean   pass   median TTD")
    for x in rows:
        ttd = x.get("median_time_to_diagnosis_s")
        typer.echo(
            f"{x['label'] or '(none)':<{w}}  {x['runs']:>4}  {x['scenarios']:>9}  "
            f"{(x['mean_score'] or 0):.2f}  {(x['pass_rate'] or 0) * 100:>4.0f}%  "
            f"{'—' if ttd is None else f'{ttd:.0f}s'}"
        )
