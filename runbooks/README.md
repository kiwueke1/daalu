# Runbooks as code

Each `*.md` file in this directory is one runbook: YAML frontmatter that says
which alerts it applies to, then the procedure in markdown. When an alert
fires, Daalu injects every **published** runbook whose `match` fits into the
AI investigation, so the agent follows your team's procedure rather than
working it out from scratch. The investigation records which runbooks it used.

Design reference: `docs/design/ai-ops-maturity.md` section 5.

## Format

```markdown
---
id: dummy-app-down                 # slug: lowercase letters, digits, dashes; unique per tenant
title: dummy-app is down           # shown in the UI
match:
  alertname: [DummyAppDown]        # any of these alert names (case-insensitive, globs allowed)
  labels: {namespace: daalu-demo}  # every label here must equal the alert's label
allowed_actions: [rollout_undo]    # write tools the Fix is expected to use
status: published                  # optional: draft (default) | published
---
## Symptoms
What the alert and the workload look like.

## Diagnosis
Read-only checks that confirm the cause.

## Fix
Numbered steps, each with the exact command.
```

Rules:

- `id`, `title` and a non-empty body are required. `match` needs at least one
  `alertname` or one label; a runbook with an empty match would apply to every
  alert, so it's rejected.
- The alert name is taken from the alert's `alertname` label (Alertmanager),
  then its `alert_name` metadata, then its title.
- Label values compare as strings. Top-level alert metadata such as
  `namespace` or `cluster` counts as a label too.
- `allowed_actions` must name real write tools: `rollout_undo`,
  `scale_deployment`, `restart_deployment`, `delete_pod`, `patch_resource`.
  The autonomy policy still decides whether each action needs approval.
- Unknown frontmatter keys are errors, so typos get caught.

## Workflow

```bash
daalu runbooks lint runbooks/                       # offline validation
export DAALU_URL=http://localhost:8000 DAALU_TOKEN=dpat_xxx
daalu runbooks import runbooks/                     # upload as drafts (updates by id)
daalu runbooks import runbooks/ --publish           # upload and publish (tenant admin)
```

Importing is idempotent: a file whose `id` already exists updates that
runbook and bumps its version. Publishing needs a tenant-admin token, because
published runbooks steer the agent. Each publish is recorded in the audit log
(`runbook.published`).

Daalu also writes runbooks itself. After the agent fixes an alert and the fix
is verified, if no published runbook covered that alert, it drafts one from
the investigation (`source: generated`, `status: draft`). Review it on the
Runbooks page, edit it, and publish it; it then applies the next time the
alert fires.

## Examples here

These target the demo lab in `demo/` (`./demo/up.sh`, then `./demo/break.sh`):

| File | Alert | Demo scenario |
|---|---|---|
| `dummy-app-down.md` | `DummyAppDown` | `break.sh bad-image`, `break.sh crashloop` |
| `metrics-app-high-error-rate.md` | `HighErrorRate` | `break.sh errors` |
| `demo-pod-crashlooping.md` | `KubePodCrashLooping` (kube-prometheus-stack) | `break.sh crashloop`, if left broken for about 15 minutes |
