# 90. The MCP server and the open specs

A standard needs two things beyond one good implementation: a way for
other tools to participate, and artefact formats others can read and write.
For AOMM those are the **MCP server** (any agent — Claude Code, Cursor, a
team's own — gets Daalu's read tools, investigations and policy, and can
propose fixes through the same policy as Daalu's own agent) and the
**`spec/` directory** (the AOMM text plus JSON Schemas for every artefact).
Both are Level 5 practices in the spec ("an open tool protocol").

Code: `core/aiops/mcp.py` (protocol + tools), `api/routers/mcp.py` (HTTP),
`core/aiops/mcp_cli.py` (stdio bridge), `cli/daalu_cli/mcp_bridge.py`
(vendored copy for the laptop CLI), `core/aiops/specs.py`,
`scripts/gen_specs.py`, `spec/`. User guide: [`docs/mcp.md`](../../mcp.md).

---

## Protocol

Model Context Protocol, JSON-RPC 2.0. `handle(message, db, tenant_id, user)`
(`core/aiops/mcp.py:871`) is transport-independent: it takes one decoded
message and returns the response, or `None` for notifications and for
responses sent by the client.

| Method | Behaviour |
|---|---|
| `initialize` | Echoes the client's `protocolVersion` if it is one of `2025-06-18`, `2025-03-26`, `2024-11-05` (`SUPPORTED_PROTOCOL_VERSIONS`, `:47`), else answers `2025-06-18`. Capabilities: `tools` (`listChanged: false`). Includes `instructions` (`:59`) telling the client what Daalu is and that writes only go through `propose_action`. |
| `ping` | `{}` |
| `tools/list` | Every tool's name, title, description, `inputSchema`, and annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`). |
| `tools/call` | `_call_tool` (`:829`). |
| notifications (`notifications/initialized`, `.../cancelled`, ...) | ignored, no response |
| anything else | `-32601 method not found` |

Batches are rejected (not part of `2025-06-18`). The design doc names only
`2025-06-18`; the code also accepts the two earlier versions because the
methods we implement are wire-identical and several clients still send
`2025-03-26`.

We hand-rolled the protocol instead of using the `mcp` Python SDK
(`core/aiops/mcp.py:22`). The surface we need is five methods; the SDK
brings its own server loop, transports and dependency tree, and we needed
the dispatcher to run inside an existing FastAPI request with the request's
DB session and authenticated user.

### Tool results and errors

A successful tool returns its JSON both as a text content block and as
`structuredContent` (`_ok`, `:156`). A `ToolError` (bad argument, denied
write, read tool error output) becomes a tool result with `isError: true`,
not a JSON-RPC error, so the model sees and can react to it. Any other
exception rolls back the session and becomes an `isError` result
`internal error: ...`: a tool bug must not kill the client's session.

After every `tools/call`, success or not, `_call_tool` writes an `mcp.call`
audit event (actor = the user's email, subject = tool name, data = tool
name, arguments truncated to 2000 characters of JSON, `is_error`, error
text) and commits.

---

## Transports

### Streamable HTTP: `POST /api/v1/mcp`

`api/routers/mcp.py:31`. JSON response mode only: one JSON-RPC message in,
one `application/json` message out; notifications and client responses get
`202` with no body; `GET` is `405` (no server-initiated stream); no
`Mcp-Session-Id` (stateless). An unsupported `MCP-Protocol-Version` header
is a 400; bodies over 1 MB are 413.

Authentication is the normal auth gate plus `current_user`. In practice
that means a personal access token, `Authorization: Bearer dpat_...`. The
tenant is the token user's tenant, and the agent acts as that user (its
verdicts are recorded under that user's email).

Why stateless JSON mode: Daalu's tools are request/response, there are no
server-initiated notifications to stream, and stateless requests go through
the existing ingress, auth gate and horizontal scaling with no sticky
sessions.

### stdio: `daalu mcp`

Most MCP clients launch a local command and speak newline-delimited
JSON-RPC on its stdin/stdout. `core/aiops/mcp_cli.py` bridges that to the
HTTP endpoint: `Bridge.forward` (`:67`) POSTs each message with the token
and `MCP-Protocol-Version` (learned from the `initialize` reply), and
writes the reply as one line. It accepts an SSE response too, in case a
server ever answers with one. Transport failures become JSON-RPC errors for
the request that hit them (`-32000`, with a hint to check the token on a
401) so the client sees an error instead of a hung call. `run_stdio`
(`:158`) points `sys.stdout` at stderr before the loop starts, because any
stray print from an imported module would corrupt the protocol stream.

There are two `daalu mcp` commands:

| CLI | Where | Credentials |
|---|---|---|
| **Laptop CLI** (`cli/daalu_cli`, chapter 77) | installed with `get.daalu.io`, Homebrew or `pipx install daalu-cli` | `daalu login` once; `daalu mcp` then uses the stored host and PAT from `~/.daalu/credentials.json`. `--url` / `--token`, or `DAALU_URL` / `DAALU_TOKEN`, override (`_cmd_mcp`, `cli/daalu_cli/__main__.py:96`). |
| **Server CLI** (`daalu_automation.cli`) | in the server image and `pip install -e .` | `--url` (default `http://localhost:8000`, env `DAALU_URL`) and `--token` (required, env `DAALU_TOKEN`) (`src/daalu_automation/cli.py:37`). |

So the primary instructions for a developer are:

```bash
daalu login                       # once, browser device flow, mints a PAT
claude mcp add daalu -- daalu mcp
```

The laptop CLI must not depend on the server package, so
`cli/daalu_cli/mcp_bridge.py` is a vendored copy of `core/aiops/mcp_cli.py`
with a three-line header. `tests/test_aiops_mcp_cli_sync.py` fails if the
two drift: edit the server copy, then re-copy it.

We considered running the MCP server directly inside the laptop CLI (tools
executing locally with a kubeconfig). That would have bypassed the policy,
the audit log and the federation tunnels, i.e. everything that makes the
external agent safe. The bridge keeps all tool execution on the server.

---

## Tools

`_build_tools` (`core/aiops/mcp.py:680`), rebuilt per call so the
`run_read_tool` enum follows `kube_tools.TOOLS` as other modules extend it:

| Tool | Handler line | Notes |
|---|---|---|
| `list_alerts` | `:211` | Filters `status`, `severity`, `module`, `limit` ≤ 200; each alert carries cluster, namespace and a short investigation summary. |
| `get_investigation` | `:261` | The `by-alert` investigation detail (chapter 84), the alert, and the latest assistant write-up. A clear error when no investigation exists. |
| `list_clusters` | `:302` | Federated cluster tunnels and kubernetes integrations. |
| `list_read_tools` | `:361` | The read tools and their input schemas. Not in the design doc. |
| `run_read_tool` | `:373` | Runs one read tool. Refuses anything `tool_requires_approval` says is a write, and `propose_change` (`_NOT_READ_ONLY`, `:71`), which kube_tools doesn't gate but which authors a SoT change proposal. `cluster` may be a tunnel slug or an integration name (`_cluster_target`, `:342`). |
| `recent_changes` | `:399` | Chapter 86. |
| `propose_action` | `:427` | See below. `destructiveHint: true`. |
| `get_policy` | `:549` | Same as `GET /aiops/policy`, optional JSON Schema. |
| `simulate_policy` | `:561` | Same as `POST /aiops/policy/simulate`, but `external` defaults to **true** (the UI simulator defaults to false). |
| `list_runbooks`, `get_runbook` | `:608`, `:621` | Listing omits bodies. |
| `get_maturity` | `:632` | Chapter 88. |
| `submit_verdict` | `:638` | By `alert_id` or `investigation_id`, as the token's user. |

### `propose_action`: an external agent can never bypass approval

`tool_propose_action` (`core/aiops/mcp.py:427`):

1. requires `alert_id`, `tool`, `input`, `reason`; the reason is stored as
   the tool input's `reason` argument (which `validate_write_input` requires
   on every write) and shown to the approver;
