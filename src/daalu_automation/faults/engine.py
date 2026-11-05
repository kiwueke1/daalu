"""Plan, inject, check and revert faults.

Every step is turned into an *op*: a plain argv (+ optional stdin) that a
:class:`Runner` executes. Revert ops are rendered at inject time, with every
captured value baked in, and written to the journal before the fault is
considered live, so ``daalu fault revert`` never needs the catalog, the
inventory or the original command line to undo a fault.
"""

from __future__ import annotations

import json
import re
import secrets
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from daalu_automation.faults.model import (
    Cluster,
    Fabric,
    Fault,
    Inventory,
    Recipe,
    Step,
    nos_ok,
    validate_value,
)

Runner = Callable[[list[str], "str | None", int], "tuple[int, str, str]"]
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class FaultError(RuntimeError):
    """Refused or failed. The message is shown to the operator as is."""


def subprocess_runner(argv: list[str], stdin: str | None, timeout: int) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    return p.returncode, p.stdout, p.stderr


@dataclass
class Op:
    desc: str
    argv: list[str]
    stdin: str | None = None
    ignore_errors: bool = False
    capture: str | None = None
    timeout: int = 120

    def to_json(self) -> dict[str, Any]:
        return {"desc": self.desc, "argv": self.argv, "stdin": self.stdin,
                "ignore_errors": self.ignore_errors, "timeout": self.timeout}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Op:
        return cls(desc=d["desc"], argv=list(d["argv"]), stdin=d.get("stdin"),
                   ignore_errors=bool(d.get("ignore_errors")), timeout=int(d.get("timeout", 120)))


# --- rendering ---------------------------------------------------------------

def render(text: str, vars_: dict[str, str], *, shell: bool, quote: set[str]) -> str:
    """Fill ``${name}``. In shell commands, ``text`` params are shell-quoted;
    every other type is validated to a shell-safe charset and goes in raw."""

    def sub(m: re.Match[str]) -> str:
        k = m.group(1)
        if k not in vars_:
            raise FaultError(f"unknown variable ${{{k}}}")
        v = vars_[k]
        return shlex.quote(v) if (shell and k in quote) else v

    return _VAR.sub(sub, text)


