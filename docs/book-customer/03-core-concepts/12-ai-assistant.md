# 12. The AI Assistant

The Assistant is the feature most people remember from a Daalu
demo. It's a chat panel that follows you across the app, but the
metaphor of "chatbot" undersells what it does. The Assistant can
investigate your infrastructure, draft change proposals, and
schedule its own follow-ups. It also doesn't *act* on anything
that mutates state without a human approving it. This chapter
explains both halves of that — what it can do, and what it
deliberately won't.

---

## Where the Assistant lives

The Assistant has two surfaces:

1. **The right panel.** Anchored to the right side of every
   page. Open and close it with the chevron at its edge, or hit
   `Cmd-K` (Mac) / `Ctrl-K` (others). On the home page it's
   tenant-wide; on every other page it inherits the page's
   context.

2. **Direct chat at `/copilot`.** A full-page version for longer
   conversations. Useful when you want all the screen real
   estate or you're working off a phone.

Both surfaces back to the same conversation history. Switching
between them doesn't lose context.

---

## What context the Assistant has

When you ask the Assistant a question, it sees:

- **The current page.** On *Alerts → alert-id*, it sees that
  alert's details. On *Operations → device-id*, it sees that
  device. On the home page, it sees your daily briefing inputs.
- **Your last few turns of conversation.** A short rolling
  history so it can follow up "yes, do that" without re-stating
  the whole context.
- **Your tenant's integrations.** It knows which clouds you've
  connected, which observability stacks it can query, which
  network device source-of-truth you've configured.
- **The tools it has access to** (next section).

It does *not* see other users' conversations, other tenants'
data, or anything from your personal computer.

---

## Tools — what the Assistant can do

The Assistant has a fixed catalog of **tools** — functions it
can call. They group into:

### Read-only tools

These let it investigate. The Assistant can call any of these
without asking; they don't change state.

- `query_prometheus(query)` — run a PromQL query.
- `query_loki(query)` — run a LogQL query.
- `read_pod_logs(namespace, pod, container, lines)` — fetch the
  last N lines of a pod's logs.
- `list_resources(cloud, resource_kind, region, filters)` —
  list cloud resources.
- `get_deployment(namespace, name, cluster)` — read a Kubernetes
  Deployment.
- `rollout_history(namespace, deploy, cluster)` — recent
  rollouts and their statuses.
- `read_device_config(device_id)` — pull the running config
  from a Nautobot-managed device.
- `search_events(filters)` — query the event timeline.
- `recent_alerts(filters)` — list recent alerts.
- And a few more — see *Settings → Assistant → Tools* for the
  live catalog with descriptions.

### Write tools

These mutate state. The Assistant **does not** call them
directly. Instead, when its reasoning concludes that one of
these should be called, it writes a **change proposal** and
hands it to you.

- `restart_pod(...)`, `scale_deployment(...)`,
  `apply_device_config(...)`, etc.

The list of write tools available depends on which integrations
you've granted write permissions to. By default, write tools
are off.

### Notification tools

- `page_user(user, severity, message)`, `post_to_slack(channel,
  text)` — the Assistant can send these without a proposal,
  but they're auditable and only fire on user-explicit ask
  ("page the on-call").

---

## The change-proposal mechanic

This is the most important paragraph of this chapter. Read it
twice if you have to.

When you ask the Assistant to do something that requires a
write — "restart the deploy," "open a port on this device,"
"scale up that ASG" — the Assistant does **not** call the
write tool. Instead, it:

1. Reasons through whether the proposed action makes sense.
2. Writes a **change proposal** — a structured artifact
   describing the proposed call (which tool, with which
   arguments, expected effects).
3. Posts the proposal to the *Operations → Proposals* page.
4. Tells you, in chat, that it's ready and posts a link.

You then read the proposal, optionally edit it, and either
approve or deny it. If you approve, the executor runs the call.
**Self-approval is blocked** — if you asked the Assistant to do
something, you can't approve the resulting proposal yourself.

This is the central safety property of Daalu. It is what
distinguishes the Assistant from an autonomous agent that "just
does stuff." You'll see this loop dozens of times in your first
month of use, and after a while you'll trust it enough to be
*disappointed* if you ever lose access to it.

---

## Two approval paths: change proposals and alert actions

The section above describes **change proposals**, which is how Daalu
gates changes to your *network and servers*: device configuration,
drift fixes, and server provisioning or power operations. Fixes the
AI proposes while it investigates an **alert** on a Kubernetes cluster
use a different, lighter gate. It's easy to mix the two up, so here
is how they differ.

| | Change proposal | Alert action |
|---|---|---|
| What it changes | A device's configuration, or a server's lifecycle | A Kubernetes workload in the alert's cluster: roll back, scale, restart, delete a pod, patch a resource |
| Where you approve it | *Operations → Proposals* | The **Approve / Reject** card inside the alert's chat |
| Who runs it | Daalu's executor, a separate service with its own identity | Daalu itself, using the cluster connection you registered |
| After it runs | Marked executed or failed | Daalu **checks the result**: it waits for the rollout, looks at the pods, and records whether the workload recovered. If it didn't, the AI investigates again; anything new it suggests needs approval again |
| Governed by your autonomy policy | No | Yes |

