# 54. Runbooks

A runbook is your team's fix for a known problem, written down. In Daalu a
runbook is also something the AI reads: when an alert fires, every
**published** runbook that matches it goes into the AI's investigation, so it
starts from what your team already knows instead of from scratch. And when
the AI fixes something new and the fix is verified, it drafts a runbook for
next time and leaves it for you to review.

URL: `https://daalu.example.com/runbooks` (sidebar: **Automate → Runbooks**).

---

## The file format

A runbook is a markdown file with a short YAML header ("frontmatter"). This
is the same format whether you write it in the browser, keep it in Git, or
import a folder. Here is `runbooks/dummy-app-down.md`, one of the examples
that ship with Daalu:

```markdown
---
id: dummy-app-down
title: dummy-app is down (0 available replicas)
match:
  alertname: [DummyAppDown]
  labels: {namespace: daalu-demo}
allowed_actions: [rollout_undo]
---
## Symptoms
- `DummyAppDown` fires: `kube_deployment_status_replicas_available{deployment="dummy-app"} == 0` for 1 minute.
- dummy-app uses `strategy: Recreate`, so a bad rollout removes the old pod
  first and the service is fully down, not degraded.
...

## Diagnosis
1. Check what changed. `recent_changes` for namespace `daalu-demo` shows the
   newest dummy-app revision and what differs from the previous one ...

## Fix
1. Roll back to the previous revision (system step: `rollout_undo`) ...
2. Verify the rollout finished and the pod is Ready ...
```

| Field | Meaning |
|---|---|
| `id` | Required. Lowercase letters, digits and dashes. Unique in your tenant; importing a file with an existing `id` updates that runbook. |
| `title` | Required, up to 255 characters. |
| `match.alertname` | A list of alert names this runbook is for. Glob patterns allowed (`DummyApp*`). |
| `match.labels` | Labels the alert must have, with exact values. |
| `allowed_actions` | Which write tools the fix uses (`rollout_undo`, `scale_deployment`, `restart_deployment`, `delete_pod`, `patch_resource`, `call_external_api`). |
| `status` | Optional: `draft` or `published`. |
| `description` | Optional. |

The body should have three sections — `## Symptoms`, `## Diagnosis`,
`## Fix`. A runbook without them still saves; the lint command warns about
it. An empty body, an unknown frontmatter key, or an unknown tool name in
`allowed_actions` is an error.

Three examples ship in the `runbooks/` folder of the Daalu repository:
`demo-pod-crashlooping`, `dummy-app-down` and `metrics-app-high-error-rate`.

---

## How a runbook matches an alert

- The **alert name** is taken from the alert's `alertname` label; if there
  isn't one, its alert name; failing that, its title.
- If `match.alertname` is set, the alert name must match one of the entries.
  Matching ignores upper/lower case and allows `*` and `?` wildcards.
- Every label in `match.labels` must be on the alert with exactly that value.
  Alert fields such as `namespace`, `deployment` or `cluster` count as labels
  too.
