"""Fault, recipe and inventory file formats.

A **fault** (``faults/catalog/<group>/<id>.yaml``) is a generic, parameterised
way to break something: ``inject`` steps, ``revert`` steps that restore the
known-good state, and an optional ``check`` that passes once things are healthy
again. Faults name no real workload; ``${param}`` placeholders are filled from
the inventory and the command line.

A **recipe** (``faults/recipes/<group>/<id>.yaml``) binds a fault to a named
target with fixed params and tells the story: what breaks, what the operator
sees, what a correct fix looks like. Episodes and drills use recipes.

The **inventory** (``~/.daalu/faults/inventory.yaml``, user-written or made by
``daalu fault init``) lists the environments you allow faults in: clusters
(kubeconfig + pinned API server) and fabrics (containerlab or SSH devices),
plus named targets inside each.

Every schema is strict (unknown keys are errors).
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ID = r"^[a-z0-9][a-z0-9-]{0,127}$"
_FAULT_REF = r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$"

# Values substituted into shell commands unquoted must be made only of these.
_SAFE = re.compile(r"^[A-Za-z0-9_.:/@=%+,\-]*$")
_PATTERNS: dict[str, re.Pattern[str]] = {
    "name": re.compile(r"^[a-z0-9]([a-z0-9.\-]{0,251}[a-z0-9])?$"),
    "int": re.compile(r"^-?[0-9]+$"),
    "percent": re.compile(r"^(100|[0-9]{1,2})(\.[0-9]+)?$"),
    "duration": re.compile(r"^[0-9]+(ms|s|m|h)$"),
    "size": re.compile(r"^[0-9]+([KMGT]i?)?$"),
    "iface": re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$"),
    "image": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@\-]{0,254}$"),
    "token": _SAFE,
    "node": re.compile(r"^[a-z0-9]([a-z0-9.\-]{0,251}[a-z0-9])?$"),
    "device": re.compile(r"^[A-Za-z0-9_.\-]{1,64}$"),
}
ParamType = Literal[
    "name", "int", "percent", "duration", "size", "iface", "image", "token",
    "node", "device", "ip", "cidr", "enum", "text",
]


class FaultFileError(ValueError):
    """A fault, recipe or inventory file didn't parse or validate."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Param(_Strict):
    type: ParamType = "token"
    default: str | None = Field(
        default=None,
        description="Used when neither the target nor --set gives a value. "
        "None = required. An empty string is a valid default.",
    )
    choices: list[str] = Field(default_factory=list, description="For type: enum.")
    help: str = ""

    @field_validator("default", mode="before")
    @classmethod
    def _str(cls, v: Any) -> Any:
        if v is None or isinstance(v, str):
            return v
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)


def validate_value(name: str, p: Param, value: str) -> str:
    """Check one param value; raise ValueError naming the param."""
    if p.type == "text":
        return value
    if value == "" and p.default == "":
        return value  # explicitly optional
    if p.type == "enum":
        if value not in p.choices:
            raise ValueError(f"{name}={value!r}: must be one of {', '.join(p.choices)}")
        return value
    if p.type == "ip":
        try:
            ipaddress.ip_address(value)
        except ValueError:
            raise ValueError(f"{name}={value!r}: not an IP address") from None
        return value
    if p.type == "cidr":
        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError:
            raise ValueError(f"{name}={value!r}: not a CIDR prefix") from None
        return value
    if not _PATTERNS[p.type].match(value):
        raise ValueError(f"{name}={value!r}: not a valid {p.type}")
    return value


StepKind = Literal["kubectl", "snapshot", "exec", "node", "device", "host", "sleep"]


