"""Load the fault catalog, recipes and the inventory from disk."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TypeVar, cast

import yaml
from pydantic import BaseModel, ValidationError

from daalu_automation.faults.model import Drill, Fault, FaultFileError, Inventory, Recipe

M = TypeVar("M", bound=BaseModel)
F = TypeVar("F", Fault, Recipe, Drill)


def _fmt(e: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}" for err in e.errors()
    )


def _load(path: Path, model: type[M]) -> M:
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise FaultFileError(f"{path}: {e}") from e
    if not isinstance(raw, dict):
        raise FaultFileError(f"{path}: must be a YAML mapping")
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        raise FaultFileError(f"{path}: {_fmt(e)}") from e


def faults_root() -> Path:
    """``$DAALU_FAULTS_DIR``, else ``<repo>/faults`` (holds catalog/ and recipes/)."""
    env = os.environ.get("DAALU_FAULTS_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    # src/daalu_automation/faults/catalog.py → parents[3] is the repo root.
    return Path(__file__).resolve().parents[3] / "faults"


def _walk(root: Path, model: type[F]) -> list[F]:
    out: list[F] = []
    if not root.is_dir():
        return out
    for f in sorted([*root.glob("*/*.yaml"), *root.glob("*/*.yml")]):
        obj = cast(F, _load(f, model))
        if obj.id != f.stem:
            raise FaultFileError(f"{f}: id {obj.id!r} must match the file name")
        if obj.group:
            raise FaultFileError(f"{f}: don't set group; it comes from the directory")
        obj.group = f.parent.name
        out.append(obj)
    seen: set[str] = set()
    for o in out:
        ref = f"{o.group}/{o.id}"
        if ref in seen:
            raise FaultFileError(f"{root}: duplicate {ref}")
        seen.add(ref)
    return out


def load_faults(root: Path | None = None) -> dict[str, Fault]:
    return {f.ref: f for f in _walk((root or faults_root()) / "catalog", Fault)}


def load_recipes(root: Path | None = None, faults: dict[str, Fault] | None = None) -> dict[str, Recipe]:
    root = root or faults_root()
    faults = faults if faults is not None else load_faults(root)
    out: dict[str, Recipe] = {}
    for r in _walk(root / "recipes", Recipe):
        if r.fault not in faults:
            raise FaultFileError(f"recipe {r.group}/{r.id}: unknown fault {r.fault!r}")
        f = faults[r.fault]
        unknown = set(r.params) - set(f.params)
        if unknown:
            raise FaultFileError(f"recipe {r.group}/{r.id}: {r.fault} has no params {sorted(unknown)}")
        out[f"{r.group}/{r.id}"] = r
    return out


def load_drills(root: Path | None = None) -> dict[str, Drill]:
    return {f"{d.group}/{d.id}": d for d in _walk((root or faults_root()) / "drills", Drill)}


def faults_home() -> Path:
    """Where the inventory and the journal live (``$DAALU_FAULTS_HOME``, else ~/.daalu/faults)."""
    env = os.environ.get("DAALU_FAULTS_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".daalu" / "faults"


def inventory_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(path).expanduser()
    env = os.environ.get("DAALU_FAULTS_INVENTORY", "").strip()
    return Path(env).expanduser() if env else faults_home() / "inventory.yaml"


def load_inventory(path: str | Path | None = None) -> Inventory:
    p = inventory_path(path)
    if not p.exists():
        raise FaultFileError(
            f"no inventory at {p}. Create one with `daalu fault init` "
            "(or copy faults/inventory.example.yaml)."
        )
    return _load(p, Inventory)
