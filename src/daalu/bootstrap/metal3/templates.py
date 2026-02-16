# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, Iterable
from textwrap import dedent

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound
import logging

log = logging.getLogger("daalu")


REQUIRED_METAL3_TEMPLATE_FILES = {
    "clusterctl-vars.yaml",
    "cluster-template-cluster.yaml",
    "cluster-template-controlplane.yaml",
    "cluster-template-workers.yaml",
}


@dataclass(frozen=True)
class RenderedTemplate:
    name: str
    path: Path


class Metal3TemplateError(RuntimeError):
    pass


def render_jinja_templates(
    *,
    templates_root: Path,
    src_files: Iterable[str],
    dst_dir: Path,
    context: Dict[str, Any],
) -> list[RenderedTemplate]:
    env = Environment(
        loader=FileSystemLoader(str(templates_root)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    log.debug(f"context is {context}")

    dst_dir.mkdir(parents=True, exist_ok=True)

    rendered: list[RenderedTemplate] = []

    for rel in src_files:
        try:
            tmpl = env.get_template(rel)
        except TemplateNotFound as e:
            raise Metal3TemplateError(f"Missing template: {rel}") from e

        text = tmpl.render(**context)
        out_path = dst_dir / Path(rel).name
        out_path.write_text(text, encoding="utf-8")

        rendered.append(RenderedTemplate(name=rel, path=out_path))

    return rendered


def resolve_release_templates_dir(
    *,
    templates_root: Path,
    release_branch: str,
) -> Path:
    """
    Resolve and validate Metal3 release template directory.
    """

    release_dir = templates_root / release_branch

    if not release_dir.is_dir():
        raise Metal3TemplateError(
            dedent(f"""
            Metal3 release directory does not exist or is not a directory.

            Expected:
              {release_dir}

            This must be copied from:
              metal3-dev-env/tests/roles/run_tests/templates/{release_branch}
            """).strip()
        )

    present = {p.name for p in release_dir.iterdir() if p.is_file()}
    missing = REQUIRED_METAL3_TEMPLATE_FILES - present

    if missing:
        raise Metal3TemplateError(
            dedent(f"""
            Metal3 release directory is incomplete: {release_branch}

            Missing files:
            {chr(10).join(sorted(missing))}

            Directory:
              {release_dir}
            """).strip()
        )

    return release_dir

def render_jinja_text(template_text: str, context: dict) -> str:
    env = Environment(
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.from_string(template_text)
    return template.render(**context)