class Step(_Strict):
    """One action. Exactly one of the action keys is set.

    * ``kubectl``: kubectl argument list (the runner adds kubeconfig/context).
    * ``snapshot``: ``kind/name`` (namespace = ``${namespace}``). Records the
      object before the fault; a matching restore is put at the head of the
      revert list automatically.
    * ``exec``: shell command run in the first Running pod of ``${kind}/${name}``.
    * ``node``: shell command run on the host of ``${node}`` (privileged
      helper pod, or SSH if the inventory says so).
    * ``device``: shell command on network device ``${device}``.
    * ``host``: shell command on the lab host itself (containerlab fabrics only).
    * ``sleep``: seconds.
    """

    kubectl: list[str] | None = None
    snapshot: str | None = None
    exec: str | None = None
    node: str | None = None
    device: str | None = None
    host: str | None = None
    sleep: int | None = Field(default=None, ge=0, le=3600)
    at: str | None = Field(
        default=None,
        description="Override the node/device/pod this step runs on (a ${param}).",
    )
    container: str | None = Field(default=None, description="For exec: container name.")
    stdin: str | None = Field(default=None, description="For kubectl: text fed to stdin (e.g. apply -f -).")
    match: str | None = Field(
        default=None,
        description="For check: the step passes only if stdout matches this regex.",
    )
    capture: str | None = Field(
        default=None,
        description="Store the step's trimmed stdout in this variable for later steps.",
    )
    when_empty: str | None = Field(
        default=None, description="Run only if this variable is empty (used to fill defaults).",
    )
    ignore_errors: bool = False

    @field_validator("kubectl", mode="before")
    @classmethod
    def _argv(cls, v: Any) -> Any:
        if v is None:
            return v
        if not isinstance(v, list) or not v:
            raise ValueError("kubectl must be a non-empty argument list")
        out = []
        for a in v:
            if isinstance(a, (dict, list)) or a is None:
                raise ValueError(f"kubectl arguments must be scalars, got {a!r}")
            out.append(("true" if a else "false") if isinstance(a, bool) else str(a))
        if out[0] in ("kubectl", "--kubeconfig", "--context"):
            raise ValueError("give only the kubectl arguments; the runner adds the rest")
        return out

    @model_validator(mode="after")
    def _one(self) -> Step:
        set_ = [k for k in ("kubectl", "snapshot", "exec", "node", "device", "host", "sleep")
                if getattr(self, k) is not None]
        if len(set_) != 1:
            raise ValueError(f"a step needs exactly one action, got {set_ or 'none'}")
        return self

    @property
    def kind(self) -> StepKind:
        for k in ("kubectl", "snapshot", "exec", "node", "device", "host", "sleep"):
            if getattr(self, k) is not None:
                return k  # type: ignore[return-value]
        raise AssertionError("unreachable")


Scope = Literal["cluster", "fabric"]
Blast = Literal["pod", "workload", "namespace", "service", "cluster", "node", "interface", "device", "link"]


class Expect(_Strict):
    """What a correct responder (human or AI) does. Informational; ``daalu eval``
    scenarios carry the graded answer key."""

    symptoms: list[str] = Field(default_factory=list, description="What you can observe.")
    root_cause: str = ""
    fix: str = Field(default="", description="The correct remediation.")
    daalu_can_fix: bool = Field(
        default=True,
        description="False when the right answer is to escalate (Daalu has no tool for it).",
    )


class Fault(_Strict):
    id: str = Field(pattern=_ID)
    group: str = Field(default="", description="Set from the directory; don't write it.")
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    scope: Scope
    blast: Blast
    nos: list[str] = Field(
        default_factory=list,
        description="Fabric faults: NOS families it works on (linux = any Linux "
        "based NOS: cumulus, sonic, frr, linux).",
    )
    requires: list[Literal["multi-node", "systemd", "sudo-host", "containerlab"]] = Field(
        default_factory=list
    )
    params: dict[str, Param] = Field(default_factory=dict)
    prepare: list[Step] = Field(
        default_factory=list,
        description="Read-only steps run before inject (and by plan) to fill variables.",
    )
    inject: list[Step] = Field(min_length=1)
    revert: list[Step] = Field(
        default_factory=list,
        description="Idempotent steps restoring the known-good state. May be empty "
        "only if the fault has a snapshot or self_heals.",
    )
    check: Step | None = Field(
        default=None, description="Succeeds once the target is healthy again.",
    )
    self_heals: bool = False
    warning: str = ""
    expect: Expect = Field(default_factory=Expect)

    @model_validator(mode="after")
    def _revertible(self) -> Fault:
        has_snap = any(s.snapshot for s in self.inject)
        if not self.revert and not has_snap and not self.self_heals:
            raise ValueError("a fault needs revert steps, a snapshot step, or self_heals: true")
        if self.scope == "fabric" and not self.nos:
            raise ValueError("fabric faults must list nos")
        for s in [*self.prepare, *self.inject, *self.revert, *([self.check] if self.check else [])]:
            if self.scope == "cluster" and s.kind in ("device", "host"):
                raise ValueError(f"cluster fault can't use a {s.kind} step")
            if self.scope == "fabric" and s.kind in ("kubectl", "snapshot", "exec", "node"):
                raise ValueError(f"fabric fault can't use a {s.kind} step")
        for s in self.prepare:
            if s.kind not in ("kubectl", "exec", "node", "device", "host") or not s.capture:
                raise ValueError("prepare steps must be read commands with capture:")
        return self

    @property
    def ref(self) -> str:
        return f"{self.group}/{self.id}"