def resolve_params(
    fault: Fault,
    env: Cluster | Fabric,
    *,
    target: str = "",
    recipe: Recipe | None = None,
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Defaults < named target < recipe params < --set overrides."""
    vals: dict[str, str] = {k: p.default for k, p in fault.params.items() if p.default is not None}
    tname = target or (recipe.target if recipe else "")
    if tname:
        if tname not in env.targets:
            known = ", ".join(sorted(env.targets)) or "(none)"
            raise FaultError(f"no target {tname!r} in this environment (known: {known})")
        vals.update({k: v for k, v in env.targets[tname].items() if k in fault.params})
    if recipe:
        vals.update(recipe.params)
    for k, v in (overrides or {}).items():
        if k not in fault.params:
            raise FaultError(f"{fault.ref} has no param {k!r} (params: {', '.join(fault.params)})")
        vals[k] = v
    missing = [k for k in fault.params if k not in vals]
    if missing:
        raise FaultError(
            f"missing params: {', '.join(missing)}. Give them with --set k=v or a --target."
        )
    for k, p in fault.params.items():
        try:
            validate_value(k, p, vals[k])
        except ValueError as e:
            raise FaultError(str(e)) from None
    return vals


# --- turning steps into ops --------------------------------------------------

def _kubectl_base(c: Cluster) -> list[str]:
    argv = ["kubectl"]
    if c.kubeconfig:
        argv += ["--kubeconfig", str(Path(c.kubeconfig).expanduser())]
    if c.context:
        argv += ["--context", c.context]
    return argv


def _ssh(ssh: Any, host_name: str, cmd: str) -> list[str]:
    host = ssh.hosts.get(host_name, host_name)
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-p", str(ssh.port)]
    for o in ssh.options:
        argv += ["-o", o]
    if ssh.identity_file:
        argv += ["-i", str(Path(ssh.identity_file).expanduser())]
    argv.append(f"{ssh.user}@{host}" if ssh.user else host)
    argv.append(f"sudo -n sh -c {shlex.quote(cmd)}" if ssh.sudo else cmd)
    return argv


def _helper_pod(c: Cluster, node: str, cmd: str) -> list[str]:
    name = f"node-shell-{secrets.token_hex(3)}"
    overrides = {
        "apiVersion": "v1",
        "spec": {
            "nodeName": node,
            "hostPID": True,
            "hostNetwork": True,
            "restartPolicy": "Never",
            "tolerations": [{"operator": "Exists"}],
            "containers": [{
                "name": name,
                "image": c.helper_image,
                "stdin": True,
                "stdinOnce": True,
                "command": ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--",
                            "sh", "-c", cmd],
                "securityContext": {"privileged": True},
            }],
        },
    }
    return [*_kubectl_base(c), "run", name, "-n", c.helper_namespace, "--rm", "-i", "--quiet",
            "--restart=Never", f"--image={c.helper_image}", "--pod-running-timeout=2m",
            f"--overrides={json.dumps(overrides, separators=(',', ':'))}"]


def step_to_op(step: Step, env: Cluster | Fabric, vars_: dict[str, str], quote: set[str]) -> Op:
    r = lambda s: render(s, vars_, shell=False, quote=quote)  # noqa: E731
    rs = lambda s: render(s, vars_, shell=True, quote=quote)  # noqa: E731
    common = {"ignore_errors": step.ignore_errors, "capture": step.capture}
    k = step.kind
    if k == "sleep":
        return Op(desc=f"sleep {step.sleep}s", argv=["sleep", str(step.sleep)], timeout=3700, **common)
    if isinstance(env, Cluster):
        base = _kubectl_base(env)
        if k == "kubectl":
            args = [r(a) for a in step.kubectl or []]
            stdin = r(step.stdin) if step.stdin is not None else None
            return Op(desc="kubectl " + " ".join(args), argv=[*base, *args], stdin=stdin, **common)
        if k == "exec":
            ref = r(step.at or "${kind}/${name}")
            cmd = rs(step.exec or "")
            argv = [*base, "exec", "-n", vars_.get("namespace", "default"), ref]
            cont = r(step.container) if step.container else vars_.get("container", "")
            if cont:
                argv += ["-c", cont]
            return Op(desc=f"exec {ref}: {cmd}", argv=[*argv, "--", "sh", "-c", cmd], **common)
        if k == "node":
            node = r(step.at or "${node}")
            cmd = rs(step.node or "")
            argv = _ssh(env.ssh, node, cmd) if env.node_access == "ssh" else _helper_pod(env, node, cmd)
            return Op(desc=f"node {node}: {cmd}", argv=argv, timeout=300, **common)
        raise FaultError(f"{k} steps need a cluster")
    # fabric
    if k == "device":
        dev = r(step.at or "${device}")
        cmd = rs(step.device or "")
        if env.transport == "containerlab":
            argv = ["docker", "exec", "-i", f"{env.prefix}{dev}", "sh", "-c", cmd]
        else:
            argv = _ssh(env.ssh, dev, cmd)
        return Op(desc=f"device {dev}: {cmd}", argv=argv, **common)
    if k == "host":
        if env.transport != "containerlab":
            raise FaultError("host steps only run on containerlab fabrics")
        cmd = rs(step.host or "")
        argv = ["sudo", "-n", "sh", "-c", cmd] if env.host_sudo else ["sh", "-c", cmd]
        return Op(desc=f"host: {cmd}", argv=argv, **common)
    raise FaultError(f"{k} steps need a fabric")


# --- snapshots ---------------------------------------------------------------

_DROP_META = ("uid", "resourceVersion", "creationTimestamp", "generation", "managedFields",
              "selfLink", "ownerReferences")


def strip_snapshot(obj: dict[str, Any]) -> dict[str, Any]:
    """What ``kubectl replace`` needs to put the object back as it was."""
    out = {k: v for k, v in obj.items() if k != "status"}
    meta = {k: v for k, v in (obj.get("metadata") or {}).items() if k not in _DROP_META}
    ann = dict(meta.get("annotations") or {})
    ann.pop("deployment.kubernetes.io/revision", None)
    if ann:
        meta["annotations"] = ann
    else:
        meta.pop("annotations", None)
    out["metadata"] = meta
    return out


# --- journal -----------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def parse_duration(s: str) -> timedelta:
    m = re.fullmatch(r"([0-9]+)(s|m|h)", s.strip())
    if not m:
        raise FaultError(f"bad duration {s!r} (use e.g. 90s, 30m, 2h)")
    n = int(m.group(1))
    return timedelta(seconds=n * {"s": 1, "m": 60, "h": 3600}[m.group(2)])


class Journal:
    """Active and past faults, one JSON file. Written before and after every
    inject so a crash mid-way still leaves the revert ops on disk."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise FaultError(f"journal {self.path} is unreadable: {e}") from e
        return data if isinstance(data, list) else []

    def save(self, entries: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, indent=2))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def upsert(self, entry: dict[str, Any]) -> None:
        entries = [e for e in self.load() if e["id"] != entry["id"]]
        entries.append(entry)
        self.save(entries)

    def get(self, fid: str) -> dict[str, Any]:
        matches = [e for e in self.load() if e["id"] == fid or e["id"].startswith(fid)]
        if len(matches) != 1:
            raise FaultError(f"no single fault matches {fid!r}" if not matches else f"{fid!r} is ambiguous")
        return matches[0]

    def active(self) -> list[dict[str, Any]]:
        return [e for e in self.load() if e["state"] in ("active", "injecting", "revert-failed")]


