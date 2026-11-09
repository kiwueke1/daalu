# 26. The Coding Assistant

The rest of Daalu helps you *operate* infrastructure. The Coding
Assistant helps you *change* it — edit the Terraform, Helm, Python,
and runbook code that defines how your systems run, with an AI
assistant that runs on your perimeter and never on the public
internet.

It looks like GitHub Codespaces, with three differences: the editor
runs in your tenant's pod (no shared multi-tenant editor), the AI is
constrained to your own GPU or the Daalu-hosted pool (no cloud LLM
fallback), and the agent has tools for editing files and running
commands directly inside that pod.

---

## What it is

The Daalu Coding Assistant is browser-based VS Code (code-server) with:

* **A terminal** with `git`, `curl`, `jq`, `python3`, `kubectl`, `helm`
  pre-installed.
* **The Daalu Assistant extension** in the activity bar — a chat
  panel that can answer questions, propose file writes, and propose
  shell commands.
* **The `daalu chat` CLI** — a Claude-Code-style terminal REPL for the
  same agent when you'd rather work in a terminal than a side panel.
  You can also install the `daalu` CLI on **your own machine** and use the
  agent over your local code — see [chapter 47](47-daalu-cli-terminal.md).
* **A persistent workspace volume** at `/workspace` (5/20/50 GiB
  depending on profile) that survives pauses.

It lives behind **Workspace** in the left sidebar, under
*Infrastructure*.

---

## How it's different from Cursor / Copilot / Codespaces

Three things:

1. **Your code never leaves your perimeter.** The LLM router refuses
   to send `purpose='coding'` prompts to *any* external provider —
   Anthropic, OpenAI, anything. If the Daalu-hosted pool and your
   own GPU are both unreachable, the agent fails with an error
   rather than falling back to a cloud model.
2. **One workspace per user, in its own pod.** Your container can't
   see anyone else's files or processes. The default-deny
   NetworkPolicy means the pod can reach DNS, the Daalu inference
   gateway, and `https://*` for git clone — nothing else.
3. **Agent actions ask before they happen.** When the assistant
   proposes a file write or a shell command, the panel renders an
   "Apply" or "Run" button (CLI prompts y/N). Nothing happens
   silently — you stay in the loop.

---

## Eligibility

The Coding Assistant needs a **coding model that is actually
serving**. Eligibility isn't a paper check against your plan — Daalu
probes the coding endpoint your tenant would use and only lets you
create a workspace when a model answers. That means:

* Your tenant has a **coding model live on your own GPU** (connected
  via cluster federation — chapter 35). A GPU that serves only the
  small classifier model does *not* qualify; the coding models are a
  separate, larger deployment.
* Or your tenant is enrolled in the **Daalu-hosted** inference tier
  with a coding model serving in the operator's pool.
* Or your operator has flipped the `workspaces_enabled` flag for your
  tenant — which lets you set the workspace up *ahead* of the coding
  model coming online.

If no coding model is serving, the **Workspace** page says exactly
that — *"No coding model is running yet"* — and what to do: ask your
operator to deploy the coding model on your GPU cluster, then come
back. It does this instead of letting you create a workspace whose
first prompt would fail. If Workspace isn't turned on for your
organisation at all, the page says so in the same neutral way and
offers a **Connect a GPU** button, which opens *AI Factory*. The
sidebar entry is always visible; clicking it shows the current
status either way.

---

## Provisioning your workspace

1. Click **Workspace** in the left sidebar.
2. Pick a **coding model**. The dropdown lists the open coding models
   in the catalog — for example **Qwen2.5-Coder 14B** (runs on a 16 GB
   GPU) and the larger, higher-quality **Qwen2.5-Coder 32B** and
   **Qwen3-Coder 30B-A3B** (need a 48 GB card). The list is honest
   about availability: a model that isn't actually serving right now
   appears greyed out with the reason, and the default selection is
   the best model that *is* live. The model you pick is the one the
   assistant inside your workspace uses; it runs on your GPU (or the
   Daalu-hosted pool), never the public internet.
