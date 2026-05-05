"""
Workflow registry — typed-form schemas for known workflows.

The registry maps workflow_type → schema (title, description, task_queue, list
of input fields). When the user opens "Start a workflow" in the UI, the form
is rendered from this schema instead of asking for raw JSON.

Source of truth lives in the *daalu* repo (``src/daalu/temporal/schemas.py``).
That code generates a JSON snapshot which is baked into this package as
``registry.json``. At runtime the path can be overridden via the
``REGISTRY_FILE`` env var so an operator can hot-reload an updated registry
without rebuilding the image.

Workflows not present in the registry fall back to the legacy raw-JSON form
at ``GET /workflows/new``.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class FieldSchema:
    name: str
    label: str
    type: str = "string"            # string | bool | int | textarea | secret
    required: bool = False
    default: Any = None
    placeholder: str = ""
    help: str = ""
    choices: Optional[List[str]] = None
    advanced: bool = False


@dataclass
class WorkflowSchema:
    workflow_type: str
    task_queue: str
    title: str
    description: str
    fields: List[FieldSchema] = field(default_factory=list)
    workflow_id_template: str = "{workflow_type}-{timestamp}"

    @property
    def basic_fields(self) -> List[FieldSchema]:
        return [f for f in self.fields if not f.advanced]

    @property
    def advanced_fields(self) -> List[FieldSchema]:
        return [f for f in self.fields if f.advanced]


def _coerce_field(d: Dict[str, Any]) -> FieldSchema:
    return FieldSchema(
        name=d["name"],
        label=d.get("label", d["name"]),
        type=d.get("type", "string"),
        required=bool(d.get("required", False)),
        default=d.get("default"),
        placeholder=d.get("placeholder", ""),
        help=d.get("help", ""),
        choices=d.get("choices"),
        advanced=bool(d.get("advanced", False)),
    )


def _coerce_workflow(d: Dict[str, Any]) -> WorkflowSchema:
    return WorkflowSchema(
        workflow_type=d["workflow_type"],
        task_queue=d.get("task_queue", "default"),
        title=d.get("title", d["workflow_type"]),
        description=d.get("description", ""),
        fields=[_coerce_field(f) for f in d.get("fields", [])],
        workflow_id_template=d.get(
            "workflow_id_template", "{workflow_type}-{timestamp}"
        ),
    )


def _registry_path() -> Path:
    override = os.getenv("REGISTRY_FILE")
    if override:
        return Path(override)
    # Bundled with the package.
    return Path(__file__).with_name("registry.json")


@lru_cache(maxsize=1)
def load_registry() -> Dict[str, WorkflowSchema]:
    path = _registry_path()
    if not path.is_file():
        logger.warning("registry.load_skipped — file not found: %s", path)
        return {}

    try:
        data = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001
        logger.error("registry.load_failed — %s: %s", path, exc)
        return {}

    out: Dict[str, WorkflowSchema] = {}
    for entry in data.get("workflows", []):
        try:
            schema = _coerce_workflow(entry)
            out[schema.workflow_type] = schema
        except KeyError as exc:
            logger.error("registry.entry_invalid missing=%s entry=%s", exc, entry)
    logger.info("registry.loaded path=%s count=%d", path, len(out))
    return out


def get(workflow_type: str) -> Optional[WorkflowSchema]:
    return load_registry().get(workflow_type)


def all_workflows() -> List[WorkflowSchema]:
    return list(load_registry().values())
