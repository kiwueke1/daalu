"""``daalu fault`` — break a lab cluster or fabric on purpose, then put it back.

    daalu fault                                  # guided: pick an environment, a fault, a target
    daalu fault list [--env openstack-lab]
    daalu fault show kubernetes/bad-image
    daalu fault plan openstack/mariadb-down --env openstack-lab
    daalu fault inject openstack/mariadb-down --env openstack-lab --ttl 30m --watch
    daalu fault inject kubernetes/oom-limit --env lab --target keystone --set limit=16Mi
    daalu fault status                           # active faults, and whether each is fixed yet
    daalu fault watch f-1a2b3c                   # wait for the fix; prints time to recover
    daalu fault revert f-1a2b3c | --all
    daalu fault reap                             # revert every fault past its --ttl
    daalu fault init cluster openstack-lab --kubeconfig ~/.kube/lab -n openstack
    daalu fault init clab spectrum-x --lab daalu-spectrum-x
    daalu fault drill openstack/gameday --env openstack-lab   # a game day: many incidents in a row
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from daalu_automation.faults.catalog import (
    faults_home,
    inventory_path,
    load_drills,
    load_faults,
    load_inventory,
    load_recipes,
)
from daalu_automation.faults.engine import (
    Engine,
    FaultError,
    Journal,
    Plan,
    parse_duration,
    parse_iso,
)
from daalu_automation.faults.model import (
    Cluster,
    Fabric,
    Fault,
    FaultFileError,
    Inventory,
    Recipe,
    nos_ok,
)

app = typer.Typer(
    help="Inject revertible faults into lab clusters and fabrics so Daalu has a real incident "
    "to fix. Runs with your own kubeconfig/SSH/docker; never through Daalu.",
    invoke_without_command=True,
)
console = Console()

_INV = typer.Option(None, "--inventory", "-i", help="Inventory file (default ~/.daalu/faults/inventory.yaml).")
_ENV = typer.Option(None, "--env", "-e", help="Environment name from the inventory.")


def _die(msg: str, code: int = 2) -> None:
    console.print(f"[red]✘[/red] {msg}")
    raise typer.Exit(code)


def _catalog() -> tuple[dict[str, Fault], dict[str, Recipe]]:
    try:
        faults = load_faults()
        recipes = load_recipes(faults=faults)
    except FaultFileError as e:
        _die(str(e))
    clash = set(faults) & set(recipes)
    if clash:
        _die(f"fault and recipe share a name: {', '.join(sorted(clash))}")
    return faults, recipes


def _inventory(path: Path | None) -> Inventory:
    try:
        return load_inventory(path)
    except FaultFileError as e:
        _die(str(e))
    raise AssertionError


def _journal() -> Journal:
    return Journal(faults_home() / "journal.json")


def _resolve(name: str, faults: dict[str, Fault], recipes: dict[str, Recipe]) -> tuple[Fault, Recipe | None]:
    if name in recipes:
        r = recipes[name]
        return faults[r.fault], r
    if name in faults:
        return faults[name], None
    bare = [k for k in [*faults, *recipes] if k.split("/", 1)[1] == name]
    if len(bare) == 1:
        return _resolve(bare[0], faults, recipes)
    if bare:
        _die(f"{name!r} is ambiguous: {', '.join(bare)}")
    _die(f"no fault or recipe {name!r}. `daalu fault list` shows them.")
    raise AssertionError


def _sets(values: list[str] | None) -> dict[str, str]:
    out = {}
    for s in values or []:
        if "=" not in s:
            _die(f"--set wants key=value, got {s!r}")
        k, v = s.split("=", 1)
        out[k.strip()] = v
    return out


def _ago(iso: str) -> str:
    secs = int(time.time() - parse_iso(iso).timestamp())
    return f"{secs // 60}m{secs % 60:02d}s" if secs < 3600 else f"{secs // 3600}h{secs % 3600 // 60:02d}m"


def _between(a: str, b: str) -> str:
    secs = int(parse_iso(b).timestamp() - parse_iso(a).timestamp())
    return f"{secs // 60}m{secs % 60:02d}s"


def _env_ok(fault: Fault, env: Cluster | Fabric) -> bool:
    if fault.scope == "cluster":
        return isinstance(env, Cluster)
    return isinstance(env, Fabric) and nos_ok(fault, env.nos) and (
        "containerlab" not in fault.requires or env.transport == "containerlab")


# --- printing ------------------------------------------------------------------

def _print_plan(plan: Plan) -> None:
    f = plan.fault
    head = f"[bold]{f.ref}[/bold] · {f.title}"
    if plan.recipe:
        head = f"[bold]{plan.recipe.group}/{plan.recipe.id}[/bold] · {plan.recipe.title}  [dim]({f.ref})[/dim]"
    console.print(head)
    console.print(f"  environment [cyan]{plan.env_name}[/cyan] · blast radius [yellow]{f.blast}[/yellow]"
                  + (f" · target [cyan]{plan.target}[/cyan]" if plan.target else ""))
    params = {k: v for k, v in plan.vars.items() if k in f.params}
    if params:
        console.print("  params " + "  ".join(f"{k}=[green]{v or '∅'}[/green]" for k, v in params.items()))
    pv = plan.preview()
    for phase in ("prepare", "inject", "revert", "check"):
        if pv[phase]:
            console.print(f"  [bold]{phase}[/bold]")
            for line in pv[phase]:
                console.print(f"    • {line}", markup=False, highlight=False)
    if f.self_heals:
        console.print("  [dim]self-heals: nothing to revert[/dim]")
    exp = plan.recipe.expect if plan.recipe and (plan.recipe.expect.root_cause or plan.recipe.expect.fix) else f.expect
    if exp.root_cause or exp.fix:
        console.print(f"  [bold]answer key[/bold] root cause: {exp.root_cause}")
        console.print(f"             fix: {exp.fix}"
                      + ("" if exp.daalu_can_fix else "  [yellow](escalate: Daalu has no tool for this)[/yellow]"))
    for w in plan.warnings:
        console.print(f"  [yellow]⚠ {w}[/yellow]")


# --- commands --------------------------------------------------------------------

@app.callback()
def _root(ctx: typer.Context, inventory: Path | None = _INV) -> None:
    if ctx.invoked_subcommand is None:
        _wizard(inventory)


@app.command("list")
def list_cmd(env: str | None = _ENV, inventory: Path | None = _INV,
             group: str | None = typer.Option(None, "--group", "-g")) -> None:
    """Faults and recipes (filtered to one environment with --env)."""
    faults, recipes = _catalog()
    e = _inventory(inventory).environments.get(env) if env else None
    if env and e is None:
        _die(f"no environment {env!r} in the inventory")
    t = Table(title="Recipes (episode-ready: fault + target + story)", show_lines=False)
    for c in ("recipe", "fault", "target", "title"):
        t.add_column(c)
    for k, r in sorted(recipes.items()):
        if group and r.group != group:
            continue
        if e is not None and (not _env_ok(faults[r.fault], e) or (r.target and r.target not in e.targets)):
            continue
        t.add_row(k, r.fault, r.target or "-", r.title)
    console.print(t)
    t = Table(title="Faults (generic: give a --target or --set the params)")
    for c in ("fault", "blast", "scope", "title"):
        t.add_column(c)
    for k, f in sorted(faults.items()):
        if group and f.group != group:
            continue
        if e is not None and not _env_ok(f, e):
            continue
        t.add_row(k, f.blast, f.scope if f.scope == "cluster" else f"fabric ({','.join(f.nos)})", f.title)
    console.print(t)


@app.command()
def show(name: str) -> None:
    """A fault's params, steps and answer key."""
    faults, recipes = _catalog()
    f, r = _resolve(name, faults, recipes)
    if r:
        console.print(f"[bold]recipe {r.group}/{r.id}[/bold] · {r.title}\n  fault {r.fault} · target {r.target or '-'}"
                      f" · params {r.params or '-'}")
        if r.story:
            console.print(f"  {r.story.strip()}")
        if r.episodes:
            console.print(f"  episodes: {', '.join(r.episodes)}")
        console.print()
    console.print(f"[bold]{f.ref}[/bold] · {f.title}  [dim]scope={f.scope} blast={f.blast}"
                  + (f" nos={','.join(f.nos)}" if f.nos else "")
                  + (f" requires={','.join(f.requires)}" if f.requires else "") + "[/dim]")
    if f.description:
        console.print(f"  {f.description.strip()}")
    for k, p in f.params.items():
        d = "required" if p.default is None else f"default {p.default!r}"
        extra = f" one of {p.choices}" if p.choices else ""
        console.print(f"  [green]{k}[/green] ({p.type}{extra}; {d}) {p.help}")
    for phase, steps in (("inject", f.inject), ("revert", f.revert)):
        console.print(f"  [bold]{phase}[/bold]")
        for s in steps:
            body = s.model_dump(exclude_none=True, exclude_defaults=True)
            console.print(f"    • {json.dumps(body)}", markup=False, highlight=False)
    if f.warning:
        console.print(f"  [yellow]⚠ {f.warning}[/yellow]")


