# Part XV — The AI-Ops Maturity Model

Everything behind the Maturity, Autonomy policy, Investigations, Runbooks, Evals
and Audit log pages (grouped by task in the sidebar, see `frontend/DESIGN.md`): the AI-Ops Maturity
Model (AOMM) itself, its metrics and how they score a team's level, the
autonomy policy that decides which writes the agent
may make on its own, the guardrails around automatic execution, the
investigation record and human verdicts, runbooks as code, change context and
write previews, the hash-chained audit log, the eval harness, and the MCP server plus the open `spec/` schemas.

DevOps spread because it came with shared metrics (DORA), a picture of what
good looks like, and practices expressed as code. AOMM is Daalu's attempt at
the same thing for how infrastructure teams work with AI, and like DORA it
starts from numbers. The standard text is
[`spec/README.md`](../../../spec/README.md); the build contract is
[`docs/design/ai-ops-maturity.md`](../../design/ai-ops-maturity.md). Where
those two and the code disagree, these chapters describe the code.

### The metrics

Five TRACE metrics, computed by `core/aiops/metrics.py` (chapter 88).
**TRACE** is their initials: Time to **T**riage, time to **R**esolve,
**A**greement rate, **C**hange-failure rate, **E**scalation rate.

| Group | TRACE metric | Measures | API field | DORA counterpart |
|---|---|---|---|---|
| Speed | Time to **T**riage | Alert firing → AI names a root cause | `median_time_to_triage_s` | Lead time for changes |
| Speed | Time to **R**esolve | Alert firing → incident resolved | `median_time_to_resolve_s` | Time to restore service |
| Quality | **A**greement rate | How often humans agree with the AI's root cause | `agreement_rate` | — |
| Quality | **C**hange-failure rate | Share of executed AI writes that failed verification | `change_failure_rate` | Change-failure rate |
| Autonomy | **E**scalation rate | Share of resolved incidents that still needed a human | `escalation_rate` | — |

Supporting metrics explain them: `alerts_total` and `ai_triage_rate`
(coverage), `verdict_coverage` (feedback, the sample behind agreement rate),
`proposal_approval_rate` (trust), runbook and eval counts (learning), and
`audit_chain_ok` (integrity).

### How they map to the levels

Each level is a set of thresholds on those metrics (chapter 81 has the full
criteria table):

| Level | Name | Gated by |
|---|---|---|
| 0 | Ad hoc | Nothing: the starting point. |
| 1 | Assisted | `alerts_total ≥ 5`, `ai_triage_rate ≥ 0.5` |
| 2 | Proposing | ≥ 1 human-approved write executed, `proposal_approval_rate` known |
| 3 | Trusted | change-failure rate ≤ 0.15 (`change_failure_rate`), an `auto` rule saved, ≥ 1 automatic write executed |
| 4 | Learning | `verdict_coverage ≥ 0.3`, `runbooks_published ≥ 1`, `eval_runs ≥ 1` |
| 5 | Autonomous | escalation rate ≤ 0.5 (`escalation_rate`), agreement rate ≥ 0.8 (`agreement_rate`), `audit_chain_ok`, guardrails saved |

The two speed metrics (time to triage, time to resolve) gate no level, on purpose: they depend on each team's
systems and alert mix. They are what climbing the levels should improve.

## Chapters

| # | Chapter | What it covers |
|---|---------|----------------|
| 81 | [The maturity model](81-the-maturity-model.md) | The TRACE and supporting metrics, how they map to the six levels, how the automatic level is computed, and a map of every Daalu feature (old and new) to the level it serves. |
| 82 | [Autonomy policy as code](82-autonomy-policy.md) | The policy schema, `decide()`, versioned storage, the API, and why the policy governs writes only. |
| 83 | [Guardrails and automatic execution](83-guardrails-and-auto-execution.md) | `auto` / `auto_after_trust`, trust stats, kill switch, circuit breaker, freeze windows, hourly cap, multi-approver, and the background execute-then-verify path. |
| 84 | [Investigations and verdicts](84-investigations-and-verdicts.md) | The per-alert investigation record, root-cause detection, outcome verification, `manual` resolution, verdicts, automatic investigation. |
| 85 | [Runbooks as code](85-runbooks-as-code.md) | File format, matching, prompt injection, AI-drafted runbooks after a verified fix, the API and `daalu runbooks`. |
| 86 | [Change context and previews](86-change-context-and-previews.md) | The `recent_changes` read tool and the blast-radius preview on every proposed write. |
| 87 | [The audit log](87-audit-log.md) | The per-tenant hash chain, the advisory lock, event kinds, verification and its limits. |
| 88 | [Metrics and maturity scoring](88-metrics-and-maturity-scoring.md) | The five TRACE and the supporting metrics as computed, the automatic level derived from them, next steps, the self-assessment. |
| 89 | [The eval harness](89-eval-harness.md) | Scenario format, the runner, grading, the leaderboard, `daalu eval`. |
| 90 | [The MCP server and open specs](90-mcp-server-and-open-specs.md) | MCP over HTTP and stdio, the tool surface, the external-agent rules, the laptop `daalu mcp`, and the `spec/` JSON Schemas. |

