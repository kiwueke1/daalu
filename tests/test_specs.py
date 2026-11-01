"""The open AOMM specs in ``spec/`` (docs/design/ai-ops-maturity.md §10):
generated schemas match ``scripts/gen_specs.py``, and every example artefact
parses with the model behind its schema."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "spec"
EXAMPLES = SPEC / "examples"


def _gen_specs():
    spec = importlib.util.spec_from_file_location("gen_specs", ROOT / "scripts" / "gen_specs.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_generated_schemas_are_up_to_date():
    generated = _gen_specs().generated()
    assert set(generated) == {
        "autonomy-policy.schema.json", "action-proposal.schema.json",
        "audit-event.schema.json", "investigation.schema.json",
        "runbook.schema.json", "eval-scenario.schema.json",
    }
    for name, text in generated.items():
        on_disk = (SPEC / name).read_text()
        assert on_disk == text, f"spec/{name} is stale: run scripts/gen_specs.py"
        schema = json.loads(on_disk)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"].endswith(name)
        assert schema["x-aomm-version"] == "0.1"


def test_every_schema_has_an_example():
    stems = {p.name.removesuffix(".schema.json") for p in SPEC.glob("*.schema.json")}
    examples = {p.name.split(".")[0] for p in EXAMPLES.iterdir()}
    assert stems <= examples


def _frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", text, re.DOTALL)
    assert m, "runbook example must start with a frontmatter block"
    return yaml.safe_load(m.group(1)), m.group(2)


@pytest.mark.parametrize("path", sorted(EXAMPLES.iterdir()), ids=lambda p: p.name)
def test_example_parses(path: Path):
    from daalu_automation.core.aiops import specs

    text = path.read_text()
    stem = path.name.split(".")[0]
    if stem == "autonomy-policy":
        from daalu_automation.core.aiops.policy import parse_policy

        doc = parse_policy(text)
        assert doc.rules and doc.guardrails.circuit_breaker is not None
    elif stem == "action-proposal":
        p = specs.ActionProposal.model_validate_json(text)
        assert p.input["reason"] == p.reason
        assert len(p.approvals) == p.approvals_required
    elif stem == "audit-event":
        from daalu_automation.core.aiops.audit import _body, compute_hash

        ev = specs.AuditEvent.model_validate_json(text)
        # The example's hash is the real chain hash: other implementations
        # can test their canonicalisation against it.
        assert ev.hash == compute_hash(
            ev.prev_hash, _body(ev.seq, ev.kind, ev.actor, ev.subject, ev.data, ev.created_at)
        )
    elif stem == "investigation":
        specs.Investigation.model_validate_json(text)
    elif stem == "runbook":
        fm, body = _frontmatter(text)
        specs.RunbookFrontmatter.model_validate(fm)
        for section in ("## Symptoms", "## Diagnosis", "## Fix"):
            assert section in body
        try:
            from daalu_automation.core.aiops.runbooks import parse_markdown
        except ImportError:
            return
        parsed = parse_markdown(text)
        assert parsed.slug == fm["id"]
    elif stem == "eval-scenario":
        from daalu_automation.evals.scenario import parse_scenario

        parse_scenario(text, source=str(path))
    else:
        pytest.fail(f"no parser for example {path.name}")


def test_audit_kind_pattern():
    from pydantic import ValidationError

    from daalu_automation.core.aiops import specs

    base = json.loads((EXAMPLES / "audit-event.json").read_text())
    specs.AuditEvent.model_validate({**base, "kind": "x-acme.custom"})
    with pytest.raises(ValidationError):
        specs.AuditEvent.model_validate({**base, "kind": "made.up"})
