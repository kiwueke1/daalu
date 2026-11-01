# Eval scenarios: benchmark your AI SRE on real outages

Each file in `scenarios/` is one reproducible outage against the
[demo lab](../demo/README.md) (`kind-daalu-demo`, namespace `daalu-demo`):
how to break a known-good workload, which alert Daalu should raise, and the
answer key the AI is graded against. The answer key is written **before** any
model runs, so every model label is scored against the same ground truth.
Design contract: `docs/design/ai-ops-maturity.md` §8.

| Scenario | Fault (kubectl) | Alert | Correct fix |
|---|---|---|---|
| `dummy-app-bad-image` | `set image … dummy=nginx:this-tag-does-not-exist-9.9.9` (= `demo/break.sh bad-image`) | `DummyAppDown` | `rollout_undo` |
| `dummy-app-crashloop` | container command → `exit 1` (= `demo/break.sh crashloop`) | `DummyAppDown` | `rollout_undo` |
| `dummy-app-scaled-to-zero` | `scale --replicas=0` | `DummyAppDown` | `scale_deployment` (rollback is *wrong* here) |
| `dummy-app-readiness-probe` | readiness probe → port 8080; pod runs but never Ready | `DummyAppDown` | `rollout_undo` or `patch_resource` |
| `dummy-app-oom-limit` | memory limit 4Mi → OOMKilled | `DummyAppDown` | `patch_resource` or `rollout_undo` |
| `metrics-app-error-mode` | `ERROR_MODE=true` (= `demo/break.sh errors`); pods healthy, only the metric shows it | `HighErrorRate` | `rollout_undo` or `patch_resource` |

Every `dummy-app` scenario's `setup` and `cleanup` re-apply the known-good spec
from `demo/manifests/dummy-app.yaml` with an idempotent strategic-merge patch,
so a run always starts and ends healthy, even if the AI's fix already ran.

## Run

```bash
export DAALU_URL=http://localhost:8000 DAALU_TOKEN=dpat_xxx
daalu eval list
daalu eval run dummy-app-bad-image --label qwen3-32b-local --context kind-daalu-demo --dry-run
daalu eval run dummy-app-bad-image --label qwen3-32b-local --context kind-daalu-demo
daalu eval run-all --label frontier-api --context kind-daalu-demo --repeat 5 --save runs/
daalu eval leaderboard
```

A run: `setup` → wait for any open alert of that name to clear → `inject` →
poll `GET /api/v1/alerts` until the alert fires → `POST
/api/v1/alerts/{id}/triage?force=true` → grade only the messages that pass added
→ reject the writes it proposed (nothing a benchmark proposes is approved;
`--keep-pending` to skip) → `cleanup` (always) → `POST /api/v1/aiops/evals/runs`
under `--label`.

Comparing models (same outage, same tools, only the model changes): switch the
model Daalu routes to, then run with a different `--label`. Pass
`--require-model <substring>` and a run answered by any other model is recorded
as **invalid** (kept for transparency, excluded from the leaderboard). Run each
scenario at least five times (`--repeat 5`); `--save DIR` keeps every run with
its transcript so raw runs can be published and re-graded.

## Grading

| Check | Weight | Passes when |
|---|---|---|
| `root_cause` | 40 % | the `## Root cause` section mentions any `root_cause_any` keyword (and all `root_cause_all`) |
| `fix` | 40 % | a `fix_tools_any` write was proposed whose input mentions `fix_target`; with `fix_tools_any: []`, no write at all was proposed (for "a human must go look" scenarios) |
| `safety` | 10 % | no `forbidden_tools` called: tools that look like a fix but are wrong for this fault |
| `timely` | 10 % | a root cause arrived within `timeout_s` of the injection |

A run passes at score ≥ 0.7. Re-grade a saved run offline:

```bash
daalu eval grade runs/dummy-app-bad-image-qwen3-32b-local-….json dummy-app-bad-image
```

## Write a scenario

```yaml
id: my-scenario            # must equal the file name
title: …
description: …
cluster: daalu-demo
namespace: daalu-demo
tags: [demo]
setup:   [[…kubectl args…]] # idempotent: restore known-good
inject:  [[…kubectl args…]] # cause the fault
alert: DummyAppDown         # alertname = Daalu alert title
timeout_s: 600
expect:
  root_cause_any: [ … ]
  root_cause_all: [ ]
  fix_tools_any: [rollout_undo]   # [] = no cluster change is right
  fix_target: dummy-app
  forbidden_tools: [delete_pod]
cleanup: [[…kubectl args…]]
```

Give only kubectl arguments; the runner adds `kubectl --kubeconfig … --context …`.
The schema is strict (unknown keys fail). Scenarios for another lab (e.g. the
LLM-serving lab) can live in another directory: `--dir path/` or
`DAALU_EVALS_DIR=path/`. The API (`GET /api/v1/aiops/evals/scenarios`) reads
the same directory; in a container, mount it and set `DAALU_EVALS_DIR`.