- A runbook must set at least one alert name or label. (An empty match would
  attach to every alert, so it isn't allowed.)
- **Only published runbooks match.** Drafts are never shown to the AI.

When several published runbooks match, the AI gets up to three of them. It is
told to follow them unless the evidence it gathers contradicts them, and to
say which runbook it followed. The investigation records which runbooks were
used ("1 runbook used" on the alert's investigation record).

### `allowed_actions` is guidance, not a lock

The AI sees "Allowed system actions: …" next to each runbook and is steered
toward those tools. It is **not** an enforcement mechanism: a runbook can't
make a fix run on its own, and it can't stop one. What actually runs, waits
or is refused is decided by the autonomy policy (chapter 52). If you want a
tool forbidden in a namespace, write a `deny` rule.

---

## The Runbooks page

### The list

A **All / Drafts / Published** switch filters the list. Each entry shows its
title, a status chip — **published**, **draft**, or **AI-drafted, needs
review** for drafts the AI wrote (these also have an amber edge) — its id,
version, source (`manual`, `imported` or `generated`), the alert names it
matches, and when it was last updated.

Click an entry to open it on the right; click **New runbook** (top right) to
write one.

### The editor

Two ways to edit, switched at the top:

- **Fields** — **Title**, **Id (slug)** (filled from the title if you leave
  it blank), **Match alert names (comma-separated)**, **Match labels
  (key=value, one per line)**, **Allowed actions** (one checkbox per write
  tool), and **Body (markdown)**. A new runbook starts with empty Symptoms /
  Diagnosis / Fix sections.
- **Edit as file** — the whole file with its frontmatter, in the same format
  `daalu runbooks import` reads. Handy for pasting in an existing runbook.

Buttons:

- **Create draft** (new runbook) / **Save changes** (existing). If anything is
  wrong you get the full list of problems.
- **Publish** — for drafts.
- **Delete** — asks you to confirm: "The AI stops using it for matching
  alerts."

### Who can do what

| Action | Who |
|---|---|
| Create or edit a draft | Anyone on the team |
| Publish a runbook | Tenant admins |
| Edit a published runbook | Tenant admins (it stays published; an edit made on this page is recorded as a new publish in the audit log) |
| Delete a published runbook | Tenant admins |
| Delete a draft | Anyone |

Published runbooks steer the AI, so they are admin-controlled. Every publish
writes a `runbook.published` event to the audit log.

Each runbook carries a **version** number that goes up whenever its title,
body, match or allowed actions change. The AI is shown the version
("Runbook dummy-app-down v3"), so you can tell which text it worked from.

---

## AI-drafted runbooks

After a fix is **verified** to have resolved an alert, Daalu checks whether a
published runbook already covers that alert. If none does, the AI writes a
draft from the investigation: the alert, the root cause it found, and the
fixes that ran. The draft has Symptoms / Diagnosis / Fix sections; its match
is set by Daalu to the alert's name and, if the alert has one, its
namespace; and its allowed actions are limited to the write tools the fix
actually used.

The draft appears in the list with **AI-drafted, needs review**, and a
`runbook.drafted` event lands in the audit log. Nothing is used until a human
publishes it.

To review a draft:

1. Open it. Read the Diagnosis and Fix against what really happened.
2. Generalise anything specific to that one incident (pod names, revision
   numbers).
3. Check the match — widen or narrow the labels as needed.
4. **Save changes**, then an admin clicks **Publish**.

Only one draft is written per investigation. If the alert keeps recurring
without a published runbook, each new verified fix can produce another draft
— publish one, and delete the rest.

---

## Runbooks as code: lint and import

If your team already keeps runbooks in a Git repository, keep them there and
import them. Both commands are part of the Daalu management CLI (the
`daalu-automation` package).

**Check a folder offline:**

```bash
daalu runbooks lint runbooks/
```

Prints `ok` or `FAIL` per file with every problem, ends with "*n*/*m* valid",
and exits non-zero if any file is invalid — useful in CI.

**Import into Daalu:**

```bash
export DAALU_URL=https://daalu.example.com DAALU_TOKEN=dpat_xxx
daalu runbooks import runbooks/
daalu runbooks import runbooks/ --publish     # also publish each one
```

- Every `*.md` file in the folder (and its subfolders) is imported;
  `README.md` files are skipped.
- Re-importing updates runbooks by their `id`, so you can run it from CI on
  every merge. New runbooks arrive as drafts unless the file says
  `status: published` or you pass `--publish`; a runbook that is already
  published stays published and takes the new content. (That update does not
  write a fresh `runbook.published` audit event — review changes to
  published runbooks in Git.)
- Publishing, or updating a runbook that is already published, needs a
  token belonging to a tenant admin.
- The token is a personal access token from **Settings → API tokens**
  (chapter 37).

---

## What's next

Chapter 55 explains each number on the **Maturity** page — the metrics that
tell you whether the learning loop is working.
