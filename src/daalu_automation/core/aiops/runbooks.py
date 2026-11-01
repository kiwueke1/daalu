"""Runbooks as code (design §5).

A runbook is a markdown file with YAML frontmatter::

    ---
    id: dummy-app-down
    title: DummyApp is down
    match:
      alertname: [DummyAppDown]
      labels: {namespace: daalu-demo}
    allowed_actions: [rollout_undo, restart_deployment]
    ---
    ## Symptoms
    ...
    ## Diagnosis
    ...
    ## Fix
    ...

Published runbooks whose ``match`` fits an alert are injected into the
triage context (:func:`match_for_alert` + :func:`render_for_prompt`). After a
verified fix with no matching runbook, :func:`maybe_draft_from_investigation`
asks the LLM to write one, stored as a draft until a human publishes it.
"""

from __future__ import annotations

import fnmatch
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

STATUSES = ("draft", "published")
SOURCES = ("manual", "generated", "imported")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_FRONTMATTER_KEYS = {"id", "title", "match", "allowed_actions", "status", "description"}
_MATCH_KEYS = {"alertname", "labels"}
_FM_RE = re.compile(r"\A﻿?---[ \t]*\r?\n(?P<fm>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(?P<body>.*)\Z", re.DOTALL)

# Prompt budget for injected runbooks.
_PROMPT_MAX_RUNBOOKS = 3
_PROMPT_MAX_BODY = 3000