**Alert actions follow your autonomy policy.** When the AI wants to
change a workload during an alert investigation, Daalu checks the
policy your team has set before anything is queued. Depending on the
rule that matches, the action is:

- **denied** — no card appears; the AI is told the action isn't
  allowed and suggests something else, or tells you the manual steps;
- **sent for approval** — the Approve / Reject card appears, showing
  which rule decided this and a preview of what the change would
  touch. A rule can require more than one approver; the card shows how
  many approvals are still needed, and the same person can't approve
  twice;
- **run automatically** — only where your policy explicitly allows it.
  Daalu still runs the same result check afterwards, and if your
  policy has a circuit breaker, a rule whose automatic action fails
  its check is switched back to "needs approval" for a cool-down
  period.

Actions proposed by an outside AI agent connected over MCP always need
approval unless your policy's rule specifically allows otherwise.

With no policy configured, every alert action waits for approval, the
behaviour this chapter describes. To set one up, see
[Chapter 52 — Setting your autonomy policy](../09-ai-operations-maturity/52-setting-your-autonomy-policy.md).

The AI can also *draft a change proposal* from inside an alert
investigation, for example when the fix is a switch configuration
change. That one is still a change proposal: it appears under
*Operations → Proposals*, not as a card in the chat.

---

## Examples of useful prompts

Things Daalu's Assistant is genuinely good at, drawn from real
customer usage:

- *"Why did alert X fire this morning?"* — It pulls the alert,
  reads the relevant metrics and logs, and writes a 3-paragraph
  explanation with sources cited.

- *"Show me everything that changed in production yesterday."*
  — It queries the deploy history, the change-proposal log,
  the cloud-resource change events, and the device-config diff
  stream, and synthesizes a list.

- *"This pod keeps OOM-killing. Suggest a fix."* — It reads the
  pod's recent OOM events, looks at memory limits, checks
  recent traffic, and proposes either a limit increase or a
  scale-out. The proposal is queued; you decide.

- *"Page the platform on-call about this alert."* — It calls
  the notification tool and confirms.

- *"What's our total monthly spend on the eu-west-1 region across
  all accounts?"* — It runs cost queries against each connected
  cloud and aggregates.

- *"List all devices whose config has drifted from source-of-
  truth in the last 7 days."* — It queries Nautobot-managed
  inventory and the drift log.

- *"Generate a runbook for alert Y."* — It writes a Markdown
  runbook based on the alert's recent firings; you edit and
  save.

---

## Things the Assistant won't do

A non-exhaustive list of intentional refusals:

- **It won't execute write tools without approval.** Even if you
  beg.
- **It won't read another tenant's data.** Even if you give it
  an ID.
- **It won't propose changes to systems it can't reach.** If
  Daalu doesn't have a connected integration for a system,
  the Assistant won't pretend it can act on it.
- **It won't follow links in alert text.** Alert payloads can
  contain URLs; the Assistant treats them as informational, not
  as instructions to fetch and execute.
- **It won't make up data.** When it doesn't know, it says so
  and proposes a tool call. (We've spent a lot of effort on
  this; hallucinations were the main barrier to early adoption.)

---

## Conversations and history

Each user has their own conversation history with the Assistant.
The right-panel chat persists across sessions — logging out and
back in resumes where you left off. The full-page `/copilot`
view exposes a history sidebar where you can revisit earlier
threads.

Conversations are tenant-scoped: an admin can read another
user's history from the audit logs (Chapter 24 covers what's
logged and who sees it), but in normal use, your chat is your
chat.

To clear: in the Assistant panel, click **⋯ → Clear conversation.**
This starts a new thread; older threads remain in history.

---

## When the Assistant is wrong

It will be wrong sometimes. The main failure modes:

- **Stale tool output.** The Assistant queries a system and the
  system was lagging behind reality. Always double-check with
  a fresh query when in doubt.
- **Wrong synthesis.** Three data points pulled correctly,
  combined into a wrong conclusion. Read the cited sources.
- **Overconfidence on ambiguous topics.** "Should we use X or
  Y?" prompts can get strongly-worded answers from the
  Assistant even when the right answer is "it depends." Use it
  as a starting point, not as a final word.

When you spot a failure, click **"Send feedback"** in the
Assistant footer. The feedback goes to the engineering team and
genuinely influences how the Assistant is trained over time.

---

## What's next

Chapter 13 covers the Source of Truth concept — the network
device inventory and how Daalu reconciles it with live state.
This is the integration the Assistant uses most heavily in
NetOps contexts.

Chapter 14 covers cluster federation — how the Assistant gains
access to your on-prem clusters.
