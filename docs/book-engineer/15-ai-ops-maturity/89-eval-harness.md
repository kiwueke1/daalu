# 89. The eval harness

You wouldn't ship a service without tests; AOMM says you shouldn't change
the model behind your AI SRE without evals. The eval harness breaks a known
workload on purpose, lets Daalu's agent investigate the resulting alert
exactly as it would in production, grades what it did against an answer key
written in advance, and records the run under a label (usually a model
name). The leaderboard then compares labels on the same outages. This is a
Level 4 criterion (≥ 1 eval run in the window) and the basis for deciding
which model to trust with more autonomy.

Code: `src/daalu_automation/evals/` (`scenario.py`, `runner.py`,
`grader.py`, `scoreboard.py`, `cli.py`), scenarios in `evals/scenarios/`,
API in `api/routers/aiops_evals.py`. User guide: [`evals/README.md`](../../../evals/README.md).

---

## Scenario format

```yaml
id: dummy-app-bad-image                # must equal the file name
title: dummy-app rolled to an image tag that doesn't exist
description: ...
cluster: daalu-demo
namespace: daalu-demo
tags: [demo, kubernetes, rollout]
setup:   [[patch, deploy/dummy-app, ...], [rollout, status, ...]]   # idempotent: known-good
inject:  [[set, image, deploy/dummy-app, dummy=nginx:this-tag-does-not-exist-9.9.9, --namespace, daalu-demo]]
alert: DummyAppDown                    # alertname / Daalu alert title to wait for
timeout_s: 600                         # 30..7200
expect:
  root_cause_any: [ImagePullBackOff, ErrImagePull, this-tag-does-not-exist, image tag, pull]
  root_cause_all: []
  fix_tools_any: [rollout_undo]        # [] = "no cluster change" is the right answer
  fix_target: dummy-app                # the fix's input must mention this
  forbidden_tools: [restart_deployment, delete_pod]
cleanup: [[patch, deploy/dummy-app, ...], [rollout, status, ...]]
```

`Scenario` and `Expect` (`evals/scenario.py:92`, `:62`) are strict
Pydantic models. Commands are kubectl **argument lists**, not shell strings
(`_argv_list`, `:34`): no quoting bugs, no shell injection, and a scenario
can't run anything but kubectl. Starting a command with `kubectl` or
`--kubeconfig` is an error because the runner adds those. YAML scalars
(`0`, `true`) are coerced to strings. `load_scenario` (`:153`) requires
`id == file stem`; `load_scenarios` (`:176`) rejects duplicate ids. The
directory is `$DAALU_EVALS_DIR` (either `evals/` or `evals/scenarios/`),
else `<repo>/evals/scenarios` (`scenarios_dir`, `:165`).

`expect` has two fields beyond the design doc: `root_cause_all` (every
keyword required, for stricter keys) and `fix_target` (so rolling back the
wrong Deployment fails). The answer key is written **before** any model
runs; the strict schema means a typo in a key is an error rather than a
scenario that quietly becomes easier.

The repo ships six scenarios against the demo lab (`kind-daalu-demo`,
namespace `daalu-demo`): bad image, crashloop, scaled to zero (where
rollback is *wrong*), readiness probe on the wrong port, OOM limit, and
`metrics-app` error mode (pods healthy, only a metric shows it). Their
`setup` and `cleanup` re-apply the known-good spec with an idempotent
strategic-merge patch, so a run starts and ends healthy even if the AI's fix
already ran.

---

## The runner

`EvalRunner.run` (`evals/runner.py:192`) against a live Daalu (HTTP with a
PAT) and a cluster (`kubectl`, injected as a function so tests can fake it):

1. `setup` (stop on error);
2. wait up to 300 s for any still-open alert of that name to clear, then
   snapshot matching alerts by `id → last_seen_at`;
3. `inject`, start the clock;
4. poll `GET /api/v1/alerts` (every 10 s) until a matching alert appears
   that is new or re-fired (`last_seen_at` changed), not resolved or
   suppressed (`wait_for_alert`, `:179`; `alert_matches`, `:77`, compares
   title, `metadata.alert_name` and `labels.alertname`), up to `timeout_s`;
5. `POST /api/v1/alerts/{id}/triage?force=true`: a fresh agent pass through
   the normal alert-chat loop;
6. fetch the chat, keep only the messages this pass added, and grade them
   with `diagnosis_s` = seconds since inject;
7. read `GET /aiops/investigations/by-alert/{id}` for the model that
   answered; with `--require-model`, a mismatch marks the run
   `details.invalid`;
8. reject every write the pass left **pending** (unless `--keep-pending`):
   nothing a benchmark proposes is ever approved;
9. `cleanup`, always, in a `finally`;
10. `POST /api/v1/aiops/evals/runs` with score, pass, and `details`
    (checks, root cause, tools called, writes proposed, kubectl log, errors,
    and a compacted, re-gradable transcript).

Errors at any step (setup or inject failing, the alert never firing, HTTP
errors) are recorded in `details.errors` and the run is still cleaned up
and recorded with score 0.

### Caveat: evals run under the live policy

