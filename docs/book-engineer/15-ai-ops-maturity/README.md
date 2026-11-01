# Part XV — The AI-Ops Maturity Model

Everything behind the **AI Ops** section of the sidebar: the AI-Ops Maturity
Model (AOMM) itself, the autonomy policy that decides which writes the agent
may make on its own, the guardrails around automatic execution, the
investigation record and human verdicts, runbooks as code, change context and
write previews, the hash-chained audit log, the metrics and maturity scoring,
the eval harness, and the MCP server plus the open `spec/` schemas.

DevOps spread because it came with a picture of what good looks like,
practices expressed as code, and shared metrics (DORA). AOMM is Daalu's
attempt at the same thing for how infrastructure teams work with AI. The
standard text is [`spec/README.md`](../../../spec/README.md); the build
contract is [`docs/design/ai-ops-maturity.md`](../../design/ai-ops-maturity.md).
Where those two and the code disagree, these chapters describe the code.

The levels:

| Level | Name | One line |
|---|---|---|
| 0 | Ad hoc | Individuals paste logs into a chatbot. No shared context, no record. |
| 1 | Assisted | AI reads alerts, logs, metrics and explains them. Read-only. |
| 2 | Proposing | AI names the root cause and proposes a fix; every change is human-approved. |
| 3 | Trusted | Policy lets low-risk, proven actions run automatically. |
| 4 | Learning | Every incident improves the next: verdicts, runbooks, evals, metrics. |
| 5 | Autonomous | AI resolves most routine incidents; humans own policy and exceptions. |

## Chapters

| # | Chapter | What it covers |
|---|---------|----------------|
| 81 | [The maturity model](81-the-maturity-model.md) | The six levels, how the automatic level is computed, and a map of every Daalu feature (old and new) to the level it serves. |
| 82 | [Autonomy policy as code](82-autonomy-policy.md) | The policy schema, `decide()`, versioned storage, the API, and why the policy governs writes only. |
| 83 | [Guardrails and automatic execution](83-guardrails-and-auto-execution.md) | `auto` / `auto_after_trust`, trust stats, kill switch, circuit breaker, freeze windows, hourly cap, multi-approver, and the background execute-then-verify path. |
| 84 | [Investigations and verdicts](84-investigations-and-verdicts.md) | The per-alert investigation record, root-cause detection, outcome verification, `manual` resolution, verdicts, automatic investigation. |
| 85 | [Runbooks as code](85-runbooks-as-code.md) | File format, matching, prompt injection, AI-drafted runbooks after a verified fix, the API and `daalu runbooks`. |
| 86 | [Change context and previews](86-change-context-and-previews.md) | The `recent_changes` read tool and the blast-radius preview on every proposed write. |
| 87 | [The audit log](87-audit-log.md) | The per-tenant hash chain, the advisory lock, event kinds, verification and its limits. |
| 88 | [Metrics and maturity scoring](88-metrics-and-maturity-scoring.md) | Metric definitions as computed, the automatic level, next steps, the self-assessment. |
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
into the per-alert **investigation** record (time to diagnosis, resolution,
human verdict), and into **runbook** drafts. Everything is written to a
per-tenant **hash-chained audit log**. **Metrics** over those records compute
the team's level. **Evals** benchmark the agent on scripted outages, and an
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