## The one-paragraph mental model

Before AOMM, Daalu's alert chat already did Level 2: the agent ran read
tools on its own and every write landed as a pending `AlertAction` that a
human approved, after which `_run_approved_action` executed it and verified
the workload recovered. AOMM keeps that loop and puts a **policy decision**
in front of every write (`core/aiops/policy.py:decide`): `deny` bounces the
write back to the model, `approve` keeps the old behaviour (now with N
distinct approvers), `auto` creates the action already approved and runs it
through the same execute-then-verify path in the background. The outcome of
each verification feeds back into the policy (trust stats, circuit breaker),
into the per-alert **investigation** record (time to triage, resolution,
human verdict), and into **runbook** drafts. Everything is written to a
per-tenant **hash-chained audit log**. **Metrics** over those records (the five
TRACE ones: time to triage, time to resolve, agreement rate, change-failure
rate, escalation rate) say how well it is working, and thresholds on
them compute the team's level. **Evals** benchmark the agent on scripted outages, and an
**MCP server** opens the same tools and the same policy to external agents
such as Claude Code and Cursor. With no saved policy, nothing changes: every
write needs one approval, exactly as before.

## Code map

| Concern | Code |
|---|---|
| Tables | `src/daalu_automation/models/aiops.py`, AOMM columns on `models/alert_chat.py:AlertAction`, migration `migrations/versions/0034_aiops_maturity.py` |
| Policy, guardrails | `core/aiops/policy.py`, `api/routers/aiops_policy.py` |
| Agent loop integration | `api/routers/alert_chat.py` (`_assistant_turn_loop`, `_start_auto_actions`, `approve_action`, `_record_outcome`) |
| Investigations | `core/aiops/investigations.py`, `api/routers/aiops_investigations.py`, `modules/infra/agent.py` (auto-investigate, manual resolution), `api/routers/alerts.py:resolve_alert` |
| Runbooks | `core/aiops/runbooks.py`, `core/aiops/runbooks_cli.py`, `api/routers/aiops_runbooks.py`, `runbooks/` |
| Change context, previews | `core/aiops/change_context.py`, `core/aiops/preview.py`, `core/kube_tools.py` (`recent_changes`) |
| Audit | `core/aiops/audit.py`, `api/routers/aiops_audit.py` |
| Metrics, maturity | `core/aiops/metrics.py`, `core/aiops/maturity.py`, `api/routers/aiops_metrics.py` |
| Evals | `src/daalu_automation/evals/`, `evals/scenarios/`, `api/routers/aiops_evals.py` |
| MCP | `core/aiops/mcp.py`, `core/aiops/mcp_cli.py`, `api/routers/mcp.py`, `cli/daalu_cli/mcp_bridge.py` |
| Specs | `spec/`, `core/aiops/specs.py`, `scripts/gen_specs.py` |
| UI | `frontend/app/{maturity,autonomy,investigations,runbooks,evals,audit}/page.tsx`, `frontend/components/aiops/` |

## Operations at a glance

* **Migration `0034_aiops_maturity`** (`migrations/versions/0034_aiops_maturity.py`,
  revises `0033_gpu_tenant_mem_util`) is additive: seven new tables and nine
  new `alert_actions` columns with server defaults (`policy_mode=''`,
  `approvals_required=1`, `approvals='[]'`, `preview='{}'`,
  `auto_executed=false`, `actor='agent'`), so existing rows read as pre-AOMM
  approve-everything actions. `downgrade()` drops the tables, **including
  `audit_events`**: take a backup first if the audit chain matters. Apply it
  like any other migration ([ch. 50](../09-operations-runbooks/50-prod-migrations.md)).
* **Kill switch**: `POST /api/v1/aiops/policy/kill-switch {"enabled": true}`,
  any user; turning it off needs an admin (chapter 83).
* **Circuit breaker reset**: `POST /api/v1/aiops/policy/breaker/reset?rule_id=<id>`,
  admin; empty `rule_id` resets the defaults breaker (chapter 83).
* **Eval scenarios in a container**: `GET /aiops/evals/scenarios` reads
  `$DAALU_EVALS_DIR` (or `<repo>/evals/scenarios`); mount the directory and
  set the variable (chapter 89).
* **Logs worth alerting on**: `aiops.audit.record_failed` (an audit event
  was lost), `alert_chat.auto_investigate_failed`,
  `alert_chat.approved_action_failed`.

## Related reading

- Customer-facing walkthrough: [Part IX of the Operator's Handbook](../../book-customer/09-ai-operations-maturity/README.md)
- Team-facing summary: [`docs/maturity-model.md`](../../maturity-model.md)
- Connecting an external agent: [`docs/mcp.md`](../../mcp.md)
- The standard: [`spec/README.md`](../../../spec/README.md)
- The alert chat tools this builds on: [ch. 9 — core libraries](../02-backend-architecture/09-core-libraries.md)
- The LLM router every investigation goes through: [ch. 31](../06-gpu-and-llm-serving/31-llm-router.md)
