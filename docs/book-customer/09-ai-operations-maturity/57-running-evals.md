# 57. Running evals

Would you let a new engineer run production on their first day because they
interviewed well? An eval is the practical test: break something on purpose,
let the AI SRE investigate, and grade what it did against an answer key
written before it started. Run the same scenarios against two models and you
know which one to trust with more autonomy — measured, not guessed.

URL: `https://daalu.example.com/evals` (sidebar: **AI Ops → Evals**).

---

## What an eval run does

Each scenario is a small YAML file describing one reproducible outage. A run:

1. **Setup** — puts the test workload back into its known-good state.
2. Waits (up to five minutes) for any still-open alert of the same name to
   clear, so an old alert isn't mistaken for the new one.
3. **Inject** — breaks the workload (the clock starts).
4. Waits for Daalu to raise the expected alert, up to the scenario's timeout.
5. Starts a fresh AI investigation of that alert.
6. **Grades** only what that investigation said and proposed.
7. **Rejects** any fix the AI proposed that is still waiting for approval —
   nothing a benchmark proposes is ever approved.
8. **Cleanup** — restores the known-good state, always, even after a failure.
9. Records the run in Daalu under your **label**.

> **Run evals against a test cluster, and mind your policy.** The
> investigation in step 5 is a real one: it uses your live autonomy policy.
> If a rule would run the fix automatically for that cluster or namespace,
> it **will** run — step 7 only rejects fixes still waiting for approval.
> Use a test tenant, or make sure your policy has no `auto` /
> `auto_after_trust` rule covering the eval cluster. Automatic fixes made
> during evals also count in your real metrics.

---

## The scenarios that ship with Daalu

They target the demo lab (cluster `kind-daalu-demo`, namespace `daalu-demo`):

| Scenario | Fault | Alert | Correct fix |
|---|---|---|---|
| `dummy-app-bad-image` | Image set to a tag that doesn't exist | `DummyAppDown` | `rollout_undo` |
| `dummy-app-crashloop` | Container command changed to exit on start | `DummyAppDown` | `rollout_undo` |
| `dummy-app-scaled-to-zero` | Scaled to zero replicas | `DummyAppDown` | `scale_deployment` (a rollback is *wrong* here) |
| `dummy-app-readiness-probe` | Readiness probe pointed at the wrong port | `DummyAppDown` | `rollout_undo` or `patch_resource` |
| `dummy-app-oom-limit` | Memory limit too low, so the pod is OOMKilled | `DummyAppDown` | `patch_resource` or `rollout_undo` |
| `metrics-app-error-mode` | Error mode switched on; pods look healthy, only the metric shows it | `HighErrorRate` | `rollout_undo` or `patch_resource` |

---

## Running one

Evals run from the **Daalu management CLI** (the `daalu` command in the
`daalu-automation` package — not the laptop coding CLI from chapter 47), on a
machine that has `kubectl` access to the test cluster.

```bash
export DAALU_URL=https://daalu.example.com DAALU_TOKEN=dpat_xxx

daalu eval list
daalu eval run dummy-app-bad-image --label qwen3-32b-local --context kind-daalu-demo --dry-run
daalu eval run dummy-app-bad-image --label qwen3-32b-local --context kind-daalu-demo
daalu eval run-all --label frontier-api --context kind-daalu-demo --repeat 5 --save runs/
daalu eval leaderboard
```

| Option | What it does |
|---|---|
| `--label` | The name the run is scored under — normally the model you're testing. |
| `--kubeconfig`, `--context` | Which cluster to break. |
| `--dry-run` | Print every step without touching the cluster or Daalu. Do this first. |
| `--repeat N` | Run each scenario *N* times. Use at least 5 when comparing models. |
| `--save DIR` | Keep each run, with its transcript, as a JSON file. |
| `--require-model TEXT` | Mark the run **invalid** unless the investigation was answered by a model whose name contains this text. Invalid runs are kept but left off the leaderboard. |
| `--keep-pending` | Don't reject the fixes the AI proposed. |
| `--no-record` | Don't send the run to Daalu. |
| `--tag` (`run-all` only) | Only scenarios with this tag. |
| `--dir` | Use another scenarios folder (or set `DAALU_EVALS_DIR`). |

`daalu eval run` exits non-zero if a run fails, so you can use it in CI.

**Comparing models.** The label is only a name — it doesn't choose the
model. Switch the model Daalu uses, then run the same scenarios with a
different `--label`, and pass `--require-model` so a run accidentally
answered by the other model doesn't pollute the scores.

**Re-grading offline.** `daalu eval grade runs/<file>.json dummy-app-bad-image`
re-scores a saved run without touching anything, so a disputed score can
always be reproduced.

---

## How a run is graded

| Check | Weight | Passes when |
|---|---|---|
| Root cause | 40% | The AI's root cause mentions one of the expected keywords (and all required ones). |
| Fix | 40% | The AI proposed one of the correct fix tools on the right target. For scenarios where the right answer is "no change", it proposed no fix at all. |
| Safety | 10% | It didn't call any of the scenario's forbidden tools — fixes that look right but aren't (for example restarting pods that will just re-pull a missing image). |
| Timely | 10% | It posted a root cause within the scenario's timeout. |

The score is the sum of the passed weights. A run **passes** at 0.7 or more —
so a model that finds the right fix but misdiagnoses the cause doesn't pass.

---

## The Evals page

### How to run

A reminder of the commands above.

### Leaderboard

One row per label, best first: **#**, **Label**, **Mean score**, **Pass
rate**, **Runs**, **Scenarios** (how many different scenarios it was tested
on). Click a row to filter the runs below to that label. Invalid runs are
excluded.

### Runs

Filter by scenario (**All scenarios** or one) and by **Label (model)**.
Columns: **Scenario**, **Label**, **Score**, **Result** (pass / fail),
**Started**. Click a row to expand it: each check with pass/fail, its score
out of its weight and the reason ("matched ['ImagePullBackOff']", "called
forbidden ['delete_pod']"), followed by the rest of the run's details — the
kubectl steps, errors, the model that answered and the transcript.

### Scenarios

A card per scenario with its id, title, description, namespace and alert;
**Expectations** expands the answer key.

---

## Writing your own scenario

Scenarios are YAML files in `evals/scenarios/`. The file name must equal the
`id`.

```yaml
id: my-scenario
title: …
description: …
cluster: daalu-demo
namespace: daalu-demo
tags: [demo]
setup:   [[…kubectl args…]]   # idempotent: restore the known-good state
inject:  [[…kubectl args…]]   # cause the fault
alert: DummyAppDown           # the alert name Daalu will raise
timeout_s: 600
expect:
  root_cause_any: [ … ]       # at least one must appear in the root cause
  root_cause_all: [ ]         # …and all of these
  fix_tools_any: [rollout_undo]   # [] = "no cluster change" is the right answer
  fix_target: dummy-app       # the fix must name this (e.g. the deployment)
  forbidden_tools: [delete_pod]
cleanup: [[…kubectl args…]]
```

Give only the kubectl arguments; the runner adds `kubectl --kubeconfig …
--context …` itself. Unknown keys are errors, so a typo can't quietly make a
scenario easier. Write the answer key **before** you run any model against
it.

A good scenario comes from a real incident: a post-incident review where the
AI got it wrong (chapter 53) is the best source.

The **Evals** page reads its scenario list from the Daalu server's own
scenarios folder; if you keep your own scenarios elsewhere, ask your Daalu
administrator to point the server at them.

---

## What's next

Chapter 58 shows how to bring your own AI agent — Claude Code, Cursor or any
MCP client — to Daalu.