# --- the engine --------------------------------------------------------------

@dataclass
class Plan:
    fault: Fault
    env_name: str
    env: Cluster | Fabric
    vars: dict[str, str]
    quote: set[str]
    recipe: Recipe | None = None
    target: str = ""
    warnings: list[str] = field(default_factory=list)

    def preview(self) -> dict[str, list[str]]:
        """Human-readable steps (``${captured}`` values shown as placeholders)."""

        v = dict(self.vars, fault_id="f-xxxxxx")
        for s in [*self.fault.prepare, *self.fault.inject]:
            if s.capture and not v.get(s.capture):
                v[s.capture] = f"<{s.capture}>"

        def show(steps: list[Step]) -> list[str]:
            out = []
            for s in steps:
                if s.when_empty and self.vars.get(s.when_empty, ""):
                    continue
                if s.snapshot:
                    out.append(f"snapshot {render(s.snapshot, v, shell=False, quote=self.quote)}"
                               f" -n {v.get('namespace', 'default')} (restored on revert)")
                    continue
                out.append(step_to_op(s, self.env, v, self.quote).desc)
            return out

        return {
            "prepare": show(self.fault.prepare),
            "inject": show(self.fault.inject),
            "revert": show(self.fault.revert),
            "check": show([self.fault.check]) if self.fault.check else [],
        }