3. Pick a **profile** (this is *compute* — CPU/RAM/disk for the editor,
   separate from the model):
    - **small** (default) — 0.5 / 2 CPU · 1 / 4 GiB RAM · 5 GiB disk
    - **medium** — 1 / 4 CPU · 4 / 8 GiB RAM · 20 GiB disk
    - **large** — 2 / 8 CPU · 8 / 16 GiB RAM · 50 GiB disk
    Pick small unless you know you need more. Your operator may cap
    the maximum profile your tenant can pick.
4. (Optional) Paste a **Git repository URL** + **branch**. The pod
   will `git clone` it into `/workspace` on first boot. Skip this if
   you want to start empty — you can drag-and-drop files into
   VS Code's file explorer or `git clone` from the terminal once the
   IDE is open.
5. Click **Create workspace**. Provisioning takes ~60 s the first
   time (the image is ~1.2 GiB, the pod has to pull it). The page
   polls and switches to **Active** once the pod is ready.
6. Click **Open IDE**. A new tab opens with VS Code loaded into
   `/workspace`. The workspace card shows your profile and model.

You can come back to the page later to delete the workspace. Click
**Delete** on the workspace card; it asks you to confirm in place
(**Delete workspace**) and warns that your `/workspace` files are
deleted, so push any changes first.

---

## Bringing in your code

Three patterns, depending on where your code lives:

### Cloned from GitHub / GitLab on first boot

This is the most common — you provide the repo URL on create, the
init container clones it. Works for any HTTPS git URL. For private
repos that need credentials, leave the URL blank and clone manually
from the terminal once the IDE is up (you can paste credentials into
the terminal session privately).

### Pushed locally via VS Code's file upload

Open the IDE, right-click `/workspace` in the file explorer, choose
**Upload…**, drop files in. Works for individual files and folders.
This is the easiest way to bring code that doesn't live in git, or to
quickly drop a single script.

### Cloned manually from the terminal

```bash
cd /workspace
git clone https://github.com/yourorg/yourrepo.git
cd yourrepo
```

Use this if the create-form URL got rejected, or if you want to clone
multiple repos.

---

## The Daalu Assistant chat panel

In the IDE's activity bar (left edge) there's a Daalu icon. Click it
to open the chat panel. You type a question; the agent answers,
optionally proposing file edits and shell commands.

When the assistant proposes a file write, the extension
**immediately opens a side-by-side red/green diff** — current file
on the left, proposed content on the right — so you see exactly
what would change, and where, *before* deciding. Both sides are
read-only virtual documents: nothing touches disk, and no stray
editable tabs are left behind. Focus stays on the chat, so you can
read the diff and click **Apply** when satisfied — Apply simply
commits the bytes you already reviewed. When the assistant proposes
a shell command (lines that start with `$`), the panel renders a
**Run** button that drops the command into a terminal called
"Daalu".

The panel keeps conversation history for the life of the IDE
session. Click **Reset** to start fresh; **Attach** lets you pin a
workspace file as context (good for "explain this 500-line file").

---

## The `daalu chat` CLI

Open a terminal in the IDE and type `daalu chat`. You get a terminal
REPL that behaves like Claude Code:

```
daalu chat — coding assistant (terminal mode)
workspace:  /workspace
model:      qwen/qwen2.5-coder-14b-instruct
commands:   /reset  /help  /quit

daalu› why does my api pod keep restarting?
```

The CLI agent has three tools:

| Tool | What happens |
|---|---|
| `daalu:read <path>` | The CLI prints the file and forwards it to the model. No prompt — reading your own files is implicit. |
| `daalu:write <path>` followed by a fenced code block | Shows a unified diff vs the existing file (or "new file"), then prompts you `y/N` before writing. |
| `daalu:run <command>` | Prompts `y/N`, then runs in `/workspace` with a 60 s timeout. Output is fed back to the model so it can react to results. |