def _make_plan(name: str, env: str | None, target: str | None, sets: list[str] | None,
               inventory: Path | None) -> tuple[Engine, Plan]:
    faults, recipes = _catalog()
    f, r = _resolve(name, faults, recipes)
    inv = _inventory(inventory)
    if not env:
        fitting = [n for n, e in inv.environments.items() if _env_ok(f, e)]
        if len(fitting) != 1:
            _die(f"give --env ({', '.join(fitting) or 'no environment fits this fault'})")
        env = fitting[0]
    eng = Engine(inv, _journal())
    try:
        return eng, eng.plan(f, env, target=target or "", recipe=r, overrides=_sets(sets))
    except FaultError as e:
        _die(str(e))
    raise AssertionError


_TARGET = typer.Option(None, "--target", "-t", help="Named target in the environment.")
_SET = typer.Option(None, "--set", "-s", help="Param override key=value (repeatable).")


@app.command()
def plan(name: str, env: str | None = _ENV, target: str | None = _TARGET,
         set_: list[str] | None = _SET, inventory: Path | None = _INV) -> None:
    """Show exactly what inject would run, without running it."""
    _, p = _make_plan(name, env, target, set_, inventory)
    _print_plan(p)


@app.command()
def inject(
    name: str,
    env: str | None = _ENV,
    target: str | None = _TARGET,
    set_: list[str] | None = _SET,
    inventory: Path | None = _INV,
    ttl: str = typer.Option("", "--ttl", help="Auto-revert deadline for `reap`/`watch`, e.g. 30m."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation."),
    watch_: bool = typer.Option(False, "--watch", "-w", help="Then wait for the fix (see `watch`)."),
    note: str = typer.Option("", "--note", help="Free text kept in the journal (e.g. the episode)."),
) -> None:
    """Break it. Prints the fault id to revert with."""
    eng, p = _make_plan(name, env, target, set_, inventory)
    _print_plan(p)
    _do_inject(eng, p, ttl=ttl, yes=yes, note=note, watch_=watch_)


def _do_inject(eng: Engine, p: Plan, *, ttl: str, yes: bool, note: str, watch_: bool) -> None:
    if not yes and not Confirm.ask(f"Inject into [cyan]{p.env_name}[/cyan]?", default=False):
        raise typer.Exit(1)
    try:
        entry = eng.inject(p, ttl=ttl, note=note)
    except FaultError as e:
        _die(str(e), 1)
    console.print(f"[green]✔[/green] injected [bold]{entry['id']}[/bold] at {entry['injected_at']}"
                  + (f", auto-revert after {entry['expires_at']}" if entry["expires_at"] else ""))
    console.print(f"  undo: [bold]daalu fault revert {entry['id']}[/bold]   ·   "
                  f"progress: [bold]daalu fault watch {entry['id']}[/bold]")
    if watch_:
        _watch(eng, entry["id"], interval=10, timeout="")


@app.command()
def status(all_: bool = typer.Option(False, "--all", "-a", help="Include reverted faults."),
           no_check: bool = typer.Option(False, "--no-check", help="Don't run health checks."),
           inventory: Path | None = _INV) -> None:
    """Active faults, and whether each one is fixed yet."""
    j = _journal()
    entries = j.load() if all_ else j.active()
    if not entries:
        console.print("No active faults." if not all_ else "The journal is empty.")
        return
    eng = Engine(Inventory(), j)
    t = Table()
    for c in ("id", "fault", "env", "target", "age", "state", "health"):
        t.add_column(c)
    for e in sorted(entries, key=lambda x: x["injected_at"]):
        health = "-"
        if e["state"] == "active" and not no_check:
            h = eng.check(e)
            health = {None: "[dim]no check[/dim]", True: "[green]healthy[/green]",
                      False: "[red]broken[/red]"}[h]
            if e.get("resolved_at"):
                health += f" (recovered in {_between(e['injected_at'], e['resolved_at'])})"
        state = e["state"]
        if e.get("expires_at") and state == "active" and parse_iso(e["expires_at"]).timestamp() < time.time():
            state = "[yellow]expired[/yellow] (daalu fault reap)"
        t.add_row(e["id"], e.get("recipe") or e["fault"], e["env"], e.get("target") or "-",
                  _ago(e["injected_at"]), state, health)
    console.print(t)


def _watch(eng: Engine, fid: str, *, interval: int, timeout: str) -> None:
    try:
        entry = eng.journal.get(fid)
    except FaultError as e:
        _die(str(e))
    if not entry.get("check_op"):
        _die(f"{entry['fault']} has no health check; watch Daalu instead")
    deadline = None
    if timeout:
        deadline = time.time() + parse_duration(timeout).total_seconds()
    console.print(f"Watching {fid} ({entry['fault']}) every {interval}s. Ctrl-C to stop watching (the fault stays).")
    try:
        while True:
            entry = eng.journal.get(fid)
            if entry["state"] != "active":
                console.print(f"{fid} is {entry['state']}.")
                return
            h = eng.check(entry)
            entry = eng.journal.get(fid)
            if entry.get("resolved_at"):
                console.print(f"[green]✔ recovered[/green] {_between(entry['injected_at'], entry['resolved_at'])} "
                              f"after injection. Run [bold]daalu fault revert {fid}[/bold] to close it "
                              "(restores the known-good state; safe even after a fix).")
                return
            console.print(f"  {time.strftime('%H:%M:%S')} {'healthy (not yet seen broken)' if h else 'broken'}"
                          f" · {_ago(entry['injected_at'])} since injection")
            if entry.get("expires_at") and parse_iso(entry["expires_at"]).timestamp() <= time.time():
                console.print("[yellow]TTL reached: reverting.[/yellow]")
                eng.revert(fid, reason="ttl")
                return
            if deadline and time.time() > deadline:
                console.print("[yellow]watch timeout; the fault is still active.[/yellow]")
                return
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print(f"\nStopped watching. {fid} is still active.")


@app.command()
def watch(fid: str, interval: int = typer.Option(10, "--interval"),
          timeout: str = typer.Option("", "--timeout", help="Stop watching after e.g. 20m.")) -> None:
    """Wait until the target is healthy again and print the time to recover."""
    _watch(Engine(Inventory(), _journal()), fid, interval=interval, timeout=timeout)


@app.command()
def revert(fid: str | None = typer.Argument(None),
           all_: bool = typer.Option(False, "--all", help="Revert every active fault.")) -> None:
    """Put the known-good state back (idempotent; safe after Daalu already fixed it)."""
    eng = Engine(Inventory(), _journal())
    ids = [e["id"] for e in eng.journal.active()] if all_ else ([fid] if fid else [])
    if not ids:
        _die("give a fault id or --all" if not all_ else "no active faults", 1 if all_ else 2)
    bad = 0
    for i in ids:
        try:
            e = eng.revert(i)
            console.print(f"[green]✔[/green] reverted {e['id']} ({e['fault']})")
        except FaultError as err:
            bad += 1
            console.print(f"[red]✘[/red] {err}")
    raise typer.Exit(1 if bad else 0)


@app.command()
def reap() -> None:
    """Revert every fault whose --ttl has passed (safe to run from cron)."""
    eng = Engine(Inventory(), _journal())
    exp = eng.expired()
    for e in exp:
        try:
            eng.revert(e["id"], reason="ttl")
            console.print(f"[green]✔[/green] reverted expired {e['id']} ({e['fault']})")
        except FaultError as err:
            console.print(f"[red]✘[/red] {err}")
    if not exp:
        console.print("Nothing expired.")


@app.command("log")
def log_cmd(fid: str) -> None:
    """Every command a fault ran, with its output."""
    try:
        e = _journal().get(fid)
    except FaultError as err:
        _die(str(err))
    for title, rows in (("inject", e.get("log") or []), ("revert", e.get("revert_log") or [])):
        console.print(f"[bold]{title}[/bold]")
        for r in rows:
            mark = "[green]✔[/green]" if r["rc"] == 0 else f"[red]✘ rc={r['rc']}[/red]"
            console.print(f"  {mark} ", end="")
            console.print(r["desc"], markup=False, highlight=False)
            for stream in ("out", "err"):
                if r.get(stream, "").strip():
                    console.print("      " + r[stream].strip().replace("\n", "\n      "),
                                  style="dim", markup=False, highlight=False)


# --- drills: a game day ---------------------------------------------------------

@app.command()
def drill(
    name: str = typer.Argument(..., help="group/id under faults/drills, or a path to a drill file."),
    env: str | None = _ENV,
    inventory: Path | None = _INV,
    yes: bool = typer.Option(False, "--yes", "-y"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan every incident; inject nothing."),
    interval: int = typer.Option(15, "--interval", help="Health-check period while waiting for a fix."),
) -> None:
    """Run a game day: inject each incident, wait for the fix, revert, record time to recover."""
    from daalu_automation.faults.catalog import _load
    from daalu_automation.faults.model import Drill

    faults, recipes = _catalog()
    if Path(name).is_file():
        try:
            d = _load(Path(name), Drill)
        except FaultFileError as e:
            _die(str(e))
    else:
        try:
            drills = load_drills()
        except FaultFileError as e:
            _die(str(e))
        if name not in drills:
            _die(f"no drill {name!r} (have: {', '.join(sorted(drills)) or 'none'})")
        d = drills[name]
    env_name = env or d.env
    if not env_name:
        _die("give --env (the drill names none)")
    inv = _inventory(inventory)
    eng = Engine(inv, _journal())
    plans: list[tuple[Plan, str]] = []
    for it in d.items:
        f, r = _resolve(it.recipe or it.fault, faults, recipes)
        try:
            plans.append((eng.plan(f, env_name, target=it.target, recipe=r, overrides=it.set),
                          it.timeout or d.timeout))
        except FaultError as e:
            _die(f"{it.recipe or it.fault}: {e}")
    console.print(f"[bold]{d.title}[/bold] · {len(plans)} incidents on [cyan]{env_name}[/cyan] · "
                  f"up to {d.timeout} each, {d.pause} apart")
    for i, (p, to) in enumerate(plans, 1):
        name_ = f"{p.recipe.group}/{p.recipe.id}" if p.recipe else p.fault.ref
        console.print(f"  {i:>2}. {name_}  [dim]({p.fault.blast}; wait ≤ {to})[/dim]")
    if dry_run:
        return
    if not yes and not Confirm.ask("Start the drill?", default=False):
        raise typer.Exit(1)
    results: list[dict[str, Any]] = []
    try:
        for i, (p, to) in enumerate(plans, 1):
            label = f"{p.recipe.group}/{p.recipe.id}" if p.recipe else p.fault.ref
            console.rule(f"{i}/{len(plans)} {label}")
            res: dict[str, Any] = {"incident": label, "fault": p.fault.ref}
            try:
                entry = eng.inject(p, ttl=to, note=f"drill {d.id}")
            except FaultError as e:
                console.print(f"[red]✘ inject failed:[/red] {e}")
                results.append({**res, "outcome": "inject failed"})
                continue
            fid = entry["id"]
            res.update(id=fid, injected_at=entry["injected_at"])
            deadline = time.time() + parse_duration(to).total_seconds()
            outcome = "not recovered"
            while time.time() < deadline:
                time.sleep(interval)
                cur = eng.journal.get(fid)
                if eng.check(cur) is None:
                    outcome = "no check"
                    continue
                cur = eng.journal.get(fid)
                if cur.get("resolved_at"):
                    outcome = f"recovered in {_between(cur['injected_at'], cur['resolved_at'])}"
                    res["resolved_at"] = cur["resolved_at"]
                    break
            console.print(f"  {outcome}")
            try:
                eng.revert(fid, reason=f"drill {d.id}")
            except FaultError as e:
                console.print(f"[red]✘ {e}[/red]")
                outcome += " · REVERT FAILED"
            results.append({**res, "outcome": outcome})
            if i < len(plans):
                time.sleep(parse_duration(d.pause).total_seconds())
    except KeyboardInterrupt:
        console.print("\n[yellow]Drill stopped.[/yellow] Active faults: `daalu fault status`; "
                      "undo them with `daalu fault revert --all`.")
    out_dir = faults_home() / "drills"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{d.id}-{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps({"drill": d.id, "env": env_name, "results": results}, indent=2))
    t = Table(title=f"{d.title}: results")
    for c in ("#", "incident", "outcome"):
        t.add_column(c)
    for i, r_ in enumerate(results, 1):
        t.add_row(str(i), r_["incident"], r_["outcome"])
    console.print(t)
    console.print(f"Saved {out}. Compare with Daalu's AI Ops metrics for the same window.")


# --- init: write inventory entries ------------------------------------------------

init_app = typer.Typer(help="Add an environment to the inventory by discovering it.")
app.add_typer(init_app, name="init")


def _save_env(path: Path | None, name: str, body: dict[str, Any], force: bool) -> Path:
    p = inventory_path(path)
    data: dict[str, Any] = {"environments": {}}
    if p.exists():
        data = yaml.safe_load(p.read_text()) or {"environments": {}}
        data.setdefault("environments", {})
    if name in data["environments"] and not force:
        _die(f"{name} is already in {p} (use --force to replace it)")
    data["environments"][name] = body
    try:
        Inventory.model_validate(data)
    except Exception as e:  # pragma: no cover - defensive
        _die(f"generated inventory is invalid: {e}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# daalu fault inventory: the environments you allow faults in.\n"
                 "# Docs: docs/fault-injection.md\n"
                 + yaml.safe_dump(data, sort_keys=False, width=100))
    return p


def _run(argv: list[str]) -> str:
    p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        _die(f"{' '.join(argv[:4])}…: {p.stderr.strip()}")
    return p.stdout


@init_app.command("cluster")
def init_cluster(
    name: str,
    kubeconfig: str = typer.Option("", "--kubeconfig"),
    context: str = typer.Option("", "--context"),
    namespace: list[str] = typer.Option([], "--namespace", "-n", help="Namespaces to list workloads from."),
    node_access: str = typer.Option("helper-pod", "--node-access", help="helper-pod | ssh"),
    inventory: Path | None = _INV,
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Pin a cluster's API server and list its workloads, Services and nodes as targets."""
    base = ["kubectl"] + (["--kubeconfig", str(Path(kubeconfig).expanduser())] if kubeconfig else []) \
        + (["--context", context] if context else [])
    server = _run([*base, "config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"]).strip()
    targets: dict[str, dict[str, str]] = {}
    for n in _run([*base, "get", "nodes", "-o", "name"]).split():
        node = n.split("/", 1)[1]
        targets[f"node-{node}"] = {"node": node}
    for ns in namespace:
        items = json.loads(_run([*base, "get", "deploy,sts,ds,svc", "-n", ns, "-o", "json"]))["items"]
        for it in items:
            kind = it["kind"].lower()
            nm = it["metadata"]["name"]
            key = f"svc-{nm}" if kind == "service" else nm
            if key in targets:
                key = f"{key}-{ns}"
            if kind == "service":
                svc = {"service": nm, "namespace": ns}
                ports = it["spec"].get("ports") or []
                if ports:
                    svc["port"] = str(ports[0]["port"])
                targets[key] = svc
                continue
            cont = it["spec"]["template"]["spec"]["containers"][0]["name"]
            t = {"kind": kind, "name": nm, "namespace": ns, "container": cont}
            labels = it["spec"].get("selector", {}).get("matchLabels") or {}
            if len(labels) >= 1:
                k, v = next(iter(labels.items()))
                t["pod_label"], t["pod_value"] = k, v
            targets[key] = t
    body: dict[str, Any] = {"kind": "cluster", "server": server, "node_access": node_access}
    if kubeconfig:
        body["kubeconfig"] = kubeconfig
    if context:
        body["context"] = context
    body["targets"] = targets
    p = _save_env(inventory, name, body, force)
    console.print(f"[green]✔[/green] {name}: pinned {server}, {len(targets)} targets → {p}")


@init_app.command("clab")
def init_clab(
    name: str,
    lab: str = typer.Option(..., "--lab", help="containerlab lab name"),
    nos: str = typer.Option("", "--nos", help="Default: cumulus if the image is cx/cumulus, else linux."),
    inventory: Path | None = _INV,
    force: bool = typer.Option(False, "--force"),
) -> None:
    """List a containerlab lab's nodes as devices and its links as targets."""
    rows = _run(["docker", "ps", "--filter", f"label=containerlab={lab}", "--format",
                 '{{.Names}}|{{.Image}}|{{.Label "clab-topo-file"}}']).split()
    if not rows:
        _die(f"no running containers for lab {lab!r}")
    prefix = f"clab-{lab}-"
    devices, images, topo = [], set(), ""
    for r in rows:
        cname, image, topo_file = r.split("|")
        devices.append(cname.removeprefix(prefix))
        images.add(image)
        topo = topo or topo_file
    def guess(image: str) -> str:
        return "cumulus" if re.search(r"(^|/)(cx|cumulus)", image) else "linux"

    per = {r.split("|")[0].removeprefix(prefix): guess(r.split("|")[1]) for r in rows}
    if not nos:
        nos = "cumulus" if "cumulus" in per.values() else "linux"
    device_nos = {d: n for d, n in per.items() if n != nos}
    targets: dict[str, dict[str, str]] = {}
    if topo and Path(topo).exists():
        spec = yaml.safe_load(Path(topo).read_text()) or {}
        for link in (spec.get("topology") or {}).get("links") or []:
            eps = link.get("endpoints") or []
            if len(eps) != 2:
                continue
            (a, ai), (b, bi) = (e.split(":", 1) for e in eps)
            # Name the link from both ends, so `leaf1-swp1` works whichever way the topology lists it.
            targets[f"{a}-{ai}"] = {"device": a, "interface": ai, "peer_device": b, "peer_interface": bi}
            targets[f"{b}-{bi}"] = {"device": b, "interface": bi, "peer_device": a, "peer_interface": ai}
    for d in devices:
        targets.setdefault(d, {"device": d})
    body = {"kind": "fabric", "nos": nos, "transport": "containerlab", "lab": lab,
            "devices": sorted(devices), "device_nos": device_nos, "targets": targets}
    p = _save_env(inventory, name, body, force)
    console.print(f"[green]✔[/green] {name}: {len(devices)} devices ({nos}), {len(targets)} targets → {p}")


# --- the guided flow ------------------------------------------------------------

def _pick(title: str, options: list[tuple[str, str]]) -> str:
    console.print(f"\n[bold]{title}[/bold]")
    for i, (_, label) in enumerate(options, 1):
        console.print(f"  [cyan]{i:>2}[/cyan]  {label}")
    choice = Prompt.ask("Number", choices=[str(i) for i in range(1, len(options) + 1)], show_choices=False)
    return options[int(choice) - 1][0]


def _wizard(inventory: Path | None) -> None:
    faults, recipes = _catalog()
    inv = _inventory(inventory)
    if not inv.environments:
        _die("the inventory has no environments; add one with `daalu fault init`")
    active = _journal().active()
    if active:
        console.print(f"[yellow]{len(active)} fault(s) already active[/yellow] (`daalu fault status`).")
    env_name = _pick("Environment", [
        (n, f"{n}  [dim]{'cluster ' + (e.context or '') if isinstance(e, Cluster) else 'fabric ' + e.nos}"
            f" · {len(e.targets)} targets[/dim]") for n, e in inv.environments.items()])
    env = inv.environments[env_name]
    opts: list[tuple[str, str]] = []
    for k, r in sorted(recipes.items()):
        if _env_ok(faults[r.fault], env) and (not r.target or r.target in env.targets):
            opts.append((k, f"[bold]{k}[/bold]  {r.title}"))
    for k, f in sorted(faults.items()):
        if _env_ok(f, env):
            opts.append((k, f"{k}  [dim]({f.blast})[/dim] {f.title}"))
    if not opts:
        _die(f"no fault fits {env_name}")
    name = _pick("What to break (recipes first, then generic faults)", opts)
    f, r = _resolve(name, faults, recipes)
    target = r.target if r else ""
    sets: dict[str, str] = {}
    if not r:
        fitting = [t for t, vals in env.targets.items() if set(vals) & set(f.params)
                   and all(k in vals or f.params[k].default is not None for k in f.params)]
        if fitting:
            target = _pick("Target", [*((t, f"{t}  [dim]{env.targets[t]}[/dim]") for t in fitting),
                                      ("", "none: I'll type the values")])
        provided = set(env.targets.get(target, {})) if target else set()
        for k, p in f.params.items():
            if k in provided:
                continue
            hint = f" [dim]{p.help}[/dim]" if p.help else ""
            if p.choices:
                sets[k] = Prompt.ask(f"{k}{hint}", choices=p.choices, default=p.default)
            else:
                v = Prompt.ask(f"{k} ({p.type}){hint}", default=p.default if p.default is not None else None)
                sets[k] = v or ""
    eng = Engine(inv, _journal())
    try:
        p = eng.plan(f, env_name, target=target, recipe=r, overrides=sets)
    except FaultError as e:
        _die(str(e))
    console.print()
    _print_plan(p)
    ttl = Prompt.ask("Auto-revert after (e.g. 30m, empty = never)", default="30m")
    _do_inject(eng, p, ttl=ttl, yes=False, note="", watch_=Confirm.ask("Watch for the fix?", default=True))