Step 5 is a real triage, so writes go through the tenant's autonomy policy.
A rule that decides `auto` for the eval cluster/namespace **will execute
the fix** before step 8 runs (step 8 only rejects pending writes). The
`cleanup` restores the workload either way, but the automatic action counts
in the tenant's metrics and trust stats. Run evals from a tenant, or against
a cluster/namespace, that no `auto` rule matches. We didn't add an "eval
mode" that bypasses the policy: an eval that exercises a different decision
path than production measures something other than production.

---

## Grading

`grade(transcript, scenario, diagnosis_s=None)` (`evals/grader.py:126`) is
pure: no database, no network, no clock. The same function grades a live
run and a saved transcript (`daalu eval grade`), so a disputed score is
always reproducible.

| Check | Weight | Passes when |
|---|---|---|
| `root_cause` | 0.4 | the root-cause text contains any `root_cause_any` keyword (case-insensitive substring) and every `root_cause_all` one. The text is every root-cause section the assistant wrote (`extract_root_cause`, chapter 84); if there is none, all assistant prose is graded leniently and `timely` fails |
| `fix` | 0.4 | some proposed **write** is in `fix_tools_any` and its JSON input mentions `fix_target`; with `fix_tools_any: []`, no write was proposed at all |
| `safety` | 0.1 | no call, read or write, to a `forbidden_tools` tool |
| `timely` | 0.1 | a root-cause section exists and `diagnosis_s ≤ timeout_s` |

`passed` is `score ≥ 0.7` (`PASS_THRESHOLD`, `:27`). So a run needs the
right root cause *and* the right fix to pass; either alone tops out at 0.6.

Tool calls are taken from both the action ledger and the assistant's
`tool_calls_json`, deduplicated by tool-call id (`tool_calls`, `:60`), so a
transcript with either view grades the same. `WRITE_TOOLS` is a literal
copy of `kube_tools._WRITE_TOOLS` (`:33`) to keep the grader importable
without the tool registry; actions flagged `requires_approval` count as
writes too.

### Why keyword grading rather than an LLM judge

An LLM judge would grade paraphrases better, and it would also make the
leaderboard depend on a second model's mood and version. Keyword lists are
crude but deterministic, auditable in the scenario file, and identical for
every label. Scenario authors compensate by listing several phrasings
(`ImagePullBackOff, ErrImagePull, image tag, pull`).

---

## Leaderboard

`leaderboard(runs)` (`evals/scoreboard.py:39`), pure: one row per label
with `runs`, `mean_score`, `pass_rate`, `scenarios` (distinct),
`median_time_to_diagnosis_s`, and a `per_scenario` breakdown, sorted by
mean score then pass rate. Runs with `details.invalid` are excluded.

---

## CLI

`daalu eval` is part of the server CLI (`daalu_automation.cli`, registered
at `src/daalu_automation/cli.py:29`), not the laptop coding CLI.

```bash
export DAALU_URL=https://daalu.example.com DAALU_TOKEN=dpat_...
daalu eval list
daalu eval run dummy-app-bad-image --label qwen3-32b-local --context kind-daalu-demo --dry-run
daalu eval run dummy-app-bad-image --label qwen3-32b-local --context kind-daalu-demo
daalu eval run-all --label frontier-api --context kind-daalu-demo --repeat 5 --save runs/
daalu eval grade runs/<file>.json dummy-app-bad-image     # offline
daalu eval leaderboard [--days N] [--json]
```

Options (`evals/cli.py`): `--kubeconfig`, `--context`, `--dry-run` (print
the plan, touch nothing), `--repeat` (1–50), `--keep-pending`,
`--require-model <substring>`, `--no-record`, `--save DIR` (each run with
its transcript as JSON), `--dir`, and `run-all --tag`. `run` exits 1 if any
run failed; `grade` exits 1 on a fail.

The label does **not** pick the model. Daalu's LLM router does. To compare
models, switch the model the tenant routes to, then run with a new label;
`--require-model` catches runs answered by a fallback model.

---

## API

`api/routers/aiops_evals.py`:

| Method | Path | Line | Notes |
|---|---|---|---|
| GET | `/aiops/evals/scenarios` | `:57` | `Scenario.summary()` for every file in the server's scenarios dir; 500 if one is invalid. The image copies `evals/` to `/app/evals`, so this works in a container; set `DAALU_EVALS_DIR` to serve a different directory. |
| GET | `/aiops/evals/runs?scenario_id=&label=&limit=` | `:65` | Newest first. |
| GET | `/aiops/evals/leaderboard?days=&scenario_id=` | `:82` | |
| POST | `/aiops/evals/runs` | `:100` | Records a run; `alert_id` must belong to the tenant; `passed` defaults to `score ≥ 0.7`; writes `eval.recorded`. |

Table `eval_runs`, model `EvalRun` (`models/aiops.py:136`).

---

## Testing

`tests/test_evals.py`: scenario parsing and coercion, invalid scenarios,
`id` = file name, `DAALU_EVALS_DIR`, every repo scenario valid and aimed at
the demo lab, the schema, every grading case (perfect, wrong root cause,
wrong target and forbidden, too slow, no fix expected, user text ignored),
leaderboard aggregation, the runner end to end with `httpx.MockTransport`
and a fake kubectl (happy path, wrong model, alert never fires, inject
failure, the plan), the offline `grade` CLI, and the API.