2. refuses unknown tools and read tools, then runs
   `kube_tools.validate_write_input`;
3. decides with `external=True`. `deny` → `action.denied` audit event and a
   tool error; nothing is queued;
4. writes an assistant message on the alert, "External agent `<email>`
   proposed `<call>` via MCP", carrying a synthetic `tool_calls_json`
   entry (`mcp_<hex>` id), so the alert transcript stays replayable to
   Daalu's own model;
5. creates the `AlertAction` with the policy fields, `actor="mcp:<email>"`
   and a preview; `auto` creates it approved and `_start_auto_actions` runs
   it after the commit, exactly as for Daalu's agent;
6. returns the action, the decision, the preview and a `next` sentence
   ("Waiting for N human approval(s) on the alert page in Daalu.").

### Why external agents are downgraded to `approve`

`decide` turns an `auto` decision into `approve` for external proposals
unless the matching rule sets `allow_external_auto: true`
(`core/aiops/policy.py:332`). A rule's trust was earned by Daalu's own
agent, under Daalu's prompt, tools and runbooks; an external agent is a
different model with a different prompt and context the policy author never
reviewed. Inheriting the trust silently would let anyone with a PAT and a
clever prompt run writes the team only meant to automate for a known agent.
The opt-in is per rule, so a team can say "any agent may restart pods in
dev" without saying it for prod. All other guardrails still apply after the
opt-in.

