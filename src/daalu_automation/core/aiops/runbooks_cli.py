"""``daalu runbooks`` — import and lint runbook files (design §5).

* ``daalu runbooks lint <dir>``   — offline: validate every ``*.md`` runbook.
* ``daalu runbooks import <dir>`` — POST each file as ``{markdown}`` to a live
  Daalu (``DAALU_URL`` / ``DAALU_TOKEN``). Re-importing updates by slug.
  ``--publish`` publishes each one too (needs a tenant-admin token).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import typer

app = typer.Typer(help="Runbooks as code: lint and import runbook markdown files.")


def _api_base(url: str) -> str:
    base = url.rstrip("/")
    if not base.endswith("/api/v1"):
        base += "/api/v1"
    return base


def _require_dir(path: Path) -> None:
    if not path.exists():
        typer.echo(f"error: {path} does not exist", err=True)
        raise typer.Exit(2)


@app.command("lint")
def lint(path: Path = typer.Argument(..., help="Directory (or single file) of runbook .md files.")) -> None:
    """Validate runbook files offline. Exits 1 if any file is invalid."""
    from daalu_automation.core.aiops import runbooks as rbk

    _require_dir(path)
    results = rbk.import_dir(path)
    if not results:
        typer.echo(f"no runbook files under {path}")
        raise typer.Exit(1)
    bad = 0
    for r in results:
        problems = r["errors"] or rbk.lint_markdown(r["markdown"])
        errors = [p for p in problems if not p.startswith("warning:")]
        if errors:
            bad += 1
            typer.echo(f"FAIL {r['path']}")
        else:
            typer.echo(f"ok   {r['path']}  ({r['runbook'].slug})")
        for p in problems:
            typer.echo(f"     - {p}")
    typer.echo(f"{len(results) - bad}/{len(results)} valid")
    if bad:
        raise typer.Exit(1)


@app.command("import")
def import_(
    path: Path = typer.Argument(..., help="Directory (or single file) of runbook .md files."),
    url: str = typer.Option("http://localhost:8000", envvar="DAALU_URL", help="Daalu API base URL."),
    token: str = typer.Option(..., envvar="DAALU_TOKEN", help="dpat_ personal access token."),
    publish: bool = typer.Option(False, "--publish", help="Publish each runbook after importing."),
) -> None:
    """Upload runbook files to a live Daalu. Exits 1 if any file failed."""
    from daalu_automation.core.aiops import runbooks as rbk

    _require_dir(path)
    results = rbk.import_dir(path)
    if not results:
        typer.echo(f"no runbook files under {path}")
        raise typer.Exit(1)
    base = _api_base(url)
    headers = {"Authorization": f"Bearer {token}"}
    failed = 0
    with httpx.Client(timeout=30.0, headers=headers) as client:
        for r in results:
            if r["errors"]:
                failed += 1
                typer.echo(f"skip {r['path']}: " + "; ".join(r["errors"]))
                continue
            try:
                resp = client.post(f"{base}/aiops/runbooks", json={"markdown": r["markdown"]})
                if resp.status_code >= 400:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                rb = resp.json()
                verb = "created" if resp.status_code == 201 else "updated"
                if publish and rb.get("status") != "published":
                    pub = client.post(f"{base}/aiops/runbooks/{rb['id']}/publish")
                    if pub.status_code >= 400:
                        raise RuntimeError(f"publish HTTP {pub.status_code}: {pub.text[:300]}")
                    rb = pub.json()
                typer.echo(f"{verb} {rb['slug']} v{rb['version']} ({rb['status']})")
            except (httpx.HTTPError, RuntimeError, ValueError, KeyError) as e:
                failed += 1
                typer.echo(f"fail {r['path']}: {e}")
    typer.echo(f"{len(results) - failed}/{len(results)} imported")
    if failed:
        raise typer.Exit(1)
