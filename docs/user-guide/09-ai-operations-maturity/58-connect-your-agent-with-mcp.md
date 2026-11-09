# 58. Connect your agent with MCP

Your engineers may already work with an AI agent in their editor or
terminal — Claude Code, Cursor, or something else. Daalu speaks the **Model
Context Protocol (MCP)**, the open standard those agents use to call tools.
Connect your agent and it can read your alerts and Daalu's investigations,
run Daalu's read-only investigation tools against your clusters, and propose
fixes — while every change it proposes still goes through your autonomy
policy, your approvers and your audit log.

---

## The short version

On your laptop, once:

```bash
curl -fsSL https://get.daalu.io/install.sh | sh     # or: brew install daalu-io/tap/daalu
                                                    # or: pipx install daalu-cli
daalu login
```

Then, for Claude Code:

```bash
claude mcp add daalu -- daalu mcp
```

That's it. `daalu mcp` uses the login you already have, and your agent acts
as you, in your tenant. Check it with `claude mcp list`, or `/mcp` inside a
Claude Code session.

---

## Step by step

### 1. Install the `daalu` CLI

The same command-line tool as chapter 47
([Using Daalu from your terminal](../04-feature-walkthroughs/47-daalu-cli-terminal.md)).
Pick one:

```sh
curl -fsSL https://get.daalu.io/install.sh | sh   # Linux / macOS
brew install daalu-io/tap/daalu                   # macOS (Homebrew)
pipx install daalu-cli                            # any OS with Python
```

### 2. Log in once

```bash
daalu login                                   # https://daalu.example.com
daalu login --host https://ops.your-company.com   # your own Daalu
```

Your browser opens, you approve the login, and the CLI stores a personal
access token in `~/.daalu/credentials.json` (readable only by you). Every
`daalu mcp` from then on uses it. `daalu status` shows who you're logged in
as.

### 3. Add Daalu to your agent

**Claude Code**

```bash
claude mcp add daalu -- daalu mcp
```

**Cursor** — `~/.cursor/mcp.json` (or `.cursor/mcp.json` in a project):

```json
{
  "mcpServers": {
    "daalu": {
      "command": "daalu",
      "args": ["mcp"]
    }
  }
}
```

**Any other MCP client** — command `daalu`, arguments `["mcp"]`. The client
starts it and talks to it over standard input and output.

---

## Other ways to connect

**With an explicit token.** If you'd rather not use `daalu login` (a shared
CI machine, say), create a token in **Settings → API tokens** (chapter 37)
and pass it:

```bash
claude mcp add daalu -- daalu mcp --url https://daalu.example.com --token dpat_xxx
claude mcp add daalu -e DAALU_URL=https://daalu.example.com -e DAALU_TOKEN=dpat_xxx -- daalu mcp
```

The CLI also honours `DAALU_HOST` / `DAALU_TOKEN` in place of a stored login.
`--url` is the base address of your Daalu; the CLI adds the rest of the path
itself.

**Over HTTP, with nothing installed.** If your client can call a remote MCP
server with a header, point it straight at Daalu:

```bash
claude mcp add --transport http daalu https://daalu.example.com/api/v1/mcp \
  --header "Authorization: Bearer dpat_xxx"
```

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

The full reference, including a `curl` smoke test, is in
[docs/mcp.md](../../mcp.md).

---

## What your agent can do

| Tool | What it does |
|---|---|
| `list_alerts` | Recent alerts, with each one's investigation status. Filter by status, severity, module. |
| `get_investigation` | Daalu's investigation of an alert: root cause, the fixes proposed with their policy decisions and outcomes, resolution, verdict, latest write-up. |
| `list_clusters` | The clusters you've connected to Daalu. |
| `list_read_tools` | The read-only tools `run_read_tool` can run, with their inputs. |
| `run_read_tool` | Run one read-only tool on a cluster: pod logs, events, describe, deployments, rollout history, Prometheus, Loki, cloud reads. |
| `recent_changes` | Rollouts, image changes, scaling events and Argo CD syncs in the last *N* hours. |
| `propose_action` | Attach a fix to an alert, with a reason. Your policy decides what happens next. |
| `get_policy` | Your autonomy policy, kill switch and breaker state. |
| `simulate_policy` | What the policy would decide for a fix right now. |
| `list_runbooks`, `get_runbook` | Your runbooks. |
| `get_maturity` | Your AI-Ops maturity level, criteria and next steps. |
| `submit_verdict` | Grade an investigation: correct, partial or wrong. |

A typical session: *"What's firing in Daalu? Look at the newest critical
alert, check what changed in its namespace, and propose a fix."*

---

## What your agent can't do

- **It can't change anything directly.** `run_read_tool` refuses every write
  tool. The only way to change something is `propose_action`, which attaches
  the fix to an alert, exactly like a fix Daalu's own AI proposed.
- **The policy still decides.** A fix your policy denies comes back to the
  agent as an error and is never queued.
- **It can't run things automatically unless you say so.** Even under an
  `auto` rule, a fix from an external agent waits for approval — unless that
  rule sets `allow_external_auto: true` (chapter 52). All guardrails still
  apply on top: kill switch, circuit breaker, freeze windows, hourly cap.
- **It only sees your tenant**, as you.

---

## What you see in Daalu

On the alert, the chat shows an assistant message: "External agent
*you@company.com* proposed `rollout_undo …` via MCP." with the agent's
reason. The approve card carries a **mcp:***you@company.com* chip, the policy
decision, the preview and the approval count, like any other fix. Once
approved it runs and is verified exactly like a fix from Daalu's AI, and the
agent can call `get_investigation` to see the outcome.

Every tool call your agent makes is written to the audit log as `mcp.call`
under your email; filter **Kind** `mcp.call` on the **Audit log** page
(chapter 56) to see them.

> **Have someone else approve.** Your agent proposes as you, and Daalu does
> not stop you approving your own agent's proposal. For fixes where a second
> pair of eyes matters, set `approvers: 2` on the policy rule that covers
> them.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Not logged in. Run `daalu login` first, or pass --url and --token." | Run `daalu login`, or pass a token. |
| "Daalu returned HTTP 401" | The token is wrong, revoked or expired. Run `daalu login` again or create a new token. |
| "cannot reach Daalu at …" | Check the URL (the base address, without `/api/v1`) and your network. |
| A tool answers "… is not available on this Daalu server" | Your Daalu is older than the tool. Ask your administrator to upgrade. |
| A fix comes back "denied by the autonomy policy" | Working as intended: a `deny` rule covers it. Check with `simulate_policy`. |
| The agent's fix is waiting even though the rule is `auto` | Expected for external agents unless the rule sets `allow_external_auto: true`. |

---

## What's next

That's the end of Part IX. To review where all of this has taken your team,
go back to the **Maturity** page — [Chapter 51](51-where-is-your-team.md).
For anything else, Part VIII covers
[troubleshooting and support](../08-support/44-troubleshooting.md).
