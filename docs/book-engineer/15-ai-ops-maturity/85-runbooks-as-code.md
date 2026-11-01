# 85. Runbooks as code

A runbook is a markdown file with YAML frontmatter that says which alerts it
applies to. Published runbooks that match an alert are injected into the
AI's triage context, so the agent follows the team's procedure instead of
rediscovering it. After a verified fix with no matching runbook, the AI
drafts one for a human to review. Runbooks are a Level 4 practice: each
incident leaves something behind that makes the next one faster.

Code: `core/aiops/runbooks.py`, `core/aiops/runbooks_cli.py`,
`api/routers/aiops_runbooks.py`; examples in `runbooks/`.

---

## The file format

```markdown
---
id: dummy-app-down
title: dummy-app is down
match:
  alertname: [DummyAppDown]
  labels: {namespace: daalu-demo}
allowed_actions: [rollout_undo, restart_deployment]
status: published        # optional: draft (default) | published
---
## Symptoms
...
## Diagnosis
...
## Fix
...
```

`parse_markdown` (`core/aiops/runbooks.py:152`) splits the frontmatter
(`_FM_RE`, `:48`, tolerates a BOM and CRLF) and `validate` (`:109`) collects
**every** problem before raising `RunbookError(errors)`:

* allowed frontmatter keys: `id`, `title`, `match`, `allowed_actions`,
  `status`, `description` (`:46`). `description` is accepted but not stored;
* `id` is a slug, `^[a-z0-9][a-z0-9-]{0,127}$`, unique per tenant;
* `title` required, ≤ 255 characters;
* `match` may only have `alertname` (a string or list) and `labels` (a
  mapping), and needs at least one of them;
* every `allowed_actions` entry must be a tool name in `kube_tools.TOOLS`;
* the body must not be empty.

`lint_markdown` (`:166`) adds warnings (not errors) for a missing
`## Symptoms`, `## Diagnosis` or `## Fix` section. The published JSON
Schema for the frontmatter is `spec/runbook.schema.json` (chapter 90).

---

## Matching

`match_for_alert` (`:351`) returns the tenant's **published** runbooks
whose `match` fits the alert. `matches` (`:340`):

* An empty match never matches (it would attach to every alert).
* `alertname`: the alert name, lower-cased, must `fnmatchcase` one of the
  patterns, lower-cased. So matching is case-insensitive and globs work
  (`DummyApp*`). The alert name is `metadata.labels.alertname`, else
  `metadata.alert_name`, else the alert title (`alert_name`, `:334`).
* `labels`: every key must **equal** the alert's value. The alert's labels
  are `metadata.labels` plus any top-level scalar metadata (`namespace`,
  `cluster`, `deployment`, ...), with `metadata.labels` winning
  (`alert_labels`, `:320`).

The design doc describes exact name matching; the code is case-insensitive
with globs, because Alertmanager rule names drift in case between teams and
`KubePod*` is a natural way to cover a family.

---

## Injection into triage

At the start of every triage turn loop, `_runbook_context`
(`api/routers/alert_chat.py:853`) calls `match_for_alert` inside a
savepoint, appends the matched ids to `investigation.runbook_ids`, and
returns `render_for_prompt` (`core/aiops/runbooks.py:361`): at most three
runbooks (`_PROMPT_MAX_RUNBOOKS`), each body truncated at 3000 characters,
under a header telling the model to follow them unless the evidence
contradicts them and to say which one it followed. `_ask_model` appends that
block to the system prompt (`api/routers/alert_chat.py:706`). Any error
returns `""`: a broken runbook must never break triage.

### `allowed_actions` is guidance, not enforcement

The block renders "Allowed system actions: rollout_undo, ..." for each
runbook. Nothing checks a proposed write against it. Enforcement is the
autonomy policy's job (chapter 82). We kept them separate deliberately:
runbooks are written by whoever knows the service and published by an
admin; the policy is the single, versioned, audited place that decides what
may run. Two enforcement points that can disagree would make "why was this
denied?" unanswerable.

---

## Drafting a runbook from a verified fix

After `_record_outcome` commits a verified-resolved action, it calls
`maybe_draft_from_investigation` (`core/aiops/runbooks.py:428`). It returns
without drafting when a published runbook already matches the alert, or a
draft from this investigation already exists. Otherwise it gathers the
alert, the investigation's root cause and the executed write actions with
their `verified_resolved`, and asks the LLM (`llm.complete_json`,
`source="aiops.runbook.draft"`) for `{title, body, allowed_actions}` with
the three standard sections.

What the model doesn't get to decide:

* **the match**: always `alertname: [<this alert's name>]`, plus
  `labels.namespace` when the alert has one (`:502`). A model-invented
  match could attach the draft to unrelated alerts;
* `allowed_actions` are filtered to real write tools, falling back to the
  tools that actually ran;
* the slug is `slugify(alert name)` made unique with `-2`, `-3`, ...

The row is `status=draft`, `source=generated`,
`from_investigation_id=<investigation>`, and a `runbook.drafted` audit event
is written. The whole function never raises; if no LLM is reachable it logs
`aiops.runbook.draft_skipped` and returns.

Drafts are never published automatically. A draft written by the same model
whose diagnosis it encodes would let one wrong root cause steer every future
triage of that alert. A human review is the circuit breaker for the learning
loop itself.

---

## Storage and versioning

Table `runbooks`, model `Runbook` (`models/aiops.py:114`): `slug` (unique
per tenant), `title`, `body`, `match` (JSON), `allowed_actions` (JSON),
`status` (`draft`/`published`), `source` (`manual`/`generated`/`imported`),
`version`, `from_investigation_id`, `published_by`, `published_at`.

`apply_fields` (`core/aiops/runbooks.py:246`) bumps `version` only when
title, body, match or allowed actions actually change, so re-importing an
unchanged file is a no-op.

---

## API

`api/routers/aiops_runbooks.py`, all under `/api/v1/aiops`:

| Method | Path | Line | Who |
|---|---|---|---|
| GET | `/runbooks?status=` | `:115` | any user |
| GET | `/runbooks/match?alert_id=` | `:126` | any user |
| POST | `/runbooks` | `:142` | any user for drafts; admin to publish or change a published one |
| GET | `/runbooks/{id or slug}` | `:187` | any user |
| PUT | `/runbooks/{id or slug}` | `:196` | any user for drafts; admin if published or publishing |
| POST | `/runbooks/{id}/publish` | `:249` | admin |
| DELETE | `/runbooks/{id}` | `:264` | any user for drafts; admin if published |

`POST` and `PUT` accept either structured fields
`{title, body, match, allowed_actions, slug?, status?}` or `{markdown}` (a
whole file). A `POST` with `markdown` **upserts by slug** (201 created, 200
updated), which is what makes `daalu runbooks import` idempotent; a
structured `POST` with an existing slug is a 409. `PUT` with
`status: draft` unpublishes. Editing a published runbook through `PUT`
re-records `runbook.published` with the editor, because the content the
agent reads changed.

Why admins: a published runbook is injected into every matching triage, so
it steers the agent the same way the policy does. Drafts are cheap and
anyone can write one.

---

## `daalu runbooks`

The server CLI (`daalu_automation.cli`, `core/aiops/runbooks_cli.py`):

```bash
daalu runbooks lint runbooks/                    # offline; exit 1 on any invalid file
DAALU_URL=https://daalu.example.com DAALU_TOKEN=dpat_... \
  daalu runbooks import runbooks/ [--publish]    # POST each file as {markdown}
```

`import_dir` (`core/aiops/runbooks.py:192`) reads every `*.md` under the
path recursively, skipping `README.md`. `import` adds `/api/v1` to the URL
if missing, reports `created`/`updated` per file, and with `--publish`
publishes each one that isn't already published (an admin token is
required).

The repo ships three examples in `runbooks/` (`demo-pod-crashlooping.md`,
`dummy-app-down.md`, `metrics-app-high-error-rate.md`) for the demo lab;
`tests/test_aiops_runbooks.py::test_repo_example_runbooks_are_valid` keeps
them valid.

---

## Failure modes and gaps

| Symptom | Cause |
|---|---|
| A published runbook isn't used | The alert's name or labels don't match; check `GET /aiops/runbooks/match?alert_id=`. Only the first three matches are injected. |
| No draft after a successful fix | A published runbook already matches, a draft for that investigation exists, no LLM was available, or the model returned an empty body (`aiops.runbook.draft_*` logs). |
| Several drafts for the same alert | One per investigation: each verified fix of a new firing of an alert with no *published* runbook drafts again. Publish (or delete) them. |
| Re-importing an edited published runbook leaves no audit trace | Known gap: the `{markdown}` upsert path changes a published runbook (admin required) without writing `runbook.published`, and `import --publish` skips runbooks that are already published. Edits through `PUT` are audited. |

---

## Testing

`tests/test_aiops_runbooks.py`: parsing and rejection, lint warnings,
round-trip to markdown, matching rules, prompt rendering, upsert/versioning,
drafting (and not drafting when a published runbook matches, and never
raising), triage injection and `runbook_ids`, the API lifecycle, the CLI
lint.