Quit with `/quit`. `/reset` clears history. `/help` lists commands.

The two surfaces — activity-bar panel and `daalu chat` — hit the same
inference gateway with the same `purpose='coding'` constraint, so the
same privacy guarantees apply. Use whichever fits your workflow.

---

## Lifecycle

* **Active**: at least one open IDE session in the last 24 h.
* **Paused** (after 24 h idle): the pod is scaled to 0; your files
  on the PVC are preserved. Re-clicking **Open IDE** schedules a
  restart; you're back in within ~10 s.
* **Destroyed** (after 14 d of continued inactivity): the PVC is
  released and your files are gone. Push any work via a normal git
  push from the terminal *before* the cliff. Your operator may also
  destroy a workspace manually if your tenant is being decommissioned.

Heartbeats are implicit — every HTTP/WS round-trip through the IDE
proxy stamps `last_heartbeat_at`. As long as you have the IDE open
in a browser tab, the 24 h clock doesn't start.

---

## What the agent can and cannot do

**Can:**

* Read any file inside `/workspace`.
* Propose writes to any file inside `/workspace` (gated on your
  Apply click in the panel, or `y/N` in the CLI).
* Propose terminal commands (same gate).
* Make HTTPS requests to public hosts for `git clone` / `pip install`
  / `curl` etc. through the workspace pod's egress allow-list.

**Cannot:**

* Reach arbitrary internal Daalu services. The pod's NetworkPolicy
  default-denies; only DNS, the inference gateway, daalu-api, and
  public HTTPS are open.
* Run a `git push` to a remote repository that requires credentials
  the user hasn't loaded into the terminal session.
* Apply file writes or shell commands without your y/N (or Apply
  button click). The chat panel will *show* you the proposed write
  diff before you confirm — you're never surprised.
* Persist anything outside `/workspace`. Anything written elsewhere
  in the container dies when the pod restarts.

---

## Privacy and security

* **Coding prompts never reach external LLM providers.** The router
  has a hard block: when `purpose='coding'` is set, Anthropic / OpenAI
  / Bedrock / any cloud provider is skipped entirely. The only
  destinations the router will consider are your own GPU or the
  operator-managed local pool.
* **Per-user pod isolation.** Your workspace has its own
  Kubernetes pod, its own filesystem, its own NetworkPolicy. Another
  user in the same tenant cannot read your files or attach to your
  shell.
* **Service tokens, not user credentials.** The Daalu Assistant
  inside the IDE authenticates to the inference gateway with a
  short-lived (120 s TTL) service-token JWT minted by a sidecar
  process. Your session cookie is never exposed to the workspace
  container.
* **Audit.** Every chat completion is logged with tenant + user +
  prompt-token + completion-token counts in the usual billing
  surface (Chapter 22). Code prompts can be quite large — keep an eye
  on usage if you run the assistant heavily.

---

## What's coming

A few features are designed but not shipped in this version:

* **Apply-to-cluster proposals.** Today the agent stops at "here's a
  diff for the file" — applying that diff to Kubernetes still goes
  through your normal git → CI → Argo loop. A planned `daalu propose`
  CLI will open a Daalu change proposal directly from the workspace
  (Chapter 18 covers proposals). When this ships you'll see a new
  CLI subcommand documented here.
* **Workspace pods inside your cluster.** Today your workspace pod
  runs in the operator's cluster (behind their NetworkPolicy + the
  privacy guarantee above). A planned mode runs the pod inside your
  *own* federated cluster, so code is at rest on storage you control.
* **Email reminder before destroy.** Today the 14-day destroy cliff
  is silent. The email at 12 days is on the roadmap.

---

## See also

* Chapter 18 — Change proposals (the path your edits take to land in
  prod).
* Chapter 38 — Daalu-hosted AI tier (the inference backend when you
  don't have your own GPU).
* Chapter 30 — Nautobot (the source-of-truth side of the equation;
  the agent can read your Nautobot data through your normal
  integrations).