class Recipe(_Strict):
    id: str = Field(pattern=_ID)
    group: str = Field(default="", description="Set from the directory; don't write it.")
    title: str = Field(min_length=1, max_length=200)
    fault: str = Field(pattern=_FAULT_REF, description="group/id of the fault.")
    target: str = Field(default="", description="Named target in the inventory environment.")
    params: dict[str, str] = Field(default_factory=dict)
    story: str = ""
    episodes: list[str] = Field(default_factory=list, description="Episode ids that use it.")
    expect: Expect = Field(default_factory=Expect)

    @field_validator("params", mode="before")
    @classmethod
    def _strs(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {k: (("true" if x else "false") if isinstance(x, bool) else str(x))
                    for k, x in v.items()}
        return v


# --- inventory ---------------------------------------------------------------

class SSH(_Strict):
    user: str = ""
    port: int = 22
    identity_file: str = ""
    hosts: dict[str, str] = Field(
        default_factory=dict, description="node or device name → address (default: the name).",
    )
    sudo: bool = Field(default=True, description="Wrap commands in `sudo -n sh -c`.")
    options: list[str] = Field(default_factory=list, description="Extra ssh -o options.")


class Cluster(_Strict):
    kind: Literal["cluster"] = "cluster"
    description: str = ""
    kubeconfig: str = ""
    context: str = ""
    server: str = Field(
        min_length=1,
        description="The API server URL this context must resolve to. Guards "
        "against a context name that now points at another (production) cluster.",
    )
    node_access: Literal["helper-pod", "ssh"] = "helper-pod"
    helper_image: str = "alpine:3.21"
    helper_namespace: str = "default"
    ssh: SSH = Field(default_factory=SSH)
    protect_namespaces: list[str] = Field(
        default_factory=lambda: ["daalu-edge", "daalu-system"],
        description="Faults may never target these (Daalu's own link to the cluster).",
    )
    targets: dict[str, dict[str, str]] = Field(default_factory=dict)

    @field_validator("targets", mode="before")
    @classmethod
    def _strs(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {t: {k: str(x) for k, x in (m or {}).items()} for t, m in v.items()}
        return v


class Fabric(_Strict):
    kind: Literal["fabric"] = "fabric"
    description: str = ""
    nos: str = Field(min_length=1, description="cumulus | sonic | frr | linux | …")
    transport: Literal["containerlab", "ssh"]
    lab: str = Field(default="", description="containerlab lab name (containers clab-<lab>-<node>).")
    container_prefix: str = Field(default="", description="Override clab-<lab>- prefix.")
    host_sudo: bool = Field(default=True, description="Use sudo -n for host steps.")
    ssh: SSH = Field(default_factory=SSH)
    devices: list[str] = Field(default_factory=list)
    device_nos: dict[str, str] = Field(
        default_factory=dict, description="Per-device NOS where it differs (e.g. servers: linux).",
    )
    targets: dict[str, dict[str, str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _need(self) -> Fabric:
        if self.transport == "containerlab" and not (self.lab or self.container_prefix):
            raise ValueError("containerlab fabrics need lab: (or container_prefix:)")
        return self

    @field_validator("targets", mode="before")
    @classmethod
    def _strs(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {t: {k: str(x) for k, x in (m or {}).items()} for t, m in v.items()}
        return v

    @property
    def prefix(self) -> str:
        return self.container_prefix or f"clab-{self.lab}-"


LINUX_NOS = {"cumulus", "sonic", "frr", "linux"}


def nos_ok(fault: Fault, nos: str) -> bool:
    return nos in fault.nos or ("linux" in fault.nos and nos in LINUX_NOS)


class Inventory(_Strict):
    environments: dict[str, Cluster | Fabric] = Field(default_factory=dict)

    @field_validator("environments", mode="before")
    @classmethod
    def _discriminate(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            raise ValueError("environments must be a mapping")
        out: dict[str, Any] = {}
        for name, e in v.items():
            if not re.match(_ID, str(name)):
                raise ValueError(f"environment name {name!r} must match {_ID}")
            if not isinstance(e, dict) or e.get("kind") not in ("cluster", "fabric"):
                raise ValueError(f"{name}: kind must be cluster or fabric")
            out[name] = Cluster.model_validate(e) if e["kind"] == "cluster" else Fabric.model_validate(e)
        return out


class DrillItem(_Strict):
    recipe: str = ""
    fault: str = ""
    target: str = ""
    set: dict[str, str] = Field(default_factory=dict)
    timeout: str = Field(default="", description="Override the drill's per-incident timeout.")

    @field_validator("set", mode="before")
    @classmethod
    def _strs(cls, v: Any) -> Any:
        return {k: str(x) for k, x in (v or {}).items()}

    @model_validator(mode="after")
    def _one(self) -> DrillItem:
        if bool(self.recipe) == bool(self.fault):
            raise ValueError("each item needs exactly one of recipe: or fault:")
        return self


class Drill(_Strict):
    """A game day: incidents injected one after another, each left for the
    responder (Daalu, a person) to fix, then reverted. Records time to recover."""

    id: str = Field(pattern=_ID)
    group: str = Field(default="", description="Set from the directory; don't write it.")
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    env: str = Field(default="", description="Default environment (override with --env).")
    timeout: str = Field(default="20m", pattern=r"^[0-9]+(s|m|h)$",
                         description="How long to wait for a fix before reverting and moving on.")
    pause: str = Field(default="2m", pattern=r"^[0-9]+(s|m|h)$",
                       description="Quiet time between incidents (lets alerts resolve).")
    items: list[DrillItem] = Field(min_length=1)