class RunbookError(ValueError):
    """A runbook file or payload is malformed. ``errors`` lists every problem."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class ParsedRunbook:
    slug: str
    title: str
    body: str
    match: dict[str, Any] = field(default_factory=lambda: {"alertname": [], "labels": {}})
    allowed_actions: list[str] = field(default_factory=list)
    status: str | None = None  # only when the frontmatter sets it


# ── Parsing / validation ────────────────────────────────────────────────────


def slugify(text: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", text or "")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:120] or "runbook"


def normalize_match(raw: Any, errors: list[str] | None = None) -> dict[str, Any]:
    """``{alertname: [str], labels: {str: str}}`` from loose input."""
    errs = errors if errors is not None else []
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        errs.append("`match` must be a mapping")
        raw = {}
    extra = set(raw) - _MATCH_KEYS
    if extra:
        errs.append(f"`match` has unknown keys: {sorted(extra)}")
    names = raw.get("alertname") or []
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list) or not all(isinstance(n, (str, int)) for n in names):
        errs.append("`match.alertname` must be a list of strings")
        names = []
    labels = raw.get("labels") or {}
    if not isinstance(labels, dict):
        errs.append("`match.labels` must be a mapping of label → value")
        labels = {}
    return {
        "alertname": [str(n).strip() for n in names if str(n).strip()],
        "labels": {str(k): "" if v is None else str(v) for k, v in labels.items()},
    }


def validate(data: dict[str, Any], body: str) -> ParsedRunbook:
    from daalu_automation.core import kube_tools

    errors: list[str] = []
    extra = set(data) - _FRONTMATTER_KEYS
    if extra:
        errors.append(f"unknown frontmatter keys: {sorted(extra)}")
    slug = str(data.get("id") or "").strip()
    if not slug:
        errors.append("`id` is required")
    elif not SLUG_RE.match(slug):
        errors.append(f"`id` {slug!r} must be lowercase letters, digits and dashes")
    title = str(data.get("title") or "").strip()
    if not title:
        errors.append("`title` is required")
    elif len(title) > 255:
        errors.append("`title` must be at most 255 characters")
    match = normalize_match(data.get("match"), errors)
    if not match["alertname"] and not match["labels"]:
        errors.append("`match` needs at least one `alertname` or label")
    actions = data.get("allowed_actions") or []
    if isinstance(actions, str):
        actions = [actions]
    if not isinstance(actions, list):
        errors.append("`allowed_actions` must be a list of tool names")
        actions = []
    actions = [str(a) for a in actions]
    unknown = [a for a in actions if a not in kube_tools.TOOLS]
    if unknown:
        errors.append(f"`allowed_actions` names unknown tools: {unknown}")
    status = data.get("status")
    if status is not None and status not in STATUSES:
        errors.append(f"`status` must be one of {list(STATUSES)}")
    if not body.strip():
        errors.append("the runbook body is empty")
    if errors:
        raise RunbookError(errors)
    return ParsedRunbook(
        slug=slug, title=title, body=body.strip() + "\n", match=match,
        allowed_actions=actions, status=status,
    )


def parse_markdown(text: str) -> ParsedRunbook:
    """Parse and validate a runbook file. Raises :class:`RunbookError`."""
    m = _FM_RE.match(text or "")
    if not m:
        raise RunbookError(["missing YAML frontmatter (the file must start with a `---` block)"])
    try:
        data = yaml.safe_load(m.group("fm")) or {}
    except yaml.YAMLError as e:
        raise RunbookError([f"frontmatter is not valid YAML: {e}"]) from e
    if not isinstance(data, dict):
        raise RunbookError(["frontmatter must be a YAML mapping"])
    return validate(data, m.group("body"))


def lint_markdown(text: str) -> list[str]:
    """Every problem with a runbook file (empty list = valid). Also warns when
    the usual Symptoms / Diagnosis / Fix sections are missing."""
    try:
        rb = parse_markdown(text)
    except RunbookError as e:
        return e.errors
    problems = []
    for section in ("Symptoms", "Diagnosis", "Fix"):
        if not re.search(rf"^#+\s*{section}\b", rb.body, re.IGNORECASE | re.MULTILINE):
            problems.append(f"warning: no `## {section}` section")
    return problems


def to_markdown(rb: Any) -> str:
    """Serialize a stored runbook back into the file format."""
    fm: dict[str, Any] = {
        "id": rb.slug,
        "title": rb.title,
        "match": normalize_match(rb.match),
        "allowed_actions": list(rb.allowed_actions or []),
    }
    head = yaml.safe_dump(fm, sort_keys=False, default_flow_style=None).strip()
    return f"---\n{head}\n---\n{(rb.body or '').strip()}\n"


def import_dir(path: str | Path) -> list[dict[str, Any]]:
    """Read every ``*.md`` runbook under ``path`` (README files skipped).

    Returns ``[{path, markdown, runbook: ParsedRunbook | None, errors: [str]}]``
    so callers (the CLI's ``import`` and ``lint``) can report per file.
    """
    root = Path(path)
    files = [root] if root.is_file() else sorted(root.rglob("*.md"))
    out: list[dict[str, Any]] = []
    for f in files:
        if f.name.lower() == "readme.md":
            continue
        text = f.read_text(encoding="utf-8")
        try:
            rb = parse_markdown(text)
            errors: list[str] = []
        except RunbookError as e:
            rb, errors = None, e.errors
        out.append({"path": str(f), "markdown": text, "runbook": rb, "errors": errors})
    return out


# ── Storage ─────────────────────────────────────────────────────────────────


def _uuid(v: Any) -> uuid.UUID | None:
    if isinstance(v, uuid.UUID):
        return v
    try:
        return uuid.UUID(str(v))
    except (ValueError, TypeError):
        return None


async def list_runbooks(db: AsyncSession, tenant_id: uuid.UUID, status: str | None = None) -> list[Any]:
    from daalu_automation.models import Runbook

    stmt = select(Runbook).where(Runbook.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(Runbook.status == status)
    stmt = stmt.order_by(Runbook.updated_at.desc())
    return list((await db.execute(stmt)).scalars().all())


async def get_runbook(db: AsyncSession, tenant_id: uuid.UUID, id_or_slug: str) -> Any | None:
    from daalu_automation.models import Runbook

    rid = _uuid(id_or_slug)
    cond = Runbook.id == rid if rid is not None else Runbook.slug == str(id_or_slug)
    return (
        await db.execute(select(Runbook).where(Runbook.tenant_id == tenant_id, cond))
    ).scalar_one_or_none()


def apply_fields(
    rb: Any, *, title: str, body: str, match: dict, allowed_actions: list[str], is_new: bool = False,
) -> bool:
    """Set content fields; on an existing runbook bump ``version`` when
    anything changed. Returns whether it changed."""
    new = (title, body.strip() + "\n", normalize_match(match), list(allowed_actions))
    old = (rb.title, rb.body, normalize_match(rb.match), list(rb.allowed_actions or []))
    if not is_new and new == old:
        return False
    rb.title, rb.body, rb.match, rb.allowed_actions = new
    if not is_new:
        rb.version = int(rb.version or 1) + 1
    return True


async def upsert_from_markdown(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    markdown: str,
    *,
    source: str = "imported",
) -> tuple[Any, bool, ParsedRunbook]:
    """Create or update (by slug) a runbook from a file. Does not publish and
    does not commit. Returns ``(runbook, created, parsed)`` — ``parsed.status``
    tells the caller whether the file asked to be published."""
    from daalu_automation.models import Runbook

    parsed = parse_markdown(markdown)
    rb = await get_runbook(db, tenant_id, parsed.slug)
    created = rb is None
    if created:
        rb = Runbook(
            tenant_id=tenant_id, slug=parsed.slug, status="draft", source=source,
            version=1, match={}, allowed_actions=[],
        )
    apply_fields(
        rb, title=parsed.title, body=parsed.body, match=parsed.match,
        allowed_actions=parsed.allowed_actions, is_new=created,
    )
    if created:
        db.add(rb)
    await db.flush()
    return rb, created, parsed


def publish(rb: Any, *, by: str) -> None:
    rb.status = "published"
    rb.published_by = (by or "")[:255]
    rb.published_at = datetime.now(tz=timezone.utc)


def to_out(rb: Any) -> dict[str, Any]:
    """``RunbookOut`` (design §13)."""
    return {
        "id": str(rb.id),
        "slug": rb.slug,
        "title": rb.title,
        "body": rb.body,
        "match": normalize_match(rb.match),
        "allowed_actions": list(rb.allowed_actions or []),
        "status": rb.status,
        "source": rb.source,
        "version": rb.version,
        "from_investigation_id": str(rb.from_investigation_id) if rb.from_investigation_id else None,
        "published_by": rb.published_by or "",
        "published_at": rb.published_at,
        "created_at": rb.created_at,
        "updated_at": rb.updated_at,
    }


# ── Matching ────────────────────────────────────────────────────────────────


def alert_labels(alert: Any) -> dict[str, str]:
    """The alert's labels. Top-level scalar metadata (namespace, deployment,
    cluster…) count as labels too, with ``metadata.labels`` taking priority."""
    md = getattr(alert, "metadata_json", None) or {}
    out = {
        str(k): str(v) for k, v in md.items()
        if isinstance(v, (str, int, float, bool)) and k != "labels"
    }
    labels = md.get("labels")
    if isinstance(labels, dict):
        out.update({str(k): str(v) for k, v in labels.items() if v is not None})
    return out


def alert_name(alert: Any) -> str:
    md = getattr(alert, "metadata_json", None) or {}
    labels = md.get("labels") if isinstance(md.get("labels"), dict) else {}
    return str(labels.get("alertname") or md.get("alert_name") or getattr(alert, "title", "") or "")


def matches(match: dict[str, Any] | None, *, name: str, labels: dict[str, str]) -> bool:
    m = normalize_match(match)
    if not m["alertname"] and not m["labels"]:
        return False  # an empty match would attach to every alert
    if m["alertname"]:
        n = name.strip().lower()
        if not any(fnmatch.fnmatchcase(n, p.lower()) for p in m["alertname"]):
            return False
    return all(labels.get(k) == v for k, v in m["labels"].items())


async def match_for_alert(db: AsyncSession, tenant_id: uuid.UUID, alert: Any) -> list[Any]:
    """Published runbooks whose ``match`` fits the alert."""
    name = alert_name(alert)
    labels = alert_labels(alert)
    return [
        rb for rb in await list_runbooks(db, tenant_id, status="published")
        if matches(rb.match, name=name, labels=labels)
    ]


def render_for_prompt(runbooks: list[Any]) -> str:
    """Compact prompt block for the triage system prompt ('' if none)."""
    if not runbooks:
        return ""
    parts = [
        "Team runbooks for this alert (written and published by the operators; "
        "follow them unless the evidence you gather contradicts them, and say "
        "which runbook you followed in Background):"
    ]
    for rb in runbooks[:_PROMPT_MAX_RUNBOOKS]:
        body = (rb.body or "").strip()
        if len(body) > _PROMPT_MAX_BODY:
            body = body[:_PROMPT_MAX_BODY].rstrip() + "\n…(truncated)"
        actions = ", ".join(rb.allowed_actions or []) or "any"
        parts.append(
            f"### Runbook {rb.slug} v{rb.version}: {rb.title}\n"
            f"Allowed system actions: {actions}\n{body}"
        )
    return "\n\n".join(parts)


# ── Drafting from a verified fix ────────────────────────────────────────────


_DRAFT_SYSTEM = """\
You write operational runbooks for an SRE team. You are given an alert, the
root cause an AI investigation found, and the remediation actions that were
executed and verified to fix it. Write a reusable runbook for the next time
this alert fires — general enough to apply again, specific about commands.