class Engine:
    def __init__(self, inventory: Inventory, journal: Journal, run: Runner = subprocess_runner,
                 sleep: Callable[[float], None] = time.sleep):
        self.inv = inventory
        self.journal = journal
        self.run = run
        self.sleep = sleep

    # -- planning --
    def plan(self, fault: Fault, env_name: str, *, target: str = "", recipe: Recipe | None = None,
             overrides: dict[str, str] | None = None) -> Plan:
        if env_name not in self.inv.environments:
            known = ", ".join(sorted(self.inv.environments)) or "(none)"
            raise FaultError(f"no environment {env_name!r} in the inventory (known: {known})")
        env = self.inv.environments[env_name]
        if fault.scope == "cluster" and not isinstance(env, Cluster):
            raise FaultError(f"{fault.ref} is a cluster fault; {env_name} is a fabric")
        if fault.scope == "fabric":
            if not isinstance(env, Fabric):
                raise FaultError(f"{fault.ref} is a network fault; {env_name} is a cluster")
            if not nos_ok(fault, env.nos):
                raise FaultError(f"{fault.ref} supports {', '.join(fault.nos)}; {env_name} runs {env.nos}")
            if "containerlab" in fault.requires and env.transport != "containerlab":
                raise FaultError(f"{fault.ref} rewires cables on the lab host; containerlab fabrics only")
        vars_ = resolve_params(fault, env, target=target, recipe=recipe, overrides=overrides)
        warnings: list[str] = []
        if isinstance(env, Cluster):
            ns = vars_.get("namespace", "")
            if ns and ns in env.protect_namespaces:
                raise FaultError(
                    f"namespace {ns!r} is protected in {env_name} (protect_namespaces): it carries "
                    "Daalu's own connection to the cluster, so a fault there tests nothing"
                )
            if fault.blast in ("node", "cluster"):
                warnings.append(
                    f"blast radius is the whole {fault.blast}: every workload on it is affected, "
                    f"including Daalu's edge agent if it runs there"
                )
        if isinstance(env, Fabric):
            dev = vars_.get("device", "")
            dnos = env.device_nos.get(dev, env.nos)
            if dev and not nos_ok(fault, dnos):
                raise FaultError(f"{fault.ref} supports {', '.join(fault.nos)}; {dev} runs {dnos}")
        if isinstance(env, Fabric) and env.devices:
            for k, p in fault.params.items():
                if p.type == "device" and vars_.get(k) and vars_[k] not in env.devices:
                    raise FaultError(f"{vars_[k]!r} is not in {env_name}'s devices list")
        if fault.warning:
            warnings.append(fault.warning)
        quote = {k for k, p in fault.params.items() if p.type == "text"}
        vars_["env"] = env_name
        if isinstance(env, Fabric) and env.transport == "containerlab":
            vars_["clab_prefix"] = env.prefix
        return Plan(fault=fault, env_name=env_name, env=env, vars=vars_, quote=quote,
                    recipe=recipe, target=target or (recipe.target if recipe else ""),
                    warnings=warnings)

    # -- preflight: is this really the lab we think it is? --
    def preflight(self, plan: Plan) -> None:
        env = plan.env
        if isinstance(env, Cluster):
            rc, out, err = self.run(
                [*_kubectl_base(env), "config", "view", "--minify", "-o",
                 "jsonpath={.clusters[0].cluster.server}"], None, 30)
            if rc != 0:
                raise FaultError(f"can't read the kube context for {plan.env_name}: {err.strip() or out}")
            got = out.strip().rstrip("/")
            if got != env.server.rstrip("/"):
                raise FaultError(
                    f"{plan.env_name}: the context points at {got or '(nothing)'} but the inventory "
                    f"pins {env.server}. Refusing: this may not be your lab. Fix the inventory if "
                    "the lab really moved."
                )
            rc, out, err = self.run([*_kubectl_base(env), "get", "nodes", "-o", "name"], None, 30)
            if rc != 0:
                raise FaultError(f"{plan.env_name}: cluster unreachable: {err.strip()}")
            if "multi-node" in plan.fault.requires and len(out.split()) < 2:
                raise FaultError(
                    f"{plan.fault.ref} needs at least two nodes: on a single-node cluster it takes "
                    "down the control plane and nothing (Daalu included) can reach it to fix it"
                )
        else:
            if env.transport == "containerlab":
                for k, p in plan.fault.params.items():
                    dev = plan.vars.get(k)
                    if p.type != "device" or not dev:
                        continue
                    rc, out, _ = self.run(
                        ["docker", "inspect", "-f", "{{.State.Running}}", f"{env.prefix}{dev}"], None, 30)
                    if rc != 0 or out.strip() != "true":
                        raise FaultError(f"{env.prefix}{dev} isn't a running container on this host")

    # -- executing --
    def _exec(self, op: Op, log: list[dict[str, Any]]) -> tuple[bool, str]:
        rc, out, err = self.run(op.argv, op.stdin, op.timeout)
        log.append({"desc": op.desc, "rc": rc, "out": out[-1500:], "err": err[-1500:]})
        return rc == 0, out

    def _run_steps(self, plan: Plan, steps: list[Step], vars_: dict[str, str],
                   revert_ops: list[Op], log: list[dict[str, Any]]) -> None:
        for s in steps:
            if s.when_empty and vars_.get(s.when_empty, ""):
                continue
            if s.snapshot:
                ref = render(s.snapshot, vars_, shell=False, quote=plan.quote)
                ns = vars_.get("namespace", "default")
                base = _kubectl_base(plan.env)  # type: ignore[arg-type]
                ok, out = self._exec(Op(desc=f"snapshot {ref} -n {ns}",
                                        argv=[*base, "get", ref, "-n", ns, "-o", "json"]), log)
                if not ok:
                    raise FaultError(f"snapshot of {ref} -n {ns} failed: {log[-1]['err'].strip()}")
                snap = strip_snapshot(json.loads(out))
                revert_ops.append(Op(desc=f"restore {ref} -n {ns} from snapshot",
                                     argv=[*base, "replace", "-f", "-"], stdin=json.dumps(snap)))
                continue
            op = step_to_op(s, plan.env, vars_, plan.quote)
            ok, out = self._exec(op, log)
            if not ok and not s.ignore_errors:
                raise FaultError(f"step failed: {op.desc}\n{log[-1]['err'].strip() or log[-1]['out'].strip()}")
            if s.capture:
                vars_[s.capture] = out.strip()

    def inject(self, plan: Plan, *, ttl: str = "", note: str = "") -> dict[str, Any]:
        self.preflight(plan)
        fid = f"f-{secrets.token_hex(3)}"
        now = _now()
        vars_ = dict(plan.vars, fault_id=fid)
        entry: dict[str, Any] = {
            "id": fid, "fault": plan.fault.ref, "recipe": (f"{plan.recipe.group}/{plan.recipe.id}"
                                                           if plan.recipe else ""),
            "title": plan.fault.title, "env": plan.env_name, "target": plan.target,
            "params": {k: v for k, v in plan.vars.items() if k in plan.fault.params},
            "note": note, "state": "injecting", "injected_at": _iso(now),
            "expires_at": _iso(now + parse_duration(ttl)) if ttl else "",
            "self_heals": plan.fault.self_heals,
            "revert_ops": [], "check_op": None, "log": [], "revert_log": [],
            "seen_unhealthy": False, "resolved_at": "", "reverted_at": "",
        }
        self.journal.upsert(entry)
        snap_ops: list[Op] = []
        log: list[dict[str, Any]] = entry["log"]
        try:
            self._run_steps(plan, plan.fault.prepare, vars_, snap_ops, log)
            # Render the fault's own revert as early as possible, so a failure
            # half-way through inject can still be undone.
            self._run_steps(plan, plan.fault.inject, vars_, snap_ops, log)
        except FaultError as e:
            entry["revert_ops"] = [o.to_json() for o in snap_ops + self._render_revert(plan, vars_)]
            entry["state"] = "inject-failed"
            self.journal.upsert(entry)
            self.revert(fid, reason="inject failed")
            raise FaultError(f"{e}\nThe partial fault was reverted (journal {fid}).") from None
        entry["revert_ops"] = [o.to_json() for o in snap_ops + self._render_revert(plan, vars_)]
        if plan.fault.check:
            entry["check_op"] = step_to_op(plan.fault.check, plan.env, vars_, plan.quote).to_json()
            entry["check_op"]["match"] = plan.fault.check.match or ""
        entry["state"] = "active"
        self.journal.upsert(entry)
        return entry

    def _render_revert(self, plan: Plan, vars_: dict[str, str]) -> list[Op]:
        ops = []
        for s in plan.fault.revert:
            if s.when_empty and vars_.get(s.when_empty, ""):
                continue
            try:
                ops.append(step_to_op(s, plan.env, vars_, plan.quote))
            except FaultError:
                continue  # a value captured later in inject never arrived
        return ops

    def check(self, entry: dict[str, Any]) -> bool | None:
        """True = healthy again, False = still broken, None = no check."""
        if not entry.get("check_op"):
            return None
        op = Op.from_json(entry["check_op"])
        rc, out, _ = self.run(op.argv, op.stdin, op.timeout)
        pattern = entry["check_op"].get("match") or ""
        healthy = rc == 0 and (not pattern or re.search(pattern, out) is not None)
        if not healthy and not entry.get("seen_unhealthy"):
            entry["seen_unhealthy"] = True
            self.journal.upsert(entry)
        if healthy and entry.get("seen_unhealthy") and not entry.get("resolved_at"):
            entry["resolved_at"] = _iso(_now())
            self.journal.upsert(entry)
        return healthy

    def revert(self, fid: str, *, reason: str = "manual") -> dict[str, Any]:
        entry = self.journal.get(fid)
        if entry["state"] == "reverted":
            return entry
        log: list[dict[str, Any]] = []
        failed = []
        for d in entry.get("revert_ops") or []:
            op = Op.from_json(d)
            ok, _ = self._exec(op, log)
            if not ok and not op.ignore_errors:
                failed.append(op.desc)
        entry["revert_log"] = log
        entry["revert_reason"] = reason
        if failed:
            entry["state"] = "revert-failed"
            self.journal.upsert(entry)
            raise FaultError(f"{entry['id']}: revert steps failed: {'; '.join(failed)} "
                             f"(see `daalu fault log {entry['id']}`); fix by hand or wait for the "
                             "fault's own safety timer, then `revert` again")
        entry["state"] = "reverted"
        entry["reverted_at"] = _iso(_now())
        self.journal.upsert(entry)
        return entry

    def expired(self) -> list[dict[str, Any]]:
        now = _now()
        return [e for e in self.journal.active()
                if e.get("expires_at") and parse_iso(e["expires_at"]) <= now]
