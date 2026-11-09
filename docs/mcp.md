# Connect an AI agent to Daalu over MCP

Daalu is a [Model Context Protocol](https://modelcontextprotocol.io) server
(protocol version `2025-06-18`). Claude Code, Cursor or any MCP client can
read your alerts, investigations, clusters and policy, run Daalu's read-only
investigation tools, and propose fixes. An external agent can never change
your infrastructure directly: `propose_action` attaches the write to an alert
and your autonomy policy decides. Usually that means a human approves it in
Daalu. A proposal only runs automatically if the matching policy rule sets
`allow_external_auto: true`.

Every call is recorded in the audit log as `mcp.call`, under your email.
Background: [engineer book ch. 90](book-engineer/15-ai-ops-maturity/90-mcp-server-and-open-specs.md),
[user guide ch. 58](user-guide/09-ai-operations-maturity/58-connect-your-agent-with-mcp.md).

## 1. Quick start: the laptop CLI

Install the `daalu` CLI on the machine where your agent runs (the same CLI
as the terminal coding agent, see the
[CLI chapter](user-guide/04-feature-walkthroughs/47-daalu-cli-terminal.md)):

```bash
curl -fsSL https://get.daalu.io/install.sh | sh     # or: brew install daalu-io/tap/daalu
                                                     # or: pipx install daalu-cli
daalu login                                          # once: browser sign-in, stores a token
```

`daalu login` stores your Daalu URL and a personal access token in
`~/.daalu/credentials.json`. From then on `daalu mcp` needs no arguments: it
serves Daalu to the MCP client over stdio, as you, in your tenant.

**Claude Code:**

```bash
claude mcp add daalu -- daalu mcp
```

**Cursor** (`~/.cursor/mcp.json`, or `.cursor/mcp.json` in a project):

```json
{
  "mcpServers": {
    "daalu": { "command": "daalu", "args": ["mcp"] }
  }
}
```

Check it with `claude mcp list`, or `/mcp` inside a Claude Code session.

## 2. Other ways to connect

| Transport | Use when | How |
|---|---|---|
| stdio, logged-in CLI | Default; the client launches a local command | `daalu mcp` after `daalu login` |
| stdio, explicit token | CI, a shared machine, or a different Daalu than the one you logged in to | `daalu mcp --url https://daalu.example.com --token dpat_…`, or `DAALU_URL` / `DAALU_TOKEN` in the client's environment |
| Streamable HTTP | The client can call a remote MCP server with a header | `POST https://<your-daalu>/api/v1/mcp` with `Authorization: Bearer dpat_…` |

For the explicit-token and HTTP forms, create a personal access token in
Daalu under **Settings → API tokens** (`dpat_…`). The agent acts as the
token's user, in that user's tenant.

`--url` is the base URL without `/api/v1`; `daalu mcp` adds `/api/v1/mcp`
itself. With neither flags nor environment variables, the laptop CLI uses
your `daalu login`; if you aren't logged in it says
"Not logged in. Run `daalu login` first, or pass --url and --token."

The server image's management CLI (`pip install -e .` from this repo, the
`daalu_automation` package) has the same command, `daalu mcp --url … --token …`,
where `--token` (or `DAALU_TOKEN`) is required and `--url` defaults to
`http://localhost:8000`.

The HTTP endpoint answers in JSON response mode (no server-sent events, no
session id). `GET /api/v1/mcp` returns 405.

## 3. Client configuration with an explicit token

### Claude Code

stdio:

```bash
claude mcp add daalu -- daalu mcp --url https://daalu.example.com --token dpat_xxx
```

or keep the token out of your shell history:

```bash
claude mcp add daalu -e DAALU_URL=https://daalu.example.com -e DAALU_TOKEN=dpat_xxx -- daalu mcp
```

HTTP:

```bash
claude mcp add --transport http daalu https://daalu.example.com/api/v1/mcp \
  --header "Authorization: Bearer dpat_xxx"
```

### Cursor

stdio with an explicit token:

```json
{
  "mcpServers": {
    "daalu": {
      "command": "daalu",
      "args": ["mcp"],
      "env": {
        "DAALU_URL": "https://daalu.example.com",
        "DAALU_TOKEN": "dpat_xxx"
      }
    }
  }
}
```

or over HTTP:

```json
{
  "mcpServers": {
    "daalu": {
      "url": "https://daalu.example.com/api/v1/mcp",
      "headers": { "Authorization": "Bearer dpat_xxx" }
    }
  }
}
```

### Any other client

* **stdio**: command `daalu`, arguments `["mcp"]` (after `daalu login`, or
  with `DAALU_URL` and `DAALU_TOKEN` in the environment). Messages are newline-delimited JSON-RPC 2.0.
  stdout carries protocol messages only; logs go to stderr.
* **HTTP**: `POST <url>/api/v1/mcp`, headers
  `Authorization: Bearer dpat_…`, `Content-Type: application/json`,
  `Accept: application/json, text/event-stream`, and after `initialize`
  `MCP-Protocol-Version: 2025-06-18`. One JSON-RPC message per request
  (no batches). Notifications get `202 Accepted` with no body.

Smoke test with curl:

```bash
curl -s https://daalu.example.com/api/v1/mcp \
  -H "Authorization: Bearer dpat_xxx" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools[].name'
```

## 4. Tools

| Tool | What it does |
|---|---|
| `list_alerts` | Recent alerts with their investigation status (`status`, `severity`, `module`, `limit`). |
| `get_investigation` | Daalu's investigation of one alert (`alert_id`): root cause, proposed actions and their outcomes, verdict, latest write-up. |
| `list_clusters` | Connected clusters and Kubernetes integrations. |
| `list_read_tools` | The read-only tools `run_read_tool` can run, with their input schemas. |
| `run_read_tool` | Run one read-only tool (`tool`, `input`, `cluster`): pod logs, events, describe, deployments, rollout history, Prometheus, Loki, cloud reads. Writes are refused. |
| `recent_changes` | Rollouts, scaling events and Argo CD syncs in the last `hours` (`cluster`, `namespace`). |
| `propose_action` | Attach a write to an alert (`alert_id`, `tool`, `input`, `reason`). The policy decides: denied (error), pending approval, or, only with `allow_external_auto`, run automatically. |
| `get_policy` | The autonomy policy, kill switch and circuit breaker state. |
| `simulate_policy` | What the policy would decide for a write right now (`external` defaults to true). |
| `list_runbooks`, `get_runbook` | Runbooks as code. |
| `get_maturity` | Your AI-Ops maturity level, criteria and next steps. |
| `submit_verdict` | Grade an investigation's root cause: `correct`, `partial` or `wrong`. |

## 5. Letting an external agent's fixes run automatically

By default every MCP proposal waits for approval, even under an `auto` rule.
To allow it for a specific rule:

```yaml
rules:
  - id: dev-restarts
    match: {tools: [restart_deployment], clusters: ["dev-*"]}
    mode: auto
    allow_external_auto: true
```

All guardrails still apply: kill switch, circuit breaker, freeze windows and
the hourly cap.

## Troubleshooting

* `Not logged in`: run `daalu login`, or pass `--url` and `--token`.
* `HTTP 401` from `daalu mcp`: the token is wrong, revoked or expired. Run
  `daalu login` again (or create a new token).
* `cannot reach Daalu at …`: check `--url`. It is the base URL; `daalu mcp`
  adds `/api/v1/mcp` itself.
* A tool answers `… is not available on this Daalu server`: the server is
  older than the tool. Upgrade Daalu.