### What the policy doesn't cover

The person who proposed a write over MCP can't approve it: the approve
endpoint refuses with 403 when the action's `actor` is `mcp:<their email>`
(`test_mcp_proposer_cannot_approve_own_action`). Someone else has to approve
it, so an external agent driven by a person always gets a second pair of
eyes. Use `approvers: 2` where you want two reviewers beyond the proposer.

---

## The open specs: `spec/`

| File | Source |
|---|---|
| `spec/README.md` | AOMM v0.1: purpose, terms, metrics (five TRACE, supporting, definitions), levels and how the metrics map to them, core practices, artefacts, versioning, conformance |
| `autonomy-policy.schema.json` | `policy.policy_json_schema()` (the validator's own model) |
| `action-proposal.schema.json` | `specs.ActionProposal` (`core/aiops/specs.py:72`) |
| `audit-event.schema.json` | `specs.AuditEvent` (`:102`), kind pattern built from `audit.KINDS` plus `x-` extensions |
| `investigation.schema.json` | `specs.Investigation` (`:125`) |
| `runbook.schema.json` | `specs.RunbookFrontmatter` (`:160`) |
| `eval-scenario.schema.json` | `evals.scenario.scenario_json_schema()` |
| `spec/examples/` | one example per schema |

All are JSON Schema draft 2020-12 with stable `$id`s under
`https://github.com/kiwueke1/daalu/spec/` and `x-aomm-version: "0.1"`.

The schemas are **generated**, never hand-edited:

```bash
.venv/bin/python scripts/gen_specs.py          # rewrite stale files
.venv/bin/python scripts/gen_specs.py --check  # exit 1 if out of date
```

`core/aiops/specs.py` holds the interchange models for artefacts whose
database rows carry internal columns (tenant ids and so on). The policy and
scenario schemas come straight from the models that validate them at
runtime, which is how "the implementation validates against the schemas"
holds without a separate JSON Schema validator: they are the same model.
`tests/test_specs.py` fails when a file on disk differs from the generator's
output, when a schema has no example, or when an example doesn't parse
(the audit-event example's hash is also recomputed).

Why publish schemas at all, for a product: a standard nobody else can
implement is a feature list. Publishing the policy, proposal, audit,
investigation, runbook and scenario formats lets a team keep its policy and
runbooks in git independently of Daalu, lets a security team verify an
exported audit chain with their own code, and lets another vendor implement
AOMM. It also keeps us honest: a change to a public artefact is visible in
review as a schema diff.

---

## Testing

* `tests/test_aiops_mcp.py`: `initialize`/`ping`/errors, `tools/list`,
  `list_alerts` plus the audit event and argument truncation,
  `run_read_tool` refuses writes, `recent_changes`, `propose_action`
  (external downgrade, validation, deny, `allow_external_auto` runs it),
  the remaining tools, tenant isolation, the HTTP route, and the stdio
  bridge (round trip, errors, `endpoint_for`).
* `tests/test_aiops_mcp_cli_sync.py`: the vendored bridge matches.
* `tests/test_specs.py`: generated schemas up to date, examples parse.
