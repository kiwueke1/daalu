#!/usr/bin/env python
"""Regenerate the generated JSON Schemas under ``spec/``.

    .venv/bin/python scripts/gen_specs.py          # write
    .venv/bin/python scripts/gen_specs.py --check  # exit 1 if out of date

Sources: ``core/aiops/policy.policy_json_schema()``, the models in
``core/aiops/specs.py`` and, when present, ``evals/scenario.scenario_json_schema()``.
``tests/test_specs.py`` fails when the files on disk drift from this output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_DIR = ROOT / "spec"
sys.path.insert(0, str(ROOT / "src"))


def render(schema: dict) -> str:
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def generated() -> dict[str, str]:
    from daalu_automation.core.aiops.specs import all_schemas

    return {name: render(schema) for name, schema in all_schemas().items()}


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    for name, text in generated().items():
        path = SPEC_DIR / name
        current = path.read_text() if path.exists() else None
        if current == text:
            continue
        stale.append(name)
        if not check:
            path.write_text(text)
            print(f"wrote {path.relative_to(ROOT)}")
    if check and stale:
        print("out of date: " + ", ".join(stale) + " (run scripts/gen_specs.py)")
        return 1
    if not stale:
        print("spec/ schemas are up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