Reply with ONE JSON object:
{"title": "<short title>",
 "body": "<markdown with exactly three level-2 sections: ## Symptoms, ## Diagnosis, ## Fix>",
 "allowed_actions": ["<write tool names the Fix uses, from the list given>"]}
Symptoms: what the alert and workload look like. Diagnosis: the read-only
checks that confirm the cause (kubectl commands). Fix: numbered steps, each
with the exact command. No secrets, no tenant-specific IDs other than
namespace / workload names."""


async def _executed_actions(db: AsyncSession, alert_id: uuid.UUID) -> list[Any]:
    from daalu_automation.models import ActionStatus, AlertAction

    return list(
        (
            await db.execute(
                select(AlertAction)
                .where(
                    AlertAction.alert_id == alert_id,
                    AlertAction.requires_approval.is_(True),
                    AlertAction.status == ActionStatus.executed,
                )
                .order_by(AlertAction.created_at)
            )
        ).scalars().all()
    )


async def _unique_slug(db: AsyncSession, tenant_id: uuid.UUID, base: str) -> str:
    base = base[:110]
    slug, n = base, 1
    while await get_runbook(db, tenant_id, slug) is not None:
        n += 1
        slug = f"{base}-{n}"
    return slug


async def maybe_draft_from_investigation(
    db: AsyncSession, *, tenant_id: Any, alert: Any, investigation: Any
) -> Any | None:
    """After a verified fix: if no published runbook covers this alert and no
    draft exists for this investigation yet, have the LLM draft one. Never
    raises; returns the draft or ``None``."""
    from daalu_automation.core import kube_tools, llm
    from daalu_automation.core.aiops import audit
    from daalu_automation.models import Runbook

    try:
        if investigation is None or alert is None:
            return None
        tid = _uuid(tenant_id)
        if tid is None:
            return None
        if await match_for_alert(db, tid, alert):
            return None
        existing = (
            await db.execute(
                select(Runbook.id).where(
                    Runbook.tenant_id == tid,
                    Runbook.from_investigation_id == investigation.id,
                ).limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return None

        actions = await _executed_actions(db, alert.id)
        name = alert_name(alert)
        labels = alert_labels(alert)
        write_tools = sorted(n for n in kube_tools.TOOLS if kube_tools.TOOLS[n].requires_approval)
        action_lines = [
            f"- {kube_tools.render_tool_call(a.tool_name, {k: v for k, v in (a.tool_input or {}).items() if k != 'reason'})}"
            f" — verified_resolved={a.verified_resolved}"
            for a in actions
        ] or ["- (no recorded write actions)"]
        user = "\n".join([
            f"Alert name: {name}",
            f"Alert title: {getattr(alert, 'title', '')}",
            f"Alert labels: {labels}",
            f"Alert body:\n{(getattr(alert, 'body', '') or '')[:1500]}",
            "",
            f"Root cause found by the investigation:\n{(investigation.root_cause or '(not recorded)')[:2000]}",
            "",
            "Executed and verified actions:",
            *action_lines,
            "",
            f"Write tool names you may list in allowed_actions: {write_tools}",
        ])
        try:
            data = await llm.complete_json(
                system=_DRAFT_SYSTEM, user=user, tenant_id=tid,
                source="aiops.runbook.draft", max_tokens=1500,
            )
        except llm.LLMUnavailable as e:
            logger.info("aiops.runbook.draft_skipped", reason=str(e))
            return None
        if not isinstance(data, dict):
            return None
        title = str(data.get("title") or f"{name}: recovery").strip()[:255]
        body = str(data.get("body") or "").strip()
        if not body:
            return None
        allowed = data.get("allowed_actions") or [a.tool_name for a in actions]
        if not isinstance(allowed, list):
            allowed = []
        allowed = [str(a) for a in allowed if str(a) in write_tools]
        if not allowed:
            allowed = sorted({a.tool_name for a in actions if a.tool_name in write_tools})

        # The match is ours, not the model's: this alert name, scoped to its
        # namespace when it has one.
        match: dict[str, Any] = {"alertname": [name] if name else [], "labels": {}}
        if labels.get("namespace"):
            match["labels"]["namespace"] = labels["namespace"]
        slug = await _unique_slug(db, tid, slugify(name or title))
        rb = Runbook(
            tenant_id=tid, slug=slug, title=title, body=body + "\n", match=match,
            allowed_actions=allowed, status="draft", source="generated", version=1,
            from_investigation_id=investigation.id,
        )
        db.add(rb)
        await db.flush()
        await audit.record(
            db, tid, "runbook.drafted", actor="agent", subject=str(alert.id),
            data={"runbook_id": str(rb.id), "slug": slug,
                  "investigation_id": str(investigation.id)},
        )
        await db.commit()
        logger.info("aiops.runbook.drafted", slug=slug, alert_id=str(alert.id))
        return rb
    except Exception as e:  # noqa: BLE001 — drafting is a nice-to-have
        logger.warning("aiops.runbook.draft_failed", error=str(e))
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
