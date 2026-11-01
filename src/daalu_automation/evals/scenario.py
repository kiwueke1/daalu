"""Eval scenario format (docs/design/ai-ops-maturity.md §8).

A scenario is a YAML file describing one reproducible outage: how to break a
known-good workload (``inject``, plain ``kubectl`` argument lists), which alert
Daalu should raise for it, and the answer key the AI's investigation is graded
against (``expect``). The answer key is written *before* any model runs, so
every model label is scored against the same ground truth.

The schema is strict (unknown keys are errors) so a typo in an answer key can't
silently make a scenario easier. :func:`scenario_json_schema` publishes it for
``spec/eval-scenario.schema.json``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,127}$"


class ScenarioError(ValueError):
    """A scenario file didn't parse or didn't match the schema."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _argv_list(v: Any) -> list[list[str]]:
    """``[[set, image, …], …]`` — each command is a list of kubectl args.
    Scalars (YAML may read ``0`` or ``true``) are coerced to strings."""
    if v is None:
        return []
    if not isinstance(v, list):
        raise ValueError("must be a list of kubectl argument lists")
    out: list[list[str]] = []
    for i, cmd in enumerate(v):
        if not isinstance(cmd, list) or not cmd:
            raise ValueError(f"item {i} must be a non-empty list of kubectl arguments")
        args: list[str] = []
        for a in cmd:
            if isinstance(a, (dict, list)) or a is None:
                raise ValueError(f"item {i}: arguments must be scalars, got {a!r}")
            if isinstance(a, bool):
                args.append("true" if a else "false")
            else:
                args.append(str(a))
        if args[0] in ("kubectl", "--kubeconfig"):
            raise ValueError(
                f"item {i}: give only the kubectl arguments; the runner adds "
                "`kubectl --kubeconfig …` itself"
            )
        out.append(args)
    return out


class Expect(_Strict):
    """The answer key."""

    root_cause_any: list[str] = Field(
        min_length=1,
        description="The root cause passes if it mentions any of these "
        "(case-insensitive substrings).",
    )
    root_cause_all: list[str] = Field(
        default_factory=list,
        description="…and every one of these. Optional, for stricter keys.",
    )
    fix_tools_any: list[str] = Field(
        default_factory=list,
        description="Proposing any of these write tools is a correct fix. "
        "Empty means no cluster change is the right answer: the fix check "
        "passes only if the AI proposes no write at all.",
    )
    fix_target: str = Field(
        default="",
        description="If set, the correct fix's tool input must mention this "
        "(e.g. the deployment name), so rolling back the wrong workload fails.",
    )
    forbidden_tools: list[str] = Field(
        default_factory=list,
        description="Tools that look like a fix but are wrong or dangerous "
        "for this fault. Calling any of them fails the safety check.",
    )


class Scenario(_Strict):
    """One eval scenario (AOMM spec v0.1)."""

    id: str = Field(pattern=_ID_PATTERN)
    title: str = Field(min_length=1, max_length=255)
    description: str = ""
    cluster: str = Field(default="", description="Daalu cluster slug the alert comes from.")
    namespace: str = ""
    tags: list[str] = Field(default_factory=list)
    setup: list[list[str]] = Field(
        default_factory=list,
        description="kubectl args run before inject; must be idempotent "
        "(restore the known-good state).",
    )
    inject: list[list[str]] = Field(min_length=1, description="kubectl args that cause the fault.")
    alert: str = Field(min_length=1, description="alertname (= Daalu alert title) to wait for.")
    timeout_s: int = Field(default=600, ge=30, le=7200)
    expect: Expect
    cleanup: list[list[str]] = Field(
        default_factory=list,
        description="kubectl args that restore the known-good state; always run.",
    )

    @field_validator("setup", "inject", "cleanup", mode="before")
    @classmethod
    def _coerce(cls, v: Any) -> list[list[str]]:
        return _argv_list(v)

    def summary(self) -> dict[str, Any]:
        """The ``GET /aiops/evals/scenarios`` item."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "namespace": self.namespace,
            "alert": self.alert,
            "expect": self.expect.model_dump(),
        }


def _format_errors(e: ValidationError) -> str:
    msgs = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        msgs.append(f"{loc}: {err['msg']}")
    return "; ".join(msgs)


def parse_scenario(text: str, *, source: str = "<string>") -> Scenario:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ScenarioError(f"{source}: YAML parse error: {e}") from e
    if not isinstance(raw, dict):
        raise ScenarioError(f"{source}: a scenario must be a YAML mapping")
    try:
        return Scenario.model_validate(raw)
    except ValidationError as e:
        raise ScenarioError(f"{source}: {_format_errors(e)}") from e


def load_scenario(path: str | Path) -> Scenario:
    p = Path(path)
    try:
        text = p.read_text()
    except OSError as e:
        raise ScenarioError(f"{p}: {e}") from e
    sc = parse_scenario(text, source=str(p))
    if sc.id != p.stem:
        raise ScenarioError(f"{p}: id {sc.id!r} must match the file name {p.stem!r}")
    return sc


def scenarios_dir() -> Path:
    """``$DAALU_EVALS_DIR`` (either ``evals/`` or ``evals/scenarios/``), else
    ``<repo>/evals/scenarios`` next to ``src/``."""
    env = os.environ.get("DAALU_EVALS_DIR", "").strip()
    if env:
        p = Path(env).expanduser()
        return p / "scenarios" if (p / "scenarios").is_dir() else p
    # src/daalu_automation/evals/scenario.py → parents[3] is the repo root.
    return Path(__file__).resolve().parents[3] / "evals" / "scenarios"


def load_scenarios(directory: str | Path | None = None) -> list[Scenario]:
    """Every ``*.yaml`` / ``*.yml`` in the directory, sorted by id. A missing
    directory yields ``[]``; an invalid file raises :class:`ScenarioError`."""
    d = Path(directory) if directory is not None else scenarios_dir()
    if not d.is_dir():
        return []
    files = sorted([*d.glob("*.yaml"), *d.glob("*.yml")])
    out = [load_scenario(f) for f in files]
    seen: set[str] = set()
    for s in out:
        if s.id in seen:
            raise ScenarioError(f"{d}: duplicate scenario id {s.id!r}")
        seen.add(s.id)
    return sorted(out, key=lambda s: s.id)


def get_scenario(scenario_id: str, directory: str | Path | None = None) -> Scenario:
    for s in load_scenarios(directory):
        if s.id == scenario_id:
            return s
    raise ScenarioError(f"unknown scenario {scenario_id!r}")


def scenario_json_schema() -> dict[str, Any]:
    schema = Scenario.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://github.com/kiwueke1/daalu/spec/eval-scenario.schema.json"
    schema["title"] = "AOMM eval scenario v0.1"
    return schema
